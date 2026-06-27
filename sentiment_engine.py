"""Market sentiment / news aggregation for the autotrader.

Four sources, ordered by cost (free-no-key first); every source degrades
gracefully — a failure returns an error marker and contributes nothing to the
aggregate, it never blocks trading:

  1. Fear & Greed Index (alternative.me) — free, no key. We pull the HISTORY and
     derive a multi-period trend + extreme-reversal hint, not just today's value.
  2. Binance new-listing / delisting watch — free, no key. Diff the futures
     exchangeInfo symbol set vs a persisted snapshot to flag fresh listings
     (potential high-vol opportunity) and removals (force-avoid).
  3. CryptoPanic news — optional token (env CRYPTOPANIC_TOKEN); bullish/bearish
     post tags. Skipped (degraded) when no token is configured.
  4. Social sentiment (FinBERT/CryptoBERT over Reddit) — optional and LAZY: the
     model is only imported if cfg.social_enabled AND the libraries are present.
     On a small VPS this stays off by default to protect disk/memory.

The engine produces an aggregate sentiment_score in [-1, 1] (negative = bearish)
plus discrete event alerts, cached to respect rate limits.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

FNG_HISTORY_URL = "https://api.alternative.me/fng/?limit={limit}&format=json"
CRYPTOPANIC_URL = "https://cryptopanic.com/api/developer/v2/posts/?auth_token={token}&public=true"

_CACHE: dict[str, Any] = {}


def _dec(value: Any, default: str = "0") -> Decimal:
    try:
        if value is None or value == "":
            return Decimal(default)
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


@dataclass
class SentimentConfig:
    enabled: bool = False
    cache_seconds: int = 300
    fng_enabled: bool = True
    fng_history: int = 14
    listings_enabled: bool = True
    listings_state_path: str = "logs/sentiment-listings-state.json"
    listings_futures_base_url: str = "https://fapi.binance.com"
    cryptopanic_enabled: bool = False
    cryptopanic_token: str = ""
    social_enabled: bool = False  # off by default (VPS disk/memory)
    # Aggregate weights (relative).
    weight_fng: Decimal = Decimal("1.0")
    weight_news: Decimal = Decimal("1.0")
    weight_social: Decimal = Decimal("1.0")


def sentiment_from_config(raw: dict[str, Any] | None) -> SentimentConfig:
    raw = raw or {}
    import os

    token = str(raw.get("cryptopanic_token", "") or "")
    if token.startswith("env:"):
        token = os.environ.get(token[4:], "")
    elif not token:
        token = os.environ.get("CRYPTOPANIC_TOKEN", "")
    return SentimentConfig(
        enabled=bool(raw.get("enabled", False)),
        cache_seconds=int(raw.get("cache_seconds", 300)),
        fng_enabled=bool(raw.get("fng_enabled", True)),
        fng_history=int(raw.get("fng_history", 14)),
        listings_enabled=bool(raw.get("listings_enabled", True)),
        listings_state_path=str(raw.get("listings_state_path", "logs/sentiment-listings-state.json")),
        listings_futures_base_url=str(raw.get("listings_futures_base_url", "https://fapi.binance.com")),
        cryptopanic_enabled=bool(raw.get("cryptopanic_enabled", bool(token))),
        cryptopanic_token=token,
        social_enabled=bool(raw.get("social_enabled", False)),
        weight_fng=_dec(raw.get("weight_fng", "1.0"), "1.0"),
        weight_news=_dec(raw.get("weight_news", "1.0"), "1.0"),
        weight_social=_dec(raw.get("weight_social", "1.0"), "1.0"),
    )


def _http_json(url: str, timeout: int = 8) -> Any:
    from proxy_http import urlopen as proxy_urlopen

    request = urllib.request.Request(url, headers={"User-Agent": "market-autotrader/1.0"})
    with proxy_urlopen(request, timeout_seconds=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_fng_trend(history: int = 14) -> dict[str, Any]:
    """Fear & Greed with a multi-period trend and extreme-reversal hint.

    Returns score in [-1, 1] where +1 = extreme greed, -1 = extreme fear, and a
    contrarian flag (extreme readings often precede mean reversion).
    """
    try:
        payload = _http_json(FNG_HISTORY_URL.format(limit=max(history, 2)))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError, OSError) as exc:
        return {"enabled": True, "error": str(exc), "score": 0.0}
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    if not rows:
        return {"enabled": True, "error": "empty fng payload", "score": 0.0}
    values = []
    for r in rows:
        try:
            values.append(int(r.get("value", 0)))
        except (TypeError, ValueError):
            continue
    if not values:
        return {"enabled": True, "error": "no numeric fng values", "score": 0.0}
    latest = values[0]
    avg = sum(values) / len(values)
    # Map 0..100 -> -1..1
    score = (Decimal(latest) - Decimal("50")) / Decimal("50")
    trend = "rising" if latest > avg + 3 else ("falling" if latest < avg - 3 else "flat")
    contrarian = None
    if latest <= 15:
        contrarian = "extreme_fear_reversal_watch"  # potential bottom
    elif latest >= 85:
        contrarian = "extreme_greed_reversal_watch"  # potential top
    bias = "neutral"
    if latest <= 25:
        bias = "extreme_fear"
    elif latest <= 45:
        bias = "fear"
    elif latest >= 75:
        bias = "extreme_greed"
    elif latest >= 55:
        bias = "greed"
    return {
        "enabled": True,
        "source": "alternative.me/fng",
        "value": latest,
        "average": round(avg, 1),
        "bias": bias,
        "trend": trend,
        "contrarian": contrarian,
        "score": float(score),
    }


def detect_listing_changes(cfg: SentimentConfig) -> dict[str, Any]:
    """Diff the futures symbol universe vs a persisted snapshot.

    New TRADING symbols are flagged as `newListings`; removed/settling symbols as
    `delistings`. Pure set diff against exchangeInfo — no public-announcement API
    exists, and scraping announcement HTML is blocked, so this is the robust path.
    """
    base = cfg.listings_futures_base_url.rstrip("/")
    try:
        payload = _http_json(f"{base}/fapi/v1/exchangeInfo", timeout=10)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError, OSError) as exc:
        return {"enabled": True, "error": str(exc)}
    symbols = payload.get("symbols", []) if isinstance(payload, dict) else []
    trading = {
        str(s.get("symbol"))
        for s in symbols
        if isinstance(s, dict) and str(s.get("status", "")).upper() == "TRADING"
    }
    if not trading:
        return {"enabled": True, "error": "no trading symbols in exchangeInfo"}

    state_path = Path(cfg.listings_state_path)
    previous: set[str] = set()
    first_run = True
    if state_path.exists():
        try:
            prev = json.loads(state_path.read_text(encoding="utf-8"))
            previous = set(prev.get("symbols", []))
            first_run = not previous
        except (OSError, json.JSONDecodeError):
            previous = set()
    new_listings = sorted(trading - previous) if previous else []
    delistings = sorted(previous - trading) if previous else []

    # Persist the new snapshot (atomic-ish; best effort).
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_name(state_path.name + ".tmp")
        tmp.write_text(json.dumps({"symbols": sorted(trading), "updatedAt": int(time.time())}), encoding="utf-8")
        tmp.replace(state_path)
    except OSError:
        pass

    return {
        "enabled": True,
        "tradingCount": len(trading),
        "firstRun": first_run,
        # On first run we have no baseline, so suppress noise.
        "newListings": [] if first_run else new_listings[:20],
        "delistings": [] if first_run else delistings[:20],
    }


def fetch_cryptopanic(cfg: SentimentConfig) -> dict[str, Any]:
    """Bullish/bearish news tally from CryptoPanic (optional token)."""
    if not cfg.cryptopanic_token:
        return {"enabled": False, "reason": "no cryptopanic_token configured"}
    try:
        payload = _http_json(CRYPTOPANIC_URL.format(token=cfg.cryptopanic_token), timeout=10)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError, OSError) as exc:
        return {"enabled": True, "error": str(exc), "score": 0.0}
    posts = payload.get("results", []) if isinstance(payload, dict) else []
    bullish = bearish = 0
    for p in posts:
        votes = p.get("votes") if isinstance(p, dict) else None
        panic = p.get("panic_score") if isinstance(p, dict) else None
        if isinstance(votes, dict):
            bullish += int(votes.get("positive", 0) or 0)
            bearish += int(votes.get("negative", 0) or 0)
        # v2 may carry a sentiment field instead of votes
        sentiment = str(p.get("sentiment", "")) if isinstance(p, dict) else ""
        if sentiment == "positive":
            bullish += 1
        elif sentiment == "negative":
            bearish += 1
    total = bullish + bearish
    score = 0.0 if total == 0 else (bullish - bearish) / total
    return {
        "enabled": True,
        "source": "cryptopanic",
        "posts": len(posts),
        "bullish": bullish,
        "bearish": bearish,
        "score": float(score),
    }


def fetch_social_sentiment(cfg: SentimentConfig) -> dict[str, Any]:
    """LAZY social model. Off by default; only runs if libs are installed."""
    if not cfg.social_enabled:
        return {"enabled": False, "reason": "social_enabled=false"}
    try:
        import transformers  # noqa: F401  (lazy import; heavy)
    except Exception:
        return {
            "enabled": True,
            "error": "transformers not installed; social sentiment skipped",
            "score": 0.0,
        }
    # Intentionally a stub hook: wiring PRAW + CryptoBERT is opt-in future work to
    # avoid pulling large model weights onto a 29G VPS. Returns neutral until then.
    return {"enabled": True, "source": "social", "score": 0.0, "note": "model hook not yet wired"}


def evaluate_sentiment(cfg: SentimentConfig, *, timestamp: int | None = None) -> dict[str, Any]:
    """Aggregate all enabled sources into a sentiment block.

    Returns: {enabled, score (-1..1), label, sources{...}, alerts[...]}.
    Cached for cfg.cache_seconds. Never raises — sources degrade individually.
    """
    if not cfg.enabled:
        return {"enabled": False}
    now = timestamp or int(time.time())
    cached = _CACHE.get("payload")
    if cached and now - int(_CACHE.get("cached_at", 0)) <= cfg.cache_seconds:
        return cached

    sources: dict[str, Any] = {}
    weighted_sum = Decimal("0")
    weight_total = Decimal("0")
    alerts: list[str] = []

    if cfg.fng_enabled:
        fng = fetch_fng_trend(cfg.fng_history)
        sources["fearGreed"] = fng
        if "score" in fng and "error" not in fng:
            weighted_sum += _dec(fng["score"]) * cfg.weight_fng
            weight_total += cfg.weight_fng
            if fng.get("contrarian"):
                alerts.append(f"fng:{fng['contrarian']} (value={fng.get('value')})")

    if cfg.listings_enabled:
        listings = detect_listing_changes(cfg)
        sources["listings"] = listings
        for sym in listings.get("newListings", []) or []:
            alerts.append(f"new_listing:{sym}")
        for sym in listings.get("delistings", []) or []:
            alerts.append(f"delisting:{sym}")

    if cfg.cryptopanic_enabled:
        news = fetch_cryptopanic(cfg)
        sources["news"] = news
        if "score" in news and "error" not in news and news.get("enabled"):
            weighted_sum += _dec(news["score"]) * cfg.weight_news
            weight_total += cfg.weight_news

    if cfg.social_enabled:
        social = fetch_social_sentiment(cfg)
        sources["social"] = social
        if "score" in social and "error" not in social and social.get("enabled"):
            weighted_sum += _dec(social["score"]) * cfg.weight_social
            weight_total += cfg.weight_social

    score = (weighted_sum / weight_total) if weight_total > 0 else Decimal("0")
    score = max(Decimal("-1"), min(Decimal("1"), score))
    if score >= Decimal("0.33"):
        label = "bullish"
    elif score <= Decimal("-0.33"):
        label = "bearish"
    else:
        label = "neutral"

    result = {
        "enabled": True,
        "evaluatedAt": now,
        "score": float(score),
        "label": label,
        "sources": sources,
        "alerts": alerts,
    }
    _CACHE["payload"] = result
    _CACHE["cached_at"] = now
    return result


def sentiment_symbol_block_reason(sentiment: dict[str, Any] | None, symbol: str) -> str | None:
    """Block opening a position on a symbol that is being delisted."""
    if not sentiment or not sentiment.get("enabled"):
        return None
    listings = (sentiment.get("sources") or {}).get("listings") or {}
    delistings = {str(s).upper() for s in (listings.get("delistings") or [])}
    if symbol.upper() in delistings:
        return f"Sentiment: {symbol} is being delisted from Binance futures; no new open."
    return None

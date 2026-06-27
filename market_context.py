"""Market session phases, funding windows, sentiment, and macro bias for autotrader."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from growth_sizing import normalize_symbol


from proxy_http import request_json


def decimal_from(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))



FNG_URL = "https://api.alternative.me/fng/?limit=1&format=json"
_CACHE: dict[str, Any] = {"payload": None, "cached_at": 0}


@dataclass(frozen=True)
class MarketContextConfig:
    enabled: bool = True
    sentiment_enabled: bool = True
    session_tags_enabled: bool = True
    funding_guard_enabled: bool = True
    funding_guard_minutes: int = 30
    block_new_opens_pre_funding: bool = False
    block_new_opens_sessions: tuple[str, ...] = ()
    session_guard_dynamic_enabled: bool = True
    session_guard_min_sample: int = 6
    session_guard_max_win_rate: Decimal = Decimal("0.30")
    session_guard_max_profit_factor: Decimal = Decimal("0.85")
    session_guard_max_total_pnl: Decimal = Decimal("0")
    macro_btc_symbol: str = "BTCUSDT"
    macro_futures_base_url: str = "https://fapi.binance.com"
    cache_seconds: int = 300
    snapshot_path: str = "logs/market-context-latest.json"
    # Raw `sentiment` config section (Stage 4). When present and enabled, the full
    # sentiment_engine (F&G trend + listings + news + social) replaces the simple
    # one-shot F&G read in the context's `sentiment` slot.
    sentiment_raw: dict[str, Any] = field(default_factory=dict)


def market_context_from_config(raw: dict[str, Any] | None) -> MarketContextConfig:
    raw = raw or {}
    return MarketContextConfig(
        enabled=bool(raw.get("enabled", True)),
        sentiment_enabled=bool(raw.get("sentiment_enabled", True)),
        session_tags_enabled=bool(raw.get("session_tags_enabled", True)),
        funding_guard_enabled=bool(raw.get("funding_guard_enabled", True)),
        funding_guard_minutes=int(raw.get("funding_guard_minutes", 30)),
        block_new_opens_pre_funding=bool(raw.get("block_new_opens_pre_funding", False)),
        block_new_opens_sessions=tuple(str(v) for v in raw.get("block_new_opens_sessions", []) if str(v)),
        session_guard_dynamic_enabled=bool(raw.get("session_guard_dynamic_enabled", True)),
        session_guard_min_sample=int(raw.get("session_guard_min_sample", 6)),
        session_guard_max_win_rate=decimal_from(raw.get("session_guard_max_win_rate", "0.30")),
        session_guard_max_profit_factor=decimal_from(raw.get("session_guard_max_profit_factor", "0.85")),
        session_guard_max_total_pnl=decimal_from(raw.get("session_guard_max_total_pnl", "0")),
        macro_btc_symbol=normalize_symbol(str(raw.get("macro_btc_symbol", "BTCUSDT"))),
        macro_futures_base_url=str(raw.get("macro_futures_base_url", "https://fapi.binance.com")),
        cache_seconds=int(raw.get("cache_seconds", 300)),
        snapshot_path=str(raw.get("snapshot_path", "logs/market-context-latest.json")),
        sentiment_raw=dict(raw.get("sentiment") or {}),
    )


def utc_hour_minute(timestamp: int | None = None) -> tuple[int, int]:
    dt = datetime.fromtimestamp(timestamp or time.time(), tz=timezone.utc)
    return dt.hour, dt.minute


def session_phase_label(timestamp: int | None = None) -> dict[str, Any]:
    hour, minute = utc_hour_minute(timestamp)
    minutes = hour * 60 + minute
    phases: list[str] = []
    if 0 <= minutes < 8 * 60:
        phases.append("asia")
    if 7 * 60 <= minutes < 16 * 60:
        phases.append("europe")
    if 13 * 60 <= minutes < 22 * 60:
        phases.append("us")
    if not phases:
        phases.append("off_hours")
    primary = phases[0]
    if len(phases) > 1:
        primary = "overlap_" + "_".join(phases)
    labels = {
        "asia": "亚盘",
        "europe": "欧盘",
        "us": "美盘",
        "off_hours": "非主要时段",
    }
    if primary.startswith("overlap"):
        label = "重叠时段(" + "/".join(labels.get(p, p) for p in phases) + ")"
    else:
        label = labels.get(primary, primary)
    return {
        "primary": primary,
        "activeSessions": phases,
        "label": label,
        "utcHour": hour,
    }


def funding_window_state(timestamp: int | None = None, *, guard_minutes: int = 30) -> dict[str, Any]:
    ts = timestamp or int(time.time())
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    hour = dt.hour
    minute = dt.minute
    minute_of_day = hour * 60 + minute
    funding_hours = (0, 8, 16)
    nearest_minutes = min(
        ((fh * 60 - minute_of_day) % (24 * 60) for fh in funding_hours),
        key=lambda delta: delta if delta <= 12 * 60 else 24 * 60 - delta,
    )
    # Find closest funding event in minutes (signed: negative = before, positive = after)
    best_event = None
    best_delta = 10**9
    for fh in funding_hours:
        event_min = fh * 60
        for day_offset in (-1, 0, 1):
            event_abs = event_min + day_offset * 24 * 60
            delta = minute_of_day - event_abs
            if abs(delta) < abs(best_delta):
                best_delta = delta
                best_event = fh
    minutes_to_funding = -best_delta
    phase = "mid"
    if abs(minutes_to_funding) <= guard_minutes:
        phase = "pre" if minutes_to_funding > 0 else "post"
    return {
        "phase": phase,
        "minutesToFunding": minutes_to_funding,
        "nextFundingHourUtc": best_event,
        "guardMinutes": guard_minutes,
        "label": {"pre": "资金费结算前", "post": "资金费结算后", "mid": "资金费周期中段"}.get(phase, phase),
    }


def fetch_fear_greed_index() -> dict[str, Any]:
    from proxy_http import urlopen as proxy_urlopen

    request = urllib.request.Request(FNG_URL, headers={"User-Agent": "market-autotrader/1.0"})
    try:
        with proxy_urlopen(request, timeout_seconds=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as exc:
        return {"enabled": True, "error": str(exc)}
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    if not rows:
        return {"enabled": True, "error": "empty fear/greed payload"}
    row = rows[0]
    value = int(row.get("value", 0))
    classification = str(row.get("value_classification", "unknown"))
    bias = "neutral"
    if value <= 25:
        bias = "extreme_fear"
    elif value <= 45:
        bias = "fear"
    elif value >= 75:
        bias = "extreme_greed"
    elif value >= 55:
        bias = "greed"
    return {
        "enabled": True,
        "source": "alternative.me/fng",
        "value": value,
        "classification": classification,
        "bias": bias,
        "timestamp": row.get("timestamp"),
    }


def fetch_btc_macro_bias(cfg: MarketContextConfig) -> dict[str, Any]:
    symbol = cfg.macro_btc_symbol
    params = urllib.parse.urlencode({"symbol": symbol, "interval": "1h", "limit": 24})
    url = f"{cfg.macro_futures_base_url.rstrip('/')}/fapi/v1/klines?{params}"
    try:
        rows = request_json(url, timeout_seconds=10)
    except RuntimeError as exc:
        return {"symbol": symbol, "error": str(exc)}
    if not isinstance(rows, list) or len(rows) < 2:
        return {"symbol": symbol, "error": "insufficient klines"}
    closes = [decimal_from(item[4]) for item in rows]
    first = closes[0]
    last = closes[-1]
    change_pct = (last - first) / first if first > 0 else Decimal("0")
    bias = "neutral"
    if change_pct >= Decimal("0.015"):
        bias = "risk_on"
    elif change_pct <= Decimal("-0.015"):
        bias = "risk_off"
    return {
        "symbol": symbol,
        "interval": "1h",
        "lookbackBars": len(closes),
        "changePct": str(change_pct.quantize(Decimal("0.0001"))),
        "lastClose": str(last),
        "bias": bias,
        "label": {"risk_on": "BTC偏强", "risk_off": "BTC偏弱", "neutral": "BTC中性"}.get(bias, bias),
    }


def evaluate_market_context(cfg: MarketContextConfig, *, timestamp: int | None = None) -> dict[str, Any]:
    if not cfg.enabled:
        return {"enabled": False}

    now = timestamp or int(time.time())
    cached = _CACHE.get("payload")
    if cached and now - int(_CACHE.get("cached_at", 0)) <= cfg.cache_seconds:
        return cached

    result: dict[str, Any] = {
        "enabled": True,
        "evaluatedAt": now,
        "evaluatedAtIso": datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if cfg.session_tags_enabled:
        result["session"] = session_phase_label(now)
    if cfg.funding_guard_enabled:
        result["funding"] = funding_window_state(now, guard_minutes=cfg.funding_guard_minutes)
    if cfg.sentiment_enabled:
        # Stage 4: when a `sentiment` section is enabled, use the full engine
        # (F&G trend + listings + news + social); otherwise fall back to the
        # simple one-shot Fear & Greed read.
        if cfg.sentiment_raw.get("enabled"):
            try:
                from sentiment_engine import evaluate_sentiment, sentiment_from_config

                result["sentiment"] = evaluate_sentiment(
                    sentiment_from_config(cfg.sentiment_raw), timestamp=now
                )
            except Exception as exc:  # never let sentiment break context
                result["sentiment"] = {"enabled": True, "error": f"sentiment_engine: {exc}"}
        else:
            result["sentiment"] = fetch_fear_greed_index()
    result["macro"] = fetch_btc_macro_bias(cfg)

    _CACHE["payload"] = result
    _CACHE["cached_at"] = now
    return result


def market_context_block_reason(
    ctx: dict[str, Any],
    *,
    is_reduce_only: bool,
    block_pre_funding: bool,
    block_sessions: tuple[str, ...] = (),
    trade_learning: dict[str, Any] | None = None,
    session_guard_dynamic_enabled: bool = False,
    session_guard_min_sample: int = 6,
    session_guard_max_win_rate: Decimal = Decimal("0.30"),
    session_guard_max_profit_factor: Decimal = Decimal("0.85"),
    session_guard_max_total_pnl: Decimal = Decimal("0"),
) -> str | None:
    if not ctx.get("enabled") or is_reduce_only:
        return None
    session = ctx.get("session") or {}
    primary = str(session.get("primary") or "")
    if primary and primary in set(block_sessions):
        label = session.get("label") or primary
        if session_guard_dynamic_enabled and trade_learning:
            stat = (trade_learning.get("sessionStats") or {}).get(primary) or {}
            sample = int(stat.get("sampleSize", 0) or 0)
            if sample >= session_guard_min_sample:
                win_rate = decimal_from(stat.get("winRate", "0"))
                profit_factor = decimal_from(stat.get("profitFactor", "0"))
                total_pnl = decimal_from(stat.get("totalPnl", "0"))
                if (
                    win_rate <= session_guard_max_win_rate
                    and profit_factor <= session_guard_max_profit_factor
                    and total_pnl <= session_guard_max_total_pnl
                ):
                    return (
                        f"Session guard: {label} live edge weak "
                        f"(n={sample}, WR={win_rate:.0%}, PF={profit_factor}, PnL={total_pnl}); "
                        "new opens blocked."
                    )
                return None
            return None
        return f"Session guard: {label} is configured as a blocked open session; new opens blocked."
    funding = ctx.get("funding") or {}
    if block_pre_funding and funding.get("phase") == "pre":
        mins = funding.get("minutesToFunding", "?")
        return f"Funding guard: {mins}m to next funding settlement; new opens blocked."
    macro = ctx.get("macro") or {}
    if macro.get("bias") == "risk_off":
        return None  # advisory only; benchmark_gate handles hard block
    return None


def market_context_rationale_block(ctx: dict[str, Any]) -> dict[str, Any]:
    if not ctx.get("enabled"):
        return {"enabled": False}
    block: dict[str, Any] = {"enabled": True}
    if session := ctx.get("session"):
        block["session"] = session
    if funding := ctx.get("funding"):
        block["funding"] = funding
    if sentiment := ctx.get("sentiment"):
        block["sentiment"] = sentiment
    if macro := ctx.get("macro"):
        block["macro"] = macro
    return block


def save_market_context_snapshot(path: str, ctx: dict[str, Any]) -> None:
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")

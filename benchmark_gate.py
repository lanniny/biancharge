"""Benchmark drawdown gate — block new opens when BTC (or configured symbol) is in stress."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from market_autotrader import decimal_from, normalize_symbol

FUTURES_BASE = "https://fapi.binance.com"
_CACHE: dict[str, Any] = {"payload": None, "cached_at": 0}


@dataclass(frozen=True)
class BenchmarkGateConfig:
    enabled: bool = False
    symbol: str = "BTCUSDT"
    lookback_days: int = 7
    max_drawdown_pct: Decimal = Decimal("0.08")
    block_new_opens_only: bool = True
    cache_seconds: int = 300
    futures_base_url: str = FUTURES_BASE


def benchmark_gate_from_config(raw: dict[str, Any] | None) -> BenchmarkGateConfig:
    raw = raw or {}
    return BenchmarkGateConfig(
        enabled=bool(raw.get("enabled", False)),
        symbol=normalize_symbol(str(raw.get("symbol", "BTCUSDT"))),
        lookback_days=int(raw.get("lookback_days", 7)),
        max_drawdown_pct=decimal_from(raw.get("max_drawdown_pct", "0.08")),
        block_new_opens_only=bool(raw.get("block_new_opens_only", True)),
        cache_seconds=int(raw.get("cache_seconds", 300)),
        futures_base_url=str(raw.get("futures_base_url", FUTURES_BASE)),
    )


def fetch_daily_closes(symbol: str, limit: int, base_url: str) -> list[Decimal]:
    from proxy_http import urlopen as proxy_urlopen

    params = urllib.parse.urlencode({"symbol": symbol, "interval": "1d", "limit": limit})
    url = f"{base_url.rstrip('/')}/fapi/v1/klines?{params}"
    request = urllib.request.Request(url, headers={"User-Agent": "benchmark-gate/1.0"})
    with proxy_urlopen(request, timeout_seconds=12) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return [decimal_from(item[4]) for item in payload]


def rolling_peak_drawdown(closes: list[Decimal]) -> Decimal:
    if not closes:
        return Decimal("0")
    peak = closes[0]
    max_dd = Decimal("0")
    for price in closes:
        if price > peak:
            peak = price
        if peak > 0:
            dd = (peak - price) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd


def evaluate_benchmark_gate(config: BenchmarkGateConfig) -> dict[str, Any]:
    if not config.enabled:
        return {"enabled": False, "active": False}

    now = int(time.time())
    cached = _CACHE.get("payload")
    if cached and now - int(_CACHE.get("cached_at", 0)) <= config.cache_seconds:
        return cached

    try:
        closes = fetch_daily_closes(config.symbol, config.lookback_days + 1, config.futures_base_url)
        drawdown = rolling_peak_drawdown(closes)
        active = drawdown >= config.max_drawdown_pct
        result = {
            "enabled": True,
            "active": active,
            "symbol": config.symbol,
            "lookbackDays": config.lookback_days,
            "drawdownPct": str(drawdown.quantize(Decimal("0.0001"))),
            "maxDrawdownPct": str(config.max_drawdown_pct),
            "lastClose": str(closes[-1]) if closes else None,
            "blockNewOpensOnly": config.block_new_opens_only,
            "evaluatedAt": now,
            "evaluatedAtIso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        }
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, RuntimeError) as exc:
        result = {
            "enabled": True,
            "active": False,
            "error": str(exc),
            "symbol": config.symbol,
            "evaluatedAt": now,
        }

    _CACHE["payload"] = result
    _CACHE["cached_at"] = now
    return result


def blocks_new_open(gate: dict[str, Any], *, is_reduce_only: bool) -> tuple[bool, str | None]:
    if not gate.get("enabled") or not gate.get("active"):
        return False, None
    if gate.get("blockNewOpensOnly") and is_reduce_only:
        return False, None
    dd = gate.get("drawdownPct", "?")
    sym = gate.get("symbol", "BTCUSDT")
    return True, f"Benchmark gate active: {sym} {gate.get('lookbackDays', 7)}d drawdown {dd} >= cap {gate.get('maxDrawdownPct')}."

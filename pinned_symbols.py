"""Runtime-pinned symbols merged into market discovery watchlists."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from market_autotrader import normalize_symbol

MARKET_FUTURES = "binance_futures"
MARKET_SPOT = "binance_spot"
DEFAULT_STORE = "config/pinned-symbols.json"


def _normalize_market(market: str) -> str:
    value = market.strip().lower()
    if value in {"futures", "binance_futures", "future"}:
        return MARKET_FUTURES
    if value in {"spot", "binance_spot"}:
        return MARKET_SPOT
    return market


def load_pins(path: str = DEFAULT_STORE) -> list[dict[str, str]]:
    target = Path(path)
    if not target.exists():
        return []
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    symbols = payload.get("symbols", payload if isinstance(payload, list) else [])
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in symbols:
        if isinstance(item, str):
            symbol = normalize_symbol(item)
            market = MARKET_FUTURES
        elif isinstance(item, dict):
            symbol = normalize_symbol(str(item.get("symbol", "")))
            market = _normalize_market(str(item.get("market", MARKET_FUTURES)))
        else:
            continue
        key = (symbol, market)
        if not symbol or key in seen:
            continue
        seen.add(key)
        result.append({"symbol": symbol, "market": market})
    return result


def save_pins(path: str, symbols: list[dict[str, str]]) -> dict[str, Any]:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "symbols": symbols,
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def add_pin(symbol: str, market: str, path: str = DEFAULT_STORE) -> dict[str, Any]:
    symbol = normalize_symbol(symbol)
    market = _normalize_market(market)
    pins = load_pins(path)
    key = (symbol, market)
    if not any((p["symbol"], p["market"]) == key for p in pins):
        pins.append({"symbol": symbol, "market": market, "pinnedAt": int(time.time())})
    return save_pins(path, pins)


def remove_pin(symbol: str, market: str, path: str = DEFAULT_STORE) -> dict[str, Any]:
    symbol = normalize_symbol(symbol)
    market = _normalize_market(market)
    pins = [p for p in load_pins(path) if not (p["symbol"] == symbol and p["market"] == market)]
    return save_pins(path, pins)


def merge_config_pins(config_pinned: tuple[dict[str, str], ...], store_path: str) -> tuple[dict[str, str], ...]:
    merged: dict[tuple[str, str], dict[str, str]] = {}
    for item in config_pinned:
        key = (normalize_symbol(item["symbol"]), _normalize_market(item["market"]))
        merged[key] = {"symbol": key[0], "market": key[1]}
    for item in load_pins(store_path):
        key = (item["symbol"], item["market"])
        merged[key] = {"symbol": key[0], "market": key[1]}
    return tuple(merged.values())

"""TTL cache for regime probes — avoids duplicate kline fetches per cycle."""

from __future__ import annotations

import time
from typing import Any

_CACHE: dict[tuple[str, str], tuple[str, int]] = {}
DEFAULT_TTL_SECONDS = 300


def get_cached_regime(symbol: str, market: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str | None:
    key = (symbol.upper(), market)
    entry = _CACHE.get(key)
    if not entry:
        return None
    regime_kind, cached_at = entry
    if int(time.time()) - cached_at > ttl_seconds:
        _CACHE.pop(key, None)
        return None
    return regime_kind


def set_cached_regime(symbol: str, market: str, regime_kind: str) -> None:
    _CACHE[(symbol.upper(), market)] = (regime_kind, int(time.time()))


def cache_stats() -> dict[str, Any]:
    now = int(time.time())
    return {"entries": len(_CACHE), "keys": [list(k) for k in _CACHE.keys()], "asOf": now}

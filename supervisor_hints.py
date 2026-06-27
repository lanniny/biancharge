"""Bridge realtime supervisor events into autotrader exit/entry bias."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REDUCE_ACTIONS = {"REDUCE_POSITION"}
REASSESS_ACTIONS = {"REASSESS_TAKE_PROFIT_OR_HOLD"}
EXIT_HINT_ACTIONS = REDUCE_ACTIONS | REASSESS_ACTIONS
PRIORITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper().replace("/", "").replace("-", "")


def _parse_event_ts(event: dict[str, Any]) -> int:
    for key in ("createdAtEpoch", "observedAt", "timestamp"):
        raw = event.get(key)
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
    created = str(event.get("createdAt") or "")
    if created:
        try:
            from datetime import datetime

            return int(datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return 0


def load_supervisor_hints(
    events_path: Path,
    *,
    max_age_seconds: int = 900,
    now: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Return newest exit-related supervisor hint per symbol."""
    if not events_path.exists():
        return {}
    import time

    current = now if now is not None else int(time.time())
    hints: dict[str, dict[str, Any]] = {}
    for line in events_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        action = str(event.get("action") or "")
        if action not in EXIT_HINT_ACTIONS:
            continue
        symbol = normalize_symbol(str(event.get("symbol") or ""))
        if not symbol:
            continue
        ts = _parse_event_ts(event)
        if ts and current - ts > max_age_seconds:
            continue
        priority = str(event.get("priority") or "medium")
        candidate = {
            "action": action,
            "symbol": symbol,
            "market": str(event.get("market") or ""),
            "priority": priority,
            "reason": str(event.get("reason") or event.get("message") or ""),
            "eventId": event.get("eventId"),
            "createdAt": event.get("createdAt"),
            "observedAt": ts or current,
        }
        prev = hints.get(symbol)
        if prev is None or PRIORITY_RANK.get(priority, 0) >= PRIORITY_RANK.get(str(prev.get("priority")), 0):
            hints[symbol] = candidate
    return hints


def supervisor_hints_from_config(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = config.get("supervisor_bridge") or {}
    if not raw.get("enabled", False):
        return {}
    path = Path(str(raw.get("events_path", "logs/realtime-supervisor-events.jsonl")))
    max_age = int(raw.get("max_age_seconds", 900))
    return load_supervisor_hints(path, max_age_seconds=max_age)

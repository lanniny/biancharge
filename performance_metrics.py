"""Aggregate live trading performance for dashboard and health checks."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any


def decimal_from(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def load_outcomes(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def summarize_performance(
    outcomes: list[dict[str, Any]],
    *,
    memory_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    closed = [row for row in outcomes if row.get("realizedPnl") is not None]
    wins = losses = 0
    total_pnl = Decimal("0")
    win_sum = loss_sum = Decimal("0")
    by_source: dict[str, int] = {}
    long_pnl = short_pnl = Decimal("0")
    long_n = short_n = 0

    for row in closed:
        pnl = decimal_from(row.get("realizedPnl", "0"))
        total_pnl += pnl
        side = str(row.get("positionSide", "LONG")).upper()
        if side == "SHORT":
            short_n += 1
            short_pnl += pnl
        else:
            long_n += 1
            long_pnl += pnl
        if pnl > 0:
            wins += 1
            win_sum += pnl
        elif pnl < 0:
            losses += 1
            loss_sum += abs(pnl)
        src = str(row.get("closeSource") or "unknown")
        by_source[src] = by_source.get(src, 0) + 1

    total = len(closed)
    win_rate = (Decimal(wins) / Decimal(total)) if total else Decimal("0")
    profit_factor = (win_sum / loss_sum) if loss_sum > 0 else None
    avg_win = (win_sum / Decimal(wins)) if wins else Decimal("0")
    avg_loss = (-loss_sum / Decimal(losses)) if losses else Decimal("0")

    commission_today = Decimal("0")
    funding_today = Decimal("0")
    daily_trades = 0
    if memory_state:
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).date().isoformat()
        commission_today = decimal_from(
            (memory_state.get("daily_commission_quote") or {}).get(today, "0")
        )
        funding_today = decimal_from(
            (memory_state.get("daily_funding_fee_quote") or {}).get(today, "0")
        )
        daily_trades = int((memory_state.get("daily_trade_counts") or {}).get(today, 0))

    net_including_costs = total_pnl - commission_today - funding_today

    return {
        "closedTrades": total,
        "wins": wins,
        "losses": losses,
        "winRate": str(win_rate.quantize(Decimal("0.01"))),
        "totalRealizedPnl": str(total_pnl.quantize(Decimal("0.0001"))),
        "netAfterTodayCosts": str(net_including_costs.quantize(Decimal("0.0001"))),
        "commissionToday": str(commission_today),
        "fundingToday": str(funding_today),
        "dailyTradesToday": daily_trades,
        "avgWin": str(avg_win.quantize(Decimal("0.0001"))),
        "avgLoss": str(avg_loss.quantize(Decimal("0.0001"))),
        "profitFactor": str(profit_factor.quantize(Decimal("0.01"))) if profit_factor else None,
        "longPnl": str(long_pnl.quantize(Decimal("0.0001"))),
        "shortPnl": str(short_pnl.quantize(Decimal("0.0001"))),
        "longTrades": long_n,
        "shortTrades": short_n,
        "closeSourceCounts": by_source,
    }


def load_performance_summary(root: Path) -> dict[str, Any]:
    logs = root / "logs"
    outcomes = load_outcomes(logs / "trade-outcomes.jsonl")
    memory_state: dict[str, Any] | None = None
    state_path = logs / "live-trading-state.json"
    if state_path.exists():
        try:
            memory_state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            memory_state = None
    return summarize_performance(outcomes, memory_state=memory_state)


def compare_live_vs_shadow(
    live_outcomes: list[dict[str, Any]],
    shadow_summary: dict[str, Any],
) -> dict[str, Any]:
    live_perf = summarize_performance(live_outcomes)
    live_buckets: dict[str, Any] = {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in live_outcomes:
        ctx = row.get("openContext") or {}
        bucket = str(ctx.get("bucket") or "")
        if not bucket and str(ctx.get("source", "")).startswith("discovery:"):
            bucket = str(ctx.get("source", "")).split(":", 1)[-1]
        if not bucket:
            bucket = "unknown"
        grouped.setdefault(bucket, []).append(row)

    for bucket, rows in grouped.items():
        if bucket == "unknown":
            continue
        wins = sum(1 for row in rows if decimal_from(row.get("realizedPnl", "0")) > 0)
        losses = sum(1 for row in rows if decimal_from(row.get("realizedPnl", "0")) < 0)
        total = len(rows)
        pnl = sum((decimal_from(row.get("realizedPnl", "0")) for row in rows), Decimal("0"))
        wr = (Decimal(wins) / Decimal(total)) if total else Decimal("0")
        live_buckets[bucket] = {
            "closedTrades": total,
            "wins": wins,
            "losses": losses,
            "winRate": str(wr.quantize(Decimal("0.01"))),
            "totalRealizedPnl": str(pnl.quantize(Decimal("0.0001"))),
        }

    shadow_buckets = shadow_summary.get("bucketStats") or {}
    bucket_names = sorted(set(live_buckets) | set(shadow_buckets))
    buckets: dict[str, Any] = {}
    for bucket in bucket_names:
        live_row = live_buckets.get(bucket) or {}
        shadow_row = shadow_buckets.get(bucket) or {}
        buckets[bucket] = {
            "live": live_row,
            "shadow": shadow_row,
        }

    return {
        "global": {
            "live": {
                "closedTrades": live_perf.get("closedTrades", 0),
                "winRate": live_perf.get("winRate"),
                "totalRealizedPnl": live_perf.get("totalRealizedPnl"),
            },
            "shadow": {
                "closedTrades": shadow_summary.get("closedCount", 0),
                "winRate": shadow_summary.get("winRate"),
                "totalRealizedPnl": shadow_summary.get("totalRealizedPnl"),
            },
        },
        "buckets": buckets,
    }


def load_live_shadow_compare(root: Path) -> dict[str, Any]:
    logs = root / "logs"
    live_outcomes = load_outcomes(logs / "trade-outcomes.jsonl")
    shadow_summary: dict[str, Any] = {"closedCount": 0, "bucketStats": {}}
    for config_path in (root / "market_autotrader.growth.example.json", root / "market_autotrader.example.json"):
        if not config_path.exists():
            continue
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        from shadow_paper import load_shadow_summary, shadow_paper_from_config

        cfg = shadow_paper_from_config(raw.get("shadow_paper"))
        if cfg.enabled:
            shadow_summary = load_shadow_summary(cfg)
            break
    return compare_live_vs_shadow(live_outcomes, shadow_summary)

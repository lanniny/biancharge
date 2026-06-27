"""Shadow paper — record discovery would-be trades and simulate TP/SL exits."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

from growth_sizing import normalize_symbol


def decimal_from(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


@dataclass(frozen=True)
class ShadowPaperConfig:
    enabled: bool = False
    ledger_path: str = "logs/shadow-paper-trades.jsonl"
    state_path: str = "logs/shadow-paper-state.json"
    outcomes_path: str = "logs/shadow-paper-outcomes.jsonl"
    shadow_discovery_opens_only: bool = True
    min_confidence: Decimal = Decimal("0")
    take_profit_pct: Decimal = Decimal("0.02")
    stop_loss_pct: Decimal = Decimal("0.015")
    max_hold_hours: int = 48
    shadow_blocked_counterfactual: bool = False
    blocked_counterfactual_min_confidence: Decimal = Decimal("0.62")
    blocked_counterfactual_path: str = "logs/shadow-blocked-counterfactual.jsonl"
    blocked_counterfactual_max_per_cycle: int = 8


def shadow_paper_from_config(raw: dict[str, Any] | None) -> ShadowPaperConfig:
    if not raw:
        return ShadowPaperConfig(enabled=False)
    return ShadowPaperConfig(
        enabled=bool(raw.get("enabled", False)),
        ledger_path=str(raw.get("ledger_path", "logs/shadow-paper-trades.jsonl")),
        state_path=str(raw.get("state_path", "logs/shadow-paper-state.json")),
        outcomes_path=str(raw.get("outcomes_path", "logs/shadow-paper-outcomes.jsonl")),
        shadow_discovery_opens_only=bool(raw.get("shadow_discovery_opens_only", True)),
        min_confidence=decimal_from(raw.get("min_confidence", "0")),
        take_profit_pct=decimal_from(raw.get("take_profit_pct", "0.02")),
        stop_loss_pct=decimal_from(raw.get("stop_loss_pct", "0.015")),
        max_hold_hours=int(raw.get("max_hold_hours", 48)),
        shadow_blocked_counterfactual=bool(raw.get("shadow_blocked_counterfactual", False)),
        blocked_counterfactual_min_confidence=decimal_from(
            raw.get("blocked_counterfactual_min_confidence", "0.62")
        ),
        blocked_counterfactual_path=str(
            raw.get("blocked_counterfactual_path", "logs/shadow-blocked-counterfactual.jsonl")
        ),
        blocked_counterfactual_max_per_cycle=int(raw.get("blocked_counterfactual_max_per_cycle", 8)),
    )


def is_discovery_open(indicators: dict[str, str], *, reduce_only: bool, position_qty: Decimal) -> bool:
    if reduce_only or position_qty != 0:
        return False
    source = str(indicators.get("discovery_source", "") or "")
    return source.startswith("discovery:")


def should_shadow_instead_of_live(
    cfg: ShadowPaperConfig,
    *,
    reduce_only: bool,
    position_qty: Decimal,
    indicators: dict[str, str],
    confidence: Decimal,
    approved: bool,
    trade_learning_shadow: bool = False,
) -> bool:
    if not approved or reduce_only:
        return False
    if trade_learning_shadow:
        return cfg.enabled
    if not cfg.enabled or not cfg.shadow_discovery_opens_only:
        return False
    if not is_discovery_open(indicators, reduce_only=reduce_only, position_qty=position_qty):
        return False
    if confidence < cfg.min_confidence:
        return False
    return True


def append_shadow_trade(path: str, record: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _load_json(path: str | Path, default: Any) -> Any:
    target = Path(path)
    if not target.exists():
        return default
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _save_json(path: str | Path, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_shadow_state(cfg: ShadowPaperConfig) -> dict[str, Any]:
    payload = _load_json(cfg.state_path, {"openPositions": {}})
    if not isinstance(payload, dict):
        return {"openPositions": {}}
    payload.setdefault("openPositions", {})
    return payload


def save_shadow_state(cfg: ShadowPaperConfig, state: dict[str, Any]) -> None:
    state["updatedAt"] = int(time.time())
    _save_json(cfg.state_path, state)


def position_side_from_action(action: str) -> str:
    return "SHORT" if str(action).upper() == "SELL" else "LONG"


def open_shadow_position(cfg: ShadowPaperConfig, row: dict[str, Any]) -> bool:
    state = load_shadow_state(cfg)
    sym = normalize_symbol(str(row.get("symbol", "")))
    if not sym or sym in state["openPositions"]:
        return False
    state["openPositions"][sym] = {
        "symbol": sym,
        "action": row.get("action"),
        "positionSide": position_side_from_action(str(row.get("action", "BUY"))),
        "entryPrice": str(row.get("price")),
        "openedAt": int(row.get("timestamp") or time.time()),
        "bucket": row.get("bucket"),
        "source": row.get("source"),
        "trigger": row.get("trigger"),
        "quoteAmount": str(row.get("quoteAmount") or "0"),
    }
    save_shadow_state(cfg, state)
    return True


def load_shadow_outcomes(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def shadow_realized_pnl(
    *,
    entry_price: Decimal,
    exit_price: Decimal,
    quote_amount: Decimal,
    position_side: str,
) -> Decimal:
    if entry_price <= 0 or quote_amount <= 0:
        return Decimal("0")
    if position_side.upper() == "SHORT":
        pnl_pct = (entry_price - exit_price) / entry_price
    else:
        pnl_pct = (exit_price - entry_price) / entry_price
    return (quote_amount * pnl_pct).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)


def _close_reason(
    *,
    entry: Decimal,
    mark: Decimal,
    position_side: str,
    take_profit_pct: Decimal,
    stop_loss_pct: Decimal,
    opened_at: int,
    observed_at: int,
    max_hold_hours: int,
) -> str | None:
    side = position_side.upper()
    if side == "SHORT":
        if mark <= entry * (Decimal("1") - take_profit_pct):
            return "take_profit"
        if mark >= entry * (Decimal("1") + stop_loss_pct):
            return "stop_loss"
    else:
        if mark >= entry * (Decimal("1") + take_profit_pct):
            return "take_profit"
        if mark <= entry * (Decimal("1") - stop_loss_pct):
            return "stop_loss"
    if max_hold_hours > 0 and observed_at - opened_at >= max_hold_hours * 3600:
        return "max_hold"
    return None


def evaluate_shadow_positions(
    cfg: ShadowPaperConfig,
    *,
    prices: dict[str, Decimal],
    observed_at: int | None = None,
    take_profit_pct: Decimal | None = None,
    stop_loss_pct: Decimal | None = None,
) -> list[dict[str, Any]]:
    if not cfg.enabled:
        return []
    now = int(observed_at or time.time())
    tp = take_profit_pct if take_profit_pct is not None else cfg.take_profit_pct
    sl = stop_loss_pct if stop_loss_pct is not None else cfg.stop_loss_pct
    state = load_shadow_state(cfg)
    closed: list[dict[str, Any]] = []

    for sym, pos in list(state["openPositions"].items()):
        mark = prices.get(normalize_symbol(sym))
        if mark is None or mark <= 0:
            continue
        entry = decimal_from(pos.get("entryPrice"))
        if entry <= 0:
            continue
        opened_at = int(pos.get("openedAt") or now)
        position_side = str(pos.get("positionSide") or "LONG")
        reason = _close_reason(
            entry=entry,
            mark=mark,
            position_side=position_side,
            take_profit_pct=tp,
            stop_loss_pct=sl,
            opened_at=opened_at,
            observed_at=now,
            max_hold_hours=cfg.max_hold_hours,
        )
        if not reason:
            continue
        quote_amount = decimal_from(pos.get("quoteAmount"))
        pnl = shadow_realized_pnl(
            entry_price=entry,
            exit_price=mark,
            quote_amount=quote_amount,
            position_side=position_side,
        )
        outcome = {
            "symbol": normalize_symbol(sym),
            "positionSide": position_side,
            "entryPrice": str(entry),
            "exitPrice": str(mark),
            "quoteAmount": str(quote_amount),
            "realizedPnl": str(pnl),
            "closeReason": reason,
            "closedAt": now,
            "openedAt": opened_at,
            "bucket": pos.get("bucket"),
            "source": pos.get("source"),
            "trigger": pos.get("trigger"),
            "mode": "shadow_paper",
        }
        append_shadow_trade(cfg.outcomes_path, outcome)
        del state["openPositions"][sym]
        closed.append(outcome)

    if closed:
        save_shadow_state(cfg, state)
    return closed


def summarize_shadow_outcomes(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    wins = losses = 0
    total_pnl = Decimal("0")
    gross_win = Decimal("0")
    gross_loss = Decimal("0")
    bucket_stats: dict[str, dict[str, Any]] = {}
    for row in outcomes:
        pnl = decimal_from(row.get("realizedPnl", "0"))
        total_pnl += pnl
        if pnl > 0:
            wins += 1
            gross_win += pnl
        elif pnl < 0:
            losses += 1
            gross_loss += abs(pnl)
        bucket = str(row.get("bucket") or "unknown")
        stat = bucket_stats.setdefault(
            bucket,
            {
                "wins": 0,
                "losses": 0,
                "sampleSize": 0,
                "totalPnl": Decimal("0"),
                "grossWin": Decimal("0"),
                "grossLoss": Decimal("0"),
            },
        )
        stat["sampleSize"] += 1
        stat["totalPnl"] += pnl
        if pnl > 0:
            stat["wins"] += 1
            stat["grossWin"] += pnl
        elif pnl < 0:
            stat["losses"] += 1
            stat["grossLoss"] += abs(pnl)
    total = len(outcomes)
    win_rate = (Decimal(wins) / Decimal(total)) if total else Decimal("0")
    avg_win = gross_win / Decimal(wins) if wins else Decimal("0")
    avg_loss = gross_loss / Decimal(losses) if losses else Decimal("0")
    profit_factor = gross_win / gross_loss if gross_loss > 0 else (Decimal("999") if gross_win > 0 else Decimal("0"))
    formatted_buckets: dict[str, Any] = {}
    for bucket, stat in bucket_stats.items():
        sample = int(stat["sampleSize"])
        wr = (Decimal(stat["wins"]) / Decimal(sample)) if sample else Decimal("0")
        bucket_wins = int(stat["wins"])
        bucket_losses = int(stat["losses"])
        bucket_gross_win = stat["grossWin"]
        bucket_gross_loss = stat["grossLoss"]
        bucket_avg_win = bucket_gross_win / Decimal(bucket_wins) if bucket_wins else Decimal("0")
        bucket_avg_loss = bucket_gross_loss / Decimal(bucket_losses) if bucket_losses else Decimal("0")
        bucket_pf = (
            bucket_gross_win / bucket_gross_loss
            if bucket_gross_loss > 0
            else (Decimal("999") if bucket_gross_win > 0 else Decimal("0"))
        )
        formatted_buckets[bucket] = {
            "sampleSize": sample,
            "wins": stat["wins"],
            "losses": stat["losses"],
            "winRate": str(wr.quantize(Decimal("0.01"))),
            "totalPnl": str(stat["totalPnl"].quantize(Decimal("0.0001"))),
            "grossWin": str(bucket_gross_win.quantize(Decimal("0.0001"))),
            "grossLoss": str(bucket_gross_loss.quantize(Decimal("0.0001"))),
            "avgWin": str(bucket_avg_win.quantize(Decimal("0.0001"))),
            "avgLoss": str(bucket_avg_loss.quantize(Decimal("0.0001"))),
            "profitFactor": str(bucket_pf.quantize(Decimal("0.01"))),
        }
    return {
        "closedCount": total,
        "wins": wins,
        "losses": losses,
        "winRate": str(win_rate.quantize(Decimal("0.01"))),
        "totalRealizedPnl": str(total_pnl.quantize(Decimal("0.0001"))),
        "grossWin": str(gross_win.quantize(Decimal("0.0001"))),
        "grossLoss": str(gross_loss.quantize(Decimal("0.0001"))),
        "avgWin": str(avg_win.quantize(Decimal("0.0001"))),
        "avgLoss": str(avg_loss.quantize(Decimal("0.0001"))),
        "profitFactor": str(profit_factor.quantize(Decimal("0.01"))),
        "bucketStats": formatted_buckets,
    }


def load_shadow_summary(cfg_or_path: ShadowPaperConfig | Path) -> dict[str, Any]:
    if isinstance(cfg_or_path, ShadowPaperConfig):
        cfg = cfg_or_path
        ledger_path = Path(cfg.ledger_path)
        outcomes = load_shadow_outcomes(cfg.outcomes_path)
        state = load_shadow_state(cfg)
        stats = summarize_shadow_outcomes(outcomes)
        ledger_rows: list[dict[str, Any]] = []
        if ledger_path.exists():
            for line in ledger_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ledger_rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        open_positions = state.get("openPositions") or {}
        return {
            "enabled": cfg.enabled,
            "count": len(ledger_rows),
            "openCount": len(open_positions),
            "recentOpens": ledger_rows[-5:],
            "openPositions": list(open_positions.values())[-8:],
            "recentClosed": outcomes[-5:],
            **stats,
        }

    path = cfg_or_path
    if not path.exists():
        return {"count": 0, "recent": []}
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {"count": len(rows), "recent": rows[-5:]}


_BLOCKED_CF_THIS_CYCLE = 0


def reset_blocked_counterfactual_cycle() -> None:
    global _BLOCKED_CF_THIS_CYCLE
    _BLOCKED_CF_THIS_CYCLE = 0


_COUNTERFACTUAL_SKIP_PREFIXES = (
    "Strategy chose HOLD",
    "Live account refresh failed",
    "Daily trade cap",
    "Cooldown active",
    "Order quote amount is zero",
)


def _blocked_reason_eligible_for_counterfactual(blocked_reasons: list[str]) -> bool:
    if not blocked_reasons:
        return False
    for reason in blocked_reasons:
        text = str(reason)
        if any(text.startswith(prefix) for prefix in _COUNTERFACTUAL_SKIP_PREFIXES):
            return False
    return True


def should_shadow_blocked_counterfactual(
    cfg: ShadowPaperConfig,
    *,
    reduce_only: bool,
    position_qty: Decimal,
    signal_action: str,
    confidence: Decimal,
    blocked_reasons: list[str],
    has_order: bool,
) -> bool:
    if not cfg.enabled or not cfg.shadow_blocked_counterfactual:
        return False
    if reduce_only or position_qty != 0 or not has_order:
        return False
    if str(signal_action).upper() not in {"BUY", "SELL"}:
        return False
    if confidence < cfg.blocked_counterfactual_min_confidence:
        return False
    if not _blocked_reason_eligible_for_counterfactual(blocked_reasons):
        return False
    if _BLOCKED_CF_THIS_CYCLE >= cfg.blocked_counterfactual_max_per_cycle:
        return False
    return True


def record_blocked_counterfactual(
    cfg: ShadowPaperConfig,
    *,
    snapshot: Any,
    signal: Any,
    decision: Any,
    watch_entry: dict[str, Any] | None = None,
) -> None:
    global _BLOCKED_CF_THIS_CYCLE
    if decision.order is None:
        return
    row = {
        "timestamp": snapshot.observed_at,
        "symbol": snapshot.asset.symbol,
        "market": snapshot.asset.market,
        "action": decision.order.action,
        "confidence": str(signal.confidence),
        "quoteAmount": str(decision.order.quote_amount),
        "price": str(snapshot.price),
        "bucket": (watch_entry or {}).get("bucket") or signal.indicators.get("discovery_bucket"),
        "source": (watch_entry or {}).get("source") or signal.indicators.get("discovery_source"),
        "reasons": decision.reasons[:6],
        "blockedReasons": decision.blocked_reasons,
        "mode": "shadow_blocked_counterfactual",
        "trigger": "blocked_counterfactual",
    }
    append_shadow_trade(cfg.blocked_counterfactual_path, row)
    if open_shadow_position(cfg, row):
        _BLOCKED_CF_THIS_CYCLE += 1


def record_shadow_decision(
    cfg: ShadowPaperConfig,
    *,
    snapshot: Any,
    signal: Any,
    decision: Any,
    watch_entry: dict[str, Any] | None = None,
) -> None:
    if decision.order is None:
        return
    row = {
        "timestamp": snapshot.observed_at,
        "symbol": snapshot.asset.symbol,
        "market": snapshot.asset.market,
        "action": decision.action,
        "confidence": str(signal.confidence),
        "quoteAmount": str(decision.order.quote_amount),
        "price": str(snapshot.price),
        "bucket": (watch_entry or {}).get("bucket") or signal.indicators.get("discovery_bucket"),
        "source": (watch_entry or {}).get("source") or signal.indicators.get("discovery_source"),
        "reasons": decision.reasons[:6],
        "blockedReasons": decision.blocked_reasons,
        "mode": "shadow_paper",
        "trigger": (watch_entry or {}).get("shadow_trigger") or signal.indicators.get("shadow_trigger"),
    }
    append_shadow_trade(cfg.ledger_path, row)
    open_shadow_position(cfg, row)


def run_shadow_paper_cycle(
    cfg: ShadowPaperConfig,
    *,
    prices: dict[str, Decimal],
    observed_at: int | None = None,
    take_profit_pct: Decimal | None = None,
    stop_loss_pct: Decimal | None = None,
) -> list[dict[str, Any]]:
    return evaluate_shadow_positions(
        cfg,
        prices=prices,
        observed_at=observed_at,
        take_profit_pct=take_profit_pct,
        stop_loss_pct=stop_loss_pct,
    )

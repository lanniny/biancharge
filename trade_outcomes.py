"""Record closed-trade outcomes and derive adaptive sizing / confidence hints."""

from __future__ import annotations

import json
import os
import shutil
import time
from collections import Counter
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
class TradeLearningConfig:
    enabled: bool = True
    outcomes_path: str = "logs/trade-outcomes.jsonl"
    state_path: str = "logs/trade-learning-state.json"
    lookback_trades: int = 20
    loss_streak_cooldown: int = 3
    sizing_penalty_per_loss: Decimal = Decimal("0.15")
    min_sizing_factor: Decimal = Decimal("0.5")
    global_loss_rate_block: Decimal = Decimal("0.65")
    min_confidence_bump: Decimal = Decimal("0.05")
    min_win_rate_block: Decimal = Decimal("0.35")
    min_win_rate_unblock: Decimal = Decimal("0.40")
    min_sample_for_win_rate_block: int = 15
    bucket_win_rate_enabled: bool = True
    min_bucket_sample: int = 4
    win_rate_shadow_first: bool = True
    shadow_graduation_enabled: bool = True
    min_shadow_bucket_sample: int = 6
    min_shadow_global_sample: int = 10
    shadow_outcomes_path: str = "logs/shadow-paper-outcomes.jsonl"
    bucket_live_mode_overrides: dict[str, str] | None = None
    min_symbol_sample_for_block: int = 4
    min_symbol_win_rate_block: Decimal = Decimal("0.30")
    bucket_sizing_enabled: bool = True
    min_bucket_sample_for_sizing: int = 4
    bucket_loss_sizing_mult: Decimal = Decimal("0.65")
    bucket_flat_sizing_mult: Decimal = Decimal("0.85")
    bucket_win_sizing_mult: Decimal = Decimal("1.10")
    min_bucket_sizing_mult: Decimal = Decimal("0.60")
    max_bucket_sizing_mult: Decimal = Decimal("1.15")
    exit_quality_enabled: bool = True
    min_exit_quality_sample: int = 4
    low_capture_ratio: Decimal = Decimal("0.45")
    high_capture_ratio: Decimal = Decimal("0.75")
    high_mfe_pct: Decimal = Decimal("0.04")
    low_capture_exit_mult: Decimal = Decimal("0.80")
    high_capture_exit_mult: Decimal = Decimal("1.10")
    min_exit_quality_mult: Decimal = Decimal("0.75")
    max_exit_quality_mult: Decimal = Decimal("1.15")
    fast_exit_quality_enabled: bool = True
    fast_exit_quality_min_sample: int = 1
    fast_exit_quality_min_mfe_pct: Decimal = Decimal("0.01")
    fast_exit_quality_max_capture_ratio: Decimal = Decimal("0")
    fast_bucket_shadow_enabled: bool = True
    fast_bucket_min_sample: int = 2
    fast_bucket_total_loss: Decimal = Decimal("-1.0")
    fast_bucket_max_profit_factor: Decimal = Decimal("0.25")
    fast_bucket_max_win_rate: Decimal = Decimal("0.10")


def trade_learning_from_config(raw: dict[str, Any] | None) -> TradeLearningConfig:
    raw = raw or {}
    return TradeLearningConfig(
        enabled=bool(raw.get("enabled", True)),
        outcomes_path=str(raw.get("outcomes_path", "logs/trade-outcomes.jsonl")),
        state_path=str(raw.get("state_path", "logs/trade-learning-state.json")),
        lookback_trades=int(raw.get("lookback_trades", 20)),
        loss_streak_cooldown=int(raw.get("loss_streak_cooldown", 3)),
        sizing_penalty_per_loss=decimal_from(raw.get("sizing_penalty_per_loss", "0.15")),
        min_sizing_factor=decimal_from(raw.get("min_sizing_factor", "0.5")),
        global_loss_rate_block=decimal_from(raw.get("global_loss_rate_block", "0.65")),
        min_confidence_bump=decimal_from(raw.get("min_confidence_bump", "0.05")),
        min_win_rate_block=decimal_from(raw.get("min_win_rate_block", "0.35")),
        min_win_rate_unblock=decimal_from(raw.get("min_win_rate_unblock", "0.40")),
        min_sample_for_win_rate_block=int(raw.get("min_sample_for_win_rate_block", 15)),
        bucket_win_rate_enabled=bool(raw.get("bucket_win_rate_enabled", True)),
        min_bucket_sample=int(raw.get("min_bucket_sample", 4)),
        win_rate_shadow_first=bool(raw.get("win_rate_shadow_first", True)),
        shadow_graduation_enabled=bool(raw.get("shadow_graduation_enabled", True)),
        min_shadow_bucket_sample=int(raw.get("min_shadow_bucket_sample", 6)),
        min_shadow_global_sample=int(raw.get("min_shadow_global_sample", 10)),
        shadow_outcomes_path=str(raw.get("shadow_outcomes_path", "logs/shadow-paper-outcomes.jsonl")),
        bucket_live_mode_overrides={
            str(k): str(v)
            for k, v in (raw.get("bucket_live_mode_overrides") or {}).items()
            if str(v) in {"live", "shadow_first"}
        }
        or None,
        min_symbol_sample_for_block=int(raw.get("min_symbol_sample_for_block", 4)),
        min_symbol_win_rate_block=decimal_from(raw.get("min_symbol_win_rate_block", "0.30")),
        bucket_sizing_enabled=bool(raw.get("bucket_sizing_enabled", True)),
        min_bucket_sample_for_sizing=int(raw.get("min_bucket_sample_for_sizing", 4)),
        bucket_loss_sizing_mult=decimal_from(raw.get("bucket_loss_sizing_mult", "0.65")),
        bucket_flat_sizing_mult=decimal_from(raw.get("bucket_flat_sizing_mult", "0.85")),
        bucket_win_sizing_mult=decimal_from(raw.get("bucket_win_sizing_mult", "1.10")),
        min_bucket_sizing_mult=decimal_from(raw.get("min_bucket_sizing_mult", "0.60")),
        max_bucket_sizing_mult=decimal_from(raw.get("max_bucket_sizing_mult", "1.15")),
        exit_quality_enabled=bool(raw.get("exit_quality_enabled", True)),
        min_exit_quality_sample=int(raw.get("min_exit_quality_sample", 4)),
        low_capture_ratio=decimal_from(raw.get("low_capture_ratio", "0.45")),
        high_capture_ratio=decimal_from(raw.get("high_capture_ratio", "0.75")),
        high_mfe_pct=decimal_from(raw.get("high_mfe_pct", "0.04")),
        low_capture_exit_mult=decimal_from(raw.get("low_capture_exit_mult", "0.80")),
        high_capture_exit_mult=decimal_from(raw.get("high_capture_exit_mult", "1.10")),
        min_exit_quality_mult=decimal_from(raw.get("min_exit_quality_mult", "0.75")),
        max_exit_quality_mult=decimal_from(raw.get("max_exit_quality_mult", "1.15")),
        fast_exit_quality_enabled=bool(raw.get("fast_exit_quality_enabled", True)),
        fast_exit_quality_min_sample=int(raw.get("fast_exit_quality_min_sample", 1)),
        fast_exit_quality_min_mfe_pct=decimal_from(raw.get("fast_exit_quality_min_mfe_pct", "0.01")),
        fast_exit_quality_max_capture_ratio=decimal_from(raw.get("fast_exit_quality_max_capture_ratio", "0")),
        fast_bucket_shadow_enabled=bool(raw.get("fast_bucket_shadow_enabled", True)),
        fast_bucket_min_sample=int(raw.get("fast_bucket_min_sample", 2)),
        fast_bucket_total_loss=decimal_from(raw.get("fast_bucket_total_loss", "-1.0")),
        fast_bucket_max_profit_factor=decimal_from(raw.get("fast_bucket_max_profit_factor", "0.25")),
        fast_bucket_max_win_rate=decimal_from(raw.get("fast_bucket_max_win_rate", "0.10")),
    )


def append_outcome(path: str, row: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_recent_outcomes(path: str, *, limit: int = 50) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []
    lines = target.read_text(encoding="utf-8").splitlines()
    rows: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _load_previous_snapshot(state_path: str) -> dict[str, Any]:
    target = Path(state_path)
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_shadow_outcome_summary(cfg: TradeLearningConfig) -> dict[str, Any]:
    outcomes_path = Path(cfg.shadow_outcomes_path)
    if not outcomes_path.exists():
        return {}
    try:
        from shadow_paper import load_shadow_outcomes, summarize_shadow_outcomes

        return summarize_shadow_outcomes(load_shadow_outcomes(outcomes_path))
    except Exception:
        return {}


def _load_shadow_bucket_stats_for_hints(cfg: TradeLearningConfig) -> dict[str, Any]:
    summary = load_shadow_outcome_summary(cfg)
    bucket_stats = summary.get("bucketStats")
    return bucket_stats if isinstance(bucket_stats, dict) else {}


def has_profitable_edge(stat: dict[str, Any] | None, *, min_profit_factor: Decimal = Decimal("1.20")) -> bool:
    if not stat:
        return False
    total_pnl = decimal_from(stat.get("totalPnl", stat.get("totalRealizedPnl", "0")))
    profit_factor = decimal_from(stat.get("profitFactor", "0"))
    return total_pnl > 0 and profit_factor >= min_profit_factor


def maybe_graduate_from_shadow(
    mode: str,
    shadow_stat: dict[str, Any] | None,
    *,
    cfg: TradeLearningConfig,
    min_sample: int,
    live_stat: dict[str, Any] | None = None,
) -> tuple[str, str | None]:
    if not cfg.shadow_graduation_enabled or mode != "shadow_first" or not shadow_stat:
        return mode, None
    if live_stat:
        live_sample = int(live_stat.get("sampleSize", 0))
        live_win_rate = decimal_from(live_stat.get("winRate", "0"))
        live_total_pnl = decimal_from(live_stat.get("totalPnl", "0"))
        live_profit_factor = decimal_from(live_stat.get("profitFactor", "0"))
        if (
            cfg.fast_bucket_shadow_enabled
            and live_sample >= cfg.fast_bucket_min_sample
            and live_total_pnl <= cfg.fast_bucket_total_loss
            and live_win_rate <= cfg.fast_bucket_max_win_rate
            and live_profit_factor <= cfg.fast_bucket_max_profit_factor
        ):
            return (
                mode,
                (
                    f"live lock: fast loss quarantine, pnl {live_total_pnl}, PF {live_profit_factor:.2f}, "
                    f"{live_win_rate:.0%} over {live_sample} live closes"
                ),
            )
        if (
            live_sample >= cfg.min_bucket_sample
            and live_win_rate < cfg.min_win_rate_block
            and not has_profitable_edge(live_stat)
        ):
            return (
                mode,
                (
                    f"live lock: {live_win_rate:.0%} over {live_sample} live closes "
                    f"below {cfg.min_win_rate_block:.0%}"
                ),
            )
    sample = int(shadow_stat.get("sampleSize", 0))
    win_rate = decimal_from(shadow_stat.get("winRate", "0"))
    total_pnl = decimal_from(shadow_stat.get("totalPnl", "0"))
    profit_factor = decimal_from(shadow_stat.get("profitFactor", "0"))
    profitable_edge = has_profitable_edge(shadow_stat)
    if sample >= min_sample and (win_rate >= cfg.min_win_rate_unblock or profitable_edge):
        if profitable_edge and win_rate < cfg.min_win_rate_unblock:
            return (
                "live",
                (
                    f"shadow graduated (PF {profit_factor:.2f}, pnl {total_pnl}, "
                    f"{win_rate:.0%} over {sample} shadow closes)"
                ),
            )
        return (
            "live",
            f"shadow graduated ({win_rate:.0%} over {sample} shadow closes)",
        )
    return mode, None


def outcome_bucket(row: dict[str, Any]) -> str:
    ctx = row.get("openContext") or {}
    bucket = str(ctx.get("bucket") or "").strip()
    if bucket:
        return bucket
    source = str(ctx.get("source") or "")
    if source.startswith("discovery:"):
        return source.split(":", 1)[1]
    return "unknown"


def outcome_pnl(row: dict[str, Any]) -> Decimal:
    """Learning PnL: prefer netPnl when present, fall back to legacy realizedPnl."""
    if row.get("netPnl") not in (None, ""):
        return decimal_from(row.get("netPnl", "0"))
    return decimal_from(row.get("realizedPnl", "0"))


def _win_rate_for_rows(rows: list[dict[str, Any]]) -> tuple[int, int, int, Decimal]:
    wins = sum(1 for row in rows if outcome_pnl(row) > 0)
    losses = sum(1 for row in rows if outcome_pnl(row) < 0)
    total = len(rows)
    win_rate = Decimal(wins) / Decimal(total) if total else Decimal("0")
    return wins, losses, total, win_rate


def row_excursion(row: dict[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    ctx = row.get("openContext") or {}
    has_mfe = ctx.get("mfePct") not in (None, "")
    has_mae = ctx.get("maePct") not in (None, "")
    mfe = decimal_from(ctx.get("mfePct")) if has_mfe else None
    mae = decimal_from(ctx.get("maePct")) if has_mae else None
    return mfe, mae


def compute_exit_quality_stats(rows: list[dict[str, Any]]) -> dict[str, str | int]:
    excursion_rows: list[tuple[dict[str, Any], Decimal, Decimal]] = []
    for row in rows:
        mfe, mae = row_excursion(row)
        if mfe is None or mae is None:
            continue
        excursion_rows.append((row, mfe, mae))
    if not excursion_rows:
        return {"sampleSize": 0}

    total = len(excursion_rows)
    avg_mfe = sum((mfe for _, mfe, _ in excursion_rows), Decimal("0")) / Decimal(total)
    avg_mae = sum((mae for _, _, mae in excursion_rows), Decimal("0")) / Decimal(total)
    missed = []
    efficiency = []
    for row, mfe, _ in excursion_rows:
        entry = decimal_from(row.get("entryPrice", "0"))
        exit_price = decimal_from(row.get("exitPrice", "0"))
        side = str(row.get("positionSide", "LONG")).upper()
        if entry <= 0:
            continue
        realized_pct = (entry - exit_price) / entry if side == "SHORT" else (exit_price - entry) / entry
        missed.append(max(mfe - realized_pct, Decimal("0")))
        if mfe > 0:
            efficiency.append(realized_pct / mfe)
    avg_missed = sum(missed, Decimal("0")) / Decimal(len(missed)) if missed else Decimal("0")
    avg_eff = sum(efficiency, Decimal("0")) / Decimal(len(efficiency)) if efficiency else Decimal("0")
    return {
        "sampleSize": total,
        "avgMfePct": str(avg_mfe.quantize(Decimal("0.0001"))),
        "avgMaePct": str(avg_mae.quantize(Decimal("0.0001"))),
        "avgMissedPct": str(avg_missed.quantize(Decimal("0.0001"))),
        "avgCaptureRatio": str(avg_eff.quantize(Decimal("0.01"))),
    }


def compute_bucket_stats(recent: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in recent:
        bucket = outcome_bucket(row)
        if bucket == "unknown":
            continue
        grouped.setdefault(bucket, []).append(row)

    stats: dict[str, Any] = {}
    for bucket, rows in grouped.items():
        wins, losses, total, win_rate = _win_rate_for_rows(rows)
        total_pnl = sum((outcome_pnl(row) for row in rows), Decimal("0"))
        gross_win = sum(
            (outcome_pnl(row) for row in rows if outcome_pnl(row) > 0),
            Decimal("0"),
        )
        gross_loss = abs(
            sum(
                (
                    outcome_pnl(row)
                    for row in rows
                    if outcome_pnl(row) < 0
                ),
                Decimal("0"),
            )
        )
        avg_win = gross_win / Decimal(wins) if wins else Decimal("0")
        avg_loss = gross_loss / Decimal(losses) if losses else Decimal("0")
        profit_factor = gross_win / gross_loss if gross_loss > 0 else (Decimal("999") if gross_win > 0 else Decimal("0"))
        exit_quality = compute_exit_quality_stats(rows)
        stats[bucket] = {
            "sampleSize": total,
            "wins": wins,
            "losses": losses,
            "winRate": str(win_rate.quantize(Decimal("0.01"))),
            "totalPnl": str(total_pnl.quantize(Decimal("0.0001"))),
            "avgWin": str(avg_win.quantize(Decimal("0.0001"))),
            "avgLoss": str(avg_loss.quantize(Decimal("0.0001"))),
            "profitFactor": str(profit_factor.quantize(Decimal("0.01"))),
            "exitQuality": exit_quality,
        }
    return stats


def resolve_bucket_sizing_factor(stat: dict[str, Any], cfg: TradeLearningConfig) -> Decimal:
    if not cfg.bucket_sizing_enabled:
        return Decimal("1")
    sample = int(stat.get("sampleSize", 0))
    if sample < cfg.min_bucket_sample_for_sizing:
        return Decimal("1")
    win_rate = decimal_from(stat.get("winRate", "0"))
    total_pnl = decimal_from(stat.get("totalPnl", "0"))
    profit_factor = decimal_from(stat.get("profitFactor", "0"))
    if total_pnl > 0 and win_rate >= cfg.min_win_rate_unblock:
        factor = cfg.bucket_win_sizing_mult
    elif total_pnl > 0 and profit_factor >= Decimal("1.20"):
        factor = Decimal("1")
    elif total_pnl < 0 or win_rate < cfg.min_win_rate_block or profit_factor < Decimal("0.85"):
        factor = cfg.bucket_loss_sizing_mult
    elif total_pnl == 0:
        factor = cfg.bucket_flat_sizing_mult
    else:
        factor = Decimal("1")
    return min(max(factor, cfg.min_bucket_sizing_mult), cfg.max_bucket_sizing_mult)


def resolve_exit_quality_factor(quality: dict[str, Any], cfg: TradeLearningConfig) -> Decimal:
    if not cfg.exit_quality_enabled:
        return Decimal("1")
    sample = int(quality.get("sampleSize", 0))
    capture = decimal_from(quality.get("avgCaptureRatio", "0"))
    avg_mfe = decimal_from(quality.get("avgMfePct", "0"))
    if (
        cfg.fast_exit_quality_enabled
        and sample >= cfg.fast_exit_quality_min_sample
        and avg_mfe >= cfg.fast_exit_quality_min_mfe_pct
        and capture <= cfg.fast_exit_quality_max_capture_ratio
    ):
        return min(max(cfg.low_capture_exit_mult, cfg.min_exit_quality_mult), cfg.max_exit_quality_mult)
    if sample < cfg.min_exit_quality_sample:
        return Decimal("1")
    if avg_mfe > 0 and capture < cfg.low_capture_ratio:
        factor = cfg.low_capture_exit_mult
    elif capture >= cfg.high_capture_ratio and avg_mfe >= cfg.high_mfe_pct:
        factor = cfg.high_capture_exit_mult
    else:
        factor = Decimal("1")
    return min(max(factor, cfg.min_exit_quality_mult), cfg.max_exit_quality_mult)


def resolve_discovery_live_mode(
    *,
    win_rate: Decimal,
    sample: int,
    cfg: TradeLearningConfig,
    previous_mode: str | None,
    min_sample: int | None = None,
    profit_factor: Decimal | None = None,
    total_pnl: Decimal | None = None,
) -> str:
    threshold_sample = min_sample if min_sample is not None else cfg.min_sample_for_win_rate_block
    pf = profit_factor if profit_factor is not None else Decimal("0")
    pnl = total_pnl if total_pnl is not None else Decimal("0")
    if (
        cfg.fast_bucket_shadow_enabled
        and sample >= cfg.fast_bucket_min_sample
        and pnl <= cfg.fast_bucket_total_loss
        and win_rate <= cfg.fast_bucket_max_win_rate
        and pf <= cfg.fast_bucket_max_profit_factor
    ):
        return "shadow_first"
    if sample < threshold_sample:
        return "live"
    if pnl > 0 and pf >= Decimal("1.20"):
        return "live"
    if win_rate < cfg.min_win_rate_block:
        return "shadow_first"
    if win_rate >= cfg.min_win_rate_unblock:
        return "live"
    if previous_mode in {"live", "shadow_first"}:
        return previous_mode
    return "shadow_first"


def compute_trade_learning_snapshot(cfg: TradeLearningConfig) -> dict[str, Any]:
    if not cfg.enabled:
        return {"enabled": False}

    previous = _load_previous_snapshot(cfg.state_path)
    rows = load_recent_outcomes(cfg.outcomes_path, limit=max(cfg.lookback_trades * 3, 30))
    recent = rows[-cfg.lookback_trades :] if rows else []
    wins, losses, total, win_rate = _win_rate_for_rows(recent)
    total_pnl = sum((outcome_pnl(row) for row in recent), Decimal("0"))
    gross_win = sum(
        (outcome_pnl(row) for row in recent if outcome_pnl(row) > 0),
        Decimal("0"),
    )
    gross_loss = abs(
        sum(
            (
                outcome_pnl(row)
                for row in recent
                if outcome_pnl(row) < 0
            ),
            Decimal("0"),
        )
    )
    avg_win = gross_win / Decimal(wins) if wins else Decimal("0")
    avg_loss = gross_loss / Decimal(losses) if losses else Decimal("0")
    profit_factor = gross_win / gross_loss if gross_loss > 0 else (Decimal("999") if gross_win > 0 else Decimal("0"))
    exit_quality = compute_exit_quality_stats(recent)

    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        sym = normalize_symbol(str(row.get("symbol", "")))
        by_symbol.setdefault(sym, []).append(row)

    symbol_stats: dict[str, Any] = {}
    for sym, sym_rows in by_symbol.items():
        tail = sym_rows[-5:]
        streak = 0
        for item in reversed(tail):
            pnl = outcome_pnl(item)
            if pnl < 0:
                streak += 1
            else:
                break
        sym_wins = sum(1 for item in tail if outcome_pnl(item) > 0)
        symbol_stats[sym] = {
            "recentTrades": len(tail),
            "lossStreak": streak,
            "winRate": str((Decimal(sym_wins) / Decimal(len(tail))).quantize(Decimal("0.01")) if tail else "0"),
        }

    loss_rate = Decimal("1") - win_rate if total else Decimal("0")
    sizing_factor = Decimal("1")
    global_edge_stat = {
        "totalPnl": str(total_pnl),
        "profitFactor": str(profit_factor),
    }
    profitable_edge = has_profitable_edge(global_edge_stat)
    if losses > 0 and not profitable_edge:
        sizing_factor = max(
            cfg.min_sizing_factor,
            Decimal("1") - cfg.sizing_penalty_per_loss * Decimal(losses),
        )
    confidence_bump = Decimal("0")
    if loss_rate >= cfg.global_loss_rate_block and total >= 5 and not profitable_edge:
        confidence_bump = cfg.min_confidence_bump

    bucket_stats = compute_bucket_stats(recent)
    prev_bucket_modes = previous.get("bucketLiveModes") or {}
    bucket_live_modes: dict[str, str] = {}
    shadow_summary = load_shadow_outcome_summary(cfg)
    shadow_bucket_stats = shadow_summary.get("bucketStats") if isinstance(shadow_summary.get("bucketStats"), dict) else {}
    bucket_graduations: dict[str, str] = {}
    bucket_sizing_factors: dict[str, str] = {}
    bucket_exit_quality_factors: dict[str, str] = {}

    if cfg.bucket_win_rate_enabled:
        bucket_names = sorted(set(bucket_stats) | set(shadow_bucket_stats))
        for bucket in bucket_names:
            stat = bucket_stats.get(bucket) or {}
            if stat:
                mode = resolve_discovery_live_mode(
                    win_rate=decimal_from(stat.get("winRate", "0")),
                    sample=int(stat.get("sampleSize", 0)),
                    cfg=cfg,
                    previous_mode=str(prev_bucket_modes.get(bucket) or ""),
                    min_sample=cfg.min_bucket_sample,
                    profit_factor=decimal_from(stat.get("profitFactor", "0")),
                    total_pnl=decimal_from(stat.get("totalPnl", "0")),
                )
            else:
                mode = str(prev_bucket_modes.get(bucket) or "shadow_first")
            graduated_mode, graduation_note = maybe_graduate_from_shadow(
                mode,
                shadow_bucket_stats.get(bucket),
                cfg=cfg,
                min_sample=cfg.min_shadow_bucket_sample,
                live_stat=stat or None,
            )
            bucket_live_modes[bucket] = graduated_mode
            if graduation_note:
                bucket_graduations[bucket] = graduation_note
            if stat:
                bucket_sizing_factor = resolve_bucket_sizing_factor(stat, cfg)
                if bucket_sizing_factor != Decimal("1"):
                    bucket_sizing_factors[bucket] = str(bucket_sizing_factor.quantize(Decimal("0.01")))
                exit_quality_factor = resolve_exit_quality_factor(stat.get("exitQuality") or {}, cfg)
                if exit_quality_factor != Decimal("1"):
                    bucket_exit_quality_factors[bucket] = str(exit_quality_factor.quantize(Decimal("0.01")))

    elif cfg.bucket_sizing_enabled:
        for bucket, stat in bucket_stats.items():
            bucket_sizing_factor = resolve_bucket_sizing_factor(stat, cfg)
            if bucket_sizing_factor != Decimal("1"):
                bucket_sizing_factors[bucket] = str(bucket_sizing_factor.quantize(Decimal("0.01")))

            exit_quality_factor = resolve_exit_quality_factor(stat.get("exitQuality") or {}, cfg)
            if exit_quality_factor != Decimal("1"):
                bucket_exit_quality_factors[bucket] = str(exit_quality_factor.quantize(Decimal("0.01")))
    elif cfg.exit_quality_enabled:
        for bucket, stat in bucket_stats.items():
            exit_quality_factor = resolve_exit_quality_factor(stat.get("exitQuality") or {}, cfg)
            if exit_quality_factor != Decimal("1"):
                bucket_exit_quality_factors[bucket] = str(exit_quality_factor.quantize(Decimal("0.01")))

    if cfg.bucket_live_mode_overrides:
        for bucket, mode in cfg.bucket_live_mode_overrides.items():
            bucket_live_modes[bucket] = mode

    discovery_live_mode = resolve_discovery_live_mode(
        win_rate=win_rate,
        sample=total,
        cfg=cfg,
        previous_mode=str(previous.get("discoveryLiveMode") or ""),
        profit_factor=profit_factor,
        total_pnl=total_pnl,
    )
    discovery_graduation: str | None = None
    if discovery_live_mode == "shadow_first":
        global_shadow_stat = {
            "sampleSize": int(shadow_summary.get("closedCount", 0)),
            "winRate": shadow_summary.get("winRate", "0"),
            "totalPnl": shadow_summary.get("totalRealizedPnl", "0"),
            "profitFactor": shadow_summary.get("profitFactor", "0"),
        }
        discovery_live_mode, discovery_graduation = maybe_graduate_from_shadow(
            discovery_live_mode,
            global_shadow_stat,
            cfg=cfg,
            min_sample=cfg.min_shadow_global_sample,
        )

    shadow_first_buckets = sorted(
        bucket for bucket, mode in bucket_live_modes.items() if mode == "shadow_first"
    )

    snapshot = {
        "enabled": True,
        "evaluatedAt": int(time.time()),
        "lookbackTrades": cfg.lookback_trades,
        "sampleSize": total,
        "wins": wins,
        "losses": losses,
        "winRate": str(win_rate.quantize(Decimal("0.01"))),
        "totalRealizedPnl": str(total_pnl.quantize(Decimal("0.0001"))),
        "grossWin": str(gross_win.quantize(Decimal("0.0001"))),
        "grossLoss": str(gross_loss.quantize(Decimal("0.0001"))),
        "avgWin": str(avg_win.quantize(Decimal("0.0001"))),
        "avgLoss": str(avg_loss.quantize(Decimal("0.0001"))),
        "profitFactor": str(profit_factor.quantize(Decimal("0.01"))),
        "exitQuality": exit_quality,
        "sizingFactor": str(sizing_factor.quantize(Decimal("0.01"))),
        "confidenceBump": str(confidence_bump),
        "symbolStats": symbol_stats,
        "bucketStats": bucket_stats,
        "bucketSizingFactors": bucket_sizing_factors,
        "bucketExitQualityFactors": bucket_exit_quality_factors,
        "discoveryLiveMode": discovery_live_mode,
        "bucketLiveModes": bucket_live_modes,
        "shadowFirstBuckets": shadow_first_buckets,
        "winRateShadowFirst": cfg.win_rate_shadow_first,
        "shadowBucketStats": shadow_bucket_stats,
        "shadowSummary": {
            "closedCount": shadow_summary.get("closedCount", 0),
            "winRate": shadow_summary.get("winRate"),
            "totalRealizedPnl": shadow_summary.get("totalRealizedPnl"),
        },
        "bucketGraduations": bucket_graduations,
        "discoveryGraduation": discovery_graduation,
        "recentOutcomes": [
            {
                "symbol": row.get("symbol"),
                "realizedPnl": row.get("realizedPnl"),
                "closedAt": row.get("closedAt"),
                "regime": row.get("regime"),
                "bucket": outcome_bucket(row),
            }
            for row in recent[-5:]
        ],
    }
    # Atomic write: a crash mid-write must not corrupt the learning state
    # (win-rate history, bucket modes) that sizing/confidence depend on.
    state_target = Path(cfg.state_path)
    state_target.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_target.with_name(f"{state_target.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(snapshot, ensure_ascii=False, indent=2))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, state_target)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    return snapshot


def record_trade_outcome(
    cfg: TradeLearningConfig,
    *,
    symbol: str,
    side: str,
    quantity: Decimal,
    exit_price: Decimal,
    entry_price: Decimal,
    position_side: str,
    regime: str | None,
    session: str | None,
    rationale_summary: str | None,
    order_id: Any = None,
    close_source: str | None = None,
    open_context: dict[str, Any] | None = None,
    fees: Decimal = Decimal("0"),
    funding: Decimal = Decimal("0"),
    slippage: Decimal = Decimal("0"),
) -> dict[str, Any]:
    if not cfg.enabled or quantity <= 0 or entry_price <= 0:
        return {}
    sym = normalize_symbol(symbol)
    if position_side.upper() == "SHORT":
        pnl = (entry_price - exit_price) * quantity
    else:
        pnl = (exit_price - entry_price) * quantity
    net_pnl = pnl - fees - funding - slippage
    row = {
        "symbol": sym,
        "side": side,
        "positionSide": position_side,
        "quantity": str(quantity.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)),
        "entryPrice": str(entry_price),
        "exitPrice": str(exit_price),
        "grossPnl": str(pnl.quantize(Decimal("0.0001"))),
        "fees": str(fees.quantize(Decimal("0.0001"))),
        "funding": str(funding.quantize(Decimal("0.0001"))),
        "slippage": str(slippage.quantize(Decimal("0.0001"))),
        "netPnl": str(net_pnl.quantize(Decimal("0.0001"))),
        "realizedPnl": str(pnl.quantize(Decimal("0.0001"))),
        "regime": regime,
        "session": session,
        "rationaleSummary": rationale_summary,
        "orderId": order_id,
        "closeSource": close_source,
        "closedAt": int(time.time()),
    }
    if open_context:
        row["openContext"] = open_context
    append_outcome(cfg.outcomes_path, row)
    return row


def trade_learning_discovery_shadow_first(
    snapshot: dict[str, Any],
    *,
    cfg: TradeLearningConfig,
    bucket: str,
    is_discovery_open: bool,
) -> tuple[bool, str | None]:
    if not snapshot.get("enabled") or not is_discovery_open or not cfg.win_rate_shadow_first:
        return False, None

    bucket_key = str(bucket or "").strip()
    global_mode = str(snapshot.get("discoveryLiveMode") or "live")
    bucket_modes = snapshot.get("bucketLiveModes") or {}

    if global_mode == "shadow_first":
        return (
            True,
            (
                f"Trade learning: global win rate {decimal_from(snapshot.get('winRate', '0')):.0%} "
                f"over {int(snapshot.get('sampleSize', 0))} closes "
                f"< {cfg.min_win_rate_block:.0%}; discovery live deferred to shadow paper "
                f"(restore live at >= {cfg.min_win_rate_unblock:.0%})."
            ),
        )

    if bucket_key and bucket_modes.get(bucket_key) == "shadow_first":
        stat = (snapshot.get("bucketStats") or {}).get(bucket_key) or {}
        graduations = snapshot.get("bucketGraduations") or {}
        grad_note = graduations.get(bucket_key)
        if grad_note and str(grad_note).startswith("shadow graduated"):
            return False, None
        return (
            True,
            (
                f"Trade learning: bucket {bucket_key} win rate "
                f"{decimal_from(stat.get('winRate', '0')):.0%} over {int(stat.get('sampleSize', 0))} closes "
                f"< {cfg.min_win_rate_block:.0%}; bucket deferred to shadow paper."
            ),
        )

    return False, None


def trade_learning_block_reason(
    snapshot: dict[str, Any],
    *,
    symbol: str,
    is_reduce_only: bool,
    cfg: TradeLearningConfig,
    is_discovery_open: bool = False,
) -> str | None:
    del is_discovery_open  # win-rate gate is shadow-first, not hard block
    if not snapshot.get("enabled") or is_reduce_only:
        return None
    sym = normalize_symbol(symbol)
    stats = (snapshot.get("symbolStats") or {}).get(sym) or {}
    streak = int(stats.get("lossStreak", 0))
    if streak >= cfg.loss_streak_cooldown:
        return (
            f"Trade learning: {sym} has {streak} consecutive losses; "
            f"new opens blocked until a winning close (cooldown={cfg.loss_streak_cooldown})."
        )
    recent_trades = int(stats.get("recentTrades", 0))
    win_rate = decimal_from(stats.get("winRate", "0"))
    if recent_trades >= cfg.min_symbol_sample_for_block and win_rate < cfg.min_symbol_win_rate_block:
        return (
            f"Trade learning: {sym} recent win rate {win_rate:.0%} over {recent_trades} closes "
            f"< {cfg.min_symbol_win_rate_block:.0%}; new opens paused for this symbol."
        )
    return None


def apply_sizing_factor(quote_amount: Decimal, snapshot: dict[str, Any]) -> Decimal:
    if not snapshot.get("enabled"):
        return quote_amount
    factor = decimal_from(snapshot.get("sizingFactor", "1"))
    if factor <= 0 or factor >= Decimal("1"):
        return quote_amount if factor >= Decimal("1") else quote_amount * factor
    return (quote_amount * factor).quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def apply_bucket_sizing_factor(
    quote_amount: Decimal,
    snapshot: dict[str, Any],
    *,
    bucket: str,
    source: str = "",
) -> tuple[Decimal, Decimal]:
    if not snapshot.get("enabled"):
        return quote_amount, Decimal("1")
    bucket_key = str(bucket or "").strip()
    if not bucket_key and str(source or "").startswith("discovery:"):
        bucket_key = str(source).split(":", 1)[1]
    if not bucket_key and str(source or "").startswith("pinned"):
        bucket_key = "pinned"
    if not bucket_key:
        return quote_amount, Decimal("1")
    factor = decimal_from((snapshot.get("bucketSizingFactors") or {}).get(bucket_key, "1"))
    if factor <= 0 or factor == Decimal("1"):
        return quote_amount, Decimal("1")
    scaled = (quote_amount * factor).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    return scaled, factor


def required_confidence_with_learning(base: Decimal, snapshot: dict[str, Any]) -> Decimal:
    if not snapshot.get("enabled"):
        return base
    bump = decimal_from(snapshot.get("confidenceBump", "0"))
    return base + bump


def trade_learning_rationale_block(snapshot: dict[str, Any]) -> dict[str, Any]:
    if not snapshot.get("enabled"):
        return {"enabled": False}
    return {
        "enabled": True,
        "winRate": snapshot.get("winRate"),
        "sampleSize": snapshot.get("sampleSize"),
        "sizingFactor": snapshot.get("sizingFactor"),
        "confidenceBump": snapshot.get("confidenceBump"),
        "totalRealizedPnl": snapshot.get("totalRealizedPnl"),
        "profitFactor": snapshot.get("profitFactor"),
        "exitQuality": snapshot.get("exitQuality"),
        "symbolStats": snapshot.get("symbolStats"),
        "bucketStats": snapshot.get("bucketStats"),
        "bucketSizingFactors": snapshot.get("bucketSizingFactors"),
        "bucketExitQualityFactors": snapshot.get("bucketExitQualityFactors"),
        "discoveryLiveMode": snapshot.get("discoveryLiveMode"),
        "bucketLiveModes": snapshot.get("bucketLiveModes"),
        "shadowFirstBuckets": snapshot.get("shadowFirstBuckets"),
        "shadowBucketStats": snapshot.get("shadowBucketStats"),
        "shadowSummary": snapshot.get("shadowSummary"),
        "bucketGraduations": snapshot.get("bucketGraduations"),
        "discoveryGraduation": snapshot.get("discoveryGraduation"),
        "recentOutcomes": snapshot.get("recentOutcomes"),
    }


def load_outcomes_jsonl(path: str | Path) -> list[dict[str, Any]]:
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


def write_outcomes_jsonl(path: str | Path, rows: list[dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def repair_outcome_rows(rows: list[dict[str, Any]], open_symbols: set[str]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    stats: Counter[str] = Counter()
    external_by_symbol: dict[str, list[dict[str, Any]]] = {}
    non_external: list[dict[str, Any]] = []

    for row in rows:
        source = row.get("closeSource")
        sym = normalize_symbol(str(row.get("symbol", "")))
        if source != "external_sl_tp":
            non_external.append(row)
            stats["kept_non_external"] += 1
            continue
        external_by_symbol.setdefault(sym, []).append(row)
        stats["external_total"] += 1

    kept_external: list[dict[str, Any]] = []
    for sym, group in external_by_symbol.items():
        if sym in open_symbols:
            stats["dropped_still_open"] += len(group)
            continue
        if len(group) > 1:
            stats["dropped_duplicate_external"] += len(group)
            continue
        kept_external.append(group[0])
        stats["kept_external"] += 1

    repaired = non_external + kept_external
    repaired.sort(key=lambda item: int(item.get("closedAt", 0)))
    return repaired, dict(stats)


def repair_trade_outcomes_file(
    cfg: TradeLearningConfig,
    *,
    live_state_path: str | Path = "logs/live-trading-state.json",
    dry_run: bool = False,
) -> dict[str, Any]:
    outcomes_path = Path(cfg.outcomes_path)
    open_symbols: set[str] = set()
    live_path = Path(live_state_path)
    if live_path.exists():
        try:
            live_state = json.loads(live_path.read_text(encoding="utf-8"))
            open_symbols = {normalize_symbol(k) for k in live_state.get("last_open_positions", {})}
        except json.JSONDecodeError:
            pass

    rows = load_outcomes_jsonl(outcomes_path)
    repaired, stats = repair_outcome_rows(rows, open_symbols)
    report: dict[str, Any] = {
        "repairedAt": int(time.time()),
        "beforeCount": len(rows),
        "afterCount": len(repaired),
        "openSymbols": sorted(open_symbols),
        "stats": stats,
        "keptSymbols": sorted({normalize_symbol(str(r.get("symbol", ""))) for r in repaired}),
        "dryRun": dry_run,
    }
    if dry_run:
        return report

    backup_path: str | None = None
    if outcomes_path.exists() and rows != repaired:
        backup = outcomes_path.with_suffix(outcomes_path.suffix + f".bak-{int(time.time())}")
        shutil.copy2(outcomes_path, backup)
        backup_path = str(backup)
        write_outcomes_jsonl(outcomes_path, repaired)
    elif not outcomes_path.exists():
        write_outcomes_jsonl(outcomes_path, repaired)

    snapshot = compute_trade_learning_snapshot(cfg)
    report["backupPath"] = backup_path
    report["learningSnapshot"] = {
        "sampleSize": snapshot.get("sampleSize"),
        "winRate": snapshot.get("winRate"),
        "sizingFactor": snapshot.get("sizingFactor"),
        "confidenceBump": snapshot.get("confidenceBump"),
        "symbolStats": snapshot.get("symbolStats"),
        "bucketStats": snapshot.get("bucketStats"),
        "discoveryLiveMode": snapshot.get("discoveryLiveMode"),
        "bucketLiveModes": snapshot.get("bucketLiveModes"),
        "recentOutcomes": snapshot.get("recentOutcomes"),
    }
    return report


def recompute_trade_learning_snapshot(cfg: TradeLearningConfig) -> dict[str, Any]:
    return compute_trade_learning_snapshot(cfg)

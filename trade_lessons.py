"""Derive win/loss patterns from closed trades and enforce lesson-based gates."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal
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
class TradeLessonsConfig:
    enabled: bool = True
    lessons_path: str = "logs/trade-lessons.json"
    block_short_on_pump_pct: Decimal = Decimal("0.50")
    block_short_in_squeeze: bool = True
    block_long_chase_gainer_pct: Decimal = Decimal("0.15")
    chase_long_min_momentum: Decimal = Decimal("0.02")
    chase_long_fusion_bypass: Decimal = Decimal("0.55")
    block_short_oversold_rsi: Decimal = Decimal("30")
    min_confidence_short_on_gainer: Decimal = Decimal("0.95")
    block_short_positive_momentum: bool = True
    block_short_from_gainers_bucket: bool = True
    min_momentum_for_short: Decimal = Decimal("0")
    late_gainer_min_24h_change: Decimal = Decimal("0.15")
    late_gainer_max_rsi_long: Decimal = Decimal("70")
    late_gainer_min_fusion_bull_pct: Decimal = Decimal("0.75")
    require_late_gainer_mtf15_bullish: bool = True
    require_late_gainer_1m_bullish: bool = True
    loser_long_min_24h_drop: Decimal = Decimal("0.10")
    require_loser_long_5m_bullish: bool = True
    loser_short_min_24h_drop: Decimal = Decimal("0.10")
    loser_short_oversold_rsi: Decimal = Decimal("30")
    # F4: block counter-trend LONGs into the day's biggest losers (futuresLosers
    # bucket). Data (shadow, 31 trades): 29% win rate, net -1.45 USDT — the weakest
    # LONG cohort, "catching a falling knife". SHORTs from the same bucket are the
    # profitable edge and are untouched (this gate is BUY-only). A high-conviction
    # genuine reversal can still pass via the confidence bypass.
    block_long_from_losers_bucket: bool = True
    long_losers_bypass_confidence: Decimal = Decimal("0.95")
    min_lesson_sample_for_block: int = 12
    min_lesson_win_rate_block: Decimal = Decimal("0.35")
    max_lesson_profit_factor_block: Decimal = Decimal("0.85")
    profitable_edge_profit_factor: Decimal = Decimal("1.20")
    fast_lesson_block_enabled: bool = True
    fast_lesson_min_sample: int = 1
    fast_lesson_total_loss: Decimal = Decimal("-1.0")
    fast_lesson_max_profit_factor: Decimal = Decimal("0.25")
    fast_lesson_max_win_rate: Decimal = Decimal("0.10")


def trade_lessons_from_config(raw: dict[str, Any] | None) -> TradeLessonsConfig:
    raw = raw or {}
    return TradeLessonsConfig(
        enabled=bool(raw.get("enabled", True)),
        lessons_path=str(raw.get("lessons_path", "logs/trade-lessons.json")),
        block_short_on_pump_pct=decimal_from(raw.get("block_short_on_pump_pct", "0.50")),
        block_short_in_squeeze=bool(raw.get("block_short_in_squeeze", True)),
        block_long_chase_gainer_pct=decimal_from(raw.get("block_long_chase_gainer_pct", "0.15")),
        chase_long_min_momentum=decimal_from(raw.get("chase_long_min_momentum", "0.02")),
        chase_long_fusion_bypass=decimal_from(raw.get("chase_long_fusion_bypass", "0.55")),
        block_short_oversold_rsi=decimal_from(raw.get("block_short_oversold_rsi", "30")),
        min_confidence_short_on_gainer=decimal_from(
            raw.get("min_confidence_short_on_gainer", "0.95")
        ),
        block_short_positive_momentum=bool(raw.get("block_short_positive_momentum", True)),
        block_short_from_gainers_bucket=bool(raw.get("block_short_from_gainers_bucket", True)),
        min_momentum_for_short=decimal_from(raw.get("min_momentum_for_short", "0")),
        late_gainer_min_24h_change=decimal_from(raw.get("late_gainer_min_24h_change", "0.15")),
        late_gainer_max_rsi_long=decimal_from(raw.get("late_gainer_max_rsi_long", "70")),
        late_gainer_min_fusion_bull_pct=decimal_from(raw.get("late_gainer_min_fusion_bull_pct", "0.75")),
        require_late_gainer_mtf15_bullish=bool(raw.get("require_late_gainer_mtf15_bullish", True)),
        require_late_gainer_1m_bullish=bool(raw.get("require_late_gainer_1m_bullish", True)),
        loser_long_min_24h_drop=decimal_from(raw.get("loser_long_min_24h_drop", "0.10")),
        require_loser_long_5m_bullish=bool(raw.get("require_loser_long_5m_bullish", True)),
        loser_short_min_24h_drop=decimal_from(raw.get("loser_short_min_24h_drop", "0.10")),
        loser_short_oversold_rsi=decimal_from(raw.get("loser_short_oversold_rsi", "30")),
        block_long_from_losers_bucket=bool(raw.get("block_long_from_losers_bucket", True)),
        long_losers_bypass_confidence=decimal_from(raw.get("long_losers_bypass_confidence", "0.95")),
        min_lesson_sample_for_block=int(raw.get("min_lesson_sample_for_block", 12)),
        min_lesson_win_rate_block=decimal_from(raw.get("min_lesson_win_rate_block", "0.35")),
        max_lesson_profit_factor_block=decimal_from(raw.get("max_lesson_profit_factor_block", "0.85")),
        profitable_edge_profit_factor=decimal_from(raw.get("profitable_edge_profit_factor", "1.20")),
        fast_lesson_block_enabled=bool(raw.get("fast_lesson_block_enabled", True)),
        fast_lesson_min_sample=int(raw.get("fast_lesson_min_sample", 1)),
        fast_lesson_total_loss=decimal_from(raw.get("fast_lesson_total_loss", "-1.0")),
        fast_lesson_max_profit_factor=decimal_from(raw.get("fast_lesson_max_profit_factor", "0.25")),
        fast_lesson_max_win_rate=decimal_from(raw.get("fast_lesson_max_win_rate", "0.10")),
    )


LESSON_RULES: dict[str, str] = {
    "short_on_pump": "24h pump short / squeeze risk",
    "short_in_squeeze": "short opened in squeeze regime",
    "long_chase_gainer_weak_momentum": "gainer long with weak immediate momentum",
    "short_oversold": "short opened into oversold RSI",
    "short_from_gainers_bucket": "short opened from gainers discovery bucket",
    "short_positive_momentum": "short opened while immediate momentum is not negative enough",
    "long_from_losers_bucket": "long opened from losers discovery bucket",
    "long_gainer_mtf15_conflict": "late gainer long while 15m trend is not bullish",
    "long_gainer_overheated": "late gainer long with overheated RSI / weak immediate confirmation",
    "long_loser_without_5m_confirmation": "losers-bucket long before 5m reversal confirmation",
    "short_loser_oversold_chase": "losers-bucket short after a large oversold drop",
}

FAST_BLOCK_RULE_IDS = {
    "long_gainer_mtf15_conflict",
    "long_gainer_overheated",
    "long_loser_without_5m_confirmation",
    "short_loser_oversold_chase",
}

STATIC_SAFETY_RULE_IDS = {
    "long_gainer_mtf15_conflict",
    "long_gainer_overheated",
    "long_loser_without_5m_confirmation",
    "short_loser_oversold_chase",
}


def _bucket_has(bucket: str, name: str) -> bool:
    bucket_name = str(bucket or "")
    return name in bucket_name or bucket_name == f"futures{name}"


def _ctx_value(ctx: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        value = ctx.get(key)
        if value not in (None, ""):
            return value
    return default


def lesson_rule_ids_for_outcome(row: dict[str, Any], cfg: TradeLessonsConfig | None = None) -> set[str]:
    cfg = cfg or TradeLessonsConfig()
    ctx = row.get("openContext") or {}
    side = str(row.get("positionSide", "LONG")).upper()
    regime = str(ctx.get("regime") or row.get("regime") or "").lower()
    bucket = str(ctx.get("bucket") or "")
    change = decimal_from(_ctx_value(ctx, "priceChangePct24h", "price_change_pct_24h"))
    rsi = decimal_from(ctx.get("rsi"))
    momentum = decimal_from(ctx.get("momentum"))
    mtf_1m = str(_ctx_value(ctx, "mtf1m", "mtf_1m")).lower()
    mtf_5m = str(_ctx_value(ctx, "mtf5m", "mtf_5m")).lower()
    mtf_15m = str(_ctx_value(ctx, "mtf15m", "mtf_15m")).lower()
    rules: set[str] = set()
    gainer_bucket = _bucket_has(bucket, "Gainers")
    loser_bucket = _bucket_has(bucket, "Losers")
    if side == "SHORT" and cfg.block_short_on_pump_pct > 0 and change >= cfg.block_short_on_pump_pct:
        rules.add("short_on_pump")
    if side == "SHORT" and regime == "squeeze":
        rules.add("short_in_squeeze")
    if (
        side == "LONG"
        and gainer_bucket
        and cfg.block_long_chase_gainer_pct > 0
        and change >= cfg.block_long_chase_gainer_pct
        and abs(momentum) < cfg.chase_long_min_momentum
    ):
        rules.add("long_chase_gainer_weak_momentum")
    if side == "LONG" and gainer_bucket and change >= cfg.late_gainer_min_24h_change:
        if cfg.require_late_gainer_mtf15_bullish and mtf_15m and mtf_15m != "bullish":
            rules.add("long_gainer_mtf15_conflict")
        if (
            cfg.late_gainer_max_rsi_long > 0
            and rsi >= cfg.late_gainer_max_rsi_long
            and (cfg.require_late_gainer_1m_bullish and mtf_1m != "bullish")
        ):
            rules.add("long_gainer_overheated")
    if side == "SHORT" and cfg.block_short_oversold_rsi > 0 and rsi > 0 and rsi <= cfg.block_short_oversold_rsi:
        rules.add("short_oversold")
    if side == "SHORT" and cfg.block_short_from_gainers_bucket and _bucket_has(bucket, "Gainers"):
        rules.add("short_from_gainers_bucket")
    if side == "SHORT" and cfg.block_short_positive_momentum and momentum > cfg.min_momentum_for_short:
        rules.add("short_positive_momentum")
    if side == "LONG" and cfg.block_long_from_losers_bucket and loser_bucket:
        rules.add("long_from_losers_bucket")
    if (
        side == "LONG"
        and loser_bucket
        and cfg.require_loser_long_5m_bullish
        and change <= -cfg.loser_long_min_24h_drop
        and mtf_5m != "bullish"
    ):
        rules.add("long_loser_without_5m_confirmation")
    if (
        side == "SHORT"
        and loser_bucket
        and change <= -cfg.loser_short_min_24h_drop
        and cfg.loser_short_oversold_rsi > 0
        and rsi > 0
        and rsi <= cfg.loser_short_oversold_rsi
    ):
        rules.add("short_loser_oversold_chase")
    return rules


def _edge_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(1 for row in rows if _outcome_pnl(row) > 0)
    losses = sum(1 for row in rows if _outcome_pnl(row) < 0)
    total = len(rows)
    total_pnl = sum((_outcome_pnl(row) for row in rows), Decimal("0"))
    gross_win = sum((_outcome_pnl(row) for row in rows if _outcome_pnl(row) > 0), Decimal("0"))
    gross_loss = abs(sum((_outcome_pnl(row) for row in rows if _outcome_pnl(row) < 0), Decimal("0")))
    win_rate = Decimal(wins) / Decimal(total) if total else Decimal("0")
    profit_factor = gross_win / gross_loss if gross_loss > 0 else (Decimal("999") if gross_win > 0 else Decimal("0"))
    expectancy = total_pnl / Decimal(total) if total else Decimal("0")
    return {
        "sampleSize": total,
        "wins": wins,
        "losses": losses,
        "winRate": str(win_rate.quantize(Decimal("0.01"))),
        "totalPnl": str(total_pnl.quantize(Decimal("0.0001"))),
        "grossWin": str(gross_win.quantize(Decimal("0.0001"))),
        "grossLoss": str(gross_loss.quantize(Decimal("0.0001"))),
        "profitFactor": str(profit_factor.quantize(Decimal("0.01"))),
        "expectancyPnl": str(expectancy.quantize(Decimal("0.0001"))),
    }


def _outcome_pnl(row: dict[str, Any]) -> Decimal:
    if row.get("netPnl") not in (None, ""):
        return decimal_from(row.get("netPnl", "0"))
    return decimal_from(row.get("realizedPnl", "0"))


def _has_profitable_edge(stat: dict[str, Any], *, min_profit_factor: Decimal) -> bool:
    total_pnl = decimal_from(stat.get("totalPnl", stat.get("totalRealizedPnl", "0")))
    profit_factor = decimal_from(stat.get("profitFactor", "0"))
    return total_pnl > 0 and profit_factor >= min_profit_factor


def lesson_rule_status(stat: dict[str, Any], cfg: TradeLessonsConfig, rule_id: str = "") -> str:
    sample = int(stat.get("sampleSize", 0))
    total_pnl = decimal_from(stat.get("totalPnl", "0"))
    win_rate = decimal_from(stat.get("winRate", "0"))
    profit_factor = decimal_from(stat.get("profitFactor", "0"))
    if _has_profitable_edge(stat, min_profit_factor=cfg.profitable_edge_profit_factor):
        return "allowed_profitable_edge"
    if (
        cfg.fast_lesson_block_enabled
        and rule_id in FAST_BLOCK_RULE_IDS
        and sample >= cfg.fast_lesson_min_sample
        and total_pnl <= cfg.fast_lesson_total_loss
        and win_rate <= cfg.fast_lesson_max_win_rate
        and profit_factor <= cfg.fast_lesson_max_profit_factor
    ):
        return "cooldown_block"
    if sample < cfg.min_lesson_sample_for_block:
        return "observing"
    if total_pnl < 0 and (profit_factor <= cfg.max_lesson_profit_factor_block or win_rate < cfg.min_lesson_win_rate_block):
        return "hard_block"
    if total_pnl < 0:
        return "soft_penalty"
    return "observing"


def _tag_outcome(row: dict[str, Any], cfg: TradeLessonsConfig | None = None) -> list[str]:
    cfg = cfg or TradeLessonsConfig()
    ctx = row.get("openContext") or {}
    tags: list[str] = []
    bucket = str(ctx.get("bucket") or "")
    regime = str(ctx.get("regime") or row.get("regime") or "")
    side = str(row.get("positionSide", "LONG")).upper()
    change = decimal_from(_ctx_value(ctx, "priceChangePct24h", "price_change_pct_24h"))
    conf = decimal_from(ctx.get("confidence"))
    rsi = decimal_from(ctx.get("rsi"))
    momentum = decimal_from(ctx.get("momentum"))
    mtf_1m = str(_ctx_value(ctx, "mtf1m", "mtf_1m")).lower()
    mtf_5m = str(_ctx_value(ctx, "mtf5m", "mtf_5m")).lower()
    mtf_15m = str(_ctx_value(ctx, "mtf15m", "mtf_15m")).lower()
    mfe = decimal_from(ctx.get("mfePct")) if ctx.get("mfePct") not in (None, "") else Decimal("0")
    pnl = _outcome_pnl(row)

    if "Gainers" in bucket or bucket == "futuresGainers":
        tags.append("discovery_gainer")
    if "Losers" in bucket or bucket == "futuresLosers":
        tags.append("discovery_loser")
    if regime == "squeeze":
        tags.append("regime_squeeze")
    if regime == "trend_up":
        tags.append("regime_trend_up")
    if regime == "trend_down":
        tags.append("regime_trend_down")
    if side == "SHORT" and change >= Decimal("0.50"):
        tags.append("short_into_pump")
    if side == "LONG" and change >= cfg.block_long_chase_gainer_pct and abs(momentum) < cfg.chase_long_min_momentum:
        tags.append("long_chase_weak_momentum")
    if side == "LONG" and ("Gainers" in bucket or bucket == "futuresGainers") and change >= cfg.late_gainer_min_24h_change:
        if mtf_15m and mtf_15m != "bullish":
            tags.append("mtf15_conflict")
        if rsi >= cfg.late_gainer_max_rsi_long and mtf_1m != "bullish":
            tags.append("overheated_no_1m_confirmation")
    if side == "LONG" and ("Losers" in bucket or bucket == "futuresLosers") and change <= -cfg.loser_long_min_24h_drop and mtf_5m != "bullish":
        tags.append("loser_long_no_5m_confirmation")
    if side == "SHORT" and ("Losers" in bucket or bucket == "futuresLosers") and change <= -cfg.loser_short_min_24h_drop and rsi > 0 and rsi <= cfg.loser_short_oversold_rsi:
        tags.append("loser_short_oversold_chase")
    if pnl < 0 and mfe >= Decimal("0.01"):
        tags.append("profit_giveback_loss")
    if side == "SHORT" and regime == "squeeze":
        tags.append("short_in_squeeze")
    if side == "SHORT" and rsi > 0 and rsi <= Decimal("30"):
        tags.append("short_oversold")
    if side == "LONG" and change < Decimal("0.30") and conf >= Decimal("0.95") and regime == "trend_up":
        tags.append("early_trend_long")
    if pnl > 0:
        tags.append("win")
    elif pnl < 0:
        tags.append("loss")
    return tags


def _lesson_text(row: dict[str, Any], tags: list[str]) -> str:
    sym = row.get("symbol", "?")
    pnl = _outcome_pnl(row)
    ctx = row.get("openContext") or {}
    if pnl > 0:
        if "early_trend_long" in tags:
            return (
                f"{sym} 盈利：涨幅榜早期 trend_up 开多（conf={ctx.get('confidence')}），"
                "在 24h 暴涨前顺势入场，TP/主动减仓锁利。"
            )
        return f"{sym} 盈利：{', '.join(tags)}"
    if "short_into_pump" in tags:
        return f"{sym} 亏损：在 24h 已暴涨后仍开空，被轧空止损。"
    if "long_chase_weak_momentum" in tags:
        return f"{sym} 亏损：涨幅榜追高开多但动量衰竭，典型 pump 回落。"
    if "mtf15_conflict" in tags:
        return f"{sym} 亏损：涨幅榜追多但 15m 趋势未确认，入场过晚。"
    if "loser_long_no_5m_confirmation" in tags:
        return f"{sym} 亏损：跌幅榜反转开多但 5m 未转多，反弹确认不足。"
    if "loser_short_oversold_chase" in tags:
        return f"{sym} 亏损：跌幅榜大跌后 RSI 超卖仍追空，反弹扫损。"
    if "profit_giveback_loss" in tags:
        return f"{sym} 亏损：入场后一度有浮盈但未锁住，利润保护偏慢。"
    if "short_in_squeeze" in tags:
        return f"{sym} 亏损：squeeze 区间开空，方向不明易被扫止损。"
    if "short_oversold" in tags:
        return f"{sym} 亏损：超卖区追空，反弹打止损。"
    if "discovery_loser" in tags and "regime_trend_down" in tags:
        return f"{sym} 亏损：跌幅榜追空但缺乏延续性，均值回归反弹。"
    return f"{sym} 亏损：{', '.join(tags)}"


def build_lessons_document(outcomes: list[dict[str, Any]], cfg: TradeLessonsConfig | None = None) -> dict[str, Any]:
    cfg = cfg or TradeLessonsConfig()
    wins: list[dict[str, Any]] = []
    losses: list[dict[str, Any]] = []
    tag_loss_counts: dict[str, int] = {}
    tag_win_counts: dict[str, int] = {}
    rows_by_rule: dict[str, list[dict[str, Any]]] = {rule_id: [] for rule_id in LESSON_RULES}

    for row in outcomes:
        tags = _tag_outcome(row, cfg)
        for rule_id in lesson_rule_ids_for_outcome(row, cfg):
            rows_by_rule.setdefault(rule_id, []).append(row)
        entry = {
            "symbol": row.get("symbol"),
            "realizedPnl": row.get("realizedPnl"),
            "positionSide": row.get("positionSide"),
            "closeSource": row.get("closeSource"),
            "closedAt": row.get("closedAt"),
            "openContext": row.get("openContext"),
            "tags": tags,
            "lesson": _lesson_text(row, tags),
        }
        pnl = _outcome_pnl(row)
        if pnl > 0:
            wins.append(entry)
        elif pnl < 0:
            losses.append(entry)
        for tag in tags:
            if tag in {"win", "loss"}:
                continue
            if pnl > 0:
                tag_win_counts[tag] = tag_win_counts.get(tag, 0) + 1
            elif pnl < 0:
                tag_loss_counts[tag] = tag_loss_counts.get(tag, 0) + 1

    rule_stats: dict[str, dict[str, Any]] = {}
    for rule_id, rows in rows_by_rule.items():
        stat = _edge_stats(rows)
        stat["description"] = LESSON_RULES.get(rule_id, rule_id)
        stat["status"] = lesson_rule_status(stat, cfg, rule_id=rule_id)
        rule_stats[rule_id] = stat
    active_rules = [
        {
            "id": rule_id,
            "enabled": stat.get("status") in {"hard_block", "cooldown_block"},
            "status": stat.get("status"),
            "description": stat.get("description"),
            "sampleSize": stat.get("sampleSize"),
            "winRate": stat.get("winRate"),
            "totalPnl": stat.get("totalPnl"),
            "profitFactor": stat.get("profitFactor"),
        }
        for rule_id, stat in rule_stats.items()
        if stat.get("status") in {"hard_block", "cooldown_block", "soft_penalty"}
    ]

    return {
        "generatedAt": int(time.time()),
        "summary": {
            "wins": len(wins),
            "losses": len(losses),
            "totalPnl": str(
                sum((_outcome_pnl(r) for r in outcomes), Decimal("0")).quantize(
                    Decimal("0.0001")
                )
            ),
            "tagLossCounts": tag_loss_counts,
            "tagWinCounts": tag_win_counts,
        },
        "wins": wins,
        "losses": losses,
        "ruleStats": rule_stats,
        "activeRules": active_rules,
    }


def save_lessons_document(path: str | Path, doc: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def load_lessons_document(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def refresh_trade_lessons(
    cfg: TradeLessonsConfig,
    outcomes_path: str,
) -> dict[str, Any]:
    if not cfg.enabled:
        return {"enabled": False}
    from trade_outcomes import load_recent_outcomes

    rows = load_recent_outcomes(outcomes_path, limit=100)
    doc = build_lessons_document(rows, cfg)
    save_lessons_document(cfg.lessons_path, doc)
    return doc


def _format_pct(value: Decimal) -> str:
    return f"{(value * 100):+.2f}%"


def trade_lesson_block_reasons(
    *,
    cfg: TradeLessonsConfig,
    order_action: str,
    reduce_only: bool,
    position_qty: Decimal,
    regime: str,
    change_24h: Decimal,
    momentum: Decimal,
    rsi: Decimal,
    confidence: Decimal,
    bucket: str,
    fusion_bull_pct: Decimal | None = None,
    entry_quadrant: str = "",
    mtf_1m: str = "",
    mtf_5m: str = "",
    mtf_15m: str = "",
    lesson_stats: dict[str, Any] | None = None,
) -> list[str]:
    if not cfg.enabled or reduce_only or position_qty != 0:
        return []
    blocked: list[str] = []
    regime_kind = str(regime or "").lower()
    bucket_name = str(bucket or "")
    mtf_1m = str(mtf_1m or "").lower()
    mtf_5m = str(mtf_5m or "").lower()
    mtf_15m = str(mtf_15m or "").lower()

    def rule_blocks(rule_id: str) -> bool:
        if lesson_stats is None:
            return True
        stat = lesson_stats.get(rule_id) or {}
        status = lesson_rule_status(stat, cfg, rule_id=rule_id)
        if rule_id in STATIC_SAFETY_RULE_IDS:
            return status != "allowed_profitable_edge"
        return status in {"hard_block", "cooldown_block"}

    def rule_note(rule_id: str) -> str:
        stat = (lesson_stats or {}).get(rule_id) or {}
        if not stat:
            return ""
        return (
            f" [rule {rule_id}: n={int(stat.get('sampleSize', 0))}, "
            f"WR={decimal_from(stat.get('winRate', '0')):.0%}, "
            f"PF={decimal_from(stat.get('profitFactor', '0')):.2f}, "
            f"PnL={stat.get('totalPnl', '0')}]"
        )

    if (
        order_action == "SELL"
        and cfg.block_short_on_pump_pct > 0
        and change_24h >= cfg.block_short_on_pump_pct
        and rule_blocks("short_on_pump")
    ):
        blocked.append(
            f"Trade lesson: block short on pump — 24h {_format_pct(change_24h)} "
            f">= {_format_pct(cfg.block_short_on_pump_pct)} (轧空风险)."
            f"{rule_note('short_on_pump')}"
        )

    if order_action == "SELL" and cfg.block_short_in_squeeze and regime_kind == "squeeze":
        losers_bucket = "Losers" in bucket_name or bucket_name == "futuresLosers"
        if not losers_bucket and rule_blocks("short_in_squeeze"):
            blocked.append(
                "Trade lesson: block short in squeeze — 方向未明，历史 EPIC 类止损教训."
                f"{rule_note('short_in_squeeze')}"
            )

    if (
        order_action == "SELL"
        and cfg.block_short_oversold_rsi > 0
        and rsi > 0
        and rsi <= cfg.block_short_oversold_rsi
        and rule_blocks("short_oversold")
    ):
        blocked.append(
            f"Trade lesson: RSI {rsi:.1f} oversold — block new short (反弹风险)."
            f"{rule_note('short_oversold')}"
        )

    if (
        order_action == "BUY"
        and ("Gainers" in bucket_name or bucket_name == "futuresGainers")
        and cfg.block_long_chase_gainer_pct > 0
        and change_24h >= cfg.block_long_chase_gainer_pct
        and abs(momentum) < cfg.chase_long_min_momentum
    ):
        if rule_blocks("long_chase_gainer_weak_momentum"):
            blocked.append(
                f"Trade lesson: gainer chase blocked — 24h {_format_pct(change_24h)} "
                f"but momentum {_format_pct(momentum)} weak (PORTAL 类教训)."
                f"{rule_note('long_chase_gainer_weak_momentum')}"
            )

    if (
        order_action == "BUY"
        and ("Gainers" in bucket_name or bucket_name == "futuresGainers")
        and cfg.late_gainer_min_24h_change > 0
        and change_24h >= cfg.late_gainer_min_24h_change
    ):
        if (
            cfg.require_late_gainer_mtf15_bullish
            and mtf_15m
            and mtf_15m != "bullish"
            and rule_blocks("long_gainer_mtf15_conflict")
        ):
            blocked.append(
                f"Trade lesson: late gainer long blocked — 24h {_format_pct(change_24h)} "
                f"but 15m is {mtf_15m}, not bullish."
                f"{rule_note('long_gainer_mtf15_conflict')}"
            )
        if (
            cfg.late_gainer_max_rsi_long > 0
            and rsi >= cfg.late_gainer_max_rsi_long
            and cfg.require_late_gainer_1m_bullish
            and mtf_1m != "bullish"
            and rule_blocks("long_gainer_overheated")
        ):
            blocked.append(
                f"Trade lesson: overheated gainer long blocked — RSI {rsi:.1f} "
                f"with 1m={mtf_1m or 'neutral'}; wait for fresh confirmation."
                f"{rule_note('long_gainer_overheated')}"
            )

    if (
        order_action == "SELL"
        and change_24h >= Decimal("0.30")
        and confidence < cfg.min_confidence_short_on_gainer
    ):
        blocked.append(
            f"Trade lesson: short on +{_format_pct(change_24h)} gainer needs "
            f"confidence >= {cfg.min_confidence_short_on_gainer}."
        )

    if (
        order_action == "SELL"
        and cfg.block_short_from_gainers_bucket
        and "Gainers" in bucket_name
        and rule_blocks("short_from_gainers_bucket")
    ):
        blocked.append(
            "Trade lesson: block short from gainers bucket — 涨幅榜做空历史 0 胜率."
            f"{rule_note('short_from_gainers_bucket')}"
        )

    if (
        order_action == "SELL"
        and cfg.block_short_positive_momentum
        and momentum > cfg.min_momentum_for_short
        and rule_blocks("short_positive_momentum")
    ):
        blocked.append(
            f"Trade lesson: momentum {_format_pct(momentum)} > required short momentum "
            f"{_format_pct(cfg.min_momentum_for_short)} — block short (EVAA 类教训)."
            f"{rule_note('short_positive_momentum')}"
        )

    # F4: block counter-trend LONGs into the day's biggest losers (futuresLosers
    # bucket). Shadow data (31 trades): 29% win rate, net -1.45 USDT — the weakest
    # LONG cohort ("catching a falling knife"). SHORTs from this bucket are the +5.37
    # USDT edge and are NOT touched (this gate is BUY-only). A genuine high-conviction
    # reversal can still pass when confidence >= long_losers_bypass_confidence.
    if (
        order_action == "BUY"
        and cfg.block_long_from_losers_bucket
        and ("Losers" in bucket_name or bucket_name == "futuresLosers")
        and rule_blocks("long_from_losers_bucket")
    ):
        confirmed_negative_edge = (
            lesson_stats is not None
            and lesson_rule_status(
                (lesson_stats or {}).get("long_from_losers_bucket") or {},
                cfg,
                rule_id="long_from_losers_bucket",
            )
            in {"hard_block", "cooldown_block"}
        )
        reversal_confirmed = mtf_5m == "bullish"
        if confidence >= cfg.long_losers_bypass_confidence and reversal_confirmed and not confirmed_negative_edge:
            return blocked
        suffix = (
            "confirmed negative expectancy; confidence-only bypass disabled"
            if confirmed_negative_edge
            else (
                f"5m={mtf_5m or 'neutral'} lacks reversal confirmation"
                if confidence >= cfg.long_losers_bypass_confidence
                else f"confidence {confidence:.2f} < {cfg.long_losers_bypass_confidence}"
            )
        )
        blocked.append(
            f"Trade lesson: block long from losers bucket — 逆势抄底下跌币历史 29% 胜率 "
            f"({suffix})."
            f"{rule_note('long_from_losers_bucket')}"
        )

    if (
        order_action == "BUY"
        and cfg.require_loser_long_5m_bullish
        and ("Losers" in bucket_name or bucket_name == "futuresLosers")
        and change_24h <= -cfg.loser_long_min_24h_drop
        and mtf_5m != "bullish"
        and rule_blocks("long_loser_without_5m_confirmation")
    ):
        blocked.append(
            f"Trade lesson: losers reversal long blocked — 24h {_format_pct(change_24h)} "
            f"and 5m={mtf_5m or 'neutral'}; wait for 5m bullish confirmation."
            f"{rule_note('long_loser_without_5m_confirmation')}"
        )

    if (
        order_action == "SELL"
        and ("Losers" in bucket_name or bucket_name == "futuresLosers")
        and change_24h <= -cfg.loser_short_min_24h_drop
        and cfg.loser_short_oversold_rsi > 0
        and rsi > 0
        and rsi <= cfg.loser_short_oversold_rsi
        and rule_blocks("short_loser_oversold_chase")
    ):
        blocked.append(
            f"Trade lesson: oversold loser short blocked — 24h {_format_pct(change_24h)}, "
            f"RSI {rsi:.1f}; wait for bounce failure before shorting."
            f"{rule_note('short_loser_oversold_chase')}"
        )

    return blocked


def trade_lessons_rationale_block(doc: dict[str, Any]) -> dict[str, Any]:
    if not doc:
        return {"enabled": False}
    summary = doc.get("summary") or {}
    rule_stats = doc.get("ruleStats") if isinstance(doc.get("ruleStats"), dict) else {}
    watched_rules = [
        {
            "id": rule_id,
            "status": stat.get("status"),
            "sampleSize": stat.get("sampleSize"),
            "winRate": stat.get("winRate"),
            "totalPnl": stat.get("totalPnl"),
            "profitFactor": stat.get("profitFactor"),
        }
        for rule_id, stat in rule_stats.items()
        if stat.get("sampleSize") or stat.get("status") in {"hard_block", "cooldown_block", "soft_penalty", "allowed_profitable_edge"}
    ]
    return {
        "enabled": True,
        "wins": summary.get("wins", 0),
        "losses": summary.get("losses", 0),
        "totalPnl": summary.get("totalPnl"),
        "recentLessons": [item.get("lesson") for item in (doc.get("losses") or [])[-3:]]
        + [item.get("lesson") for item in (doc.get("wins") or [])[-2:]],
        "activeRules": [r.get("description") for r in (doc.get("activeRules") or []) if r.get("enabled")],
        "ruleStats": watched_rules,
    }


def open_context_from_signal(
    *,
    signal_indicators: dict[str, Any],
    confidence: Decimal | str | None,
    discovery_meta: dict[str, Any] | None,
    entry_price: Decimal | str | None = None,
) -> dict[str, Any]:
    meta = discovery_meta or {}
    ctx: dict[str, Any] = {
        "regime": signal_indicators.get("regime"),
        "bucket": meta.get("bucket") or meta.get("source"),
        "source": meta.get("source"),
        "priceChangePct24h": str(
            signal_indicators.get("price_change_pct_24h")
            or meta.get("priceChangePct24h")
            or "0"
        ),
        "confidence": str(confidence or "0"),
        "rsi": str(signal_indicators.get("rsi", "0")),
        "momentum": str(signal_indicators.get("momentum", "0")),
        "fusionBullPct": str(signal_indicators.get("fusion_bull_pct", "")),
        "fusionBearPct": str(Decimal("1") - decimal_from(signal_indicators.get("fusion_bull_pct", "0") or "0")),
        "mtf1m": str(signal_indicators.get("mtf_1m", "")),
        "mtf5m": str(signal_indicators.get("mtf_5m", "")),
        "mtf15m": str(signal_indicators.get("mtf_15m", "")),
        "entryQuadrant": str(signal_indicators.get("entry_quadrant", "")),
        "entryQuadrantMode": str(signal_indicators.get("entry_quadrant_mode", "")),
        "capturedAt": int(time.time()),
    }
    # RC2: persist the ACTUAL fill price at open so trade-outcome PnL/analytics use
    # the real entry, not the portfolio's running average_price (which collapses to
    # a stale/rounded value after partial reduces). Only stored when a real fill
    # price is known; readers fall back to average_price when it's absent.
    if entry_price is not None and str(entry_price) not in ("", "0"):
        ctx["entryPrice"] = str(entry_price)
    return ctx

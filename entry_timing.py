"""Smart entry timing — limit orders at pullbacks / structure instead of blind MARKET chase."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from quadrant_strategy import (
    QUADRANT_REVERSAL_LONG,
    QUADRANT_REVERSAL_SHORT,
    QUADRANT_TREND_LONG,
    QUADRANT_TREND_SHORT,
    entry_quadrant,
)

BUY = "BUY"
SELL = "SELL"
ORDER_LIMIT = "LIMIT"
ORDER_MARKET = "MARKET"


def decimal_from(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


@dataclass(frozen=True)
class EntryTimingConfig:
    enabled: bool = True
    mode: str = "smart_limit"
    trend_pullback_atr_mult: Decimal = Decimal("0.35")
    trend_chase_limit_atr_mult: Decimal = Decimal("0.12")
    reversal_limit_atr_mult: Decimal = Decimal("0.20")
    use_fast_sma_anchor: bool = True
    sma_pullback_buffer_pct: Decimal = Decimal("0.0005")
    block_trend_chase_without_pullback: bool = False
    require_mtf_pullback_for_trend_long: bool = True
    require_mtf_pullback_for_trend_short: bool = True
    min_limit_offset_pct: Decimal = Decimal("0.0003")
    max_limit_offset_pct: Decimal = Decimal("0.012")
    limit_time_in_force: str = ""
    movement_aware_enabled: bool = True
    chase_max_momentum: Decimal = Decimal("0.025")
    chase_max_24h_change: Decimal = Decimal("0.20")
    require_1m_alignment_for_chase: bool = True
    defer_on_reversal_candle: bool = True
    strong_continuation_enabled: bool = True
    strong_continuation_min_confidence: Decimal = Decimal("0.95")
    strong_continuation_min_fusion_bull_pct: Decimal = Decimal("0.80")
    strong_continuation_min_fusion_bear_pct: Decimal = Decimal("0.80")
    strong_continuation_min_volume_ratio: Decimal = Decimal("1.00")
    strong_continuation_max_rsi_long: Decimal = Decimal("72")
    strong_continuation_min_rsi_short: Decimal = Decimal("28")
    strong_continuation_max_momentum: Decimal = Decimal("0.025")
    strong_continuation_max_24h_change: Decimal = Decimal("0.25")
    strong_continuation_require_1m_alignment: bool = True
    strong_continuation_min_quality_score: Decimal = Decimal("0.78")


def entry_timing_from_config(raw: dict[str, Any] | None) -> EntryTimingConfig:
    if not raw:
        return EntryTimingConfig(enabled=False)
    return EntryTimingConfig(
        enabled=bool(raw.get("enabled", True)),
        mode=str(raw.get("mode", "smart_limit")).lower(),
        trend_pullback_atr_mult=decimal_from(raw.get("trend_pullback_atr_mult", "0.35")),
        trend_chase_limit_atr_mult=decimal_from(raw.get("trend_chase_limit_atr_mult", "0.12")),
        reversal_limit_atr_mult=decimal_from(raw.get("reversal_limit_atr_mult", "0.20")),
        use_fast_sma_anchor=bool(raw.get("use_fast_sma_anchor", True)),
        sma_pullback_buffer_pct=decimal_from(raw.get("sma_pullback_buffer_pct", "0.0005")),
        block_trend_chase_without_pullback=bool(raw.get("block_trend_chase_without_pullback", False)),
        require_mtf_pullback_for_trend_long=bool(raw.get("require_mtf_pullback_for_trend_long", True)),
        require_mtf_pullback_for_trend_short=bool(raw.get("require_mtf_pullback_for_trend_short", True)),
        min_limit_offset_pct=decimal_from(raw.get("min_limit_offset_pct", "0.0003")),
        max_limit_offset_pct=decimal_from(raw.get("max_limit_offset_pct", "0.012")),
        limit_time_in_force=str(raw.get("limit_time_in_force", "") or "").upper(),
        movement_aware_enabled=bool(raw.get("movement_aware_enabled", True)),
        chase_max_momentum=decimal_from(raw.get("chase_max_momentum", "0.025")),
        chase_max_24h_change=decimal_from(raw.get("chase_max_24h_change", "0.20")),
        require_1m_alignment_for_chase=bool(raw.get("require_1m_alignment_for_chase", True)),
        defer_on_reversal_candle=bool(raw.get("defer_on_reversal_candle", True)),
        strong_continuation_enabled=bool(raw.get("strong_continuation_enabled", True)),
        strong_continuation_min_confidence=decimal_from(
            raw.get("strong_continuation_min_confidence", "0.95")
        ),
        strong_continuation_min_fusion_bull_pct=decimal_from(
            raw.get("strong_continuation_min_fusion_bull_pct", "0.80")
        ),
        strong_continuation_min_fusion_bear_pct=decimal_from(
            raw.get("strong_continuation_min_fusion_bear_pct", "0.80")
        ),
        strong_continuation_min_volume_ratio=decimal_from(
            raw.get("strong_continuation_min_volume_ratio", "1.00")
        ),
        strong_continuation_max_rsi_long=decimal_from(
            raw.get("strong_continuation_max_rsi_long", "72")
        ),
        strong_continuation_min_rsi_short=decimal_from(
            raw.get("strong_continuation_min_rsi_short", "28")
        ),
        strong_continuation_max_momentum=decimal_from(
            raw.get("strong_continuation_max_momentum", "0.025")
        ),
        strong_continuation_max_24h_change=decimal_from(
            raw.get("strong_continuation_max_24h_change", "0.25")
        ),
        strong_continuation_require_1m_alignment=bool(
            raw.get("strong_continuation_require_1m_alignment", True)
        ),
        strong_continuation_min_quality_score=decimal_from(
            raw.get("strong_continuation_min_quality_score", "0.78")
        ),
    )


def _atr_move(market_price: Decimal, atr_pct: Decimal) -> Decimal:
    if market_price <= 0 or atr_pct <= 0:
        return Decimal("0")
    return market_price * atr_pct


def _clamp_buy_limit(market_price: Decimal, limit: Decimal, cfg: EntryTimingConfig) -> Decimal:
    floor = market_price * (Decimal("1") - cfg.max_limit_offset_pct)
    ceiling = market_price * (Decimal("1") - cfg.min_limit_offset_pct)
    return max(floor, min(ceiling, limit))


def _clamp_sell_short_limit(market_price: Decimal, limit: Decimal, cfg: EntryTimingConfig) -> Decimal:
    floor = market_price * (Decimal("1") + cfg.min_limit_offset_pct)
    ceiling = market_price * (Decimal("1") + cfg.max_limit_offset_pct)
    return min(ceiling, max(floor, limit))


def _mtf_has_pullback_for_long(mtf_5m: str) -> bool:
    return mtf_5m == "bearish"


def _mtf_has_pullback_for_short(mtf_5m: str) -> bool:
    return mtf_5m == "bullish"


def _normalized_change_24h(value: Any) -> Decimal:
    parsed = decimal_from(value)
    if parsed == 0:
        return Decimal("0")
    if abs(parsed) > Decimal("1.5"):
        return parsed / Decimal("100")
    return parsed


def _strong_continuation_allows_chase(
    *,
    action: str,
    ind: dict[str, Any],
    cfg: EntryTimingConfig,
    confidence: Decimal,
    mtf_1m: str,
    mtf_5m: str,
    momentum: Decimal,
    change_24h: Decimal,
    kline_label: str,
) -> bool:
    if not cfg.strong_continuation_enabled:
        return False
    if confidence < cfg.strong_continuation_min_confidence:
        return False
    volume_ratio = decimal_from(ind.get("volume_ratio", "0") or "0")
    if volume_ratio < cfg.strong_continuation_min_volume_ratio:
        return False
    if cfg.defer_on_reversal_candle:
        if action == BUY and kline_label == "bearish":
            return False
        if action == SELL and kline_label == "bullish":
            return False
    if cfg.strong_continuation_max_24h_change > 0:
        if action == BUY and change_24h > cfg.strong_continuation_max_24h_change:
            return False
        if action == SELL and change_24h < -cfg.strong_continuation_max_24h_change:
            return False

    rsi = decimal_from(ind.get("rsi", "0") or "0")
    fusion_bull = decimal_from(ind.get("fusion_bull_pct", "0") or "0")
    fusion_bear = Decimal("1") - fusion_bull
    score = Decimal("0.20")  # confidence gate already passed.

    if action == BUY:
        if mtf_5m != "bullish":
            return False
        if mtf_1m == "bearish":
            return False
        if fusion_bull < cfg.strong_continuation_min_fusion_bull_pct:
            return False
        if rsi <= 0 or rsi > cfg.strong_continuation_max_rsi_long:
            return False
        if momentum <= 0:
            return False
        if cfg.strong_continuation_max_momentum > 0 and momentum > cfg.strong_continuation_max_momentum:
            return False
        if cfg.strong_continuation_require_1m_alignment and mtf_1m != "bullish":
            return False

        score += Decimal("0.25")
        score += Decimal("0.15")
        if mtf_1m == "bullish":
            score += Decimal("0.15")
        elif mtf_1m in {"neutral", ""}:
            score += Decimal("0.07")
        if cfg.chase_max_momentum > 0 and momentum < cfg.chase_max_momentum:
            score += Decimal("0.10")
        else:
            score += Decimal("0.05")
        if cfg.chase_max_24h_change > 0 and change_24h < cfg.chase_max_24h_change:
            score += Decimal("0.10")
        else:
            score += Decimal("0.05")
        if rsi <= cfg.strong_continuation_max_rsi_long - Decimal("4"):
            score += Decimal("0.10")
        else:
            score += Decimal("0.05")
        ind["entry_timing_strong_continuation_score"] = str(score.quantize(Decimal("0.01")))
        return score >= cfg.strong_continuation_min_quality_score

    if action == SELL:
        if mtf_5m != "bearish":
            return False
        if mtf_1m == "bullish":
            return False
        if fusion_bear < cfg.strong_continuation_min_fusion_bear_pct:
            return False
        if rsi <= 0 or rsi < cfg.strong_continuation_min_rsi_short:
            return False
        if momentum >= 0:
            return False
        if cfg.strong_continuation_max_momentum > 0 and abs(momentum) > cfg.strong_continuation_max_momentum:
            return False
        if cfg.strong_continuation_require_1m_alignment and mtf_1m != "bearish":
            return False

        score += Decimal("0.25")
        score += Decimal("0.15")
        if mtf_1m == "bearish":
            score += Decimal("0.15")
        elif mtf_1m in {"neutral", ""}:
            score += Decimal("0.07")
        if cfg.chase_max_momentum > 0 and abs(momentum) < cfg.chase_max_momentum:
            score += Decimal("0.10")
        else:
            score += Decimal("0.05")
        if cfg.chase_max_24h_change > 0 and abs(change_24h) < cfg.chase_max_24h_change:
            score += Decimal("0.10")
        else:
            score += Decimal("0.05")
        if rsi >= cfg.strong_continuation_min_rsi_short + Decimal("4"):
            score += Decimal("0.10")
        else:
            score += Decimal("0.05")
        ind["entry_timing_strong_continuation_score"] = str(score.quantize(Decimal("0.01")))
        return score >= cfg.strong_continuation_min_quality_score

    return False


def entry_timing_defer_reason(
    *,
    action: str,
    indicators: dict[str, str] | None,
    cfg: EntryTimingConfig,
    reduce_only: bool,
    confidence: Decimal | str | None = None,
) -> str | None:
    if not cfg.enabled or reduce_only or cfg.mode == "off":
        return None

    quad = entry_quadrant(indicators)
    ind = indicators or {}
    ind.pop("entry_timing_strong_continuation", None)
    ind.pop("entry_timing_chase_override_reason", None)
    ind.pop("entry_timing_strong_continuation_score", None)
    mtf_5m = str(ind.get("mtf_5m", "") or "")
    mtf_1m = str(ind.get("mtf_1m", "") or "")
    momentum = decimal_from(ind.get("momentum", "0") or "0")
    change_24h = _normalized_change_24h(ind.get("price_change_pct_24h", "0") or "0")
    kline_label = str(ind.get("kline_pattern_label", "") or "")
    signal_confidence = decimal_from(confidence or "0")

    if cfg.movement_aware_enabled:
        strong_continuation_ok = _strong_continuation_allows_chase(
            action=action,
            ind=ind,
            cfg=cfg,
            confidence=signal_confidence,
            mtf_1m=mtf_1m,
            mtf_5m=mtf_5m,
            momentum=momentum,
            change_24h=change_24h,
            kline_label=kline_label,
        )
        if strong_continuation_ok:
            ind["entry_timing_strong_continuation"] = "true"
            ind["entry_timing_chase_override_reason"] = (
                "Entry timing: high-confidence trend continuation allowed within hard "
                "momentum/24h/RSI limits."
            )
            return None
        if action == BUY:
            if cfg.require_1m_alignment_for_chase and mtf_1m == "bearish" and mtf_5m == "bullish":
                return "Entry timing: long direction kept, but 1m turned bearish against 5m; wait for pullback stabilization."
            if (
                cfg.chase_max_momentum > 0
                and momentum >= cfg.chase_max_momentum
                and mtf_5m == "bullish"
                and not strong_continuation_ok
            ):
                return (
                    f"Entry timing: long direction kept, but momentum {momentum:.2%} is extended; "
                    "wait for pullback instead of chasing."
                )
            if (
                cfg.chase_max_24h_change > 0
                and change_24h >= cfg.chase_max_24h_change
                and mtf_5m == "bullish"
                and not strong_continuation_ok
            ):
                return (
                    f"Entry timing: long direction kept, but 24h move {change_24h:.2%} is extended; "
                    "wait for a cleaner entry."
                )
            if cfg.defer_on_reversal_candle and kline_label == "bearish":
                return "Entry timing: long direction kept, but bearish reversal candle appeared; wait for confirmation."
        elif action == SELL:
            if cfg.require_1m_alignment_for_chase and mtf_1m == "bullish" and mtf_5m == "bearish":
                return "Entry timing: short direction kept, but 1m bounced against 5m; wait for bounce failure."
            if (
                cfg.chase_max_momentum > 0
                and momentum <= -cfg.chase_max_momentum
                and mtf_5m == "bearish"
                and not strong_continuation_ok
            ):
                return (
                    f"Entry timing: short direction kept, but downside momentum {momentum:.2%} is extended; "
                    "wait for bounce instead of chasing."
                )
            if (
                cfg.chase_max_24h_change > 0
                and change_24h <= -cfg.chase_max_24h_change
                and mtf_5m == "bearish"
                and not strong_continuation_ok
            ):
                return (
                    f"Entry timing: short direction kept, but 24h drop {change_24h:.2%} is extended; "
                    "wait for a cleaner entry."
                )
            if cfg.defer_on_reversal_candle and kline_label == "bullish":
                return "Entry timing: short direction kept, but bullish reversal candle appeared; wait for confirmation."

    if quad == QUADRANT_TREND_LONG and action == BUY:
        if cfg.require_mtf_pullback_for_trend_long and not _mtf_has_pullback_for_long(mtf_5m):
            return (
                "Entry timing: trend long deferred — waiting for 5m pullback "
                f"(mtf_5m={mtf_5m or 'neutral'}, need bearish)."
            )
        if cfg.block_trend_chase_without_pullback and mtf_5m == "bullish":
            return "Entry timing: block trend-long chase without 5m pullback."

    if quad == QUADRANT_TREND_SHORT and action == SELL:
        if cfg.require_mtf_pullback_for_trend_short and not _mtf_has_pullback_for_short(mtf_5m):
            return (
                "Entry timing: trend short deferred — waiting for 5m bounce "
                f"(mtf_5m={mtf_5m or 'neutral'}, need bullish)."
            )
        if cfg.block_trend_chase_without_pullback and mtf_5m == "bearish":
            return "Entry timing: block trend-short chase without 5m bounce."

    return None


def resolve_entry_limit_price(
    *,
    action: str,
    market_price: Decimal,
    indicators: dict[str, str] | None,
    cfg: EntryTimingConfig,
    fallback_offset_pct: Decimal,
) -> tuple[str, Decimal | None, list[str]]:
    """Return (order_type, limit_price, notes)."""
    if not cfg.enabled or cfg.mode == "off" or market_price <= 0:
        return ORDER_MARKET, None, []

    ind = indicators or {}
    quad = entry_quadrant(ind)
    atr_pct = decimal_from(ind.get("atr_pct", "0"))
    atr = _atr_move(market_price, atr_pct)
    fast_sma = decimal_from(ind.get("fast_sma", "0"))
    mtf_5m = str(ind.get("mtf_5m", "") or "")
    notes: list[str] = []

    if cfg.mode == "chase_fill":
        if quad == QUADRANT_TREND_LONG and action == BUY and mtf_5m == "bullish":
            notes.append("Chase-fill: trend long with 5m bullish — MARKET for fill priority.")
            return ORDER_MARKET, None, notes
        if quad == QUADRANT_TREND_SHORT and action == SELL and mtf_5m == "bearish":
            notes.append("Chase-fill: trend short with 5m bearish — MARKET for fill priority.")
            return ORDER_MARKET, None, notes

    if action == BUY:
        if quad == QUADRANT_REVERSAL_LONG:
            mult = cfg.reversal_limit_atr_mult
            limit = market_price - atr * mult if atr > 0 else market_price * (Decimal("1") - fallback_offset_pct)
            notes.append(f"Reversal long limit: -{mult}x ATR below market (RSI dip-buy).")
        elif quad == QUADRANT_TREND_LONG:
            if cfg.mode == "chase_fill":
                mult = cfg.trend_chase_limit_atr_mult
                limit = market_price - atr * mult if atr > 0 else market_price * (Decimal("1") - fallback_offset_pct)
                notes.append(f"Chase-fill trend long: tight limit -{mult}x ATR.")
            elif _mtf_has_pullback_for_long(mtf_5m):
                mult = cfg.trend_pullback_atr_mult
                limit = market_price - atr * mult if atr > 0 else market_price * (Decimal("1") - fallback_offset_pct)
                notes.append(f"Trend long pullback: 5m bearish, limit -{mult}x ATR.")
            else:
                mult = cfg.trend_chase_limit_atr_mult
                limit = market_price - atr * mult if atr > 0 else market_price * (Decimal("1") - fallback_offset_pct)
                if cfg.use_fast_sma_anchor and fast_sma > 0 and fast_sma < market_price:
                    sma_limit = fast_sma * (Decimal("1") + cfg.sma_pullback_buffer_pct)
                    limit = min(limit, sma_limit)
                    notes.append("Trend long: anchored to fast SMA support.")
                else:
                    notes.append(f"Trend long: soft limit -{mult}x ATR (no 5m pullback yet).")
        else:
            limit = market_price * (Decimal("1") - fallback_offset_pct)
            notes.append("Generic long limit offset.")

        limit = _clamp_buy_limit(market_price, limit, cfg)
        return ORDER_LIMIT, limit, notes

    if action == SELL:
        if quad == QUADRANT_REVERSAL_SHORT:
            mult = cfg.reversal_limit_atr_mult
            limit = market_price + atr * mult if atr > 0 else market_price * (Decimal("1") + fallback_offset_pct)
            notes.append(f"Reversal short limit: +{mult}x ATR above market (RSI fade).")
        elif quad == QUADRANT_TREND_SHORT:
            if cfg.mode == "chase_fill":
                mult = cfg.trend_chase_limit_atr_mult
                limit = market_price + atr * mult if atr > 0 else market_price * (Decimal("1") + fallback_offset_pct)
                notes.append(f"Chase-fill trend short: tight limit +{mult}x ATR.")
            elif _mtf_has_pullback_for_short(mtf_5m):
                mult = cfg.trend_pullback_atr_mult
                limit = market_price + atr * mult if atr > 0 else market_price * (Decimal("1") + fallback_offset_pct)
                notes.append(f"Trend short bounce: 5m bullish, limit +{mult}x ATR.")
            else:
                mult = cfg.trend_chase_limit_atr_mult
                limit = market_price + atr * mult if atr > 0 else market_price * (Decimal("1") + fallback_offset_pct)
                if cfg.use_fast_sma_anchor and fast_sma > 0 and fast_sma > market_price:
                    sma_limit = fast_sma * (Decimal("1") - cfg.sma_pullback_buffer_pct)
                    limit = max(limit, sma_limit)
                    notes.append("Trend short: anchored to fast SMA resistance.")
                else:
                    notes.append(f"Trend short: soft limit +{mult}x ATR (no 5m bounce yet).")
        else:
            limit = market_price * (Decimal("1") + fallback_offset_pct)
            notes.append("Generic short limit offset.")

        limit = _clamp_sell_short_limit(market_price, limit, cfg)
        return ORDER_LIMIT, limit, notes

    return ORDER_MARKET, None, notes


def enrich_order_intent(
    order: Any,
    auto_exec: Any,
    strategy: Any,
    asset: Any,
    *,
    atr_pct: Decimal,
    signal: Any = None,
    entry_timing_cfg: EntryTimingConfig | None = None,
    profitability_raw: dict[str, Any] | None = None,
) -> Any:
    """Apply smart limit pricing and TIF to an order intent."""
    from dataclasses import replace

    _ = (strategy, asset, profitability_raw)
    cfg = entry_timing_cfg or EntryTimingConfig(enabled=False)
    if getattr(order, "reduce_only", False):
        return order
    indicators = dict(getattr(signal, "indicators", None) or {})
    if atr_pct > 0 and "atr_pct" not in indicators:
        indicators["atr_pct"] = str(atr_pct)
    fallback = decimal_from(getattr(auto_exec, "limit_offset_pct", "0.001"))
    order_type, limit_price, _notes = resolve_entry_limit_price(
        action=str(order.action),
        market_price=decimal_from(order.estimated_price),
        indicators=indicators,
        cfg=cfg,
        fallback_offset_pct=fallback,
    )
    tif = cfg.limit_time_in_force or str(getattr(auto_exec, "time_in_force", "GTC"))
    if order_type == ORDER_LIMIT and limit_price is not None:
        return replace(order, order_type=ORDER_LIMIT, limit_price=limit_price, time_in_force=tif)
    return replace(order, order_type=ORDER_MARKET, limit_price=None, time_in_force=tif)

"""Unified exit engine for open futures positions — holding priority, peak giveback, force reduce."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from market_autotrader import (
        MarketSnapshot,
        PaperPortfolio,
        RiskConfig,
        Signal,
        StrategyConfig,
        TradingMemory,
    )

_MA: Any = None


def _ma() -> Any:
    global _MA
    if _MA is None:
        import market_autotrader as _MA
    return _MA


def _sym(symbol: str) -> str:
    return _ma().normalize_symbol(symbol)


def _dec(value: Any) -> Decimal:
    return _ma().decimal_from(value)


def position_peak_key(symbol: str, quantity: Decimal) -> str:
    sym = _sym(symbol)
    return f"{sym}:LONG" if quantity > 0 else f"{sym}:SHORT"


def sync_position_peak(
    memory: TradingMemory,
    symbol: str,
    price: Decimal,
    quantity: Decimal,
) -> None:
    if price <= 0:
        return
    sym = _sym(symbol)
    if quantity == 0:
        memory.position_peak_price.pop(f"{sym}:LONG", None)
        memory.position_peak_price.pop(f"{sym}:SHORT", None)
        return
    if quantity > 0:
        key = f"{sym}:LONG"
        prev = _dec(memory.position_peak_price.get(key, str(price)))
        memory.position_peak_price[key] = str(max(prev, price))
        memory.position_peak_price.pop(f"{sym}:SHORT", None)
    else:
        key = f"{sym}:SHORT"
        prev = _dec(memory.position_peak_price.get(key, str(price)))
        memory.position_peak_price[key] = str(min(prev, price))
        memory.position_peak_price.pop(f"{sym}:LONG", None)


def holding_thresholds_for_regime(
    strategy: StrategyConfig,
    regime: str,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Return (loss_pct, take_pct, trail_pct, giveback_pct) scaled for swing + regime."""
    loss_pct = strategy.holding_reduce_loss_pct
    take_pct = strategy.holding_take_profit_pct
    trail_pct = strategy.holding_trailing_activate_pct
    giveback_pct = strategy.holding_peak_giveback_pct
    if strategy.trade_horizon == "swing":
        loss_pct *= Decimal("1.5")
        take_pct *= Decimal("2")
        trail_pct *= Decimal("1.5")
    regime_kind = str(regime or "").lower()
    if regime_kind == "trend_up":
        giveback_pct *= strategy.holding_trend_up_giveback_mult
        take_pct *= Decimal("1.15")
    elif regime_kind in {"trend_down", "squeeze", "range"}:
        giveback_pct *= Decimal("0.85")
    return loss_pct, take_pct, trail_pct, giveback_pct


def peak_giveback_exit_reason(
    *,
    position_qty: Decimal,
    entry: Decimal,
    price: Decimal,
    peak_price: Decimal,
    trail_activate_pct: Decimal,
    giveback_pct: Decimal,
) -> str | None:
    if peak_price <= 0 or entry <= 0 or giveback_pct <= 0:
        return None
    if position_qty > 0:
        peak_pnl = (peak_price - entry) / entry
        if peak_pnl < trail_activate_pct:
            return None
        drawdown = (peak_price - price) / peak_price
        if drawdown >= giveback_pct:
            current_pnl = (price - entry) / entry
            return (
                f"Holding priority: peak giveback exit on long — "
                f"peak {peak_price} (+{peak_pnl:.2%}), mark {price} ({current_pnl:+.2%}), "
                f"drawdown from peak {drawdown:.2%} >= {giveback_pct:.2%}."
            )
    elif position_qty < 0:
        peak_pnl = (entry - peak_price) / entry
        if peak_pnl < trail_activate_pct:
            return None
        drawdown = (price - peak_price) / peak_price
        if drawdown >= giveback_pct:
            current_pnl = (entry - price) / entry
            return (
                f"Holding priority: peak giveback exit on short — "
                f"trough {peak_price} (+{peak_pnl:.2%}), mark {price} ({current_pnl:+.2%}), "
                f"bounce from trough {drawdown:.2%} >= {giveback_pct:.2%}."
            )
    return None


def mtf_5m_confirms_loss_exit(*, position_qty: Decimal, mtf_5m: str) -> bool:
    """Require 5m trend alignment before discretionary loss cuts (not force tier)."""
    side = str(mtf_5m or "").lower()
    if position_qty > 0:
        return side != "bullish"
    if position_qty < 0:
        return side != "bearish"
    return True


def mtf_5m_accelerates_profit_exit(*, position_qty: Decimal, mtf_5m: str) -> bool:
    side = str(mtf_5m or "").lower()
    if position_qty > 0:
        return side == "bearish"
    if position_qty < 0:
        return side == "bullish"
    return False


def _bucket_from_open_context(ctx: dict[str, Any] | None) -> str:
    raw = str((ctx or {}).get("bucket") or (ctx or {}).get("source") or "").strip()
    if raw.startswith("discovery:"):
        raw = raw.split(":", 1)[1]
    if raw.startswith("pinned"):
        return "pinned"
    return raw


def exit_quality_threshold_mult(
    trade_learning: dict[str, Any] | None,
    open_context: dict[str, Any] | None,
    *,
    min_sample: int = 4,
    low_capture: Decimal = Decimal("0.45"),
) -> Decimal:
    if not trade_learning or not trade_learning.get("enabled"):
        return Decimal("1")
    bucket = _bucket_from_open_context(open_context)
    if not bucket:
        return Decimal("1")
    configured = _dec((trade_learning.get("bucketExitQualityFactors") or {}).get(bucket, "0"))
    if configured > 0:
        return min(max(configured, Decimal("0.50")), Decimal("1.50"))
    stat = (trade_learning.get("bucketStats") or {}).get(bucket) or {}
    quality = stat.get("exitQuality") or {}
    if int(quality.get("sampleSize", 0)) < min_sample:
        return Decimal("1")
    capture = _dec(quality.get("avgCaptureRatio", "0"))
    has_mfe = quality.get("avgMfePct") not in (None, "")
    avg_mfe = _dec(quality.get("avgMfePct", "0"))
    if capture < low_capture and (avg_mfe > 0 or (capture > 0 and not has_mfe)):
        return Decimal("0.80")
    return Decimal("1")


def apply_supervisor_hint_bias(
    symbol: str,
    signal: Signal,
    supervisor_hints: dict[str, dict[str, Any]] | None,
) -> Signal:
    if not supervisor_hints:
        return signal
    ma = _ma()
    hint = supervisor_hints.get(_sym(symbol))
    if not hint:
        return signal
    action = str(hint.get("action", ""))
    reason = str(hint.get("reason") or "Supervisor hint")
    priority = str(hint.get("priority") or "medium")
    indicators = dict(signal.indicators)
    indicators["supervisor_hint"] = action
    indicators["supervisor_priority"] = priority
    warnings = list(signal.warnings)
    if action == "REDUCE_POSITION" and "supervisor_reduce" not in warnings:
        warnings.append("supervisor_reduce")
    elif action == "REASSESS_TAKE_PROFIT_OR_HOLD" and "supervisor_reassess" not in warnings:
        warnings.append("supervisor_reassess")
    return ma.Signal(
        action=signal.action,
        confidence=signal.confidence,
        reasons=list(signal.reasons),
        warnings=warnings,
        indicators=indicators,
    )


def apply_holding_priority_signal(
    snapshot: MarketSnapshot,
    signal: Signal,
    strategy: StrategyConfig,
    portfolio: PaperPortfolio,
    memory: TradingMemory | None = None,
    risk: RiskConfig | None = None,
    supervisor_hints: dict[str, dict[str, Any]] | None = None,
    trade_learning: dict[str, Any] | None = None,
) -> Signal:
    """Upgrade HOLD/BUY to reduce/take-profit when an open futures position needs risk management."""
    ma = _ma()
    BUY, HOLD, SELL = ma.BUY, ma.HOLD, ma.SELL
    SignalCls = ma.Signal
    normalize_symbol = ma.normalize_symbol
    decimal_from = ma.decimal_from
    is_futures_market = ma.is_futures_market
    position_exit_tiers_taken = ma.position_exit_tiers_taken
    position_age_seconds = ma.position_age_seconds

    signal = apply_supervisor_hint_bias(snapshot.asset.symbol, signal, supervisor_hints)
    symbol = normalize_symbol(snapshot.asset.symbol)
    position = portfolio.positions.get(symbol)
    if position is None or position.quantity == 0:
        if signal.action not in {HOLD, BUY}:
            return signal
        return signal

    # Open book: always evaluate exits. Bearish SELL on an existing short is not a cover —
    # treat as HOLD so underwater / force-reduce logic can emit BUY.
    if position.quantity < 0 and signal.action == SELL:
        signal = replace(signal, action=HOLD)
    elif position.quantity > 0 and signal.action == BUY:
        signal = replace(signal, action=HOLD)
    elif signal.action not in {HOLD, BUY, SELL}:
        return signal

    if not is_futures_market(snapshot.asset.market):
        return signal
    price = snapshot.price
    if position.average_price <= 0 or price <= 0:
        return signal

    hint = (supervisor_hints or {}).get(symbol)
    supervisor_reduce = hint and str(hint.get("action")) == "REDUCE_POSITION"
    supervisor_reassess = hint and str(hint.get("action")) == "REASSESS_TAKE_PROFIT_OR_HOLD"

    regime = signal.indicators.get("regime", "")
    momentum = decimal_from(signal.indicators.get("momentum", "0") or "0")
    mtf_5m = str(signal.indicators.get("mtf_5m", "") or "")
    loss_pct, take_pct, trail_pct, giveback_pct = holding_thresholds_for_regime(strategy, regime)
    early_take_pct = strategy.holding_early_take_pct
    if strategy.trade_horizon == "swing":
        early_take_pct *= Decimal("1.5")
    if supervisor_reassess:
        take_pct *= Decimal("0.90")
        early_take_pct *= Decimal("0.90")
    if mtf_5m_accelerates_profit_exit(position_qty=position.quantity, mtf_5m=mtf_5m):
        early_take_pct *= Decimal("0.85")
    open_ctx = memory.position_open_context.get(symbol) if memory is not None else None
    exit_mult = exit_quality_threshold_mult(trade_learning, open_ctx)
    if exit_mult != Decimal("1"):
        early_take_pct *= exit_mult
        take_pct *= exit_mult
        trail_pct *= exit_mult
    tiers_taken = position_exit_tiers_taken(memory, symbol)

    if memory is not None and strategy.holding_max_hours > 0:
        age = position_age_seconds(memory, symbol, snapshot.observed_at)
        if age is not None and age >= strategy.holding_max_hours * 3600:
            action = SELL if position.quantity > 0 else BUY
            return SignalCls(
                action=action,
                confidence=max(signal.confidence, strategy.min_sell_confidence),
                reasons=list(signal.reasons)
                + [f"Holding priority: position age {age // 3600}h exceeds max {strategy.holding_max_hours}h; exit review."],
                warnings=list(signal.warnings),
                indicators=dict(signal.indicators),
            )

    def reduce_signal(action: str, reason: str, *, exit_tier: str = "") -> Signal:
        indicators = dict(signal.indicators)
        if exit_tier:
            indicators["exit_tier"] = exit_tier
        return SignalCls(
            action=action,
            confidence=max(signal.confidence, strategy.min_sell_confidence),
            reasons=list(signal.reasons) + [reason],
            warnings=list(signal.warnings),
            indicators=indicators,
        )

    force_loss_pct = risk.max_position_loss_pct if risk is not None else Decimal("0")

    if supervisor_reduce and signal.action == HOLD:
        action = SELL if position.quantity > 0 else BUY
        return reduce_signal(
            action,
            f"Supervisor REDUCE_POSITION ({hint.get('priority', 'medium')}): {hint.get('reason', 'risk review')}.",
            exit_tier="supervisor",
        )

    if position.quantity > 0:
        pnl_pct = (price - position.average_price) / position.average_price
        if force_loss_pct > 0 and pnl_pct <= -force_loss_pct:
            return reduce_signal(
                SELL,
                f"Exit engine: long deep loss {pnl_pct:.2%} <= -{force_loss_pct:.2%}; "
                f"force reduce 50% (protection orders may be invalid).",
                exit_tier="force",
            )
        adverse = momentum < 0 and pnl_pct < 0
        if adverse:
            loss_pct *= strategy.holding_adverse_momentum_cut_mult
        if pnl_pct <= -loss_pct:
            deep_cut = loss_pct * Decimal("1.5")
            if pnl_pct <= -deep_cut:
                return reduce_signal(
                    SELL,
                    f"Holding priority: long deep loss {pnl_pct:.2%} <= -{deep_cut:.2%}; "
                    f"cut without 5m confirm.",
                )
            if (
                strategy.holding_require_5m_exit_confirm
                and not mtf_5m_confirms_loss_exit(position_qty=position.quantity, mtf_5m=mtf_5m)
                and pnl_pct > -(force_loss_pct or Decimal("1"))
            ):
                pass
            else:
                return reduce_signal(
                    SELL,
                    f"Holding priority: long underwater {pnl_pct:.2%} "
                    f"(entry {position.average_price}, mark {price}); cut loss.",
                )
        if (
            pnl_pct >= early_take_pct
            and "early" not in tiers_taken
            and pnl_pct < take_pct
        ):
            return reduce_signal(
                SELL,
                f"Holding priority: long skim +{pnl_pct:.2%} >= early {early_take_pct:.2%}; "
                f"lock partial profit, let runner continue.",
                exit_tier="early",
            )
        if pnl_pct >= take_pct and "standard" not in tiers_taken:
            if (
                strategy.holding_require_5m_exit_confirm
                and regime == "trend_up"
                and mtf_5m == "bullish"
                and not supervisor_reassess
            ):
                pass
            else:
                return reduce_signal(
                    SELL,
                    f"Holding priority: long profit {pnl_pct:.2%} reached take-profit "
                    f"{take_pct:.2%}; partial exit.",
                    exit_tier="standard",
                )
        if memory is not None and giveback_pct > 0:
            peak_raw = memory.position_peak_price.get(position_peak_key(symbol, position.quantity))
            if peak_raw:
                giveback_reason = peak_giveback_exit_reason(
                    position_qty=position.quantity,
                    entry=position.average_price,
                    price=price,
                    peak_price=decimal_from(peak_raw),
                    trail_activate_pct=trail_pct,
                    giveback_pct=giveback_pct,
                )
                if giveback_reason:
                    return reduce_signal(SELL, giveback_reason)
        if (
            trail_pct > 0
            and pnl_pct >= trail_pct
            and (regime == "trend_down" or "downtrend_regime" in signal.warnings)
        ):
            return reduce_signal(
                SELL,
                f"Holding priority: trailing exit on long (+{pnl_pct:.2%}) as trend turned down.",
            )
        if pnl_pct >= early_take_pct and regime == "trend_down" and momentum < 0:
            return reduce_signal(
                SELL,
                f"Holding priority: long +{pnl_pct:.2%} but trend_down + negative momentum; protect profit.",
            )
        return signal

    if position.quantity < 0:
        pnl_pct = (position.average_price - price) / position.average_price
        if force_loss_pct > 0 and pnl_pct <= -force_loss_pct:
            return reduce_signal(
                BUY,
                f"Exit engine: short deep loss {pnl_pct:.2%} <= -{force_loss_pct:.2%}; "
                f"force cover 50% (protection orders may be invalid).",
                exit_tier="force",
            )
        adverse = momentum > 0 and pnl_pct < 0
        if adverse:
            loss_pct *= strategy.holding_adverse_momentum_cut_mult
        if pnl_pct <= -loss_pct:
            deep_cut = loss_pct * Decimal("1.5")
            if pnl_pct <= -deep_cut:
                return reduce_signal(
                    BUY,
                    f"Holding priority: short deep loss {pnl_pct:.2%} <= -{deep_cut:.2%}; "
                    f"cover without 5m confirm.",
                )
            if (
                strategy.holding_require_5m_exit_confirm
                and not mtf_5m_confirms_loss_exit(position_qty=position.quantity, mtf_5m=mtf_5m)
                and pnl_pct > -(force_loss_pct or Decimal("1"))
            ):
                pass
            else:
                return reduce_signal(
                    BUY,
                    f"Holding priority: short underwater {pnl_pct:.2%} "
                    f"(entry {position.average_price}, mark {price}); cut loss.",
                )
        if (
            pnl_pct >= early_take_pct
            and "early" not in tiers_taken
            and pnl_pct < take_pct
        ):
            return reduce_signal(
                BUY,
                f"Holding priority: short skim +{pnl_pct:.2%} >= early {early_take_pct:.2%}; "
                f"lock partial profit.",
                exit_tier="early",
            )
        if pnl_pct >= take_pct and "standard" not in tiers_taken:
            if (
                strategy.holding_require_5m_exit_confirm
                and regime == "trend_down"
                and mtf_5m == "bearish"
                and not supervisor_reassess
            ):
                pass
            else:
                return reduce_signal(
                    BUY,
                    f"Holding priority: short profit {pnl_pct:.2%} reached take-profit "
                    f"{take_pct:.2%}; partial cover.",
                    exit_tier="standard",
                )
        if memory is not None and giveback_pct > 0:
            peak_raw = memory.position_peak_price.get(position_peak_key(symbol, position.quantity))
            if peak_raw:
                giveback_reason = peak_giveback_exit_reason(
                    position_qty=position.quantity,
                    entry=position.average_price,
                    price=price,
                    peak_price=decimal_from(peak_raw),
                    trail_activate_pct=trail_pct,
                    giveback_pct=giveback_pct,
                )
                if giveback_reason:
                    return reduce_signal(BUY, giveback_reason)
    return signal

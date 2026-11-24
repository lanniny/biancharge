"""Risk-bounded contract leverage for paper simulation.

Contract leverage is the isolated-margin multiplier. Effective leverage is
notional divided by equity. The planner uses the smallest leverage that can
fund a fixed risk budget. It never raises size to reach a leverage target.
A stop that would sit beyond the estimated liquidation price is rejected or
cut down. The maintenance schedule is an approximation, not Binance's live
bracket table: that endpoint returned HTTP 451 here and was not proxied.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class MaintenanceModel:
    rate: Decimal = Decimal("0.004")
    liquidation_fee_rate: Decimal = Decimal("0.005")
    source: str = "approximate_first_bracket_not_exchange_exact"
    exchange_schedule_verified: bool = False


@dataclass(frozen=True)
class LeveragePlan:
    accepted: bool
    contract_leverage: int
    qty: Decimal
    notional: Decimal
    initial_margin: Decimal
    effective_leverage: Decimal
    stop_distance: Decimal
    liquidation_distance: Decimal
    required_buffer: Decimal
    risk_budget: Decimal
    risk_at_stop: Decimal
    reason: str


def _move_to_exhaust_initial_margin(leverage: int) -> Decimal:
    """Price move that consumes initial margin, ignoring maintenance and fees."""
    if leverage < 1:
        raise ValueError("leverage must be >= 1")
    return Decimal(1) / Decimal(leverage)


def liquidation_distance(
    *,
    side: str,
    leverage: int,
    model: MaintenanceModel,
) -> Decimal:
    """Fraction from entry to the estimated isolated liquidation price.

    This is not the exchange's tiered bracket. At 50x with a 0.4% maintenance
    rate and a 0.5% liquidation fee, the distance is about 1.1%, inside the
    2% initial-margin move.
    """
    if leverage < 1:
        raise ValueError("leverage must be >= 1")
    if model.rate < 0 or model.liquidation_fee_rate < 0:
        raise ValueError("rates must be non-negative")
    if model.rate + model.liquidation_fee_rate >= 1:
        raise ValueError("maintenance plus liquidation fee must be < 1")
    margin_fraction = Decimal(1) / Decimal(leverage)
    if side == "long":
        price_ratio = (Decimal(1) - margin_fraction) / (Decimal(1) - model.rate - model.liquidation_fee_rate)
        return Decimal(1) - price_ratio
    if side == "short":
        price_ratio = (Decimal(1) + margin_fraction) / (Decimal(1) + model.rate + model.liquidation_fee_rate)
        return price_ratio - Decimal(1)
    raise ValueError(f"unknown side {side}")


def plan_leverage(
    *,
    equity: Decimal,
    cash: Decimal,
    price: Decimal,
    side: str,
    stop_distance: Decimal,
    risk_fraction: Decimal,
    model: MaintenanceModel,
    fee_rate: Decimal,
    slippage_rate: Decimal,
    extra_buffer: Decimal,
    max_leverage: int = 50,
    min_leverage: int = 1,
) -> LeveragePlan:
    """Choose the smallest contract leverage that funds a fixed loss budget.

    ``stop_distance`` and ``extra_buffer`` are fractions of price. The required
    cushion is the stop plus fees, slippage, and the extra buffer. If the
    risk-sized notional cannot fit inside that cushion at any allowed
    leverage, size is cut. It is not increased. If even one unit would be
    liquidated before the stop, the trade is rejected.
    """
    if equity <= 0 or cash <= 0 or price <= 0:
        return _reject("non_positive_equity_or_price", stop_distance, Decimal("0"), Decimal("0"))
    if not min_leverage <= max_leverage <= 50 or min_leverage < 1:
        raise ValueError("leverage bounds must sit inside 1..50")
    if stop_distance <= 0 or risk_fraction <= 0:
        return _reject("stop_or_risk_not_positive", stop_distance, Decimal("0"), Decimal("0"))
    required = stop_distance + (fee_rate * 2) + slippage_rate + extra_buffer
    risk_budget = equity * risk_fraction
    per_unit = price * stop_distance
    if per_unit <= 0:
        return _reject("stop_distance_invalid", stop_distance, required, risk_budget)
    target_qty = risk_budget / per_unit
    target_notional = target_qty * price

    def attempt(notional: Decimal) -> LeveragePlan | None:
        if notional <= 0:
            return None
        for leverage in range(min_leverage, max_leverage + 1):
            distance = liquidation_distance(side=side, leverage=leverage, model=model)
            if distance <= required:
                # Higher leverage only moves liquidation closer. Stop searching.
                break
            margin = notional / Decimal(leverage)
            fee = notional * fee_rate
            if margin + fee <= cash:
                qty = notional / price
                return LeveragePlan(
                    accepted=True,
                    contract_leverage=leverage,
                    qty=qty,
                    notional=notional,
                    initial_margin=margin,
                    effective_leverage=notional / equity,
                    stop_distance=stop_distance,
                    liquidation_distance=distance,
                    required_buffer=required,
                    risk_budget=risk_budget,
                    risk_at_stop=notional * stop_distance,
                    reason="smallest_leverage_that_funds_risk_budget"
                    if notional == target_notional
                    else "notional_cut_to_keep_liquidation_outside_stop",
                )
        return None

    planned = attempt(target_notional)
    if planned is not None:
        return planned
    # Cut notional. The safest room is at the lowest leverage whose liquidation
    # distance still clears the stop. Never step leverage up to manufacture size.
    for leverage in range(min_leverage, max_leverage + 1):
        distance = liquidation_distance(side=side, leverage=leverage, model=model)
        if distance <= required:
            break
        fee_rate_cash = fee_rate
        # margin + fee = notional/L + notional*fee <= cash
        unit = (Decimal(1) / Decimal(leverage)) + fee_rate_cash
        if unit <= 0:
            continue
        affordable = cash / unit
        safe_loss = affordable * stop_distance
        notional = min(target_notional, affordable, (risk_budget / stop_distance) if stop_distance else affordable)
        # notional * stop_distance is the planned loss; keep it <= risk budget
        notional = min(notional, risk_budget / stop_distance)
        if safe_loss <= 0:
            continue
        planned = attempt(notional)
        if planned is not None:
            return planned
    return _reject("liquidation_inside_stop_at_every_allowed_leverage", stop_distance, required, risk_budget)


def _reject(reason: str, stop_distance: Decimal, required: Decimal, risk_budget: Decimal) -> LeveragePlan:
    return LeveragePlan(
        accepted=False,
        contract_leverage=1,
        qty=Decimal("0"),
        notional=Decimal("0"),
        initial_margin=Decimal("0"),
        effective_leverage=Decimal("0"),
        stop_distance=stop_distance,
        liquidation_distance=Decimal("0"),
        required_buffer=required,
        risk_budget=risk_budget,
        risk_at_stop=Decimal("0"),
        reason=reason,
    )

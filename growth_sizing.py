"""Equity-scaled position sizing and growth-path metrics for the autotrader."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from market_autotrader import PaperPortfolio, RiskConfig, StrategyConfig


def normalize_symbol(symbol: str) -> str:
    return symbol.upper().replace("/", "")


def mark_prices_from_portfolio(portfolio: PaperPortfolio, symbol: str, price: Decimal) -> dict[str, Decimal]:
    marks: dict[str, Decimal] = {}
    for sym, position in portfolio.positions.items():
        if position.quantity != 0 and position.average_price > 0:
            marks[sym] = position.average_price
    marks[normalize_symbol(symbol)] = price
    return marks


def portfolio_equity(portfolio: PaperPortfolio, mark_prices: dict[str, Decimal]) -> Decimal:
    if portfolio.wallet_balance is not None and portfolio.wallet_balance > 0:
        return portfolio.wallet_balance.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    equity = sum(portfolio.cash.values(), Decimal("0"))
    for symbol, position in portfolio.positions.items():
        if position.quantity == 0:
            continue
        if position.initial_margin > 0:
            equity += position.initial_margin
            continue
        mark = mark_prices.get(symbol, position.average_price)
        notional = abs(position.quantity) * mark
        if position.leverage > 0:
            equity += (notional / Decimal(position.leverage)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        else:
            equity += notional
    return equity.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)


def portfolio_heat(portfolio: PaperPortfolio, mark_prices: dict[str, Decimal]) -> Decimal:
    equity = portfolio_equity(portfolio, mark_prices)
    if equity <= 0:
        return Decimal("0")
    exposure = Decimal("0")
    for symbol, position in portfolio.positions.items():
        if position.quantity == 0:
            continue
        if position.initial_margin > 0:
            exposure += position.initial_margin
            continue
        mark = mark_prices.get(symbol, position.average_price)
        notional = abs(position.quantity) * mark
        if position.leverage > 0:
            exposure += notional / Decimal(position.leverage)
        else:
            exposure += notional
    return (exposure / equity).quantize(Decimal("0.0001"))


def effective_max_daily_loss(risk: RiskConfig, equity: Decimal) -> Decimal:
    fixed = risk.max_daily_loss_quote if risk.max_daily_loss_quote > 0 else Decimal("0")
    pct_loss = Decimal("0")
    if risk.max_daily_loss_pct > 0 and equity > 0:
        pct_loss = (equity * risk.max_daily_loss_pct).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)

    if risk.scale_sizing_with_equity:
        candidate = pct_loss
        if fixed > 0:
            # Small accounts: at least 25% of max cap so one bad hour does not halt all day.
            candidate = max(candidate, (fixed * Decimal("0.25")).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN))
        if risk.min_daily_loss_cap_quote > 0:
            candidate = max(candidate, risk.min_daily_loss_cap_quote)
        if fixed > 0:
            return min(candidate, fixed)
        return candidate

    if fixed > 0:
        return fixed
    return pct_loss


def growth_target_metrics(equity: Decimal, target: Decimal) -> dict[str, str]:
    if target <= 0:
        return {
            "targetQuote": str(target),
            "equityQuote": str(equity),
            "progressPct": "0",
            "remainingQuote": str(target),
        }
    progress = min(equity / target, Decimal("1"))
    return {
        "targetQuote": str(target.quantize(Decimal("0.01"))),
        "equityQuote": str(equity.quantize(Decimal("0.01"))),
        "progressPct": str(progress.quantize(Decimal("0.0001"))),
        "remainingQuote": str(max(target - equity, Decimal("0")).quantize(Decimal("0.01"))),
    }


def confidence_size_multiplier(confidence: Decimal, floor: Decimal) -> Decimal:
    if confidence <= floor:
        return Decimal("0.75")
    span = Decimal("1") - floor
    if span <= 0:
        return Decimal("1")
    normalized = min((confidence - floor) / span, Decimal("1"))
    return (Decimal("0.75") + normalized * Decimal("0.50")).quantize(Decimal("0.0001"))


def margin_required_for_notional(notional: Decimal, leverage: int) -> Decimal:
    """Initial margin needed for a given notional at cross/isolated leverage."""
    if notional <= 0:
        return Decimal("0")
    lev = max(int(leverage), 1)
    return (notional / Decimal(lev)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)


def compute_open_notional(
    risk: RiskConfig,
    strategy: StrategyConfig,
    *,
    equity: Decimal,
    available_margin: Decimal,
    signal_confidence: Decimal,
    current_position_value: Decimal,
    leverage: int = 1,
    min_margin_per_trade: Decimal | None = None,
) -> Decimal:
    """Return target **order notional** (USDT), not margin.

    ``available_margin`` is collateral (wallet available). With leverage, notional
    can exceed available — e.g. 48 USDT margin @ 5x supports ~240 USDT notional.
    ``min_trade_quote`` is the minimum **notional** (exchange min), not margin.
    """
    lev = max(int(leverage), 1)
    margin_budget = (available_margin * strategy.buy_quote_fraction).quantize(
        Decimal("0.00000001"), rounding=ROUND_DOWN
    )
    notional_from_margin = (margin_budget * Decimal(lev)).quantize(
        Decimal("0.00000001"), rounding=ROUND_DOWN
    )

    if risk.scale_sizing_with_equity and risk.risk_per_trade_pct > 0 and equity > 0:
        equity_notional_cap = (equity * risk.risk_per_trade_pct).quantize(
            Decimal("0.00000001"), rounding=ROUND_DOWN
        )
        base = min(equity_notional_cap, notional_from_margin, risk.max_trade_quote)
    else:
        base = min(notional_from_margin, risk.max_trade_quote)

    if strategy.confidence_scale_sizing and signal_confidence > 0:
        floor = min(strategy.min_buy_confidence, strategy.min_sell_confidence)
        base = (base * confidence_size_multiplier(signal_confidence, floor)).quantize(
            Decimal("0.00000001"), rounding=ROUND_DOWN
        )

    min_notional = risk.min_trade_quote
    if min_notional > 0 and base < min_notional:
        margin_for_min = margin_required_for_notional(min_notional, lev)
        if margin_budget >= margin_for_min:
            base = min_notional
        else:
            base = Decimal("0")

    room = max(risk.max_position_quote - current_position_value, Decimal("0"))
    result = max(Decimal("0"), min(base, room)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    floor_margin = min_margin_per_trade if min_margin_per_trade is not None else risk.min_margin_per_trade
    if floor_margin > 0 and result > 0:
        min_notional_for_margin = (floor_margin * Decimal(lev)).quantize(
            Decimal("0.00000001"), rounding=ROUND_DOWN
        )
        if result < min_notional_for_margin and margin_budget >= floor_margin:
            result = min(min_notional_for_margin, risk.max_trade_quote, room)
    return result


def growth_rationale_block(
    portfolio: PaperPortfolio,
    risk: RiskConfig,
    symbol: str,
    price: Decimal,
    memory: Any | None = None,
) -> dict[str, Any]:
    marks = mark_prices_from_portfolio(portfolio, symbol, price)
    equity = portfolio_equity(portfolio, marks)
    heat = portfolio_heat(portfolio, marks)
    block: dict[str, Any] = {
        "equityQuote": str(equity),
        "portfolioHeat": str(heat),
        "maxPortfolioHeat": str(risk.max_portfolio_heat_pct),
        "target": growth_target_metrics(equity, risk.target_equity_quote),
        "effectiveMaxDailyLoss": str(effective_max_daily_loss(risk, equity)),
    }
    pos = portfolio.positions.get(normalize_symbol(symbol))
    if pos and pos.quantity != 0:
        block["position"] = {
            "notional": str(pos.notional.normalize()) if pos.notional > 0 else str((abs(pos.quantity) * price).quantize(Decimal("0.01"))),
            "initialMargin": str(pos.initial_margin.normalize()) if pos.initial_margin > 0 else None,
            "leverage": pos.leverage or None,
        }
    if memory is not None:
        block["dailyDrag"] = str(memory.total_daily_drag_today())
    return block

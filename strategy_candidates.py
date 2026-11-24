"""Predeclared paper signals. Parameters are not fitted on the 2024-2026 window.

``baseline_sma_momentum`` is the daily MA 20/50 track already inspected.
``atr_trend_breakout_4h`` is a separate 4-hour candidate: a close above the
prior 20-bar high, only when the previous completed UTC day is above its
50-day average, with a stop 2.5 ATR(14) away. It is not a scalp and it is
not an optimized search.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from research_backtest import StrategySpec, desired_side


def baseline_side(closes: list[Decimal]) -> str | None:
    strategy = StrategySpec(
        name="baseline_sma_momentum",
        fast_window=20,
        slow_window=50,
        momentum_window=20,
        momentum_threshold=Decimal("0"),
        stop_pct=Decimal("0.08"),
    )
    return desired_side(closes, strategy, allow_short=False)


def true_range(high: Decimal, low: Decimal, previous_close: Decimal | None) -> Decimal:
    span = high - low
    if previous_close is None:
        return span
    return max(span, abs(high - previous_close), abs(low - previous_close))


def atr_at(highs: list[Decimal], lows: list[Decimal], closes: list[Decimal], index: int, window: int = 14) -> Decimal | None:
    """ATR ending at ``index`` using only bars up to that index."""
    if index + 1 < window or index >= len(closes):
        return None
    total = Decimal("0")
    for cursor in range(index - window + 1, index + 1):
        previous = closes[cursor - 1] if cursor else None
        total += true_range(highs[cursor], lows[cursor], previous)
    return total / Decimal(window)


def daily_trend_up(daily_closes_before_today: list[Decimal], window: int = 50) -> bool:
    if len(daily_closes_before_today) < window:
        return False
    sample = daily_closes_before_today[-window:]
    average = sum(sample, Decimal("0")) / Decimal(window)
    return daily_closes_before_today[-1] > average


def atr_breakout_decision(
    highs: list[Decimal],
    lows: list[Decimal],
    closes: list[Decimal],
    index: int,
    *,
    breakout_window: int = 20,
    atr_window: int = 14,
    atr_multiple: Decimal = Decimal("2.5"),
    daily_closes_before_today: list[Decimal] | None = None,
) -> tuple[str | None, Decimal | None]:
    """Return (side, stop fraction) from completed bars through ``index`` only.

    ``side`` is ``long``, ``flat``, ``hold``, or None when the window is short.
    ``hold`` means the open position is left unchanged.
    """
    if index < 0 or index >= len(closes):
        return None, None
    if index < max(breakout_window, atr_window):
        return None, None
    volatility = atr_at(highs, lows, closes, index, atr_window)
    if volatility is None or volatility <= 0 or closes[index] <= 0:
        return None, None
    stop_fraction = (volatility * atr_multiple) / closes[index]
    prior_high = max(highs[index - breakout_window : index])
    slow = sum(closes[index - breakout_window + 1 : index + 1], Decimal("0")) / Decimal(breakout_window)
    trend_ok = daily_trend_up(daily_closes_before_today or [])
    if closes[index] > prior_high and trend_ok:
        return "long", stop_fraction
    if closes[index] < slow:
        return "flat", stop_fraction
    return "hold", stop_fraction



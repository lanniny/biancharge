"""RSRS (阻力支撑相对强度) — OLS high~low slope with R² weighting."""

from __future__ import annotations

from decimal import Decimal

from market_autotrader import MarketBar, decimal_from


def ols_slope_r2(xs: list[Decimal], ys: list[Decimal]) -> tuple[Decimal, Decimal]:
    if len(xs) < 3 or len(xs) != len(ys):
        return Decimal("0"), Decimal("0")
    n = Decimal(len(xs))
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return Decimal("0"), Decimal("0")
    sxy = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(len(xs)))
    slope = sxy / sxx
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    intercept = mean_y - slope * mean_x
    ss_res = sum((ys[i] - (slope * xs[i] + intercept)) ** 2 for i in range(len(xs)))
    r2 = Decimal("1") - (ss_res / ss_tot) if ss_tot > 0 else Decimal("0")
    r2 = max(Decimal("0"), min(r2, Decimal("1")))
    return slope, r2


def rsrs_score(bars: list[MarketBar], window: int = 18) -> Decimal:
    """Higher = stronger support/resistance trend alignment (Qbot-style RSRS lite)."""
    if len(bars) < window + 5:
        return Decimal("0")
    lows = [bar.low for bar in bars]
    highs = [bar.high for bar in bars]
    slopes: list[Decimal] = []
    for end in range(window, len(bars)):
        segment_low = lows[end - window : end]
        segment_high = highs[end - window : end]
        slope, _ = ols_slope_r2(segment_low, segment_high)
        slopes.append(slope)
    if not slopes:
        return Decimal("0")
    current = slopes[-1]
    mean = sum(slopes) / Decimal(len(slopes))
    variance = sum((s - mean) ** 2 for s in slopes) / Decimal(len(slopes))
    if variance <= 0:
        z = Decimal("0")
    else:
        import math

        std = decimal_from(str(math.sqrt(float(variance))))
        z = (current - mean) / std if std > 0 else Decimal("0")
    _, r2 = ols_slope_r2(lows[-window:], highs[-window:])
    return (z * r2).quantize(Decimal("0.0001"))

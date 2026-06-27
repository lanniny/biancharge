"""Pure-Python candlestick pattern + structure analysis (Stage 5).

No TA-Lib / numpy / pandas dependency (the VPS has minimal libs and TA-Lib needs
a C build). Operates on any sequence of bar-like objects exposing Decimal
attributes open / high / low / close. Each detector returns a signed strength in
[-1, 1] (positive = bullish), and detect_patterns() returns a compact summary
suitable for injecting into the signal indicators and the persona council.

These are classic reversal/continuation candles plus a light swing-based
support/resistance read. They are deliberately conservative: detectors return 0
when the geometry is ambiguous, so they add signal only when a pattern is clear.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Sequence

ZERO = Decimal("0")


def _body(bar: Any) -> Decimal:
    return abs(bar.close - bar.open)


def _range(bar: Any) -> Decimal:
    return bar.high - bar.low


def _upper_wick(bar: Any) -> Decimal:
    return bar.high - max(bar.open, bar.close)


def _lower_wick(bar: Any) -> Decimal:
    return min(bar.open, bar.close) - bar.low


def _is_bull(bar: Any) -> bool:
    return bar.close > bar.open


def _is_bear(bar: Any) -> bool:
    return bar.close < bar.open


def doji(bar: Any) -> Decimal:
    """Indecision: tiny body relative to range. Neutral-strength marker."""
    rng = _range(bar)
    if rng <= 0:
        return ZERO
    if _body(bar) <= rng * Decimal("0.1"):
        return Decimal("0.1")  # weak/neutral
    return ZERO


def hammer(bar: Any) -> Decimal:
    """Bullish reversal: small body up top, long lower wick."""
    rng = _range(bar)
    body = _body(bar)
    if rng <= 0 or body <= 0:
        return ZERO
    if _lower_wick(bar) >= body * Decimal("2") and _upper_wick(bar) <= body:
        return Decimal("0.6")
    return ZERO


def shooting_star(bar: Any) -> Decimal:
    """Bearish reversal: small body down low, long upper wick."""
    rng = _range(bar)
    body = _body(bar)
    if rng <= 0 or body <= 0:
        return ZERO
    if _upper_wick(bar) >= body * Decimal("2") and _lower_wick(bar) <= body:
        return Decimal("-0.6")
    return ZERO


def marubozu(bar: Any) -> Decimal:
    """Strong conviction candle: body fills nearly the whole range."""
    rng = _range(bar)
    if rng <= 0:
        return ZERO
    if _body(bar) >= rng * Decimal("0.9"):
        return Decimal("0.5") if _is_bull(bar) else Decimal("-0.5")
    return ZERO


def engulfing(prev: Any, cur: Any) -> Decimal:
    """Two-candle reversal: current body engulfs the prior opposite body."""
    if _is_bear(prev) and _is_bull(cur) and cur.close >= prev.open and cur.open <= prev.close:
        return Decimal("0.7")  # bullish engulfing
    if _is_bull(prev) and _is_bear(cur) and cur.open >= prev.close and cur.close <= prev.open:
        return Decimal("-0.7")  # bearish engulfing
    return ZERO


def three_bar_momentum(bars: Sequence[Any]) -> Decimal:
    """Three consecutive same-direction bodies (soldiers / crows)."""
    if len(bars) < 3:
        return ZERO
    a, b, c = bars[-3], bars[-2], bars[-1]
    if all(_is_bull(x) for x in (a, b, c)) and c.close > b.close > a.close:
        return Decimal("0.5")
    if all(_is_bear(x) for x in (a, b, c)) and c.close < b.close < a.close:
        return Decimal("-0.5")
    return ZERO


def swing_support_resistance(bars: Sequence[Any], lookback: int = 20) -> dict[str, Any]:
    """Light structure read: nearest swing high/low and proximity of price.

    Returns the recent swing high/low and how close the last close sits to each
    (as a fraction). Proximity to support is mildly bullish, to resistance mildly
    bearish — a place where reversals cluster.
    """
    window = list(bars[-lookback:]) if lookback > 0 else list(bars)
    if len(window) < 3:
        return {}
    highs = [b.high for b in window]
    lows = [b.low for b in window]
    swing_high = max(highs)
    swing_low = min(lows)
    last = window[-1].close
    span = swing_high - swing_low
    if span <= 0 or last <= 0:
        return {"swingHigh": str(swing_high), "swingLow": str(swing_low)}
    to_resistance = (swing_high - last) / last
    to_support = (last - swing_low) / last
    bias = ZERO
    # Within ~0.5% of a level is "near".
    near = Decimal("0.005")
    if to_support <= near:
        bias = Decimal("0.3")  # near support
    elif to_resistance <= near:
        bias = Decimal("-0.3")  # near resistance
    return {
        "swingHigh": str(swing_high),
        "swingLow": str(swing_low),
        "toResistancePct": str(to_resistance.quantize(Decimal("0.0001"))),
        "toSupportPct": str(to_support.quantize(Decimal("0.0001"))),
        "structureBias": str(bias),
    }


def detect_patterns(bars: Sequence[Any], *, lookback: int = 20) -> dict[str, Any]:
    """Run all detectors over the most recent bars; return a compact summary.

    `patternScore` is the net signed strength (clamped to [-1, 1]); `patterns`
    lists the named hits with their strengths; `structure` carries the S/R read.
    """
    if not bars:
        return {"patternScore": "0", "patterns": [], "structure": {}}
    cur = bars[-1]
    prev = bars[-2] if len(bars) >= 2 else None

    hits: list[dict[str, str]] = []
    score = ZERO

    def add(name: str, strength: Decimal) -> None:
        nonlocal score
        if strength != 0:
            hits.append({"name": name, "strength": str(strength)})
            score += strength

    add("hammer", hammer(cur))
    add("shooting_star", shooting_star(cur))
    add("marubozu", marubozu(cur))
    add("doji", doji(cur))
    if prev is not None:
        add("engulfing", engulfing(prev, cur))
    add("three_bar_momentum", three_bar_momentum(bars))

    structure = swing_support_resistance(bars, lookback=lookback)
    if structure.get("structureBias"):
        sb = Decimal(structure["structureBias"])
        if sb != 0:
            add("structure", sb)

    # Clamp net score.
    if score > 1:
        score = Decimal("1")
    elif score < -1:
        score = Decimal("-1")

    label = "neutral"
    if score >= Decimal("0.4"):
        label = "bullish"
    elif score <= Decimal("-0.4"):
        label = "bearish"

    return {
        "patternScore": str(score.quantize(Decimal("0.0001"))),
        "patternLabel": label,
        "patterns": hits,
        "structure": structure,
    }

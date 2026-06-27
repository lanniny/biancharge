"""Tests for Stage 5 candlestick pattern + structure analysis."""

import unittest
from decimal import Decimal

from kline_patterns import (
    detect_patterns,
    doji,
    engulfing,
    hammer,
    shooting_star,
    three_bar_momentum,
)


class Bar:
    def __init__(self, o, h, l, c, v="1000"):
        self.open = Decimal(str(o))
        self.high = Decimal(str(h))
        self.low = Decimal(str(l))
        self.close = Decimal(str(c))
        self.volume = Decimal(str(v))


class SingleCandleTests(unittest.TestCase):
    def test_hammer_is_bullish(self):
        # small body up top, long lower wick
        self.assertGreater(hammer(Bar(100, 101.2, 96, 101)), 0)

    def test_shooting_star_is_bearish(self):
        self.assertLess(shooting_star(Bar(101, 105, 99.8, 100)), 0)

    def test_doji_marks_indecision(self):
        self.assertGreater(doji(Bar(100, 102, 98, 100.05)), 0)

    def test_non_hammer_returns_zero(self):
        # Symmetric candle: body centered, wicks balanced -> not a hammer.
        self.assertEqual(hammer(Bar(100, 101, 99, 100)), Decimal("0"))


class MultiCandleTests(unittest.TestCase):
    def test_bullish_engulfing(self):
        prev = Bar(100, 100.5, 98, 98.5)  # bearish
        cur = Bar(98, 101.5, 97.5, 101)  # bullish engulfs
        self.assertGreater(engulfing(prev, cur), 0)

    def test_bearish_engulfing(self):
        prev = Bar(98, 101, 97.5, 101)  # bullish
        cur = Bar(101, 101.5, 97, 97.5)  # bearish engulfs
        self.assertLess(engulfing(prev, cur), 0)

    def test_three_white_soldiers(self):
        bars = [Bar(100, 101, 99, 100.5), Bar(100.5, 102, 100, 101.5), Bar(101.5, 103, 101, 102.5)]
        self.assertGreater(three_bar_momentum(bars), 0)

    def test_three_black_crows(self):
        bars = [Bar(103, 103, 101, 101.5), Bar(101.5, 101.5, 99, 99.5), Bar(99.5, 99.5, 97, 97.5)]
        self.assertLess(three_bar_momentum(bars), 0)


class DetectPatternsTests(unittest.TestCase):
    def test_summary_shape(self):
        bars = [Bar(100, 101, 99, 100) for _ in range(20)]
        r = detect_patterns(bars)
        self.assertIn("patternScore", r)
        self.assertIn("patternLabel", r)
        self.assertIn("patterns", r)
        self.assertIn("structure", r)

    def test_three_black_crows_labels_bearish(self):
        bars = [Bar(100, 101, 99, 100) for _ in range(17)]
        bars += [Bar(103, 103, 101, 101.5), Bar(101.5, 101.5, 99, 99.5), Bar(99.5, 99.5, 97, 97.5)]
        r = detect_patterns(bars)
        self.assertLess(Decimal(r["patternScore"]), 0)

    def test_empty_bars_safe(self):
        r = detect_patterns([])
        self.assertEqual(r["patternScore"], "0")

    def test_structure_reports_swing_levels(self):
        bars = [Bar(100 + i, 101 + i, 99 + i, 100 + i) for i in range(20)]
        r = detect_patterns(bars)
        self.assertIn("swingHigh", r["structure"])
        self.assertIn("swingLow", r["structure"])


if __name__ == "__main__":
    unittest.main()

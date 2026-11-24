"""Guards for the predeclared 4h long/short walk-forward."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from leverage_policy import MaintenanceModel
from research_swing_study import Candle, simulate_preset, walk_forward_folds
from strategy_candidates import ATR_BREAKOUT_GRID, ROUND2_GRIDS, SwingBook, preset_matches



class WalkForwardGuardTests(unittest.TestCase):
    def test_folds_stop_at_the_reserved_segment(self) -> None:
        folds = walk_forward_folds(date(2021, 1, 1), date(2026, 8, 1))
        self.assertEqual(folds[0], (date(2021, 1, 1), date(2021, 7, 1)))
        self.assertEqual(folds[-1][1], date(2026, 8, 1))
        self.assertTrue(all(end <= date(2026, 8, 1) and start < date(2026, 8, 1) for start, end in folds))

    def test_short_pnl_is_reported_separately_and_missing_funding_is_not_zero(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        candles = []
        price = Decimal("100")
        for index in range(8):
            price += Decimal("1")
            candles.append(
                Candle(
                    ts=start + timedelta(hours=4 * index),
                    open=price,
                    high=price + Decimal("0.2"),
                    low=price - Decimal("0.2"),
                    close=price,
                    mark_open=price,
                    mark_high=price + Decimal("0.2"),
                    mark_low=price - Decimal("0.2"),
                    mark_close=price,
                )
            )
        preset = dict(ATR_BREAKOUT_GRID[0])
        signals = [("short", Decimal("0.02"))] + [(None, None)] * (len(candles) - 1)
        result = simulate_preset(
            candles,
            [(candles[2].ts, Decimal("0.001"))],
            preset,
            trade_start=candles[0].ts,
            trade_end=candles[-1].ts + timedelta(hours=4),
            equity0=Decimal("10000"),
            model=MaintenanceModel(),
            fee_rate=Decimal("0.0005"),
            slippage=Decimal("0.0002"),
            extra_buffer=Decimal("0.001"),
            risk_fraction=Decimal("0.01"),
            signals=signals,
        )
        shorts = [trade for trade in result["trades"] if trade["side"] == "short"]
        longs = [trade for trade in result["trades"] if trade["side"] == "long"]
        self.assertGreaterEqual(len(shorts), 1)
        self.assertEqual(longs, [])
        self.assertEqual(result["missed_funding"], 0)
        self.assertNotEqual(result["funding_pnl"], Decimal("0"))

        missed = simulate_preset(
            candles,
            [(candles[2].ts, Decimal("0.001"))],
            preset,
            trade_start=candles[0].ts,
            trade_end=candles[-1].ts + timedelta(hours=4),
            equity0=Decimal("10000"),
            model=MaintenanceModel(),
            fee_rate=Decimal("0"),
            slippage=Decimal("0"),
            extra_buffer=Decimal("0"),
            risk_fraction=Decimal("0.01"),
            signals=signals,
        )
        # Drop the mark on the funding bar and confirm the cash identity does not invent a zero rate.
        bare = []
        for candle in candles:
            bare.append(
                Candle(
                    ts=candle.ts,
                    open=candle.open,
                    high=candle.high,
                    low=candle.low,
                    close=candle.close,
                    mark_open=None,
                    mark_high=None,
                    mark_low=None,
                    mark_close=None,
                )
            )
        unfunded = simulate_preset(
            bare,
            [(bare[2].ts, Decimal("0.001"))],
            preset,
            trade_start=bare[0].ts,
            trade_end=bare[-1].ts + timedelta(hours=4),
            equity0=Decimal("10000"),
            model=MaintenanceModel(),
            fee_rate=Decimal("0"),
            slippage=Decimal("0"),
            extra_buffer=Decimal("0"),
            risk_fraction=Decimal("0.01"),
            signals=signals,
        )
        self.assertGreater(unfunded["missed_funding"], 0)
        self.assertEqual(unfunded["funding_pnl"], Decimal("0"))
        self.assertNotEqual(missed["funding_pnl"], unfunded["funding_pnl"])


class Round2RuleTests(unittest.TestCase):
    def test_grid_is_six_long_short_presets_without_atr(self) -> None:
        presets = [preset for grid in ROUND2_GRIDS.values() for preset in grid]
        self.assertEqual(len(presets), 6)
        self.assertEqual(len(ROUND2_GRIDS), 3)
        for preset in presets:
            self.assertIs(preset["allow_short"], True)
            self.assertNotIn("atr_breakout", preset["name"])
            self.assertIn(preset["filter"], {"weekly_both", "daily_long_weekly_short"})
            self.assertEqual(preset["min_hold_bars"], 30)
            self.assertEqual(preset["confirm_bars"], 3)
            self.assertGreaterEqual(max(preset["lookbacks"]), 540)

    def test_short_is_blocked_until_the_weekly_trend_is_down(self) -> None:
        preset = ROUND2_GRIDS["donchian_slow_4h"][0]
        book = SwingBook(preset)
        book.completed_weekly = [Decimal(100 + index) for index in range(20)]
        sides = [book._finalize("short", Decimal("0.02"))[0] for _ in range(3)]
        self.assertEqual(sides[-1], "flat")
        self.assertNotIn("short", sides)
        book = SwingBook(preset)
        book.completed_weekly = [Decimal(200 - index) for index in range(20)]
        sides = [book._finalize("short", Decimal("0.02"))[0] for _ in range(3)]
        self.assertEqual(sides[-1], "short")

    def test_minimum_hold_delays_a_flat_exit(self) -> None:
        preset = ROUND2_GRIDS["tsmom_slow_4h"][1]
        book = SwingBook(preset)
        book.completed_weekly = [Decimal(200 - index) for index in range(20)]
        book.closes = [Decimal("1")] * 10
        book._held_side = "short"
        book._hold_until = 40
        for _ in range(3):
            side, _fraction = book._finalize("flat", Decimal("0.02"))
        self.assertEqual(side, "hold")
        book.closes = [Decimal("1")] * 41
        for _ in range(3):
            side, _fraction = book._finalize("flat", Decimal("0.02"))
        self.assertEqual(side, "flat")

    def test_downtrend_emits_short_and_uptrend_does_not(self) -> None:
        start = datetime(2021, 1, 4, tzinfo=timezone.utc)
        for preset in (item for grid in ROUND2_GRIDS.values() for item in grid):
            falling = SwingBook(preset)
            rising = SwingBook(preset)
            fall_sides = []
            rise_sides = []
            for index in range(1100):
                when = start + timedelta(hours=4 * index)
                down = Decimal("2000") - Decimal(index) * Decimal("0.4")
                up = Decimal("1000") + Decimal(index) * Decimal("0.4")
                fall_sides.append(falling.on_bar(when, down + Decimal("0.1"), down - Decimal("0.1"), down)[0])
                rise_sides.append(rising.on_bar(when, up + Decimal("0.1"), up - Decimal("0.1"), up)[0])
            self.assertIn("short", fall_sides, preset["name"] + " " + preset["filter"])
            self.assertNotIn("long", fall_sides, preset["name"] + " " + preset["filter"])
            self.assertIn("long", rise_sides, preset["name"] + " " + preset["filter"])
            self.assertNotIn("short", rise_sides, preset["name"] + " " + preset["filter"])

    def test_public_preset_still_matches_the_frozen_grid(self) -> None:
        from research_swing_study import _public_preset

        preset = ROUND2_GRIDS["combo_slow_4h"][1]
        self.assertTrue(preset_matches(preset, _public_preset(preset)))

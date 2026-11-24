"""Bounds and liquidation tests for the paper leverage planner."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from leverage_policy import MaintenanceModel, liquidation_distance, plan_leverage, _move_to_exhaust_initial_margin
from strategy_candidates import ATR_BREAKOUT_GRID, DONCHIAN_GRID, TSMOM_GRID, SwingBook, atr_at, atr_breakout_decision, baseline_side


MODEL = MaintenanceModel()


class LeveragePolicyTests(unittest.TestCase):
    def test_fifty_times_is_about_two_percent_before_maintenance(self) -> None:
        self.assertEqual(_move_to_exhaust_initial_margin(50), Decimal("0.02"))
        distance = liquidation_distance(side="long", leverage=50, model=MODEL)
        self.assertLess(distance, Decimal("0.02"))
        self.assertGreater(distance, Decimal("0.01"))

    def test_eight_percent_stop_stays_at_one_times(self) -> None:
        plan = plan_leverage(
            equity=Decimal("10000"),
            cash=Decimal("10000"),
            price=Decimal("100"),
            side="long",
            stop_distance=Decimal("0.08"),
            risk_fraction=Decimal("0.01"),
            model=MODEL,
            fee_rate=Decimal("0.0005"),
            slippage_rate=Decimal("0.0002"),
            extra_buffer=Decimal("0.001"),
        )
        self.assertTrue(plan.accepted)
        self.assertEqual(plan.contract_leverage, 1)
        self.assertLess(plan.effective_leverage, Decimal("1"))
        self.assertGreater(plan.liquidation_distance, plan.required_buffer)
        self.assertLessEqual(plan.risk_at_stop, plan.risk_budget + Decimal("0.0001"))

    def test_tight_stop_uses_smallest_leverage_not_fifty(self) -> None:
        plan = plan_leverage(
            equity=Decimal("10000"),
            cash=Decimal("10000"),
            price=Decimal("100"),
            side="short",
            stop_distance=Decimal("0.004"),
            risk_fraction=Decimal("0.02"),
            model=MODEL,
            fee_rate=Decimal("0.0005"),
            slippage_rate=Decimal("0.0002"),
            extra_buffer=Decimal("0.001"),
        )
        self.assertTrue(plan.accepted)
        self.assertEqual(plan.contract_leverage, 6)
        self.assertLess(plan.contract_leverage, 50)
        self.assertGreater(plan.liquidation_distance, plan.stop_distance)

    def test_stop_beyond_even_unlevered_room_is_rejected(self) -> None:
        plan = plan_leverage(
            equity=Decimal("10000"),
            cash=Decimal("10000"),
            price=Decimal("100"),
            side="long",
            stop_distance=Decimal("0.99"),
            risk_fraction=Decimal("0.01"),
            model=MODEL,
            fee_rate=Decimal("0"),
            slippage_rate=Decimal("0"),
            extra_buffer=Decimal("0.02"),
            max_leverage=50,
        )
        self.assertFalse(plan.accepted)
        self.assertEqual(plan.qty, 0)

    def test_oversized_risk_is_cut_instead_of_forcing_fifty(self) -> None:
        plan = plan_leverage(
            equity=Decimal("10000"),
            cash=Decimal("10000"),
            price=Decimal("100"),
            side="long",
            stop_distance=Decimal("0.08"),
            risk_fraction=Decimal("1"),
            model=MODEL,
            fee_rate=Decimal("0.0005"),
            slippage_rate=Decimal("0.0002"),
            extra_buffer=Decimal("0.001"),
        )
        self.assertTrue(plan.accepted)
        self.assertEqual(plan.contract_leverage, 1)
        self.assertLess(plan.risk_at_stop, plan.risk_budget)
        self.assertIn("cut", plan.reason)

    def test_bounds_reject_leverage_above_50(self) -> None:
        with self.assertRaises(ValueError):
            plan_leverage(
                equity=Decimal("10000"),
                cash=Decimal("10000"),
                price=Decimal("100"),
                side="long",
                stop_distance=Decimal("0.08"),
                risk_fraction=Decimal("0.01"),
                model=MODEL,
                fee_rate=Decimal("0"),
                slippage_rate=Decimal("0"),
                extra_buffer=Decimal("0"),
                max_leverage=51,
            )

    def test_short_and_long_liquidation_are_on_opposite_sides(self) -> None:
        long_distance = liquidation_distance(side="long", leverage=10, model=MODEL)
        short_distance = liquidation_distance(side="short", leverage=10, model=MODEL)
        self.assertGreater(long_distance, 0)
        self.assertGreater(short_distance, 0)
        self.assertLess(long_distance, _move_to_exhaust_initial_margin(10))


class SignalTests(unittest.TestCase):
    def test_breakout_does_not_look_at_the_next_bar(self) -> None:
        highs = [Decimal(100 + index) for index in range(30)]
        lows = [value - Decimal("1") for value in highs]
        closes = [value - Decimal("0.2") for value in highs]
        side, _stop = atr_breakout_decision(
            highs,
            lows,
            closes,
            25,
            daily_closes_before_today=[Decimal(50 + index) for index in range(60)],
        )
        highs[-1] = Decimal("1000000")
        closes[-1] = Decimal("1000000")
        later, _later_stop = atr_breakout_decision(
            highs,
            lows,
            closes,
            25,
            daily_closes_before_today=[Decimal(50 + index) for index in range(60)],
        )
        self.assertEqual(side, later)

    def test_baseline_side_needs_fifty_closes(self) -> None:
        self.assertIsNone(baseline_side([Decimal(100 + index) for index in range(40)]))

    def test_every_swing_preset_allows_shorts_and_can_signal_one(self) -> None:
        for grid in (ATR_BREAKOUT_GRID, TSMOM_GRID, DONCHIAN_GRID):
            for preset in grid:
                self.assertIs(preset["allow_short"], True)
                book = SwingBook(preset)
                start = datetime(2024, 1, 2, tzinfo=timezone.utc)
                last = None
                for index in range(180):
                    price = Decimal("200") - Decimal(index) * Decimal("0.5")
                    last = book.on_bar(start + timedelta(days=index), price + Decimal("0.2"), price - Decimal("0.2"), price)
                self.assertEqual(last[0], "short")

    def test_daily_baseline_stays_long_only(self) -> None:
        falling = [Decimal("200") - Decimal(index) for index in range(80)]
        self.assertNotEqual(baseline_side(falling), "short")

    def test_atr_uses_only_the_prefix(self) -> None:
        highs = [Decimal("10")] * 20
        lows = [Decimal("9")] * 20
        closes = [Decimal("9.5")] * 20
        early = atr_at(highs, lows, closes, 13)
        highs[-1] = Decimal("50")
        later = atr_at(highs, lows, closes, 13)
        self.assertEqual(early, later)


if __name__ == "__main__":
    unittest.main()

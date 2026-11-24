"""The alt-coin check must keep the BTC-selected rule and the reserved segment."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from research_alt_validation import ALT_SYMBOLS, FROZEN_PRESET, equal_weight_curve, run_alt_validation, validation_passes
from research_swing_study import Candle, HOLDOUT_START_DT
from strategy_candidates import ROUND2_GRIDS


def _row(symbol: str, net: str, stress: str, liquidations: int = 0, stress_liquidations: int = 0) -> dict:
    return {
        "symbol": symbol,
        "net_pnl": net,
        "stress_net_pnl": stress,
        "liquidations": liquidations,
        "stress_liquidations": stress_liquidations,
    }


class FrozenRuleTests(unittest.TestCase):
    def test_preset_is_the_btc_selected_asymmetric_rule(self) -> None:
        self.assertIs(FROZEN_PRESET, ROUND2_GRIDS["tsmom_slow_4h"][1])
        self.assertEqual(FROZEN_PRESET["filter"], "daily_long_weekly_short")
        self.assertIs(FROZEN_PRESET["allow_short"], True)
        self.assertEqual(FROZEN_PRESET["weekly_sma"], 20)
        self.assertEqual(FROZEN_PRESET["daily_sma"], 50)
        self.assertEqual(FROZEN_PRESET["confirm_bars"], 3)
        self.assertEqual(FROZEN_PRESET["min_hold_bars"], 30)
        self.assertEqual(FROZEN_PRESET["lookbacks"], (48, 180, 540))
        self.assertEqual(ALT_SYMBOLS, ("ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"))

    def test_three_profitable_coins_and_portfolio_sharpe_pass(self) -> None:
        rows = [
            _row("ETHUSDT", "10", "1"),
            _row("SOLUSDT", "10", "1"),
            _row("BNBUSDT", "10", "1"),
            _row("XRPUSDT", "-1", "-2"),
        ]
        decision = validation_passes(rows, Decimal("0.5"))
        self.assertTrue(decision["passed"])
        self.assertEqual(decision["positive_count"], 3)

    def test_two_coins_or_a_liquidation_or_low_sharpe_fail(self) -> None:
        rows = [
            _row("ETHUSDT", "10", "1"),
            _row("SOLUSDT", "10", "1"),
            _row("BNBUSDT", "-1", "1"),
            _row("XRPUSDT", "10", "-1"),
        ]
        self.assertFalse(validation_passes(rows, Decimal("1"))["passed"])
        liquidated = [
            _row("ETHUSDT", "10", "1"),
            _row("SOLUSDT", "10", "1", stress_liquidations=1),
            _row("BNBUSDT", "10", "1"),
            _row("XRPUSDT", "10", "1"),
        ]
        self.assertFalse(validation_passes(liquidated, Decimal("1"))["passed"])
        self.assertFalse(validation_passes(
            [_row(symbol, "10", "1") for symbol in ALT_SYMBOLS],
            Decimal("0.499"),
        )["passed"])

    def test_equal_weight_keeps_unstarted_cash(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        later = start + timedelta(hours=4)
        curve = equal_weight_curve(
            [[(start, Decimal("11000"))], [(later, Decimal("9000"))]],
            Decimal("10000"),
        )
        self.assertEqual(curve[0][1], Decimal("21000"))
        self.assertEqual(curve[1][1], Decimal("20000"))

    def test_failed_gate_does_not_score_the_reserved_segment(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        candles = []
        for index in range(8):
            price = Decimal("100")
            candles.append(
                Candle(
                    ts=start + timedelta(hours=4 * index),
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    mark_open=price,
                    mark_high=price,
                    mark_low=price,
                    mark_close=price,
                )
            )

        def loader(symbol: str, _start: date, end: date):
            self.assertLess(end, date(2026, 11, 1))
            return candles, [], []

        seen = []
        original = __import__("research_alt_validation").simulate_preset

        def guard(*args, **kwargs):
            seen.append(kwargs["trade_start"])
            if kwargs["trade_start"] >= HOLDOUT_START_DT:
                raise AssertionError("reserved segment was scored")
            return original(*args, **kwargs)

        module = __import__("research_alt_validation")
        module.simulate_preset = guard
        try:
            report = run_alt_validation(
                {
                    "sample_end": "2026-10-31",
                    "maintenance_rate": "0.004",
                    "liquidation_fee_rate": "0.005",
                    "maintenance_source": "approximate_first_bracket_not_exchange_exact",
                    "fee_bps": "5",
                    "slippage_bps": "2",
                    "starting_equity": "10000",
                    "risk_per_trade_pct": "0.01",
                    "extra_buffer": "0.001",
                    "stress_multiplier": "2",
                },
                loader=loader,
            )
        finally:
            module.simulate_preset = original
        self.assertFalse(report["decision"]["passed"])
        self.assertFalse(report["holdout_evaluated"])
        self.assertIsNone(report["holdout"])
        self.assertTrue(seen)
        self.assertTrue(all(item < HOLDOUT_START_DT for item in seen))

"""Guards for the predeclared 4h long/short walk-forward."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from leverage_policy import MaintenanceModel
from research_swing_study import Candle, simulate_preset, walk_forward_folds
from strategy_candidates import ATR_BREAKOUT_GRID


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

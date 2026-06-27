"""Tests for Stage 1 live-safety fixes.

Covers:
- F1: record_trade accepts is_open/is_add and increments the per-symbol add cap
- F1/STATE-03: daily counters use the UTC day-key on both read and write
- F2: realized losses from REALIZED_PNL income feed the daily-loss cap
- CRASH-02: state writes are atomic (atomic_write_json round-trips)
- F1-leverage: the sizing leverage is propagated onto the futures OrderIntent
"""

import json
import os
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

# Income rows are bucketed by their own UTC timestamp; the daily-loss cap reads
# today's UTC key. Use a current-day timestamp so the two line up in the test.
NOW_MS = int(time.time() * 1000)

import market_autotrader as ma
from market_autotrader import (
    BUY,
    SELL,
    AssetConfig,
    AutoExecutionConfig,
    MarketSnapshot,
    PaperPortfolio,
    RiskConfig,
    Signal,
    StrategyConfig,
    TradingMemory,
    atomic_write_json,
    build_futures_order_intent,
    effective_breakeven_activate_pct,
    ensure_futures_position_protection,
    maybe_trail_futures_take_profit,
    open_context_with_excursion,
    protection_breakeven_activate_multiplier,
    update_position_excursion,
    utc_today_key,
)
from trading_costs import apply_new_income_rows


class RecordTradeSignatureTests(unittest.TestCase):
    def test_record_trade_accepts_is_open_is_add_without_crashing(self):
        # F1: this exact call shape used to raise TypeError on every live fill.
        memory = TradingMemory()
        memory.record_trade("BTCUSDT", 1_700_000_000, is_open=True, is_add=False)
        self.assertEqual(memory.count_for_today(), 1)
        self.assertEqual(memory.last_trade_at["BTCUSDT"], 1_700_000_000)

    def test_is_add_increments_per_symbol_add_cap_with_matching_key(self):
        # F1: the add cap read side uses f"{symbol}:{utc_today_key()}".
        memory = TradingMemory()
        memory.record_trade("ethusdt", 1_700_000_000, is_open=True, is_add=True)
        add_key = f"ETHUSDT:{utc_today_key()}"
        self.assertEqual(memory.daily_symbol_add_counts.get(add_key), 1)

    def test_non_add_does_not_touch_add_counts(self):
        memory = TradingMemory()
        memory.record_trade("ETHUSDT", 1_700_000_000, is_open=True, is_add=False)
        self.assertEqual(memory.daily_symbol_add_counts, {})


class DayKeyTimezoneTests(unittest.TestCase):
    def test_record_trade_uses_utc_day_key_read_by_count_for_today(self):
        # STATE-03: write key must equal the UTC read key, else counts vanish.
        memory = TradingMemory()
        memory.record_trade("BTCUSDT", 1_700_000_000)
        self.assertIn(utc_today_key(), memory.daily_trade_counts)
        self.assertEqual(memory.count_for_today(), 1)

    def test_record_loss_uses_utc_day_key(self):
        memory = TradingMemory()
        memory.record_loss(Decimal("12.5"))
        self.assertEqual(memory.daily_loss_today(), Decimal("12.5"))


class RealizedPnlDailyLossTests(unittest.TestCase):
    def test_realized_pnl_loss_feeds_daily_loss_cap(self):
        # F2: a negative REALIZED_PNL income row must increase daily_loss_quote.
        memory = TradingMemory()
        rows = [
            {"incomeType": "REALIZED_PNL", "income": "-25.0", "tranId": "1", "time": NOW_MS},
            {"incomeType": "REALIZED_PNL", "income": "10.0", "tranId": "2", "time": NOW_MS},
        ]
        apply_new_income_rows(memory, rows, set())
        # Only the loss portion counts; the +10 winner does not reduce the cap.
        self.assertEqual(memory.daily_loss_today(), Decimal("25.0"))

    def test_realized_pnl_dedups_by_tran_id(self):
        memory = TradingMemory()
        rows = [{"incomeType": "REALIZED_PNL", "income": "-25.0", "tranId": "1", "time": NOW_MS}]
        seen = apply_new_income_rows(memory, rows, set())
        apply_new_income_rows(memory, rows, seen)  # replay must not double-count
        self.assertEqual(memory.daily_loss_today(), Decimal("25.0"))

    def test_record_realized_pnl_ignores_profit(self):
        memory = TradingMemory()
        memory.record_realized_pnl(Decimal("50"))
        self.assertEqual(memory.daily_loss_today(), Decimal("0"))


class AtomicWriteTests(unittest.TestCase):
    def test_atomic_write_json_round_trips_and_leaves_no_temp(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "state.json"
            payload = {"a": 1, "b": [1, 2, 3]}
            atomic_write_json(target, payload)
            self.assertEqual(json.loads(target.read_text()), payload)
            # no stray temp files left behind
            leftovers = [p.name for p in Path(d).iterdir() if p.name != "state.json"]
            self.assertEqual(leftovers, [])

    def test_atomic_write_overwrites_existing(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "state.json"
            atomic_write_json(target, {"v": 1})
            atomic_write_json(target, {"v": 2})
            self.assertEqual(json.loads(target.read_text()), {"v": 2})

    def test_trading_memory_save_is_atomic_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "live-trading-state.json")
            memory = TradingMemory()
            memory.record_trade("BTCUSDT", 1_700_000_000, is_open=True, is_add=False)
            memory.record_loss(Decimal("5"))
            memory.save(path)
            reloaded = TradingMemory.load(path)
            self.assertEqual(reloaded.count_for_today(), 1)
            self.assertEqual(reloaded.daily_loss_today(), Decimal("5"))

    def test_position_excursion_save_load_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "live-trading-state.json")
            memory = TradingMemory()
            memory.position_excursion["BTCUSDT"] = {"mfePct": "0.0123", "maePct": "-0.0045"}
            memory.save(path)
            reloaded = TradingMemory.load(path)
            self.assertEqual(reloaded.position_excursion["BTCUSDT"]["mfePct"], "0.0123")
            self.assertEqual(reloaded.position_excursion["BTCUSDT"]["maePct"], "-0.0045")


class PositionExcursionTests(unittest.TestCase):
    def test_update_position_excursion_tracks_long_mfe_mae(self):
        memory = TradingMemory()
        update_position_excursion(
            memory,
            "btcusdt",
            entry=Decimal("100"),
            mark=Decimal("105"),
            side_long=True,
        )
        update_position_excursion(
            memory,
            "BTCUSDT",
            entry=Decimal("100"),
            mark=Decimal("97"),
            side_long=True,
        )
        self.assertEqual(memory.position_excursion["BTCUSDT"]["mfePct"], "0.0500")
        self.assertEqual(memory.position_excursion["BTCUSDT"]["maePct"], "-0.0300")

    def test_open_context_with_excursion_appends_mfe_mae(self):
        memory = TradingMemory()
        memory.position_open_context["BTCUSDT"] = {"bucket": "configured"}
        memory.position_excursion["BTCUSDT"] = {"mfePct": "0.0500", "maePct": "-0.0300"}
        ctx = open_context_with_excursion(memory, "btcusdt")
        self.assertEqual(ctx["bucket"], "configured")
        self.assertEqual(ctx["mfePct"], "0.0500")
        self.assertEqual(ctx["maePct"], "-0.0300")


class FuturesLeveragePropagationTests(unittest.TestCase):
    def test_sizing_leverage_is_set_on_open_intent(self):
        # F1-leverage: the leverage used to size notional must be carried on the
        # OrderIntent so enrich/set_futures_leverage use the SAME number.
        asset = AssetConfig(
            symbol="BTCUSDT",
            market="binance_futures",
            base_asset="BTC",
            quote_asset="USDT",
            provider={},
        )
        strategy = StrategyConfig()
        risk = RiskConfig(
            mode="live",
            risk_per_trade_pct=Decimal("0.5"),
            max_trade_quote=Decimal("10000"),
            max_position_quote=Decimal("100000"),
            min_trade_quote=Decimal("5"),
            min_margin_per_trade=Decimal("1"),
        )
        auto_exec = AutoExecutionConfig(auto_leverage=True, default_leverage=5, max_leverage=20)
        portfolio = PaperPortfolio(cash={"USDT": Decimal("1000")}, positions={})
        signal = Signal(
            action=BUY,
            confidence=Decimal("0.9"),
            reasons=["trend", "momentum", "volume"],
            indicators={},
            warnings=[],
        )
        snapshot = MarketSnapshot(asset=asset, bars=[], observed_at=1_700_000_000)
        intent = build_futures_order_intent(
            asset,
            "BTCUSDT",
            Decimal("100"),
            signal,
            strategy,
            risk,
            portfolio,
            auto_exec=auto_exec,
            equity=Decimal("1000"),
        )
        self.assertIsNotNone(intent)
        self.assertIsNotNone(intent.leverage)
        self.assertGreaterEqual(int(intent.leverage), 1)


class AdaptiveBreakevenProtectionTests(unittest.TestCase):
    def test_shadow_or_compressed_bucket_tightens_breakeven_activation(self):
        learning = {
            "enabled": True,
            "bucketSizingFactors": {"futuresTopVolume": "0.65"},
            "bucketLiveModes": {"futuresTopVolume": "shadow_first"},
        }
        ctx = {"bucket": "futuresTopVolume", "source": "discovery:futuresTopVolume"}
        mult = protection_breakeven_activate_multiplier(learning, ctx)
        self.assertEqual(mult, Decimal("0.65"))

        strategy = StrategyConfig(
            trade_horizon="swing",
            protection_breakeven_activate_pct=Decimal("0.03"),
        )
        self.assertEqual(
            effective_breakeven_activate_pct(strategy, activate_multiplier=mult),
            Decimal("0.02925"),
        )

    def test_unknown_bucket_keeps_default_breakeven_activation(self):
        learning = {"enabled": True, "bucketSizingFactors": {"configured": "1.10"}}
        self.assertEqual(
            protection_breakeven_activate_multiplier(learning, {"source": "configured"}),
            Decimal("1"),
        )

    def test_exit_quality_factor_accelerates_take_profit_trailing(self):
        memory = TradingMemory()
        memory.position_peak_price["BTCUSDT:LONG"] = "102.5"
        strategy = StrategyConfig(
            holding_trailing_activate_pct=Decimal("0.03"),
            holding_peak_giveback_pct=Decimal("0.005"),
            holding_trend_up_giveback_mult=Decimal("1"),
        )
        with (
            patch("market_autotrader.fetch_symbol_filters", return_value={"tick_size": Decimal("0.1")}),
            patch("market_autotrader.fetch_futures_open_algo_orders", return_value=[]),
            patch(
                "market_autotrader.replace_futures_position_protection",
                return_value={"status": "replaced", "takeProfitPrice": "100.4"},
            ) as replace_mock,
        ):
            skipped = maybe_trail_futures_take_profit(
                "BTCUSDT",
                ma.ExecutionConfig(),
                ma.AutoExecutionConfig(),
                strategy,
                memory,
                entry=Decimal("100"),
                mark=Decimal("102"),
                side_long=True,
            )
            trailed = maybe_trail_futures_take_profit(
                "BTCUSDT",
                ma.ExecutionConfig(),
                ma.AutoExecutionConfig(),
                strategy,
                memory,
                entry=Decimal("100"),
                mark=Decimal("102"),
                side_long=True,
                activate_multiplier=Decimal("0.80"),
            )

        self.assertEqual(skipped["status"], "trail_tp_skip")
        self.assertEqual(trailed["status"], "replaced")
        self.assertEqual(replace_mock.call_count, 1)

    def test_exit_quality_factor_tightens_initial_take_profit_protection(self):
        submitted: list[tuple[str, Decimal]] = []

        def fake_submit(order, quantity, execution, auto_exec, *, order_type, trigger_price):
            submitted.append((order_type, trigger_price))
            return {"endpoint": "/mock", "response": {"orderId": len(submitted)}}

        learning = {
            "enabled": True,
            "bucketExitQualityFactors": {"configured": "0.80"},
        }
        with (
            patch(
                "market_autotrader.fetch_futures_positions",
                return_value=[
                    {
                        "symbol": "BTCUSDT",
                        "positionAmt": "1",
                        "positionSide": "BOTH",
                        "entryPrice": "100",
                    }
                ],
            ),
            patch(
                "market_autotrader.fetch_symbol_filters",
                return_value={"step_size": Decimal("0.001"), "tick_size": Decimal("0.1")},
            ),
            patch("market_autotrader.fetch_futures_open_algo_orders", return_value=[]),
            patch("market_autotrader.fetch_futures_hedge_mode", return_value=False),
            patch("market_autotrader.submit_futures_algo_protection_order", side_effect=fake_submit),
        ):
            result = ensure_futures_position_protection(
                "BTCUSDT",
                ma.ExecutionConfig(),
                ma.AutoExecutionConfig(take_profit_pct=Decimal("0.03"), stop_loss_pct=Decimal("0.015")),
                trade_learning=learning,
                open_context={"bucket": "configured"},
                force=True,
            )

        self.assertEqual(result["status"], "protection_attached")
        self.assertEqual(result["exitQualityMultiplier"], "0.80")
        tp_prices = [price for order_type, price in submitted if order_type == ma.ORDER_TAKE_PROFIT_MARKET]
        self.assertEqual(tp_prices, [Decimal("102.400")])


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import market_autotrader as ma
from market_autotrader import (
    APPROVAL_REQUIRED,
    BINANCE_ORDER_TEST,
    BUY,
    LIVE,
    BLOCKED,
    HOLD,
    AssetConfig,
    MarketBar,
    MarketSnapshot,
    PaperPortfolio,
    RiskConfig,
    StrategyConfig,
    TradingMemory,
    apply_risk_controls,
    build_signal,
    execute_decision,
    execute_paper,
    execution_from_config,
    record_to_json,
    run_once,
    submit_binance_order_test,
)


def rising_bars(count: int = 40) -> list[MarketBar]:
    bars = []
    price = Decimal("100")
    for index in range(count):
        price += Decimal("1.0")
        bars.append(
            MarketBar(
                timestamp=index,
                open=price - Decimal("0.5"),
                high=price + Decimal("0.5"),
                low=price - Decimal("1"),
                close=price,
                volume=Decimal("1000") + Decimal(index * 25),
            )
        )
    return bars


def falling_bars(count: int = 40) -> list[MarketBar]:
    bars = []
    price = Decimal("150")
    for index in range(count):
        price -= Decimal("1.5")
        bars.append(
            MarketBar(
                timestamp=index,
                open=price + Decimal("0.5"),
                high=price + Decimal("1"),
                low=price - Decimal("0.5"),
                close=price,
                volume=Decimal("1200"),
            )
        )
    return bars


def reasonable_buy_bars() -> list[MarketBar]:
    bars = [
        MarketBar(
            timestamp=index,
            open=Decimal("100") + Decimal(index),
            high=Decimal("101") + Decimal(index),
            low=Decimal("99") + Decimal(index),
            close=Decimal("100") + Decimal(index),
            volume=Decimal("1000") + Decimal(index * 20),
        )
        for index in range(26)
    ]
    for offset, close in enumerate(["124", "123", "125", "124.5", "126", "125.5", "127", "126.5", "128", "127.5", "129", "128.5", "130", "131"]):
        bars.append(
            MarketBar(
                timestamp=26 + offset,
                open=Decimal(close) - Decimal("0.4"),
                high=Decimal(close) + Decimal("0.8"),
                low=Decimal(close) - Decimal("0.8"),
                close=Decimal(close),
                volume=Decimal("1600") + Decimal(offset * 30),
            )
        )
    return bars


def snapshot_for(bars: list[MarketBar], symbol: str = "BTCUSDT") -> MarketSnapshot:
    asset = AssetConfig(
        symbol=symbol,
        market="binance_spot",
        base_asset="BTC" if symbol == "BTCUSDT" else symbol,
        quote_asset="USDT",
        provider={"type": "static", "bars": []},
    )
    return MarketSnapshot(asset=asset, bars=bars, observed_at=1000)


class MarketAutotraderTests(unittest.TestCase):
    def test_build_signal_detects_constructive_market(self) -> None:
        signal = build_signal(snapshot_for(rising_bars()), StrategyConfig())

        self.assertEqual(signal.action, BUY)
        self.assertGreaterEqual(signal.confidence, Decimal("0.68"))
        self.assertGreaterEqual(len(signal.reasons), 5)
        self.assertIn("fast_sma", signal.indicators)

    def test_build_signal_holds_when_history_is_too_short(self) -> None:
        signal = build_signal(snapshot_for(rising_bars(5)), StrategyConfig())

        self.assertEqual(signal.action, HOLD)
        self.assertIn("insufficient_market_history", signal.warnings)

    def test_build_signal_holds_flat_market(self) -> None:
        flat_bars = [
            MarketBar(timestamp=index, open=Decimal("100"), high=Decimal("100"), low=Decimal("100"), close=Decimal("100"), volume=Decimal("1000"))
            for index in range(40)
        ]

        signal = build_signal(snapshot_for(flat_bars), StrategyConfig())

        self.assertEqual(signal.action, HOLD)
        self.assertEqual(signal.indicators["rsi"], "50")

    def test_bullish_reversal_candle_vetoes_counter_trend_short(self) -> None:
        # RC1: a downtrend that ends in a bullish reversal candle (hammer) must NOT
        # be shorted — the kline veto downgrades SELL -> HOLD. This is the fix for
        # the live losses where the bot shorted bottoms right before they reversed.
        bars = []
        price = Decimal("150")
        for index in range(39):
            price -= Decimal("1.0")
            bars.append(
                MarketBar(
                    timestamp=index,
                    open=price + Decimal("0.3"),
                    high=price + Decimal("0.5"),
                    low=price - Decimal("0.4"),
                    close=price,
                    volume=Decimal("1200"),
                )
            )
        # Final bar = hammer: small body up top, long lower wick (bullish reversal).
        bars.append(
            MarketBar(
                timestamp=39,
                open=Decimal("111.0"),
                high=Decimal("112.2"),
                low=Decimal("108.0"),
                close=Decimal("112.0"),
                volume=Decimal("2200"),
            )
        )
        strategy = StrategyConfig(regime_adaptive_signal=True, short_in_downtrend_boost=True)
        signal = build_signal(snapshot_for(bars), strategy)

        self.assertEqual(signal.indicators["kline_pattern_label"], "bullish")
        self.assertEqual(signal.indicators["regime"], "trend_down")
        # The veto must turn the would-be SELL into HOLD with an explanatory reason.
        self.assertEqual(signal.action, HOLD)
        self.assertTrue(
            any("vetoes" in r and "short" in r for r in signal.reasons),
            f"expected a kline veto reason, got {signal.reasons}",
        )

    def test_bearish_candle_does_not_veto_short(self) -> None:
        # Inverse guard: a downtrend ending in a NON-bullish candle is still allowed
        # to short. The veto only removes trades; it must not falsely fire on a
        # plain falling market (which would re-introduce the missed-short problem).
        bars = []
        price = Decimal("150")
        for index in range(40):
            price -= Decimal("1.0")
            bars.append(
                MarketBar(
                    timestamp=index,
                    open=price + Decimal("0.3"),
                    high=price + Decimal("0.5"),
                    low=price - Decimal("0.4"),
                    close=price,
                    volume=Decimal("1200"),
                )
            )
        strategy = StrategyConfig(regime_adaptive_signal=True, short_in_downtrend_boost=True)
        signal = build_signal(snapshot_for(bars), strategy)

        self.assertNotEqual(signal.indicators["kline_pattern_label"], "bullish")
        # No bullish reversal -> no veto reason (action may be SELL or HOLD by other rules).
        self.assertFalse(
            any("vetoes" in r and "short" in r for r in signal.reasons),
            f"veto fired on a non-bullish candle: {signal.reasons}",
        )

    def test_open_context_persists_actual_fill_price(self) -> None:
        # RC2: open context must carry the real fill price so trade-outcome entry
        # is the actual entry, not the portfolio's stale average_price.
        from trade_lessons import open_context_from_signal

        ind = {
            "regime": "trend_down",
            "rsi": "31",
            "momentum": "-0.003",
            "fusion_bull_pct": "0.08",
            "mtf_1m": "bearish",
            "mtf_5m": "bearish",
            "mtf_15m": "neutral",
            "entry_quadrant": "trend_short",
            "entry_quadrant_mode": "live",
        }
        with_price = open_context_from_signal(
            signal_indicators=ind, confidence="0.99", discovery_meta={"source": "pinned"},
            entry_price=Decimal("60.84"),
        )
        self.assertEqual(with_price["entryPrice"], "60.84")
        self.assertEqual(with_price["fusionBearPct"], "0.92")
        self.assertEqual(with_price["mtf5m"], "bearish")
        self.assertEqual(with_price["entryQuadrant"], "trend_short")
        self.assertEqual(with_price["entryQuadrantMode"], "live")

        # Back-compat: no fill price -> no entryPrice key (reader falls back to avg).
        without_price = open_context_from_signal(
            signal_indicators=ind, confidence="0.99", discovery_meta={"source": "pinned"},
        )
        self.assertNotIn("entryPrice", without_price)

        # A zero/empty fill price must not be persisted (avoids a bogus 0 entry).
        zero_price = open_context_from_signal(
            signal_indicators=ind, confidence="0.99", discovery_meta={}, entry_price="0",
        )
        self.assertNotIn("entryPrice", zero_price)

    def test_risk_blocks_live_trading_without_explicit_gate(self) -> None:
        snapshot = snapshot_for(rising_bars())
        signal = build_signal(snapshot, StrategyConfig())
        portfolio = PaperPortfolio.from_config({"cash": {"USDT": "1000"}})
        risk = RiskConfig(mode=LIVE, allow_live_trading=False)

        decision = apply_risk_controls(snapshot, signal, StrategyConfig(), risk, portfolio, TradingMemory())

        self.assertFalse(decision.approved)
        self.assertEqual(decision.action, BLOCKED)
        self.assertIn("Unattended live", " ".join(decision.blocked_reasons))

    def test_risk_blocks_live_trading_even_with_boolean_enabled(self) -> None:
        snapshot = snapshot_for(rising_bars())
        signal = build_signal(snapshot, StrategyConfig())
        portfolio = PaperPortfolio.from_config({"cash": {"USDT": "1000"}})
        with tempfile.TemporaryDirectory() as tmpdir:
            risk = RiskConfig(
                mode=LIVE,
                allow_live_trading=True,
                live_arm_path=str(Path(tmpdir) / "live-trading.armed"),
            )

            decision = apply_risk_controls(snapshot, signal, StrategyConfig(), risk, portfolio, TradingMemory())

            self.assertFalse(decision.approved)
            self.assertEqual(decision.action, BLOCKED)
            self.assertIn("not supported", " ".join(decision.blocked_reasons))

    def test_paper_execution_records_reasoned_buy_and_updates_portfolio(self) -> None:
        snapshot = snapshot_for(reasonable_buy_bars())
        strategy = StrategyConfig(min_buy_confidence=Decimal("0.60"))
        signal = build_signal(snapshot, strategy)
        portfolio = PaperPortfolio.from_config({"cash": {"USDT": "1000"}})
        memory = TradingMemory()
        risk = RiskConfig(max_trade_quote=Decimal("100"), max_position_quote=Decimal("500"))
        decision = apply_risk_controls(snapshot, signal, strategy, risk, portfolio, memory)

        record = execute_paper(snapshot, decision, signal, strategy, risk, portfolio, memory)

        self.assertEqual(record.status, "executed")
        self.assertEqual(record.action, BUY)
        self.assertGreater(record.quantity, Decimal("0"))
        self.assertGreaterEqual(len(record.reasons), 5)
        self.assertEqual(memory.count_for_today(), 1)

    def test_approval_required_writes_manual_ticket_without_executing_trade(self) -> None:
        snapshot = snapshot_for(reasonable_buy_bars())
        strategy = StrategyConfig(min_buy_confidence=Decimal("0.60"))
        signal = build_signal(snapshot, strategy)
        portfolio = PaperPortfolio.from_config({"cash": {"USDT": "1000"}})
        memory = TradingMemory()
        risk = RiskConfig(mode=APPROVAL_REQUIRED, max_trade_quote=Decimal("100"), max_position_quote=Decimal("500"))
        decision = apply_risk_controls(snapshot, signal, strategy, risk, portfolio, memory)

        with tempfile.TemporaryDirectory() as tmpdir:
            execution = execution_from_config({"mode": APPROVAL_REQUIRED, "approval_dir": tmpdir})
            record = execute_decision(snapshot, decision, signal, strategy, risk, portfolio, memory, execution)

            self.assertEqual(record.status, "approval_required")
            self.assertEqual(memory.count_for_today(), 0)
            ticket_path = Path(record.execution_details["approval_ticket"])
            self.assertTrue(ticket_path.exists())
            ticket = json.loads(ticket_path.read_text(encoding="utf-8"))
            self.assertEqual(ticket["status"], "awaiting_manual_approval")
            self.assertEqual(ticket["order"]["symbol"], "BTCUSDT")
            self.assertNotIn("api_secret", json.dumps(ticket).lower())

    def test_binance_order_test_uses_test_endpoint_only(self) -> None:
        snapshot = snapshot_for(reasonable_buy_bars())
        strategy = StrategyConfig(min_buy_confidence=Decimal("0.60"))
        signal = build_signal(snapshot, strategy)
        portfolio = PaperPortfolio.from_config({"cash": {"USDT": "1000"}})
        risk = RiskConfig(mode=BINANCE_ORDER_TEST, max_trade_quote=Decimal("50"), max_position_quote=Decimal("500"))
        decision = apply_risk_controls(snapshot, signal, strategy, risk, portfolio, TradingMemory())
        execution = execution_from_config(
            {
                "mode": BINANCE_ORDER_TEST,
                "binance_base_url": "https://api.example",
                "api_key": "key",
                "api_secret": "secret",
            }
        )
        calls = []

        def fake_request_json(url, method="GET", headers=None, timeout_seconds=10):
            calls.append((url, method, headers))
            if url.endswith("/api/v3/time"):
                return {"serverTime": 123456}
            return {}

        import market_autotrader

        original = market_autotrader.request_json
        market_autotrader.request_json = fake_request_json
        try:
            result = submit_binance_order_test(decision.order, execution)
        finally:
            market_autotrader.request_json = original

        self.assertEqual(result["endpoint"], "/api/v3/order/test")
        self.assertEqual(calls[-1][1], "POST")
        self.assertIn("/api/v3/order/test?", calls[-1][0])
        self.assertNotIn("/api/v3/order?", calls[-1][0])

    def test_risk_blocks_overheated_buy(self) -> None:
        snapshot = snapshot_for(rising_bars())
        strategy = StrategyConfig()
        signal = build_signal(snapshot, strategy)
        portfolio = PaperPortfolio.from_config({"cash": {"USDT": "1000"}})

        decision = apply_risk_controls(snapshot, signal, strategy, RiskConfig(), portfolio, TradingMemory())

        self.assertFalse(decision.approved)
        self.assertIn("RSI is overheated", " ".join(decision.blocked_reasons))

    def test_risk_blocks_daily_trade_cap(self) -> None:
        snapshot = snapshot_for(rising_bars())
        signal = build_signal(snapshot, StrategyConfig())
        portfolio = PaperPortfolio.from_config({"cash": {"USDT": "1000"}})
        memory = TradingMemory()
        memory.record_trade("BTCUSDT", 500)
        risk = RiskConfig(max_daily_trades=1)

        decision = apply_risk_controls(snapshot, signal, StrategyConfig(), risk, portfolio, memory)

        self.assertFalse(decision.approved)
        self.assertIn("Daily trade cap", " ".join(decision.blocked_reasons))

    def test_growth_scheduler_throttles_concurrent_open_gate(self) -> None:
        asset = AssetConfig(
            symbol="BTCUSDT",
            market="binance_futures",
            base_asset="BTC",
            quote_asset="USDT",
            provider={},
        )
        snapshot = MarketSnapshot(asset=asset, bars=rising_bars(), observed_at=1000)
        signal = build_signal(snapshot, StrategyConfig())
        signal = signal.__class__(
            action=signal.action,
            confidence=Decimal("0.90"),
            reasons=signal.reasons,
            warnings=[],
            indicators={**signal.indicators, "discovery_bucket": "configured"},
        )
        portfolio = PaperPortfolio(
            cash={"USDT_FUTURES": Decimal("200")},
            positions={
                f"OPEN{i}USDT": ma.Position(quantity=Decimal("1"), average_price=Decimal("10"))
                for i in range(4)
            },
            wallet_balance=Decimal("200"),
        )
        cfg = __import__("capital_scaling").capital_scaling_from_config(
            {"enabled": True, "growth_scheduler": {"enabled": True, "min_sample": 10}}
        )
        decision = apply_risk_controls(
            snapshot,
            signal,
            StrategyConfig(confidence_scale_sizing=False, buy_quote_fraction=Decimal("0.20")),
            RiskConfig(mode="paper", max_concurrent_positions=8, max_daily_trades=0),
            portfolio,
            TradingMemory(),
            auto_exec=ma.AutoExecutionConfig(default_leverage=5, max_leverage=5),
            trade_learning={
                "enabled": True,
                "sampleSize": 20,
                "winRate": "0.25",
                "profitFactor": "0.50",
                "totalRealizedPnl": "-6",
            },
            capital_scaling_cfg=cfg,
        )
        self.assertFalse(decision.approved)
        self.assertIn("Max concurrent positions 4 reached", " ".join(decision.blocked_reasons))
        self.assertEqual(signal.indicators["capitalScaling"]["growthScheduler"]["mode"], "throttle")
        self.assertIsNotNone(decision.effective_risk)
        self.assertEqual(decision.effective_risk.max_concurrent_positions, 4)

    def test_growth_scheduler_effective_risk_is_used_in_rationale(self) -> None:
        asset = AssetConfig(
            symbol="BTCUSDT",
            market="binance_futures",
            base_asset="BTC",
            quote_asset="USDT",
            provider={},
        )
        snapshot = MarketSnapshot(asset=asset, bars=rising_bars(), observed_at=1000)
        signal = build_signal(snapshot, StrategyConfig())
        signal = signal.__class__(
            action=signal.action,
            confidence=Decimal("0.90"),
            reasons=signal.reasons,
            warnings=[],
            indicators={**signal.indicators, "discovery_bucket": "configured"},
        )
        portfolio = PaperPortfolio(
            cash={"USDT_FUTURES": Decimal("200")},
            wallet_balance=Decimal("200"),
        )
        cfg = __import__("capital_scaling").capital_scaling_from_config(
            {"enabled": True, "growth_scheduler": {"enabled": True, "min_sample": 10}}
        )
        memory = TradingMemory()
        decision = apply_risk_controls(
            snapshot,
            signal,
            StrategyConfig(confidence_scale_sizing=False, buy_quote_fraction=Decimal("0.20")),
            RiskConfig(mode="paper", max_concurrent_positions=8, max_daily_trades=0, cooldown_seconds=600),
            portfolio,
            memory,
            auto_exec=ma.AutoExecutionConfig(default_leverage=5, max_leverage=5),
            trade_learning={
                "enabled": True,
                "sampleSize": 20,
                "winRate": "0.25",
                "profitFactor": "0.50",
                "totalRealizedPnl": "-6",
            },
            capital_scaling_cfg=cfg,
        )
        rationale = ma.build_trade_rationale(
            snapshot,
            signal,
            decision,
            decision.effective_risk or RiskConfig(),
            portfolio,
            memory,
        )

        self.assertIn("Daily trade cap 30", rationale["emotionGuardrails"][0])
        self.assertIn("Cooldown 900s", rationale["emotionGuardrails"][1])

    def test_risk_resizes_quote_to_max_trade_after_learning_scaling(self) -> None:
        asset = AssetConfig(
            symbol="BTCUSDT",
            market="binance_futures",
            base_asset="BTC",
            quote_asset="USDT",
            provider={},
        )
        snapshot = MarketSnapshot(asset=asset, bars=reasonable_buy_bars(), observed_at=1000)
        signal = ma.Signal(
            action=BUY,
            confidence=Decimal("0.95"),
            reasons=["strong"],
            warnings=[],
            indicators={"atr_pct": "0.01", "discovery_bucket": "configured"},
        )
        portfolio = PaperPortfolio(
            cash={"USDT_FUTURES": Decimal("200")},
            wallet_balance=Decimal("200"),
        )

        decision = apply_risk_controls(
            snapshot,
            signal,
            StrategyConfig(confidence_scale_sizing=False, buy_quote_fraction=Decimal("0.20")),
            RiskConfig(
                mode="paper",
                max_trade_quote=Decimal("90"),
                max_position_quote=Decimal("500"),
                min_trade_quote=Decimal("10"),
                reserve_futures_available_usdt=Decimal("0"),
                max_daily_trades=0,
                require_reason_count=1,
            ),
            portfolio,
            TradingMemory(),
            auto_exec=ma.AutoExecutionConfig(default_leverage=5, max_leverage=5),
            trade_learning={"enabled": True, "sizingFactor": "1.20"},
        )

        self.assertTrue(decision.approved, decision.blocked_reasons)
        self.assertIsNotNone(decision.order)
        self.assertEqual(decision.order.quote_amount, Decimal("90.00000000"))
        self.assertNotIn("exceeds max trade quote", " ".join(decision.blocked_reasons))

    def test_risk_resizes_quote_to_remaining_portfolio_heat_room(self) -> None:
        asset = AssetConfig(
            symbol="BTCUSDT",
            market="binance_futures",
            base_asset="BTC",
            quote_asset="USDT",
            provider={},
        )
        snapshot = MarketSnapshot(asset=asset, bars=reasonable_buy_bars(), observed_at=1000)
        signal = ma.Signal(
            action=BUY,
            confidence=Decimal("0.95"),
            reasons=["strong"],
            warnings=[],
            indicators={"atr_pct": "0.01", "discovery_bucket": "configured"},
        )
        portfolio = PaperPortfolio(
            cash={"USDT_FUTURES": Decimal("20")},
            positions={
                "ETHUSDT": ma.Position(
                    quantity=Decimal("1"),
                    average_price=Decimal("100"),
                    initial_margin=Decimal("90"),
                    notional=Decimal("450"),
                    leverage=5,
                )
            },
            wallet_balance=Decimal("200"),
        )

        decision = apply_risk_controls(
            snapshot,
            signal,
            StrategyConfig(confidence_scale_sizing=False, buy_quote_fraction=Decimal("1.0")),
            RiskConfig(
                mode="paper",
                max_trade_quote=Decimal("90"),
                max_position_quote=Decimal("500"),
                min_trade_quote=Decimal("10"),
                reserve_futures_available_usdt=Decimal("0"),
                max_portfolio_heat_pct=Decimal("0.50"),
                max_daily_trades=0,
                require_reason_count=1,
            ),
            portfolio,
            TradingMemory(),
            auto_exec=ma.AutoExecutionConfig(default_leverage=5, max_leverage=5),
        )

        self.assertTrue(decision.approved, decision.blocked_reasons)
        self.assertIsNotNone(decision.order)
        self.assertEqual(decision.order.quote_amount, Decimal("50.00000000"))
        self.assertTrue(any("Portfolio heat room applied" in r for r in decision.reasons))

    def test_run_once_uses_static_provider_and_writes_ledger(self) -> None:
        bars = [
            {
                "timestamp": bar.timestamp,
                "open": str(bar.open),
                "high": str(bar.high),
                "low": str(bar.low),
                "close": str(bar.close),
                "volume": str(bar.volume),
            }
            for bar in rising_bars()
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = str(Path(tmpdir) / "ledger.jsonl")
            config = {
                "ledger_path": ledger_path,
                "portfolio": {"cash": {"USDT": "1000"}},
                "risk": {"max_trade_quote": "100", "max_position_quote": "500"},
                "market_context": {"enabled": False},
                "assets": [
                    {
                        "symbol": "BTCUSDT",
                        "market": "binance_spot",
                        "base_asset": "BTC",
                        "quote_asset": "USDT",
                        "provider": {"type": "static", "bars": bars},
                    }
                ],
            }

            records = run_once(config, memory=TradingMemory())

            self.assertEqual(len(records), 1)
            self.assertTrue(Path(ledger_path).exists())
            raw = Path(ledger_path).read_text(encoding="utf-8").strip()
            self.assertEqual(json.loads(raw)["symbol"], "BTCUSDT")

    def test_record_json_serializes_decimals(self) -> None:
        snapshot = snapshot_for(falling_bars())
        signal = build_signal(snapshot, StrategyConfig())
        portfolio = PaperPortfolio.from_config({"cash": {"USDT": "1000"}})
        risk = RiskConfig()
        decision = apply_risk_controls(snapshot, signal, StrategyConfig(), risk, portfolio, TradingMemory())
        record = execute_paper(snapshot, decision, signal, StrategyConfig(), risk, portfolio, TradingMemory())

        payload = json.loads(record_to_json(record))

        self.assertIsInstance(payload["price"], str)


class F4LongLosersGateTests(unittest.TestCase):
    """F4: block counter-trend LONGs into the futuresLosers bucket (29% WR, -1.45)
    without touching the profitable SHORT flow or the winning Gainers LONGs."""

    def _block(self, *, action, bucket, confidence):
        from trade_lessons import trade_lessons_from_config, trade_lesson_block_reasons

        cfg = trade_lessons_from_config({"enabled": True})
        reasons = trade_lesson_block_reasons(
            cfg=cfg,
            order_action=action,
            reduce_only=False,
            position_qty=Decimal("0"),
            regime="trend_down",
            change_24h=Decimal("-0.05"),
            momentum=Decimal("-0.003"),
            rsi=Decimal("35"),
            confidence=Decimal(str(confidence)),
            bucket=bucket,
        )
        return [r for r in reasons if "losers bucket" in r]

    def test_blocks_low_conviction_long_into_losers(self):
        self.assertTrue(self._block(action="BUY", bucket="futuresLosers", confidence="0.80"))

    def test_does_not_touch_short_from_losers(self):
        # SHORTs from futuresLosers are the +5.37 USDT edge — must pass.
        self.assertFalse(self._block(action="SELL", bucket="futuresLosers", confidence="0.80"))

    def test_does_not_touch_long_from_gainers(self):
        # Gainers LONGs (chasing winners) are net positive — must pass.
        self.assertFalse(self._block(action="BUY", bucket="futuresGainers", confidence="0.80"))

    def test_high_conviction_reversal_bypass(self):
        # A genuine high-conviction reversal can still open.
        self.assertFalse(self._block(action="BUY", bucket="futuresLosers", confidence="0.96"))

    def test_disabled_passes_through(self):
        from trade_lessons import trade_lessons_from_config, trade_lesson_block_reasons

        cfg = trade_lessons_from_config({"enabled": True, "block_long_from_losers_bucket": False})
        reasons = trade_lesson_block_reasons(
            cfg=cfg, order_action="BUY", reduce_only=False, position_qty=Decimal("0"),
            regime="trend_down", change_24h=Decimal("-0.05"), momentum=Decimal("-0.003"),
            rsi=Decimal("35"), confidence=Decimal("0.80"), bucket="futuresLosers",
        )
        self.assertFalse([r for r in reasons if "losers bucket" in r])


if __name__ == "__main__":
    unittest.main()

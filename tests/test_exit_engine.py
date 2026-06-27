"""Tests for exit_engine module."""

from decimal import Decimal
import unittest

from market_autotrader import (
    BUY,
    HOLD,
    SELL,
    MarketBar,
    MarketSnapshot,
    AssetConfig,
    PaperPortfolio,
    Position,
    RiskConfig,
    Signal,
    StrategyConfig,
    TradingMemory,
)
from exit_engine import (
    apply_holding_priority_signal,
    exit_quality_threshold_mult,
    mtf_5m_confirms_loss_exit,
    peak_giveback_exit_reason,
)


def _snapshot(symbol: str = "TESTUSDT", price: str = "100") -> MarketSnapshot:
    asset = AssetConfig(
        symbol=symbol,
        market="binance_futures",
        base_asset="TEST",
        quote_asset="USDT",
        provider={},
    )
    bars = [
        MarketBar(timestamp=i, open=Decimal(price), high=Decimal(price), low=Decimal(price), close=Decimal(price), volume=Decimal("1"))
        for i in range(5)
    ]
    return MarketSnapshot(asset=asset, bars=bars, observed_at=1_700_000_000)


class ExitEngineTests(unittest.TestCase):
    def test_supervisor_reduce_upgrades_hold(self) -> None:
        portfolio = PaperPortfolio(
            cash={"USDT_FUTURES": Decimal("1000")},
            positions={"TESTUSDT": Position(quantity=Decimal("10"), average_price=Decimal("100"))},
        )
        signal = Signal(action=HOLD, confidence=Decimal("0.5"), reasons=[], warnings=[], indicators={})
        hints = {
            "TESTUSDT": {
                "action": "REDUCE_POSITION",
                "priority": "high",
                "reason": "mark below danger buffer",
            }
        }
        out = apply_holding_priority_signal(
            _snapshot(), signal, StrategyConfig(), portfolio, supervisor_hints=hints
        )
        self.assertEqual(out.action, SELL)
        self.assertTrue(any("Supervisor REDUCE_POSITION" in r for r in out.reasons))

    def test_5m_blocks_premature_loss_cut(self) -> None:
        portfolio = PaperPortfolio(
            cash={"USDT_FUTURES": Decimal("1000")},
            positions={"TESTUSDT": Position(quantity=Decimal("10"), average_price=Decimal("100"))},
        )
        signal = Signal(
            action=HOLD,
            confidence=Decimal("0.5"),
            reasons=[],
            warnings=[],
            indicators={"regime": "range", "momentum": "0", "mtf_5m": "bullish"},
        )
        strategy = StrategyConfig(holding_reduce_loss_pct=Decimal("0.02"), holding_require_5m_exit_confirm=True)
        out = apply_holding_priority_signal(
            _snapshot(price="98.5"), signal, strategy, portfolio
        )
        self.assertEqual(out.action, HOLD)

    def test_mtf_5m_confirms_loss_exit(self) -> None:
        self.assertFalse(mtf_5m_confirms_loss_exit(position_qty=Decimal("1"), mtf_5m="bullish"))
        self.assertTrue(mtf_5m_confirms_loss_exit(position_qty=Decimal("1"), mtf_5m="bearish"))

    def test_peak_giveback_long(self) -> None:
        reason = peak_giveback_exit_reason(
            position_qty=Decimal("1"),
            entry=Decimal("100"),
            price=Decimal("104"),
            peak_price=Decimal("110"),
            trail_activate_pct=Decimal("0.03"),
            giveback_pct=Decimal("0.05"),
        )
        self.assertIsNotNone(reason)

    def test_exit_quality_low_capture_accelerates_early_take(self) -> None:
        portfolio = PaperPortfolio(
            cash={"USDT_FUTURES": Decimal("1000")},
            positions={"TESTUSDT": Position(quantity=Decimal("10"), average_price=Decimal("100"))},
        )
        memory = TradingMemory()
        memory.position_open_context["TESTUSDT"] = {"bucket": "configured", "source": "configured"}
        signal = Signal(
            action=HOLD,
            confidence=Decimal("0.5"),
            reasons=[],
            warnings=[],
            indicators={"regime": "range", "momentum": "0", "mtf_5m": "neutral"},
        )
        learning = {
            "enabled": True,
            "bucketStats": {
                "configured": {
                    "exitQuality": {"sampleSize": 4, "avgCaptureRatio": "0.40"}
                }
            },
        }
        strategy = StrategyConfig(trade_horizon="swing", holding_early_take_pct=Decimal("0.03"))
        out = apply_holding_priority_signal(
            _snapshot(price="103.7"),
            signal,
            strategy,
            portfolio,
            memory,
            trade_learning=learning,
        )
        self.assertEqual(out.action, SELL)
        self.assertEqual(out.indicators.get("exit_tier"), "early")

    def test_exit_quality_sample_floor_keeps_default_thresholds(self) -> None:
        learning = {
            "enabled": True,
            "bucketStats": {
                "configured": {
                    "exitQuality": {"sampleSize": 3, "avgCaptureRatio": "0.20"}
                }
            },
        }
        self.assertEqual(
            exit_quality_threshold_mult(learning, {"bucket": "configured"}),
            Decimal("1"),
        )

    def test_exit_quality_uses_persisted_bucket_factor(self) -> None:
        learning = {
            "enabled": True,
            "bucketExitQualityFactors": {"configured": "1.10"},
            "bucketStats": {
                "configured": {
                    "exitQuality": {"sampleSize": 0}
                }
            },
        }
        self.assertEqual(
            exit_quality_threshold_mult(learning, {"bucket": "configured"}),
            Decimal("1.10"),
        )


if __name__ == "__main__":
    unittest.main()

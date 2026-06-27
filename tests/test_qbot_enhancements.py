import unittest
from decimal import Decimal

from benchmark_gate import benchmark_gate_from_config, rolling_peak_drawdown
from market_autotrader import MarketBar, decimal_from
from regime_cache import get_cached_regime, set_cached_regime
from rsrs import rsrs_score


class BenchmarkGateTests(unittest.TestCase):
    def test_rolling_peak_drawdown(self) -> None:
        closes = [Decimal("100"), Decimal("110"), Decimal("99"), Decimal("105")]
        dd = rolling_peak_drawdown(closes)
        self.assertGreater(dd, Decimal("0.09"))

    def test_benchmark_gate_defaults_disabled(self) -> None:
        cfg = benchmark_gate_from_config({})
        self.assertFalse(cfg.enabled)


class RegimeCacheTests(unittest.TestCase):
    def test_set_and_get(self) -> None:
        set_cached_regime("ETHUSDT", "binance_futures", "trend_up")
        self.assertEqual(get_cached_regime("ETHUSDT", "binance_futures", 300), "trend_up")


class RsrsTests(unittest.TestCase):
    def test_rsrs_score_on_trending_bars(self) -> None:
        bars = []
        price = Decimal("100")
        for idx in range(40):
            price += Decimal("0.5")
            bars.append(
                MarketBar(
                    timestamp=idx,
                    open=price,
                    high=price + Decimal("1"),
                    low=price - Decimal("1"),
                    close=price,
                    volume=decimal_from("1000"),
                )
            )
        score = rsrs_score(bars, 18)
        self.assertIsInstance(score, Decimal)


if __name__ == "__main__":
    unittest.main()

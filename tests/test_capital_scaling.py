"""Tests for the equity-adaptive capital scaling layer."""

import unittest
from decimal import Decimal

import market_autotrader as ma
from capital_scaling import (
    apply_growth_scheduler,
    capital_scaling_from_config,
    scale_risk_for_equity,
    scaled_leverage_cap,
    select_tier,
)


def base_risk():
    return ma.risk_from_config(
        {
            "mode": "live",
            "min_trade_quote": "50",
            "max_trade_quote": "800",
            "min_margin_per_trade": "10",
            "reserve_futures_available_usdt": "30",
            "risk_per_trade_pct": "0.015",
            "max_position_quote": "5000",
        }
    )


class TierSelectionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = capital_scaling_from_config({"enabled": True})

    def test_micro_tier_for_tiny_account(self):
        self.assertEqual(select_tier(self.cfg, Decimal("14.74")).name, "micro")

    def test_small_tier(self):
        self.assertEqual(select_tier(self.cfg, Decimal("200")).name, "small")

    def test_mid_tier(self):
        self.assertEqual(select_tier(self.cfg, Decimal("2000")).name, "mid")

    def test_large_tier_for_big_account(self):
        self.assertEqual(select_tier(self.cfg, Decimal("50000")).name, "large")

    def test_boundary_inclusive(self):
        # exactly 50 -> micro (max_equity inclusive)
        self.assertEqual(select_tier(self.cfg, Decimal("50")).name, "micro")
        self.assertEqual(select_tier(self.cfg, Decimal("50.01")).name, "small")


class ScaleRiskTests(unittest.TestCase):
    def setUp(self):
        self.cfg = capital_scaling_from_config({"enabled": True})

    def test_micro_lowers_floors_so_small_account_can_trade(self):
        scaled, info = scale_risk_for_equity(self.cfg, base_risk(), Decimal("14.74"))
        self.assertEqual(scaled.min_trade_quote, Decimal("5"))
        self.assertLessEqual(scaled.min_margin_per_trade, Decimal("2"))
        self.assertLess(scaled.reserve_futures_available_usdt, Decimal("30"))
        self.assertGreater(scaled.risk_per_trade_pct, Decimal("0.015"))
        self.assertEqual(info["tier"], "micro")

    def test_disabled_returns_unchanged(self):
        cfg = capital_scaling_from_config({"enabled": False})
        scaled, info = scale_risk_for_equity(cfg, base_risk(), Decimal("14.74"))
        self.assertEqual(scaled.min_trade_quote, Decimal("50"))
        self.assertFalse(info["enabled"])

    def test_does_not_mutate_input(self):
        r = base_risk()
        scale_risk_for_equity(self.cfg, r, Decimal("14.74"))
        self.assertEqual(r.min_trade_quote, Decimal("50"))  # original untouched

    def test_leverage_cap_grows_with_equity(self):
        self.assertLessEqual(scaled_leverage_cap(self.cfg, Decimal("14.74")), 3)
        self.assertGreaterEqual(scaled_leverage_cap(self.cfg, Decimal("50000")), 15)

    def test_micro_account_produces_placeable_notional(self):
        from growth_sizing import compute_open_notional
        from market_autotrader import StrategyConfig

        scaled, _ = scale_risk_for_equity(self.cfg, base_risk(), Decimal("14.74"))
        strat = StrategyConfig(buy_quote_fraction=Decimal("0.55"), confidence_scale_sizing=False)
        sizing_margin = Decimal("14.74") - scaled.reserve_futures_available_usdt
        notional = compute_open_notional(
            scaled,
            strat,
            equity=Decimal("14.74"),
            available_margin=sizing_margin,
            signal_confidence=Decimal("0.8"),
            current_position_value=Decimal("0"),
            leverage=3,
            min_margin_per_trade=scaled.min_margin_per_trade,
        )
        self.assertGreaterEqual(notional, scaled.min_trade_quote)
        self.assertGreater(notional, Decimal("0"))

    def test_small_tier_targets_half_capital_use_at_full_concurrency(self):
        from growth_sizing import compute_open_notional, margin_required_for_notional
        from market_autotrader import StrategyConfig

        equity = Decimal("200")
        scaled, info = scale_risk_for_equity(self.cfg, base_risk(), equity)
        strat = StrategyConfig(buy_quote_fraction=Decimal("0.55"), confidence_scale_sizing=False)
        notional = compute_open_notional(
            scaled,
            strat,
            equity=equity,
            available_margin=equity - scaled.reserve_futures_available_usdt,
            signal_confidence=Decimal("0.8"),
            current_position_value=Decimal("0"),
            leverage=5,
            min_margin_per_trade=scaled.min_margin_per_trade,
        )
        margin = margin_required_for_notional(notional, 5)
        full_heat = (margin * Decimal(info["maxConcurrentPositions"]) / equity).quantize(Decimal("0.01"))

        self.assertEqual(info["tier"], "small")
        self.assertEqual(scaled.risk_per_trade_pct, Decimal("0.50"))
        self.assertEqual(notional, Decimal("90.00000000"))
        self.assertEqual(full_heat, Decimal("0.45"))


class GrowthSchedulerTests(unittest.TestCase):
    def _cfg(self):
        return capital_scaling_from_config(
            {
                "enabled": True,
                "growth_scheduler": {
                    "enabled": True,
                    "min_sample": 10,
                    "max_concurrency_boost": 2,
                    "max_daily_trade_boost": 20,
                    "min_cooldown_mult": "0.50",
                    "loss_cooldown_mult": "1.50",
                    "min_cooldown_seconds": 120,
                    "max_daily_trades": 120,
                },
            }
        )

    def test_positive_expectancy_expands_frequency_and_concurrency(self):
        cfg = self._cfg()
        risk = ma.RiskConfig(max_concurrent_positions=5, max_daily_trades=60, cooldown_seconds=600)
        adjusted, info = apply_growth_scheduler(
            cfg,
            risk,
            equity=Decimal("200"),
            trade_learning={
                "sampleSize": 20,
                "winRate": "0.60",
                "profitFactor": "1.80",
                "totalRealizedPnl": "12",
            },
            today_net=Decimal("1.0"),
            daily_drag=Decimal("0"),
            effective_daily_loss_cap=Decimal("10"),
            portfolio_heat=Decimal("0.20"),
        )
        self.assertEqual(info["mode"], "expand")
        self.assertGreaterEqual(adjusted.max_concurrent_positions, 5)
        self.assertGreater(adjusted.max_daily_trades, 60)
        self.assertLess(adjusted.cooldown_seconds, 600)

    def test_negative_expectancy_throttles_frequency(self):
        cfg = self._cfg()
        risk = ma.RiskConfig(max_concurrent_positions=5, max_daily_trades=60, cooldown_seconds=600)
        adjusted, info = apply_growth_scheduler(
            cfg,
            risk,
            equity=Decimal("200"),
            trade_learning={
                "sampleSize": 20,
                "winRate": "0.25",
                "profitFactor": "0.50",
                "totalRealizedPnl": "-6",
            },
            today_net=Decimal("-1"),
            daily_drag=Decimal("0"),
            effective_daily_loss_cap=Decimal("10"),
            portfolio_heat=Decimal("0.20"),
        )
        self.assertEqual(info["mode"], "throttle")
        self.assertLess(adjusted.max_concurrent_positions, 5)
        self.assertLess(adjusted.max_daily_trades, 60)
        self.assertGreater(adjusted.cooldown_seconds, 600)

    def test_low_win_rate_positive_profit_factor_stays_neutral(self):
        cfg = self._cfg()
        risk = ma.RiskConfig(max_concurrent_positions=5, max_daily_trades=60, cooldown_seconds=600)
        adjusted, info = apply_growth_scheduler(
            cfg,
            risk,
            equity=Decimal("200"),
            trade_learning={
                "sampleSize": 20,
                "winRate": "0.25",
                "profitFactor": "1.69",
                "totalRealizedPnl": "0.4988",
            },
            today_net=Decimal("0.3"),
            daily_drag=Decimal("0"),
            effective_daily_loss_cap=Decimal("10"),
            portfolio_heat=Decimal("0.20"),
        )
        self.assertEqual(info["mode"], "neutral")
        self.assertEqual(info["expectancyProfile"], "positive_pnl_low_win_rate")
        self.assertEqual(adjusted.max_concurrent_positions, 5)
        self.assertEqual(adjusted.max_daily_trades, 60)
        self.assertEqual(adjusted.cooldown_seconds, 600)

    def test_sample_wait_keeps_base_limits(self):
        cfg = self._cfg()
        risk = ma.RiskConfig(max_concurrent_positions=5, max_daily_trades=0, cooldown_seconds=600)
        adjusted, info = apply_growth_scheduler(
            cfg,
            risk,
            equity=Decimal("200"),
            trade_learning={"sampleSize": 3, "winRate": "1.0", "profitFactor": "999", "totalRealizedPnl": "6"},
            today_net=Decimal("0"),
            daily_drag=Decimal("0"),
            effective_daily_loss_cap=Decimal("10"),
            portfolio_heat=Decimal("0.20"),
        )
        self.assertEqual(info["mode"], "sample_wait")
        self.assertEqual(adjusted.max_concurrent_positions, 5)
        self.assertEqual(adjusted.max_daily_trades, 0)
        self.assertEqual(adjusted.cooldown_seconds, 600)


if __name__ == "__main__":
    unittest.main()

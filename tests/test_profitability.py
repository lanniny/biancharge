"""Tests for profitability tuning."""

from __future__ import annotations

from decimal import Decimal

from bucket_strategy import bucket_quote_fraction_mult, bucket_strategy_from_config
from growth_sizing import effective_max_daily_loss
from market_autotrader import RiskConfig
from profitability import (
    ProfitabilityConfig,
    bucket_regime_match_soft,
    profitability_from_config,
)


def test_soft_regime_gainers_trend_down_passes():
    cfg = ProfitabilityConfig(enabled=True, regime_filter_mode="soft", gainers_allow_trend_down=True)
    assert bucket_regime_match_soft("futuresGainers", "trend_down", cfg) is True


def test_soft_regime_gainers_squeeze_passes():
    cfg = ProfitabilityConfig(
        enabled=True,
        regime_filter_mode="soft",
        gainers_allow_squeeze=True,
        gainers_allow_trend_down=False,
    )
    assert bucket_regime_match_soft("futuresGainers", "squeeze", cfg) is True
    assert bucket_regime_match_soft("futuresGainers", "trend_down", cfg) is False


def test_daily_loss_not_stuck_at_min_floor_only():
    risk = RiskConfig(
        max_daily_loss_quote=Decimal("25"),
        max_daily_loss_pct=Decimal("0.025"),
        scale_sizing_with_equity=True,
        min_daily_loss_cap_quote=Decimal("5"),
    )
    # ~138 USDT equity scenario: pct ~3.45, but 25% of cap = 6.25 wins
    assert effective_max_daily_loss(risk, Decimal("138")) >= Decimal("6.25")
    assert effective_max_daily_loss(risk, Decimal("138")) <= Decimal("25")


def test_bucket_quote_fraction_mult_wired():
    cfg = bucket_strategy_from_config(
        {
            "enabled": True,
            "policies": {"futuresTradFi": {"quote_fraction_mult": "0.80"}},
        }
    )
    assert bucket_quote_fraction_mult(cfg, "futuresTradFi", "discovery:futuresTradFi") == Decimal("0.80")


def test_profitability_from_config_defaults():
    cfg = profitability_from_config({"enabled": True})
    assert cfg.regime_filter_mode == "soft"
    assert cfg.force_exit_fraction == Decimal("0.75")

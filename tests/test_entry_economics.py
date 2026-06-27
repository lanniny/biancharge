"""Tests for entry_economics fee gate."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from entry_economics import (
    discovery_short_block_reason,
    entry_economics_block_reason,
    round_trip_fee_pct,
)


def test_round_trip_fee():
    assert round_trip_fee_pct() == Decimal("0.0008")


def test_entry_economics_blocks_thin_edge():
    reason = entry_economics_block_reason(
        enabled=True,
        is_new_open=True,
        quote_amount=Decimal("50"),
        atr_pct=Decimal("0.0005"),
        min_edge_fee_multiple=Decimal("3"),
        take_profit_pct=Decimal("0.03"),
        atr_tp_multiplier=Decimal("3.5"),
        use_atr_stops=True,
    )
    assert reason is not None
    assert "Entry economics" in reason


def test_entry_economics_allows_wide_atr():
    reason = entry_economics_block_reason(
        enabled=True,
        is_new_open=True,
        quote_amount=Decimal("50"),
        atr_pct=Decimal("0.02"),
        min_edge_fee_multiple=Decimal("3"),
        take_profit_pct=Decimal("0.03"),
        atr_tp_multiplier=Decimal("3.5"),
        use_atr_stops=True,
    )
    assert reason is None


def test_discovery_short_blocked():
    reason = discovery_short_block_reason(
        discovery_short_mode="off",
        allow_discovery_shorts=False,
        order_action="SELL",
        is_discovery_open=True,
    )
    assert reason is not None
    assert "Discovery short" in reason


def test_discovery_short_losers_trend_down_allows():
    reason = discovery_short_block_reason(
        discovery_short_mode="losers_trend_down",
        order_action="SELL",
        is_discovery_open=True,
        bucket="futuresLosers",
        regime="trend_down",
    )
    assert reason is None


def test_discovery_short_losers_trend_down_blocks_wrong_regime():
    reason = discovery_short_block_reason(
        discovery_short_mode="losers_trend_down",
        order_action="SELL",
        is_discovery_open=True,
        bucket="futuresLosers",
        regime="trend_up",
    )
    assert reason is not None
    assert "losers_trend_down" in reason


def test_discovery_short_losers_only_blocks_gainers():
    reason = discovery_short_block_reason(
        discovery_short_mode="losers_only",
        order_action="SELL",
        is_discovery_open=True,
        bucket="futuresGainers",
    )
    assert reason is not None


def test_discovery_short_losers_soft_allows_squeeze():
    reason = discovery_short_block_reason(
        discovery_short_mode="losers_soft",
        order_action="SELL",
        is_discovery_open=True,
        bucket="futuresLosers",
        regime="squeeze",
        profitability_raw={"enabled": True, "regime_filter_mode": "soft", "losers_allow_squeeze": True},
    )
    assert reason is None


def test_discovery_short_losers_soft_blocks_gainers():
    reason = discovery_short_block_reason(
        discovery_short_mode="losers_soft",
        order_action="SELL",
        is_discovery_open=True,
        bucket="futuresGainers",
        regime="trend_down",
        profitability_raw={"enabled": True, "regime_filter_mode": "soft"},
    )
    assert reason is not None
    assert "losers_soft" in reason


def test_suppress_ineligible_discovery_short():
    from entry_economics import suppress_ineligible_discovery_short

    sig = SimpleNamespace(
        action="SELL",
        confidence=Decimal("0.9"),
        reasons=["base"],
        warnings=[],
        indicators={
            "discovery_source": "discovery:futuresGainers",
            "discovery_bucket": "futuresGainers",
            "regime": "trend_down",
            "entry_quadrant": "unclassified",
        },
    )
    out = suppress_ineligible_discovery_short(
        sig,
        discovery_short_mode="losers_soft",
        profitability_raw={"enabled": True, "regime_filter_mode": "soft"},
    )
    assert out.action == "HOLD"
    assert any("Discovery short guard" in r for r in out.reasons)


def test_discovery_short_bypass_reversal_short():
    reason = discovery_short_block_reason(
        discovery_short_mode="losers_trend_down",
        order_action="SELL",
        is_discovery_open=True,
        bucket="futuresTradFi",
        regime="trend_up",
        entry_quadrant="reversal_short",
    )
    assert reason is None

"""Tests for shadow_paper."""

from decimal import Decimal

from shadow_paper import shadow_paper_from_config, should_shadow_instead_of_live


def test_shadow_discovery_open_only():
    cfg = shadow_paper_from_config(
        {"enabled": True, "shadow_discovery_opens_only": True, "min_confidence": "0.5"}
    )
    assert should_shadow_instead_of_live(
        cfg,
        reduce_only=False,
        position_qty=Decimal("0"),
        indicators={"discovery_source": "discovery:futuresGainers"},
        confidence=Decimal("0.8"),
        approved=True,
    )
    assert not should_shadow_instead_of_live(
        cfg,
        reduce_only=True,
        position_qty=Decimal("10"),
        indicators={"discovery_source": "holding:BTCUSDT"},
        confidence=Decimal("0.8"),
        approved=True,
    )


def test_trade_learning_forces_shadow_even_below_min_confidence():
    cfg = shadow_paper_from_config(
        {"enabled": True, "shadow_discovery_opens_only": True, "min_confidence": "0.9"}
    )
    assert should_shadow_instead_of_live(
        cfg,
        reduce_only=False,
        position_qty=Decimal("0"),
        indicators={"discovery_source": "discovery:futuresLosers"},
        confidence=Decimal("0.6"),
        approved=True,
        trade_learning_shadow=True,
    )


def test_no_shadow_when_discovery_only_disabled_without_learning():
    cfg = shadow_paper_from_config(
        {"enabled": True, "shadow_discovery_opens_only": False, "min_confidence": "0.5"}
    )
    assert not should_shadow_instead_of_live(
        cfg,
        reduce_only=False,
        position_qty=Decimal("0"),
        indicators={"discovery_source": "discovery:futuresGainers"},
        confidence=Decimal("0.9"),
        approved=True,
        trade_learning_shadow=False,
    )

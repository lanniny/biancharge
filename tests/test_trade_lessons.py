"""Tests for trade_lessons pattern gates."""

from __future__ import annotations

from decimal import Decimal
import unittest

from trade_lessons import (
    build_lessons_document,
    lesson_rule_status,
    trade_lesson_block_reasons,
    trade_lessons_from_config,
)


def test_block_short_on_pump():
    cfg = trade_lessons_from_config({"enabled": True})
    reasons = trade_lesson_block_reasons(
        cfg=cfg,
        order_action="SELL",
        reduce_only=False,
        position_qty=Decimal("0"),
        regime="trend_down",
        change_24h=Decimal("0.96"),
        momentum=Decimal("0.03"),
        rsi=Decimal("40"),
        confidence=Decimal("0.93"),
        bucket="futuresGainers",
    )
    assert any("block short on pump" in r for r in reasons)


def test_block_long_chase_weak_momentum():
    cfg = trade_lessons_from_config({"enabled": True})
    reasons = trade_lesson_block_reasons(
        cfg=cfg,
        order_action="BUY",
        reduce_only=False,
        position_qty=Decimal("0"),
        regime="trend_up",
        change_24h=Decimal("0.16"),
        momentum=Decimal("0.001"),
        rsi=Decimal("50"),
        confidence=Decimal("0.95"),
        bucket="futuresGainers",
    )
    assert any("gainer chase blocked" in r for r in reasons)


def test_block_short_in_squeeze_losers_allowed():
    cfg = trade_lessons_from_config({"enabled": True, "block_short_in_squeeze": True})
    reasons = trade_lesson_block_reasons(
        cfg=cfg,
        order_action="SELL",
        reduce_only=False,
        position_qty=Decimal("0"),
        regime="squeeze",
        change_24h=Decimal("-0.10"),
        momentum=Decimal("-0.02"),
        rsi=Decimal("45"),
        confidence=Decimal("0.90"),
        bucket="futuresLosers",
    )
    assert not any("squeeze" in r for r in reasons)


def test_block_short_in_squeeze_losers_trend_short_allowed():
    cfg = trade_lessons_from_config({"enabled": True, "block_short_in_squeeze": True})
    reasons = trade_lesson_block_reasons(
        cfg=cfg,
        order_action="SELL",
        reduce_only=False,
        position_qty=Decimal("0"),
        regime="squeeze",
        change_24h=Decimal("-0.10"),
        momentum=Decimal("-0.02"),
        rsi=Decimal("45"),
        confidence=Decimal("0.90"),
        bucket="futuresLosers",
        entry_quadrant="trend_short",
    )
    assert not any("squeeze" in r for r in reasons)


def test_chase_long_fusion_bypass():
    cfg = trade_lessons_from_config(
        {"enabled": True, "chase_long_fusion_bypass": "0.55"}
    )
    reasons = trade_lesson_block_reasons(
        cfg=cfg,
        order_action="BUY",
        reduce_only=False,
        position_qty=Decimal("0"),
        regime="trend_up",
        change_24h=Decimal("0.16"),
        momentum=Decimal("0.001"),
        rsi=Decimal("50"),
        confidence=Decimal("0.80"),
        bucket="futuresGainers",
        fusion_bull_pct=Decimal("0.60"),
        entry_quadrant="trend_long",
    )
    assert not any("gainer chase blocked" in r for r in reasons)


def test_build_lessons_from_portal_loss():
    outcomes = [
        {
            "symbol": "PORTALUSDT",
            "positionSide": "LONG",
            "realizedPnl": "-0.5171",
            "openContext": {
                "bucket": "futuresGainers",
                "regime": "trend_up",
                "priceChangePct24h": "0.15862",
                "momentum": "0.001183",
                "confidence": "0.95",
                "rsi": "49.5",
            },
        }
    ]
    doc = build_lessons_document(outcomes)
    assert doc["summary"]["losses"] == 1
    assert "long_chase_weak_momentum" in doc["losses"][0]["tags"]


def test_early_trend_long_win_tagged():
    outcomes = [
        {
            "symbol": "REUSDT",
            "positionSide": "LONG",
            "realizedPnl": "3.3665",
            "openContext": {
                "bucket": "futuresGainers",
                "regime": "trend_up",
                "priceChangePct24h": "0.12",
                "confidence": "0.99",
                "momentum": "0.04",
                "rsi": "58",
            },
        }
    ]
    doc = build_lessons_document(outcomes)
    assert doc["summary"]["wins"] == 1
    assert "early_trend_long" in doc["wins"][0]["tags"]


def test_single_portal_loss_rule_stats_only_observing():
    cfg = trade_lessons_from_config({"enabled": True, "min_lesson_sample_for_block": 12})
    outcomes = [
        {
            "symbol": "PORTALUSDT",
            "positionSide": "LONG",
            "realizedPnl": "-0.5171",
            "openContext": {
                "bucket": "futuresGainers",
                "regime": "trend_up",
                "priceChangePct24h": "0.15862",
                "momentum": "0.001183",
                "confidence": "0.95",
                "rsi": "49.5",
            },
        }
    ]
    doc = build_lessons_document(outcomes, cfg)
    stat = doc["ruleStats"]["long_chase_gainer_weak_momentum"]

    assert stat["sampleSize"] == 1
    assert stat["status"] == "observing"
    assert not any(rule["id"] == "long_chase_gainer_weak_momentum" for rule in doc["activeRules"])


def test_sample_aware_rule_does_not_block_before_min_sample():
    cfg = trade_lessons_from_config({"enabled": True, "min_lesson_sample_for_block": 12})
    lesson_stats = {
        "long_chase_gainer_weak_momentum": {
            "sampleSize": 1,
            "winRate": "0.00",
            "totalPnl": "-0.5000",
            "profitFactor": "0.00",
        }
    }
    reasons = trade_lesson_block_reasons(
        cfg=cfg,
        order_action="BUY",
        reduce_only=False,
        position_qty=Decimal("0"),
        regime="trend_up",
        change_24h=Decimal("0.16"),
        momentum=Decimal("0.001"),
        rsi=Decimal("50"),
        confidence=Decimal("0.95"),
        bucket="futuresGainers",
        lesson_stats=lesson_stats,
    )

    assert not any("gainer chase blocked" in r for r in reasons)


def test_sample_aware_rule_blocks_after_negative_expectancy_min_sample():
    cfg = trade_lessons_from_config({"enabled": True, "min_lesson_sample_for_block": 12})
    lesson_stats = {
        "short_from_gainers_bucket": {
            "sampleSize": 15,
            "winRate": "0.20",
            "totalPnl": "-3.5000",
            "profitFactor": "0.40",
        }
    }
    reasons = trade_lesson_block_reasons(
        cfg=cfg,
        order_action="SELL",
        reduce_only=False,
        position_qty=Decimal("0"),
        regime="trend_down",
        change_24h=Decimal("0.20"),
        momentum=Decimal("-0.01"),
        rsi=Decimal("45"),
        confidence=Decimal("0.99"),
        bucket="futuresGainers",
        lesson_stats=lesson_stats,
    )

    assert any("gainers bucket" in r for r in reasons)
    assert "n=15" in " ".join(reasons)


def test_profitable_edge_rule_status_overrides_low_win_rate():
    cfg = trade_lessons_from_config({"enabled": True, "min_lesson_sample_for_block": 12})
    stat = {
        "sampleSize": 15,
        "winRate": "0.20",
        "totalPnl": "2.5000",
        "profitFactor": "1.50",
    }

    assert lesson_rule_status(stat, cfg) == "allowed_profitable_edge"


def test_block_short_from_gainers_bucket():
    cfg = trade_lessons_from_config({"enabled": True})
    reasons = trade_lesson_block_reasons(
        cfg=cfg,
        order_action="SELL",
        reduce_only=False,
        position_qty=Decimal("0"),
        regime="trend_down",
        change_24h=Decimal("0.20"),
        momentum=Decimal("-0.01"),
        rsi=Decimal("45"),
        confidence=Decimal("0.99"),
        bucket="futuresGainers",
    )
    assert any("gainers bucket" in r for r in reasons)


def test_block_short_positive_momentum():
    cfg = trade_lessons_from_config({"enabled": True})
    reasons = trade_lesson_block_reasons(
        cfg=cfg,
        order_action="SELL",
        reduce_only=False,
        position_qty=Decimal("0"),
        regime="trend_down",
        change_24h=Decimal("0.10"),
        momentum=Decimal("0.012"),
        rsi=Decimal("48"),
        confidence=Decimal("0.77"),
        bucket="futuresLosers",
    )
    assert any("momentum" in r and "block short" in r for r in reasons)


class TradeLessonsUnittestCoverage(unittest.TestCase):
    def test_single_portal_loss_rule_stats_only_observing(self) -> None:
        cfg = trade_lessons_from_config({"enabled": True, "min_lesson_sample_for_block": 12})
        doc = build_lessons_document(
            [
                {
                    "symbol": "PORTALUSDT",
                    "positionSide": "LONG",
                    "realizedPnl": "-0.5171",
                    "openContext": {
                        "bucket": "futuresGainers",
                        "regime": "trend_up",
                        "priceChangePct24h": "0.15862",
                        "momentum": "0.001183",
                        "confidence": "0.95",
                        "rsi": "49.5",
                    },
                }
            ],
            cfg,
        )

        stat = doc["ruleStats"]["long_chase_gainer_weak_momentum"]
        self.assertEqual(stat["sampleSize"], 1)
        self.assertEqual(stat["status"], "observing")
        self.assertFalse(any(rule["id"] == "long_chase_gainer_weak_momentum" for rule in doc["activeRules"]))

    def test_sample_aware_rule_does_not_block_before_min_sample(self) -> None:
        cfg = trade_lessons_from_config({"enabled": True, "min_lesson_sample_for_block": 12})
        reasons = trade_lesson_block_reasons(
            cfg=cfg,
            order_action="BUY",
            reduce_only=False,
            position_qty=Decimal("0"),
            regime="trend_up",
            change_24h=Decimal("0.16"),
            momentum=Decimal("0.001"),
            rsi=Decimal("50"),
            confidence=Decimal("0.95"),
            bucket="futuresGainers",
            lesson_stats={
                "long_chase_gainer_weak_momentum": {
                    "sampleSize": 1,
                    "winRate": "0.00",
                    "totalPnl": "-0.5000",
                    "profitFactor": "0.00",
                }
            },
        )

        self.assertFalse(any("gainer chase blocked" in r for r in reasons))

    def test_sample_aware_rule_blocks_after_negative_expectancy_min_sample(self) -> None:
        cfg = trade_lessons_from_config({"enabled": True, "min_lesson_sample_for_block": 12})
        reasons = trade_lesson_block_reasons(
            cfg=cfg,
            order_action="SELL",
            reduce_only=False,
            position_qty=Decimal("0"),
            regime="trend_down",
            change_24h=Decimal("0.20"),
            momentum=Decimal("-0.01"),
            rsi=Decimal("45"),
            confidence=Decimal("0.99"),
            bucket="futuresGainers",
            lesson_stats={
                "short_from_gainers_bucket": {
                    "sampleSize": 15,
                    "winRate": "0.20",
                    "totalPnl": "-3.5000",
                    "profitFactor": "0.40",
                }
            },
        )

        self.assertTrue(any("gainers bucket" in r for r in reasons))
        self.assertIn("n=15", " ".join(reasons))

    def test_profitable_edge_rule_status_overrides_low_win_rate(self) -> None:
        cfg = trade_lessons_from_config({"enabled": True, "min_lesson_sample_for_block": 12})
        stat = {
            "sampleSize": 15,
            "winRate": "0.20",
            "totalPnl": "2.5000",
            "profitFactor": "1.50",
        }

        self.assertEqual(lesson_rule_status(stat, cfg), "allowed_profitable_edge")

    def test_lessons_document_prefers_net_pnl_over_gross_realized_pnl(self) -> None:
        cfg = trade_lessons_from_config({"enabled": True, "min_lesson_sample_for_block": 12})
        doc = build_lessons_document(
            [
                {
                    "symbol": "COSTUSDT",
                    "positionSide": "LONG",
                    "realizedPnl": "0.1000",
                    "netPnl": "-0.0500",
                    "openContext": {
                        "bucket": "futuresGainers",
                        "regime": "trend_up",
                        "priceChangePct24h": "0.20",
                        "momentum": "0.001",
                        "confidence": "0.80",
                        "rsi": "55",
                    },
                }
            ],
            cfg,
        )

        self.assertEqual(doc["summary"]["wins"], 0)
        self.assertEqual(doc["summary"]["losses"], 1)
        self.assertEqual(doc["summary"]["totalPnl"], "-0.0500")
        self.assertEqual(doc["ruleStats"]["long_chase_gainer_weak_momentum"]["totalPnl"], "-0.0500")

    def test_high_confidence_loser_long_bypass_disabled_for_confirmed_negative_edge(self) -> None:
        cfg = trade_lessons_from_config(
            {
                "enabled": True,
                "min_lesson_sample_for_block": 12,
                "long_losers_bypass_confidence": "0.95",
            }
        )
        reasons = trade_lesson_block_reasons(
            cfg=cfg,
            order_action="BUY",
            reduce_only=False,
            position_qty=Decimal("0"),
            regime="trend_down",
            change_24h=Decimal("-0.12"),
            momentum=Decimal("0.02"),
            rsi=Decimal("28"),
            confidence=Decimal("0.99"),
            bucket="futuresLosers",
            lesson_stats={
                "long_from_losers_bucket": {
                    "sampleSize": 31,
                    "winRate": "0.29",
                    "totalPnl": "-1.4500",
                    "profitFactor": "0.70",
                }
            },
        )

        self.assertTrue(any("losers bucket" in r for r in reasons))
        self.assertIn("confirmed negative expectancy", " ".join(reasons))

    def test_short_requires_negative_momentum_when_threshold_configured(self) -> None:
        cfg = trade_lessons_from_config(
            {
                "enabled": True,
                "block_short_positive_momentum": True,
                "min_momentum_for_short": "-0.002",
            }
        )
        reasons = trade_lesson_block_reasons(
            cfg=cfg,
            order_action="SELL",
            reduce_only=False,
            position_qty=Decimal("0"),
            regime="trend_down",
            change_24h=Decimal("-0.08"),
            momentum=Decimal("-0.001"),
            rsi=Decimal("48"),
            confidence=Decimal("0.90"),
            bucket="futuresLosers",
        )
        self.assertTrue(any("momentum" in r and "block short" in r for r in reasons))

    def test_strong_negative_momentum_short_passes_momentum_gate(self) -> None:
        cfg = trade_lessons_from_config(
            {
                "enabled": True,
                "block_short_positive_momentum": True,
                "min_momentum_for_short": "-0.002",
            }
        )
        reasons = trade_lesson_block_reasons(
            cfg=cfg,
            order_action="SELL",
            reduce_only=False,
            position_qty=Decimal("0"),
            regime="trend_down",
            change_24h=Decimal("-0.08"),
            momentum=Decimal("-0.01"),
            rsi=Decimal("48"),
            confidence=Decimal("0.90"),
            bucket="futuresLosers",
        )
        self.assertFalse(any("momentum" in r and "block short" in r for r in reasons))

    def test_short_momentum_gate_uses_configured_threshold_in_message(self) -> None:
        cfg = trade_lessons_from_config(
            {
                "enabled": True,
                "block_short_positive_momentum": True,
                "min_momentum_for_short": "-0.002",
            }
        )
        reasons = trade_lesson_block_reasons(
            cfg=cfg,
            order_action="SELL",
            reduce_only=False,
            position_qty=Decimal("0"),
            regime="trend_down",
            change_24h=Decimal("-0.08"),
            momentum=Decimal("-0.001"),
            rsi=Decimal("48"),
            confidence=Decimal("0.90"),
            bucket="futuresLosers",
        )
        joined = " ".join(reasons)
        self.assertIn("required short momentum -0.20%", joined)
        self.assertNotIn("> 0", joined)

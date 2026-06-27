"""Tests for bucket_strategy."""

from decimal import Decimal
import unittest

from bucket_strategy import (
    bucket_open_block_reasons,
    bucket_strategy_from_config,
)


def test_gainer_long_requires_5m_bullish():
    cfg = bucket_strategy_from_config({"enabled": True})
    reasons = bucket_open_block_reasons(
        cfg=cfg,
        bucket="futuresGainers",
        source="discovery:futuresGainers",
        order_action="BUY",
        reduce_only=False,
        indicators={"mtf_5m": "neutral", "fusion_bull_pct": "0.60"},
        confidence=Decimal("0.80"),
        base_min_confidence=Decimal("0.65"),
    )
    assert any("5m bullish" in r for r in reasons)


def test_reduce_skips_bucket_gates():
    cfg = bucket_strategy_from_config({"enabled": True})
    reasons = bucket_open_block_reasons(
        cfg=cfg,
        bucket="futuresGainers",
        source="discovery:futuresGainers",
        order_action="SELL",
        reduce_only=True,
        indicators={"mtf_5m": "neutral"},
        confidence=Decimal("0.50"),
        base_min_confidence=Decimal("0.65"),
    )
    assert reasons == []


def test_pinned_policy_can_require_mtf_and_fusion():
    cfg = bucket_strategy_from_config(
        {
            "enabled": True,
            "policies": {
                "pinned": {
                    "require_mtf_5m_align": True,
                    "require_mtf_5m_align_short": True,
                    "min_fusion_bull_pct": "0.55",
                    "min_fusion_bear_pct": "0.55",
                }
            },
        }
    )
    long_reasons = bucket_open_block_reasons(
        cfg=cfg,
        bucket="",
        source="pinned",
        order_action="BUY",
        reduce_only=False,
        indicators={"mtf_5m": "neutral", "fusion_bull_pct": "0.49"},
        confidence=Decimal("0.80"),
        base_min_confidence=Decimal("0.62"),
    )
    short_reasons = bucket_open_block_reasons(
        cfg=cfg,
        bucket="pinned",
        source="pinned",
        order_action="SELL",
        reduce_only=False,
        indicators={"mtf_5m": "neutral", "fusion_bull_pct": "0.60"},
        confidence=Decimal("0.80"),
        base_min_confidence=Decimal("0.65"),
    )
    assert any("5m bullish" in r for r in long_reasons)
    assert any("fusion bull" in r for r in long_reasons)
    assert any("5m bearish" in r for r in short_reasons)
    assert any("fusion bear" in r for r in short_reasons)


class BucketStrategyUnittestCoverage(unittest.TestCase):
    def test_pinned_policy_can_require_mtf_and_fusion(self) -> None:
        cfg = bucket_strategy_from_config(
            {
                "enabled": True,
                "policies": {
                    "pinned": {
                        "require_mtf_5m_align": True,
                        "require_mtf_5m_align_short": True,
                        "min_fusion_bull_pct": "0.55",
                        "min_fusion_bear_pct": "0.55",
                    }
                },
            }
        )
        long_reasons = bucket_open_block_reasons(
            cfg=cfg,
            bucket="",
            source="pinned",
            order_action="BUY",
            reduce_only=False,
            indicators={"mtf_5m": "neutral", "fusion_bull_pct": "0.49"},
            confidence=Decimal("0.80"),
            base_min_confidence=Decimal("0.62"),
        )
        short_reasons = bucket_open_block_reasons(
            cfg=cfg,
            bucket="pinned",
            source="pinned",
            order_action="SELL",
            reduce_only=False,
            indicators={"mtf_5m": "neutral", "fusion_bull_pct": "0.60"},
            confidence=Decimal("0.80"),
            base_min_confidence=Decimal("0.65"),
        )
        self.assertTrue(any("5m bullish" in r for r in long_reasons))
        self.assertTrue(any("fusion bull" in r for r in long_reasons))
        self.assertTrue(any("5m bearish" in r for r in short_reasons))
        self.assertTrue(any("fusion bear" in r for r in short_reasons))

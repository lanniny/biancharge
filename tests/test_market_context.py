"""Tests for market_context module."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from market_context import (
    evaluate_market_context,
    funding_window_state,
    market_context_block_reason,
    market_context_from_config,
    session_phase_label,
)


def test_session_phase_asia():
    # 2026-06-19 02:00 UTC -> asia
    result = session_phase_label(1750298400)
    assert "asia" in result["activeSessions"]


def test_funding_window_mid():
    result = funding_window_state(1750298400, guard_minutes=30)
    assert result["phase"] in {"pre", "post", "mid"}
    assert "minutesToFunding" in result


def test_market_context_disabled():
    cfg = market_context_from_config({"enabled": False})
    result = evaluate_market_context(cfg)
    assert result == {"enabled": False}


@patch("market_context.fetch_btc_macro_bias")
@patch("market_context.fetch_fear_greed_index")
def test_evaluate_market_context_cached(mock_fng, mock_btc):
    import market_context as mc

    mc._CACHE.clear()
    mock_fng.return_value = {"enabled": True, "value": 50, "bias": "neutral"}
    mock_btc.return_value = {"symbol": "BTCUSDT", "bias": "neutral", "changePct": "0.01"}
    cfg = market_context_from_config({"enabled": True, "cache_seconds": 3600})
    first = evaluate_market_context(cfg, timestamp=1000)
    second = evaluate_market_context(cfg, timestamp=1001)
    assert first["enabled"] is True
    assert second is first
    assert mock_fng.call_count == 1


def test_market_context_rationale_fields():
    from market_context import market_context_rationale_block

    ctx = {
        "enabled": True,
        "session": {"label": "亚盘", "primary": "asia"},
        "sentiment": {"value": 30, "bias": "fear"},
        "macro": {"bias": "risk_off", "label": "BTC偏弱"},
    }
    block = market_context_rationale_block(ctx)
    assert block["session"]["label"] == "亚盘"
    assert block["sentiment"]["value"] == 30


class MarketContextUnittestCoverage(unittest.TestCase):
    def test_session_guard_blocks_configured_new_open_session(self) -> None:
        ctx = {
            "enabled": True,
            "session": {"label": "欧盘", "primary": "europe"},
        }
        reason = market_context_block_reason(
            ctx,
            is_reduce_only=False,
            block_pre_funding=False,
            block_sessions=("europe",),
        )
        self.assertIsNotNone(reason)
        self.assertIn("Session guard", reason)
        self.assertIn("configured as a blocked open session", reason)

    def test_dynamic_session_guard_requires_negative_sample(self) -> None:
        ctx = {
            "enabled": True,
            "session": {"label": "欧盘", "primary": "europe"},
        }
        reason = market_context_block_reason(
            ctx,
            is_reduce_only=False,
            block_pre_funding=False,
            block_sessions=("europe",),
            trade_learning={
                "enabled": True,
                "sessionStats": {
                    "europe": {
                        "sampleSize": 8,
                        "winRate": "0.25",
                        "profitFactor": "0.50",
                        "totalPnl": "-1.2500",
                    }
                },
            },
            session_guard_dynamic_enabled=True,
            session_guard_min_sample=6,
        )
        self.assertIsNotNone(reason)
        self.assertIn("live edge weak", reason)

    def test_dynamic_session_guard_does_not_block_small_sample(self) -> None:
        ctx = {
            "enabled": True,
            "session": {"label": "欧盘", "primary": "europe"},
        }
        reason = market_context_block_reason(
            ctx,
            is_reduce_only=False,
            block_pre_funding=False,
            block_sessions=("europe",),
            trade_learning={
                "enabled": True,
                "sessionStats": {
                    "europe": {
                        "sampleSize": 2,
                        "winRate": "0.00",
                        "profitFactor": "0.00",
                        "totalPnl": "-0.2000",
                    }
                },
            },
            session_guard_dynamic_enabled=True,
            session_guard_min_sample=6,
        )
        self.assertIsNone(reason)

    def test_dynamic_session_guard_does_not_block_positive_edge(self) -> None:
        ctx = {
            "enabled": True,
            "session": {"label": "重叠时段(欧盘/美盘)", "primary": "overlap_europe_us"},
        }
        reason = market_context_block_reason(
            ctx,
            is_reduce_only=False,
            block_pre_funding=False,
            block_sessions=("overlap_europe_us",),
            trade_learning={
                "enabled": True,
                "sessionStats": {
                    "overlap_europe_us": {
                        "sampleSize": 8,
                        "winRate": "0.25",
                        "profitFactor": "1.10",
                        "totalPnl": "0.0500",
                    }
                },
            },
            session_guard_dynamic_enabled=True,
            session_guard_min_sample=6,
        )
        self.assertIsNone(reason)

    def test_session_guard_does_not_block_reduce_only(self) -> None:
        ctx = {
            "enabled": True,
            "session": {"label": "欧盘", "primary": "europe"},
        }
        reason = market_context_block_reason(
            ctx,
            is_reduce_only=True,
            block_pre_funding=False,
            block_sessions=("europe",),
        )
        self.assertIsNone(reason)

    def test_config_parses_blocked_sessions(self) -> None:
        cfg = market_context_from_config({"block_new_opens_sessions": ["europe", "overlap_europe_us"]})
        self.assertEqual(cfg.block_new_opens_sessions, ("europe", "overlap_europe_us"))

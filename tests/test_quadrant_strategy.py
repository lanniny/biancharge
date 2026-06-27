"""Tests for four-quadrant entry modes."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
import unittest

from entry_economics import discovery_short_block_reason
from quadrant_strategy import (
    QUADRANT_REVERSAL_LONG,
    QUADRANT_REVERSAL_SHORT,
    QUADRANT_TREND_LONG,
    apply_quadrant_signal,
    macro_gainer_long_block_reason,
    quadrant_should_shadow,
    quadrant_strategy_from_config,
)


def _signal(action: str, conf: str, indicators: dict) -> SimpleNamespace:
    return SimpleNamespace(
        action=action,
        confidence=Decimal(conf),
        reasons=["base"],
        warnings=list(indicators.get("_warnings", [])),
        indicators={k: v for k, v in indicators.items() if k != "_warnings"},
    )


def test_trend_long_quadrant_tagged():
    cfg = quadrant_strategy_from_config({"enabled": True})
    indicators = {
        "regime": "trend_up",
        "rsi": "60",
        "mtf_5m": "bullish",
        "momentum": "0.02",
    }
    out = apply_quadrant_signal(_signal("BUY", "0.80", indicators), cfg, bucket="futuresGainers")
    assert out.action == "BUY"
    assert out.indicators["entry_quadrant"] == QUADRANT_TREND_LONG


def test_trend_short_only_on_losers_bucket():
    cfg = quadrant_strategy_from_config({"enabled": True})
    indicators = {"regime": "trend_down", "rsi": "35", "mtf_5m": "bearish", "momentum": "-0.02"}
    gainers = apply_quadrant_signal(
        _signal("SELL", "0.85", indicators),
        cfg,
        bucket="futuresGainers",
        source="discovery:futuresGainers",
    )
    assert gainers.indicators.get("entry_quadrant") != "trend_short"
    losers = apply_quadrant_signal(
        _signal("SELL", "0.85", indicators),
        cfg,
        bucket="futuresLosers",
        source="discovery:futuresLosers",
    )
    assert losers.indicators["entry_quadrant"] == "trend_short"


def test_reversal_short_on_tradfi_overbought():
    cfg = quadrant_strategy_from_config({"enabled": True})
    indicators = {
        "regime": "trend_up",
        "rsi": "82",
        "mtf_5m": "bearish",
        "momentum": "-0.005",
    }
    out = apply_quadrant_signal(
        _signal("HOLD", "0.50", indicators),
        cfg,
        bucket="futuresTradFi",
        source="discovery:futuresTradFi",
    )
    assert out.action == "SELL"
    assert out.indicators["entry_quadrant"] == QUADRANT_REVERSAL_SHORT
    assert out.indicators["entry_quadrant_mode"] == "shadow"


def test_reversal_short_preempts_trend_long_on_extreme_rsi_mtf_flip():
    cfg = quadrant_strategy_from_config({"enabled": True})
    indicators = {
        "regime": "trend_up",
        "rsi": "84",
        "mtf_5m": "bearish",
        "momentum": "-0.002",
    }
    out = apply_quadrant_signal(
        _signal("BUY", "0.82", indicators),
        cfg,
        bucket="futuresTopVolume",
        source="discovery:futuresTopVolume",
    )
    assert out.action == "SELL"
    assert out.indicators["entry_quadrant"] == QUADRANT_REVERSAL_SHORT
    assert out.indicators["entry_quadrant_mode"] == "shadow"


def test_reversal_short_blocked_on_gainers():
    cfg = quadrant_strategy_from_config({"enabled": True})
    indicators = {"regime": "trend_up", "rsi": "85", "mtf_5m": "bearish", "momentum": "-0.01"}
    out = apply_quadrant_signal(
        _signal("HOLD", "0.50", indicators),
        cfg,
        bucket="futuresGainers",
    )
    assert out.action == "HOLD"


def test_trend_long_not_preempted_without_required_mtf_flip():
    cfg = quadrant_strategy_from_config({"enabled": True})
    indicators = {
        "regime": "trend_up",
        "rsi": "84",
        "mtf_5m": "bullish",
        "momentum": "-0.002",
    }
    out = apply_quadrant_signal(
        _signal("BUY", "0.82", indicators),
        cfg,
        bucket="futuresTopVolume",
        source="discovery:futuresTopVolume",
    )
    assert out.action == "BUY"
    assert out.indicators["entry_quadrant"] == QUADRANT_TREND_LONG


def test_reversal_long_oversold_downtrend():
    cfg = quadrant_strategy_from_config({"enabled": True})
    indicators = {
        "regime": "trend_down",
        "rsi": "28",
        "mtf_5m": "bullish",
        "momentum": "-0.02",
        "_warnings": ["downtrend_regime"],
    }
    out = apply_quadrant_signal(
        _signal("HOLD", "0.40", indicators),
        cfg,
        bucket="futuresLosers",
    )
    assert out.action == "BUY"
    assert out.indicators["entry_quadrant"] == QUADRANT_REVERSAL_LONG
    assert "downtrend_regime" not in out.warnings


def test_reversal_long_preempts_trend_short_on_oversold_mtf_flip():
    cfg = quadrant_strategy_from_config({"enabled": True})
    indicators = {
        "regime": "trend_down",
        "rsi": "27",
        "mtf_5m": "bullish",
        "momentum": "-0.01",
        "_warnings": ["downtrend_regime"],
    }
    out = apply_quadrant_signal(
        _signal("SELL", "0.86", indicators),
        cfg,
        bucket="futuresLosers",
        source="discovery:futuresLosers",
    )
    assert out.action == "BUY"
    assert out.indicators["entry_quadrant"] == QUADRANT_REVERSAL_LONG
    assert out.indicators["entry_quadrant_mode"] == "shadow"
    assert "downtrend_regime" not in out.warnings


def test_discovery_short_bypass_for_reversal_short():
    reason = discovery_short_block_reason(
        discovery_short_mode="losers_trend_down",
        order_action="SELL",
        is_discovery_open=True,
        bucket="futuresTradFi",
        regime="trend_up",
        entry_quadrant="reversal_short",
    )
    assert reason is None


def test_macro_blocks_gainer_long_on_risk_off():
    cfg = quadrant_strategy_from_config({"enabled": True, "macro_block_gainer_long_on_risk_off": True})
    reason = macro_gainer_long_block_reason(
        {"macro": {"bias": "risk_off"}},
        order_action="BUY",
        bucket="futuresGainers",
        source="discovery:futuresGainers",
        cfg=cfg,
    )
    assert reason is not None
    assert "risk_off" in reason


def test_quadrant_reversal_routes_shadow():
    cfg = quadrant_strategy_from_config({"enabled": True})
    shadow, reason = quadrant_should_shadow(
        {"entry_quadrant": QUADRANT_REVERSAL_LONG, "entry_quadrant_mode": "shadow"},
        cfg,
    )
    assert shadow is True
    assert reason is not None


class QuadrantStrategyUnittestCoverage(unittest.TestCase):
    def test_reversal_short_preempts_trend_long_on_extreme_rsi_mtf_flip(self) -> None:
        cfg = quadrant_strategy_from_config({"enabled": True})
        out = apply_quadrant_signal(
            _signal(
                "BUY",
                "0.82",
                {
                    "regime": "trend_up",
                    "rsi": "84",
                    "mtf_5m": "bearish",
                    "momentum": "-0.002",
                },
            ),
            cfg,
            bucket="futuresTopVolume",
            source="discovery:futuresTopVolume",
        )

        self.assertEqual(out.action, "SELL")
        self.assertEqual(out.indicators["entry_quadrant"], QUADRANT_REVERSAL_SHORT)
        self.assertEqual(out.indicators["entry_quadrant_mode"], "shadow")

    def test_reversal_long_preempts_trend_short_on_oversold_mtf_flip(self) -> None:
        cfg = quadrant_strategy_from_config({"enabled": True})
        out = apply_quadrant_signal(
            _signal(
                "SELL",
                "0.86",
                {
                    "regime": "trend_down",
                    "rsi": "27",
                    "mtf_5m": "bullish",
                    "momentum": "-0.01",
                    "_warnings": ["downtrend_regime"],
                },
            ),
            cfg,
            bucket="futuresLosers",
            source="discovery:futuresLosers",
        )

        self.assertEqual(out.action, "BUY")
        self.assertEqual(out.indicators["entry_quadrant"], QUADRANT_REVERSAL_LONG)
        self.assertEqual(out.indicators["entry_quadrant_mode"], "shadow")
        self.assertNotIn("downtrend_regime", out.warnings)

    def test_trend_long_not_preempted_without_required_mtf_flip(self) -> None:
        cfg = quadrant_strategy_from_config({"enabled": True})
        out = apply_quadrant_signal(
            _signal(
                "BUY",
                "0.82",
                {
                    "regime": "trend_up",
                    "rsi": "84",
                    "mtf_5m": "bullish",
                    "momentum": "-0.002",
                },
            ),
            cfg,
            bucket="futuresTopVolume",
            source="discovery:futuresTopVolume",
        )

        self.assertEqual(out.action, "BUY")
        self.assertEqual(out.indicators["entry_quadrant"], QUADRANT_TREND_LONG)

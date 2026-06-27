"""Tests for smart entry timing (pullback limits)."""

from __future__ import annotations

from decimal import Decimal

from entry_timing import (
    entry_timing_defer_reason,
    entry_timing_from_config,
    resolve_entry_limit_price,
)
from quadrant_strategy import QUADRANT_TREND_LONG
from market_autotrader import (
    AutoExecutionConfig,
    AssetConfig,
    OrderIntent,
    StrategyConfig,
    enrich_order_intent,
    INTENT_OPEN_LONG,
    BUY,
)


def _indicators(**kwargs: str) -> dict[str, str]:
    base = {
        "atr_pct": "0.02",
        "fast_sma": "98",
        "mtf_5m": "bearish",
        "entry_quadrant": QUADRANT_TREND_LONG,
    }
    base.update(kwargs)
    return base


def test_trend_long_pullback_limit_below_market():
    cfg = entry_timing_from_config({"enabled": True, "mode": "smart_limit"})
    order_type, limit, notes = resolve_entry_limit_price(
        action="BUY",
        market_price=Decimal("100"),
        indicators=_indicators(mtf_5m="bearish"),
        cfg=cfg,
        fallback_offset_pct=Decimal("0.001"),
    )
    assert order_type == "LIMIT"
    assert limit is not None
    assert limit < Decimal("100")
    assert limit >= Decimal("98.8")
    assert any("pullback" in n.lower() for n in notes)


def test_trend_long_defer_when_require_pullback():
    cfg = entry_timing_from_config(
        {"enabled": True, "require_mtf_pullback_for_trend_long": True}
    )
    reason = entry_timing_defer_reason(
        action="BUY",
        indicators=_indicators(mtf_5m="bullish"),
        cfg=cfg,
        reduce_only=False,
    )
    assert reason is not None
    assert "pullback" in reason.lower()


def test_movement_aware_defers_overextended_long_without_rejecting_direction():
    cfg = entry_timing_from_config(
        {
            "enabled": True,
            "mode": "chase_fill",
            "movement_aware_enabled": True,
            "chase_max_momentum": "0.018",
        }
    )
    reason = entry_timing_defer_reason(
        action="BUY",
        indicators=_indicators(mtf_1m="bullish", mtf_5m="bullish", momentum="0.025"),
        cfg=cfg,
        reduce_only=False,
    )
    assert reason is not None
    assert "long direction kept" in reason
    assert "wait" in reason.lower()


def test_high_confidence_trend_continuation_can_chase_near_momentum_limit():
    cfg = entry_timing_from_config(
        {
            "enabled": True,
            "mode": "chase_fill",
            "movement_aware_enabled": True,
            "chase_max_momentum": "0.018",
            "chase_max_24h_change": "0.18",
            "strong_continuation_enabled": True,
            "strong_continuation_max_momentum": "0.025",
            "strong_continuation_max_24h_change": "0.25",
        }
    )
    indicators = _indicators(
        mtf_1m="bullish",
        mtf_5m="bullish",
        momentum="0.0186",
        price_change_pct_24h="0.181",
        rsi="68.34",
        volume_ratio="1.01",
        fusion_bull_pct="0.8367",
        kline_pattern_label="neutral",
    )
    reason = entry_timing_defer_reason(
        action="BUY",
        indicators=indicators,
        cfg=cfg,
        reduce_only=False,
        confidence=Decimal("0.99"),
    )
    assert reason is None
    assert indicators["entry_timing_strong_continuation"] == "true"
    assert "high-confidence trend continuation" in indicators["entry_timing_chase_override_reason"]


def test_high_confidence_continuation_still_blocks_extreme_24h_move():
    cfg = entry_timing_from_config(
        {
            "enabled": True,
            "mode": "chase_fill",
            "movement_aware_enabled": True,
            "chase_max_momentum": "0.018",
            "chase_max_24h_change": "0.18",
            "strong_continuation_enabled": True,
            "strong_continuation_max_momentum": "0.025",
            "strong_continuation_max_24h_change": "0.25",
        }
    )
    indicators = _indicators(
        mtf_1m="bullish",
        mtf_5m="bullish",
        momentum="0.020",
        price_change_pct_24h="0.31",
        rsi="68",
        volume_ratio="1.2",
        fusion_bull_pct="0.86",
    )
    reason = entry_timing_defer_reason(
        action="BUY",
        indicators=indicators,
        cfg=cfg,
        reduce_only=False,
        confidence=Decimal("0.99"),
    )
    assert reason is not None
    assert "24h move" in reason
    assert "entry_timing_strong_continuation" not in indicators


def test_high_confidence_continuation_still_blocks_overheated_rsi():
    cfg = entry_timing_from_config(
        {
            "enabled": True,
            "mode": "chase_fill",
            "movement_aware_enabled": True,
            "chase_max_momentum": "0.018",
            "strong_continuation_enabled": True,
            "strong_continuation_max_momentum": "0.025",
            "strong_continuation_max_rsi_long": "72",
        }
    )
    indicators = _indicators(
        mtf_1m="bullish",
        mtf_5m="bullish",
        momentum="0.020",
        price_change_pct_24h="0.19",
        rsi="74",
        volume_ratio="1.2",
        fusion_bull_pct="0.86",
    )
    reason = entry_timing_defer_reason(
        action="BUY",
        indicators=indicators,
        cfg=cfg,
        reduce_only=False,
        confidence=Decimal("0.99"),
    )
    assert reason is not None
    assert "momentum" in reason


def test_aave_like_late_gainer_waits_for_15m_confirmation():
    cfg = entry_timing_from_config(
        {
            "enabled": True,
            "mode": "chase_fill",
            "movement_aware_enabled": True,
            "late_chase_min_24h_change": "0.15",
            "require_15m_alignment_for_late_chase": True,
        }
    )
    indicators = _indicators(
        discovery_bucket="futuresGainers",
        mtf_1m="neutral",
        mtf_5m="bullish",
        mtf_15m="bearish",
        momentum="0.01572",
        price_change_pct_24h="0.17504",
        rsi="70.26",
        volume_ratio="1.2",
        fusion_bull_pct="0.7292",
    )
    reason = entry_timing_defer_reason(
        action="BUY",
        indicators=indicators,
        cfg=cfg,
        reduce_only=False,
        confidence=Decimal("0.95"),
    )
    assert reason is not None
    assert "15m bearish conflict" in reason


def test_ousdt_like_loser_long_waits_for_5m_reversal():
    cfg = entry_timing_from_config(
        {
            "enabled": True,
            "mode": "chase_fill",
            "movement_aware_enabled": True,
            "loser_reversal_min_24h_drop": "0.10",
            "require_5m_bullish_for_loser_long": True,
        }
    )
    indicators = _indicators(
        discovery_bucket="futuresLosers",
        mtf_1m="bullish",
        mtf_5m="neutral",
        mtf_15m="bullish",
        momentum="-0.00863",
        price_change_pct_24h="-0.18596",
        rsi="56.1",
    )
    reason = entry_timing_defer_reason(
        action="BUY",
        indicators=indicators,
        cfg=cfg,
        reduce_only=False,
        confidence=Decimal("0.99"),
    )
    assert reason is not None
    assert "losers-bucket long deferred" in reason
    assert "5m=neutral" in reason


def test_rif_like_loser_short_waits_after_oversold_drop():
    cfg = entry_timing_from_config(
        {
            "enabled": True,
            "mode": "chase_fill",
            "movement_aware_enabled": True,
            "loser_reversal_min_24h_drop": "0.10",
            "loser_short_oversold_rsi": "30",
        }
    )
    indicators = {
        "entry_quadrant": "trend_short",
        "discovery_bucket": "futuresLosers",
        "mtf_1m": "bearish",
        "mtf_5m": "bearish",
        "mtf_15m": "bearish",
        "momentum": "-0.0127",
        "price_change_pct_24h": "-0.1462",
        "rsi": "27.94",
        "atr_pct": "0.02",
    }
    reason = entry_timing_defer_reason(
        action="SELL",
        indicators=indicators,
        cfg=cfg,
        reduce_only=False,
        confidence=Decimal("1"),
    )
    assert reason is not None
    assert "losers-bucket short deferred" in reason


def test_high_confidence_continuation_can_accept_neutral_1m_with_quality_score():
    cfg = entry_timing_from_config(
        {
            "enabled": True,
            "mode": "chase_fill",
            "movement_aware_enabled": True,
            "chase_max_momentum": "0.018",
            "chase_max_24h_change": "0.18",
            "strong_continuation_enabled": True,
            "strong_continuation_min_volume_ratio": "0.95",
            "strong_continuation_max_momentum": "0.025",
            "strong_continuation_max_24h_change": "0.25",
            "strong_continuation_require_1m_alignment": False,
            "strong_continuation_min_quality_score": "0.78",
        }
    )
    indicators = _indicators(
        mtf_1m="neutral",
        mtf_5m="bullish",
        momentum="0.0213",
        price_change_pct_24h="0.2348",
        rsi="70.15",
        volume_ratio="0.99",
        fusion_bull_pct="0.8367",
    )
    reason = entry_timing_defer_reason(
        action="BUY",
        indicators=indicators,
        cfg=cfg,
        reduce_only=False,
        confidence=Decimal("0.99"),
    )
    assert reason is None
    assert indicators["entry_timing_strong_continuation"] == "true"
    assert Decimal(indicators["entry_timing_strong_continuation_score"]) >= Decimal("0.78")


def test_high_confidence_continuation_rejects_bearish_1m_even_when_relaxed():
    cfg = entry_timing_from_config(
        {
            "enabled": True,
            "mode": "chase_fill",
            "movement_aware_enabled": True,
            "chase_max_momentum": "0.018",
            "strong_continuation_enabled": True,
            "strong_continuation_min_volume_ratio": "0.95",
            "strong_continuation_max_momentum": "0.025",
            "strong_continuation_require_1m_alignment": False,
        }
    )
    indicators = _indicators(
        mtf_1m="bearish",
        mtf_5m="bullish",
        momentum="0.0213",
        price_change_pct_24h="0.20",
        rsi="68",
        volume_ratio="1.2",
        fusion_bull_pct="0.86",
    )
    reason = entry_timing_defer_reason(
        action="BUY",
        indicators=indicators,
        cfg=cfg,
        reduce_only=False,
        confidence=Decimal("0.99"),
    )
    assert reason is not None
    assert "1m turned bearish" in reason


def test_movement_aware_defers_short_when_1m_bounces_against_5m():
    cfg = entry_timing_from_config(
        {"enabled": True, "mode": "chase_fill", "movement_aware_enabled": True}
    )
    reason = entry_timing_defer_reason(
        action="SELL",
        indicators={
            "entry_quadrant": "trend_short",
            "mtf_1m": "bullish",
            "mtf_5m": "bearish",
            "momentum": "-0.01",
            "atr_pct": "0.02",
        },
        cfg=cfg,
        reduce_only=False,
    )
    assert reason is not None
    assert "short direction kept" in reason
    assert "bounce" in reason.lower()


def test_movement_aware_allows_order_when_not_extended():
    cfg = entry_timing_from_config(
        {"enabled": True, "mode": "chase_fill", "movement_aware_enabled": True}
    )
    reason = entry_timing_defer_reason(
        action="BUY",
        indicators=_indicators(mtf_1m="bullish", mtf_5m="bullish", momentum="0.006"),
        cfg=cfg,
        reduce_only=False,
    )
    assert reason is None


def test_reversal_short_limit_above_market():
    cfg = entry_timing_from_config({"enabled": True})
    order_type, limit, notes = resolve_entry_limit_price(
        action="SELL",
        market_price=Decimal("100"),
        indicators={
            "atr_pct": "0.02",
            "entry_quadrant": "reversal_short",
            "mtf_5m": "bearish",
        },
        cfg=cfg,
        fallback_offset_pct=Decimal("0.001"),
    )
    assert order_type == "LIMIT"
    assert limit is not None
    assert limit > Decimal("100")
    assert any("reversal" in n.lower() for n in notes)


def test_enrich_order_intent_uses_smart_limit():
    cfg = entry_timing_from_config({"enabled": True, "mode": "smart_limit"})
    auto = AutoExecutionConfig(entry_order_type="MARKET", auto_leverage=False)
    asset = AssetConfig(
        "BTCUSDT",
        "binance_futures",
        "BTC",
        "USDT",
        provider={"type": "static"},
    )
    strategy = StrategyConfig()
    order = OrderIntent(
        BUY,
        "BTCUSDT",
        "binance_futures",
        "USDT",
        Decimal("50"),
        Decimal("100"),
        intent_kind=INTENT_OPEN_LONG,
    )
    from types import SimpleNamespace

    signal = SimpleNamespace(
        indicators=_indicators(mtf_5m="bearish"),
        confidence=Decimal("0.8"),
    )
    enriched = enrich_order_intent(
        order,
        auto,
        strategy,
        asset,
        atr_pct=Decimal("0.02"),
        signal=signal,
        entry_timing_cfg=cfg,
    )
    assert enriched.order_type == "LIMIT"
    assert enriched.limit_price is not None
    assert enriched.limit_price < Decimal("100")

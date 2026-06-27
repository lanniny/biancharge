"""Tests for asset_profile leverage tiers and TradFi classification."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import Mock

from asset_profile import (
    ASSET_TRADFI_STOCK,
    classify_asset,
    is_tradfi_symbol,
    resolve_entry_leverage,
)
from market_autotrader import AutoExecutionConfig, Signal, SELL, BUY


def _auto_exec(**overrides) -> AutoExecutionConfig:
    base = AutoExecutionConfig(
        max_leverage=50,
        default_leverage=5,
        leverage_mid=12,
        leverage_high=25,
        leverage_extreme=50,
        max_leverage_crypto=50,
        max_leverage_tradfi_stock=20,
    )
    for key, value in overrides.items():
        object.__setattr__(base, key, value)
    return base


def test_classify_tradfi_stock():
    assert classify_asset("TSLAUSDT") == ASSET_TRADFI_STOCK
    assert classify_asset("BTCUSDT") != ASSET_TRADFI_STOCK
    assert is_tradfi_symbol("XAUUSDT")


def test_leverage_extreme_on_aligned_short():
    auto_exec = _auto_exec(account_max_leverage=0)
    signal = Mock(spec=Signal)
    signal.action = SELL
    signal.confidence = Decimal("0.93")
    indicators = {
        "regime": "trend_down",
        "mtf_1m": "bearish",
        "mtf_5m": "bearish",
        "mtf_15m": "bearish",
        "atr_pct": "0.02",
    }
    lev = resolve_entry_leverage(auto_exec, signal, indicators, "ETHUSDT")
    assert lev == 50


def test_leverage_capped_for_tradfi_stock():
    auto_exec = _auto_exec()
    signal = Mock(spec=Signal)
    signal.action = BUY
    signal.confidence = Decimal("0.95")
    indicators = {
        "regime": "trend_up",
        "mtf_1m": "bullish",
        "mtf_5m": "bullish",
        "mtf_15m": "bullish",
        "atr_pct": "0.01",
    }
    lev = resolve_entry_leverage(auto_exec, signal, indicators, "TSLAUSDT")
    assert lev <= 20


def test_account_max_leverage_cap():
    auto_exec = _auto_exec(account_max_leverage=20)
    signal = Mock(spec=Signal)
    signal.action = BUY
    signal.confidence = Decimal("0.95")
    indicators = {
        "regime": "trend_up",
        "mtf_1m": "bullish",
        "mtf_5m": "bullish",
        "mtf_15m": "bullish",
        "atr_pct": "0.01",
    }
    lev = resolve_entry_leverage(auto_exec, signal, indicators, "ETHUSDT")
    assert lev <= 20


def test_leverage_volatility_penalty():
    auto_exec = _auto_exec()
    signal = Mock(spec=Signal)
    signal.action = BUY
    signal.confidence = Decimal("0.93")
    indicators = {
        "regime": "trend_up",
        "mtf_1m": "bullish",
        "mtf_5m": "bullish",
        "mtf_15m": "bullish",
        "atr_pct": "0.12",
    }
    lev = resolve_entry_leverage(auto_exec, signal, indicators, "BTCUSDT")
    assert lev < 50

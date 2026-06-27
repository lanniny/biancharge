"""Reduce orders must not quantize to zero quantity."""

from decimal import Decimal
from unittest.mock import patch

from market_autotrader import (
    AssetConfig,
    AutoExecutionConfig,
    MarketBar,
    MarketSnapshot,
    OrderIntent,
    PaperPortfolio,
    Position,
    RiskConfig,
    Signal,
    StrategyConfig,
    build_futures_order_intent,
)


def test_reduce_short_never_zero_quote_when_step_large():
    asset = AssetConfig(
        symbol="GUAUSDT",
        market="binance_futures",
        base_asset="GUA",
        quote_asset="USDT",
        provider={},
    )
    portfolio = PaperPortfolio(
        cash={"USDT_FUTURES": Decimal("50")},
        positions={"GUAUSDT": Position(quantity=Decimal("-12"), average_price=Decimal("0.9078"))},
    )
    signal = Signal(
        "BUY",
        Decimal("0.75"),
        ["Holding priority: short skim"],
        [],
        {"exit_tier": "early"},
    )
    filters = {"step_size": Decimal("10"), "min_qty": Decimal("1")}
    with patch("market_autotrader.fetch_symbol_filters", return_value=filters):
        order = build_futures_order_intent(
            asset,
            "GUAUSDT",
            Decimal("0.86"),
            signal,
            StrategyConfig(holding_early_take_fraction=Decimal("0.30")),
            RiskConfig(),
            portfolio,
            auto_exec=AutoExecutionConfig(),
        )
    assert order is not None
    assert isinstance(order, OrderIntent)
    assert order.quote_amount > 0
    assert order.quantity is not None and order.quantity > 0

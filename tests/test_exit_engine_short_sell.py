"""Exit engine: SELL on short book triggers exit evaluation."""

from decimal import Decimal

from market_autotrader import (
    HOLD,
    SELL,
    AssetConfig,
    MarketBar,
    MarketSnapshot,
    PaperPortfolio,
    Position,
    RiskConfig,
    Signal,
    StrategyConfig,
)
from exit_engine import apply_holding_priority_signal


def test_short_sell_signal_still_triggers_force_cover():
    asset = AssetConfig(
        symbol="GUAUSDT",
        market="binance_futures",
        base_asset="GUA",
        quote_asset="USDT",
        provider={},
    )
    mark = Decimal("1.20")
    bar = MarketBar(
        timestamp=1,
        open=mark,
        high=mark,
        low=mark,
        close=mark,
        volume=Decimal("1"),
    )
    snapshot = MarketSnapshot(asset=asset, bars=[bar], observed_at=1000)
    portfolio = PaperPortfolio(
        cash={"USDT_FUTURES": Decimal("100")},
        positions={"GUAUSDT": Position(quantity=Decimal("-12"), average_price=Decimal("0.9078"))},
    )
    bearish = Signal(
        SELL,
        Decimal("0.8"),
        ["bearish"],
        ["downtrend_regime"],
        {"regime": "trend_down", "momentum": "-0.1", "mtf_5m": "bearish"},
    )
    risk = RiskConfig(max_position_loss_pct=Decimal("0.08"))
    out = apply_holding_priority_signal(snapshot, bearish, StrategyConfig(), portfolio, None, risk)
    assert out.action == "BUY"
    assert out.indicators.get("exit_tier") == "force"

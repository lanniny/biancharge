import unittest
from decimal import Decimal

from growth_sizing import (
    compute_open_notional,
    effective_max_daily_loss,
    growth_target_metrics,
    margin_required_for_notional,
    mark_prices_from_portfolio,
    portfolio_equity,
    portfolio_heat,
)
from market_autotrader import CASH_USDT_FUTURES, PaperPortfolio, Position, RiskConfig, StrategyConfig


class GrowthSizingTests(unittest.TestCase):
    def test_portfolio_equity_and_heat(self) -> None:
        portfolio = PaperPortfolio(
            cash={CASH_USDT_FUTURES: Decimal("11")},
            positions={
                "BTCUSDT": Position(
                    quantity=Decimal("0.01"),
                    average_price=Decimal("50000"),
                    initial_margin=Decimal("100"),
                    notional=Decimal("520"),
                    leverage=5,
                )
            },
            wallet_balance=Decimal("600"),
        )
        marks = mark_prices_from_portfolio(portfolio, "BTCUSDT", Decimal("52000"))
        equity = portfolio_equity(portfolio, marks)
        self.assertEqual(equity, Decimal("600"))
        heat = portfolio_heat(portfolio, marks)
        self.assertLess(heat, Decimal("0.2"))

    def test_compute_open_notional_scales_with_equity(self) -> None:
        risk = RiskConfig(
            scale_sizing_with_equity=True,
            risk_per_trade_pct=Decimal("0.02"),
            max_trade_quote=Decimal("1000"),
            min_trade_quote=Decimal("50"),
            max_position_quote=Decimal("5000"),
        )
        strategy = StrategyConfig(buy_quote_fraction=Decimal("0.5"), confidence_scale_sizing=False)
        notional = compute_open_notional(
            risk,
            strategy,
            equity=Decimal("10000"),
            available_margin=Decimal("3000"),
            signal_confidence=Decimal("0.7"),
            current_position_value=Decimal("0"),
        )
        self.assertEqual(notional, Decimal("200.00"))

    def test_compute_open_notional_leverage_allows_min_notional_with_low_margin(self) -> None:
        """48 USDT margin @ 5x can open 50 USDT notional (needs ~10 USDT margin)."""
        risk = RiskConfig(
            scale_sizing_with_equity=False,
            max_trade_quote=Decimal("800"),
            min_trade_quote=Decimal("50"),
            max_position_quote=Decimal("5000"),
        )
        strategy = StrategyConfig(buy_quote_fraction=Decimal("1.0"), confidence_scale_sizing=False)
        notional = compute_open_notional(
            risk,
            strategy,
            equity=Decimal("100"),
            available_margin=Decimal("48"),
            signal_confidence=Decimal("0.8"),
            current_position_value=Decimal("0"),
            leverage=5,
        )
        self.assertGreaterEqual(notional, Decimal("50"))
        self.assertLessEqual(margin_required_for_notional(notional, 5), Decimal("48"))

    def test_margin_required_for_notional(self) -> None:
        self.assertEqual(margin_required_for_notional(Decimal("50"), 5), Decimal("10"))

    def test_effective_max_daily_loss_pct(self) -> None:
        risk = RiskConfig(max_daily_loss_quote=Decimal("15"), max_daily_loss_pct=Decimal("0.02"))
        self.assertEqual(effective_max_daily_loss(risk, Decimal("1000")), Decimal("15"))

    def test_effective_max_daily_loss_floor(self) -> None:
        risk = RiskConfig(
            max_daily_loss_quote=Decimal("25"),
            max_daily_loss_pct=Decimal("0.025"),
            scale_sizing_with_equity=True,
            min_daily_loss_cap_quote=Decimal("5"),
        )
        self.assertEqual(effective_max_daily_loss(risk, Decimal("35")), Decimal("6.25"))

    def test_growth_target_metrics(self) -> None:
        metrics = growth_target_metrics(Decimal("15000"), Decimal("300000"))
        self.assertEqual(metrics["progressPct"], "0.0500")
        self.assertEqual(metrics["remainingQuote"], "285000.00")


if __name__ == "__main__":
    unittest.main()

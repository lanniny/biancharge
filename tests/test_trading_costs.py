import unittest
from decimal import Decimal

from market_autotrader import TradingMemory
from trading_costs import aggregate_income_rows, apply_new_income_rows


class TradingCostsTests(unittest.TestCase):
    def test_aggregate_income_rows_cost_totals(self) -> None:
        rows = [
            {"symbol": "SPCXUSDT", "incomeType": "FUNDING_FEE", "income": "-0.12", "time": 1_700_000_000_000, "tranId": 1},
            {"symbol": "SPCXUSDT", "incomeType": "COMMISSION", "income": "-0.03", "time": 1_700_000_100_000, "tranId": 2},
            {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "1.5", "time": 1_700_000_200_000, "tranId": 3},
        ]
        summary = aggregate_income_rows(rows)
        self.assertEqual(summary["lookbackRows"], 3)
        self.assertIn("FUNDING_FEE", summary["totalsByType"])
        self.assertEqual(summary["fundingBySymbol"]["SPCXUSDT"], "-0.12")

    def test_apply_new_income_rows_updates_memory_once(self) -> None:
        import time

        memory = TradingMemory()
        now_ms = int(time.time() * 1000)
        rows = [
            {"symbol": "SPCXUSDT", "incomeType": "FUNDING_FEE", "income": "-0.5", "time": now_ms, "tranId": 99},
            {"symbol": "SPCXUSDT", "incomeType": "FUNDING_FEE", "income": "-0.5", "time": now_ms, "tranId": 99},
        ]
        seen = apply_new_income_rows(memory, rows, set())
        self.assertIn("99", seen)
        self.assertEqual(memory.daily_funding_fee_today(), Decimal("0.5"))
        self.assertEqual(memory.total_daily_drag_today(), Decimal("0.5"))

    def test_total_daily_drag_includes_price_loss_and_costs(self) -> None:
        memory = TradingMemory()
        memory.record_loss(Decimal("2"))
        memory.record_commission(Decimal("0.4"))
        memory.record_income("FUNDING_FEE", Decimal("-0.6"))
        self.assertEqual(memory.total_daily_drag_today(), Decimal("3"))


if __name__ == "__main__":
    unittest.main()

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from review_events import append_reviews, newest_pending_events, review_event


def reserve_cash_event(event_id: str = "evt-1", available: str = "24") -> dict:
    return {
        "eventId": event_id,
        "createdAt": "2026-06-19T08:12:54+08:00",
        "priority": "high",
        "action": "RESERVE_CASH",
        "symbol": "USDT",
        "market": "futures",
        "reason": f"Futures available balance {available} is below reserve 30; do not open new leveraged trades.",
        "requiresModelReview": True,
        "realOrderAllowed": False,
        "context": {
            "account": {
                "futuresAccount": {
                    "availableBalance": available,
                    "totalMarginBalance": "100",
                    "totalUnrealizedProfit": "-35",
                },
                "futuresNonZeroPositions": [
                    {
                        "symbol": "SPCXUSDT",
                        "markPrice": "180",
                        "liquidationPrice": "157",
                    }
                ],
            }
        },
    }


def reduce_position_event(event_id: str = "evt-reduce", available: str = "22") -> dict:
    event = reserve_cash_event(event_id=event_id, available=available)
    event.update(
        {
            "action": "REDUCE_POSITION",
            "symbol": "SPCXUSDT",
            "market": "futures",
            "priority": "critical",
            "reason": "SPCXUSDT is near the configured reduce/danger zone.",
            "details": {"distanceToLiqPct": "0.06"},
        }
    )
    event["context"]["account"]["futuresNonZeroPositions"][0].update(
        {
            "positionAmt": "4.22",
            "entryPrice": "188.5",
            "unRealizedProfit": "-30",
        }
    )
    return event


def spot_buy_event(event_id: str = "evt-buy", free_usdt: str = "20", futures_available: str = "50") -> dict:
    return {
        "eventId": event_id,
        "createdAt": "2026-06-19T08:12:54+08:00",
        "priority": "medium",
        "action": "SPOT_BUY_APPROVAL",
        "symbol": "BTCUSDT",
        "market": "spot",
        "reason": "Spot signal is BUY with confidence 0.72; approval required before any real order.",
        "details": {"maxQuoteUSDT": "5", "price": "60000", "warnings": []},
        "requiresModelReview": True,
        "realOrderAllowed": False,
        "context": {
            "account": {
                "spotNonZeroBalances": [{"asset": "USDT", "free": free_usdt, "locked": "0", "total": free_usdt}],
                "futuresAccount": {"availableBalance": futures_available},
                "futuresNonZeroPositions": [],
            },
            "signal": {
                "symbol": "BTCUSDT",
                "market": "spot",
                "price": "60000",
                "action": "BUY",
                "confidence": "0.72",
                "warnings": [],
            },
        },
    }


def reassess_event(event_id: str = "evt-reassess") -> dict:
    event = reduce_position_event(event_id=event_id, available="50")
    event.update(
        {
            "action": "REASSESS_TAKE_PROFIT_OR_HOLD",
            "priority": "medium",
            "reason": "SPCXUSDT is near reassessment/entry zone.",
        }
    )
    event["context"]["account"]["futuresNonZeroPositions"][0].update(
        {
            "markPrice": "190",
            "breakEvenPrice": "187",
            "unRealizedProfit": "4",
        }
    )
    return event


def data_unavailable_event(event_id: str = "evt-data") -> dict:
    return {
        "eventId": event_id,
        "createdAt": "2026-06-19T08:12:54+08:00",
        "priority": "high",
        "action": "DATA_UNAVAILABLE",
        "symbol": "ALL",
        "market": "all",
        "reason": "One or more account or market data sources failed.",
        "details": {
            "accountDataError": True,
            "signalErrors": [{"symbol": "BTCUSDT", "market": "spot", "error": "network blocked"}],
        },
        "requiresModelReview": True,
        "realOrderAllowed": False,
        "context": {"account": {}, "signal": None},
    }


class ReviewEventsTests(unittest.TestCase):
    def test_reserve_cash_event_is_confirmed_when_available_balance_is_below_reserve(self) -> None:
        review = review_event(reserve_cash_event())

        self.assertEqual(review["verdict"], "confirm")
        self.assertFalse(review["realOrderAllowed"])
        self.assertIn("Do not open new leveraged positions.", review["manualPlan"])
        self.assertEqual(review["evidence"]["availableBalanceUSDT"], "24")

    def test_reserve_cash_event_is_downgraded_when_balance_recovered(self) -> None:
        review = review_event(reserve_cash_event(available="35"))

        self.assertEqual(review["verdict"], "downgrade")

    def test_reduce_position_event_gets_actionable_review(self) -> None:
        review = review_event(reduce_position_event())

        self.assertEqual(review["verdict"], "confirm")
        self.assertFalse(review["realOrderAllowed"])
        self.assertEqual(review["manualOrderPlan"]["side"], "平多 / 卖出 / reduce-only")
        self.assertIn("quantityHint", review["manualOrderPlan"])

    def test_spot_buy_approval_is_confirmed_when_cash_and_risk_pass(self) -> None:
        review = review_event(spot_buy_event())

        self.assertEqual(review["verdict"], "confirm")
        self.assertEqual(review["manualOrderPlan"]["venue"], "Binance 现货")
        self.assertEqual(review["manualOrderPlan"]["quoteAmountUSDT"], "5")

    def test_spot_buy_approval_is_blocked_when_futures_reserve_is_low(self) -> None:
        review = review_event(spot_buy_event(futures_available="20"))

        self.assertEqual(review["verdict"], "block")
        self.assertTrue(any("futures available balance" in item for item in review["evidence"]["blockers"]))

    def test_reassess_review_confirms_reduce_only_into_strength(self) -> None:
        review = review_event(reassess_event())

        self.assertEqual(review["verdict"], "confirm")
        self.assertEqual(review["manualOrderPlan"]["orderType"], "reduce-only partial close if manually approved")

    def test_data_unavailable_blocks_all_new_trades(self) -> None:
        review = review_event(data_unavailable_event())

        self.assertEqual(review["verdict"], "block")
        self.assertEqual(review["manualOrderPlan"]["side"], "不下单")
        self.assertIn("Do not place new spot or futures orders", review["manualPlan"][0])

    def test_newest_pending_events_skips_already_reviewed_events(self) -> None:
        with TemporaryDirectory() as tmp:
            events_path = Path(tmp) / "events.jsonl"
            reviews_path = Path(tmp) / "reviews.jsonl"
            events_path.write_text(
                "\n".join(
                    [
                        '{"eventId":"evt-1","createdAt":"2026-06-19T08:00:00+08:00","priority":"high"}',
                        '{"eventId":"evt-2","createdAt":"2026-06-19T08:01:00+08:00","priority":"high"}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            append_reviews(str(reviews_path), [{"eventId": "evt-1"}])

            pending = newest_pending_events(str(events_path), str(reviews_path), 5)

            self.assertEqual([event["eventId"] for event in pending], ["evt-2"])


if __name__ == "__main__":
    unittest.main()
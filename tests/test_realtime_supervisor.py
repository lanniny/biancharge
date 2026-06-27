import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from realtime_supervisor import SupervisorConfig, make_recommendations, run_cycle, signed_get, write_notification_events


def config() -> SupervisorConfig:
    return SupervisorConfig(
        poll_seconds=30,
        request_timeout_seconds=3,
        request_attempts=1,
        ledger_path="logs/test.jsonl",
        event_queue_path="logs/test-events.jsonl",
        notify_dedup_path="logs/test-notify-state.json",
        latest_alert_path="logs/test-latest-alert.txt",
        model_review_path="logs/test-model-reviews.jsonl",
        approval_dir="approvals",
        recv_window=60000,
        futures_symbols=["SPCXUSDT"],
        spot_symbols=["BTCUSDT"],
        reserve_futures_available_usdt=Decimal("30"),
        max_new_spot_quote_usdt=Decimal("5"),
        min_spot_cash_usdt=Decimal("5"),
        spcx_reduce_below=Decimal("172.5"),
        spcx_reassess_above=Decimal("188.5"),
        spcx_danger_buffer_pct=Decimal("0.08"),
        write_approval_tickets=True,
        notify_actions=["DATA_UNAVAILABLE", "REDUCE_POSITION", "SPOT_BUY_APPROVAL", "REASSESS_TAKE_PROFIT_OR_HOLD", "RESERVE_CASH"],
        notification_cooldown_seconds=900,
        min_notify_priority="medium",
        desktop_notifications=False,
        desktop_notification_seconds=3,
    )


class RealtimeSupervisorTests(unittest.TestCase):
    def test_recommends_reserve_when_available_balance_is_low(self) -> None:
        snapshot = {"futuresAccount": {"availableBalance": "10"}, "futuresNonZeroPositions": []}

        recommendations = make_recommendations(config(), snapshot, [])

        self.assertEqual(recommendations[0].action, "RESERVE_CASH")

    def test_recommends_reduce_when_spcx_near_danger_zone(self) -> None:
        snapshot = {
            "futuresAccount": {"availableBalance": "50"},
            "futuresNonZeroPositions": [
                {"symbol": "SPCXUSDT", "markPrice": "170", "liquidationPrice": "160", "entryPrice": "188.5", "breakEvenPrice": "197"}
            ],
        }

        recommendations = make_recommendations(config(), snapshot, [])

        self.assertTrue(any(item.action == "REDUCE_POSITION" for item in recommendations))

    def test_recommends_spot_buy_approval_for_high_confidence_spot_signal(self) -> None:
        snapshot = {
            "spotNonZeroBalances": [{"asset": "USDT", "free": "20", "locked": "0", "total": "20"}],
            "futuresAccount": {"availableBalance": "50"},
            "futuresNonZeroPositions": [],
        }
        signals = [{"market": "spot", "symbol": "BTCUSDT", "action": "BUY", "confidence": "0.72", "price": "60000", "warnings": []}]

        recommendations = make_recommendations(config(), snapshot, signals)

        self.assertTrue(any(item.action == "SPOT_BUY_APPROVAL" for item in recommendations))

    def test_blocks_spot_buy_approval_when_cash_is_too_low(self) -> None:
        snapshot = {
            "spotNonZeroBalances": [{"asset": "USDT", "free": "1", "locked": "0", "total": "1"}],
            "futuresAccount": {"availableBalance": "50"},
            "futuresNonZeroPositions": [],
        }
        signals = [{"market": "spot", "symbol": "BTCUSDT", "action": "BUY", "confidence": "0.72", "price": "60000", "warnings": []}]

        recommendations = make_recommendations(config(), snapshot, signals)

        self.assertTrue(any(item.action == "SPOT_BUY_BLOCKED_CASH" for item in recommendations))
        self.assertFalse(any(item.action == "SPOT_BUY_APPROVAL" for item in recommendations))

    def test_data_unavailable_blocks_trading_before_cash_inference(self) -> None:
        snapshot = {
            "futuresAccount": {"availableBalance": None},
            "futuresNonZeroPositions": [],
            "futuresOpenOrders": {"_error": "network blocked"},
        }
        signals = [{"market": "spot", "symbol": "BTCUSDT", "error": "network blocked"}]

        recommendations = make_recommendations(config(), snapshot, signals)

        self.assertEqual(recommendations[0].action, "DATA_UNAVAILABLE")
        self.assertFalse(any(item.action == "RESERVE_CASH" for item in recommendations))
        self.assertFalse(any(item.action == "SPOT_BUY_APPROVAL" for item in recommendations))

    def test_emits_notification_event_for_spot_buy_approval(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg = config_with_paths(tmp)
            snapshot = {
                "spotNonZeroBalances": [{"asset": "USDT", "free": "20", "locked": "0", "total": "20"}],
                "futuresAccount": {"availableBalance": "50"},
                "futuresNonZeroPositions": [],
            }
            signals = [{"market": "spot", "symbol": "BTCUSDT", "action": "BUY", "confidence": "0.72", "price": "60000", "warnings": []}]
            recommendations = make_recommendations(cfg, snapshot, signals)
            cycle = {"createdAt": "2026-06-19T00:00:00+08:00", "account": snapshot, "signals": signals}

            events = write_notification_events(cfg, recommendations, cycle, {}, now_ts=1000)

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["action"], "SPOT_BUY_APPROVAL")
            self.assertTrue(events[0]["requiresModelReview"])
            self.assertFalse(events[0]["realOrderAllowed"])
            self.assertIn("modelReview", events[0])
            self.assertEqual(events[0]["modelReviewStatus"], "confirm")
            self.assertEqual(events[0]["manualOrderPlan"]["venue"], "Binance 现货")
            self.assertEqual(events[0]["modelReview"]["manualOrderPlan"]["quoteAmountUSDT"], "5")
            self.assertTrue(Path(cfg.event_queue_path).exists())
            self.assertTrue(Path(cfg.latest_alert_path).exists())
            self.assertTrue(Path(cfg.model_review_path).exists())

    def test_emits_notification_event_for_reduce_position(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg = config_with_paths(tmp)
            snapshot = {
                "futuresAccount": {"availableBalance": "50"},
                "futuresNonZeroPositions": [
                    {
                        "symbol": "SPCXUSDT",
                        "positionAmt": "4.22",
                        "markPrice": "170",
                        "liquidationPrice": "160",
                        "entryPrice": "188.5",
                        "breakEvenPrice": "197",
                    }
                ],
            }
            recommendations = make_recommendations(cfg, snapshot, [])
            cycle = {"createdAt": "2026-06-19T00:00:00+08:00", "account": snapshot, "signals": []}

            events = write_notification_events(cfg, recommendations, cycle, {}, now_ts=1000)

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["action"], "REDUCE_POSITION")
            self.assertEqual(events[0]["priority"], "critical")
            self.assertEqual(events[0]["modelReviewStatus"], "confirm")
            self.assertEqual(events[0]["modelReview"]["manualOrderPlan"]["side"], "平多 / 卖出 / reduce-only")

    def test_latest_alert_includes_model_review_and_manual_order_plan(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg = config_with_paths(tmp)
            snapshot = {
                "spotNonZeroBalances": [{"asset": "USDT", "free": "20", "locked": "0", "total": "20"}],
                "futuresAccount": {"availableBalance": "50"},
                "futuresNonZeroPositions": [],
            }
            signals = [{"market": "spot", "symbol": "BTCUSDT", "action": "BUY", "confidence": "0.72", "price": "60000", "warnings": []}]
            recommendations = make_recommendations(cfg, snapshot, signals)
            cycle = {"createdAt": "2026-06-19T00:00:00+08:00", "account": snapshot, "signals": signals}

            write_notification_events(cfg, recommendations, cycle, {}, now_ts=1000)

            alert = Path(cfg.latest_alert_path).read_text(encoding="utf-8")
            self.assertIn("[model review]", alert)
            self.assertIn("verdict: confirm", alert)
            self.assertIn("manualOrderPlan", alert)

    def test_notification_event_is_deduped_within_cooldown(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg = config_with_paths(tmp)
            snapshot = {
                "spotNonZeroBalances": [{"asset": "USDT", "free": "20", "locked": "0", "total": "20"}],
                "futuresAccount": {"availableBalance": "50"},
                "futuresNonZeroPositions": [],
            }
            signals = [{"market": "spot", "symbol": "BTCUSDT", "action": "BUY", "confidence": "0.72", "price": "60000", "warnings": []}]
            recommendations = make_recommendations(cfg, snapshot, signals)
            cycle = {"createdAt": "2026-06-19T00:00:00+08:00", "account": snapshot, "signals": signals}

            first = write_notification_events(cfg, recommendations, cycle, {}, now_ts=1000)
            second = write_notification_events(cfg, recommendations, cycle, {}, now_ts=1100)

            self.assertEqual(len(first), 1)
            self.assertEqual(second, [])

    def test_no_trade_does_not_emit_notification_event(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg = config_with_paths(tmp)
            snapshot = {"futuresAccount": {"availableBalance": "50"}, "futuresNonZeroPositions": []}
            recommendations = make_recommendations(cfg, snapshot, [])
            cycle = {"createdAt": "2026-06-19T00:00:00+08:00", "account": snapshot, "signals": []}

            events = write_notification_events(cfg, recommendations, cycle, {}, now_ts=1000)

            self.assertEqual(events, [])

    def test_run_cycle_records_autonomy_summary(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg = config_with_paths(tmp)

            import realtime_supervisor

            original_account_snapshot = realtime_supervisor.account_snapshot
            original_signal_for = realtime_supervisor.signal_for
            try:
                realtime_supervisor.account_snapshot = lambda _config: {
                    "spotNonZeroBalances": [],
                    "futuresAccount": {"availableBalance": "50"},
                    "futuresNonZeroPositions": [],
                }
                realtime_supervisor.signal_for = lambda symbol, market, _config=None: {
                    "symbol": symbol,
                    "market": market,
                    "price": "100",
                    "action": "HOLD",
                    "confidence": "0.20",
                    "warnings": [],
                    "indicators": {},
                    "reasons": [],
                }

                cycle = run_cycle(cfg)
            finally:
                realtime_supervisor.account_snapshot = original_account_snapshot
                realtime_supervisor.signal_for = original_signal_for

            self.assertEqual(cycle["autonomy"]["mode"], "autonomous_analysis_approval_required")
            self.assertFalse(cycle["autonomy"]["realOrderAllowed"])
            self.assertIn("signal scoring", cycle["autonomy"]["whatIsAutomatic"])

    def test_signed_get_returns_error_when_server_time_fails(self) -> None:
        import os
        import realtime_supervisor

        original_server_time = realtime_supervisor.server_time
        original_key = os.environ.get("BINANCE_API_KEY")
        original_secret = os.environ.get("BINANCE_API_SECRET")
        try:
            os.environ["BINANCE_API_KEY"] = "key"
            os.environ["BINANCE_API_SECRET"] = "secret"
            realtime_supervisor.server_time = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("network blocked"))

            payload = signed_get("https://api.binance.com", "/api/v3/account", {}, "/api/v3/time", 60000)
        finally:
            realtime_supervisor.server_time = original_server_time
            if original_key is None:
                os.environ.pop("BINANCE_API_KEY", None)
            else:
                os.environ["BINANCE_API_KEY"] = original_key
            if original_secret is None:
                os.environ.pop("BINANCE_API_SECRET", None)
            else:
                os.environ["BINANCE_API_SECRET"] = original_secret

        self.assertIn("_error", payload)
        self.assertIn("server time unavailable", payload["_error"])


def config_with_paths(tmp: str) -> SupervisorConfig:
    cfg = config()
    return SupervisorConfig(
        poll_seconds=cfg.poll_seconds,
        request_timeout_seconds=cfg.request_timeout_seconds,
        request_attempts=cfg.request_attempts,
        ledger_path=str(Path(tmp) / "ledger.jsonl"),
        event_queue_path=str(Path(tmp) / "events.jsonl"),
        notify_dedup_path=str(Path(tmp) / "notify-state.json"),
        latest_alert_path=str(Path(tmp) / "latest-alert.txt"),
        model_review_path=str(Path(tmp) / "model-reviews.jsonl"),
        approval_dir=str(Path(tmp) / "approvals"),
        recv_window=cfg.recv_window,
        futures_symbols=cfg.futures_symbols,
        spot_symbols=cfg.spot_symbols,
        reserve_futures_available_usdt=cfg.reserve_futures_available_usdt,
        max_new_spot_quote_usdt=cfg.max_new_spot_quote_usdt,
        min_spot_cash_usdt=cfg.min_spot_cash_usdt,
        spcx_reduce_below=cfg.spcx_reduce_below,
        spcx_reassess_above=cfg.spcx_reassess_above,
        spcx_danger_buffer_pct=cfg.spcx_danger_buffer_pct,
        write_approval_tickets=cfg.write_approval_tickets,
        notify_actions=cfg.notify_actions,
        notification_cooldown_seconds=cfg.notification_cooldown_seconds,
        min_notify_priority=cfg.min_notify_priority,
        desktop_notifications=cfg.desktop_notifications,
        desktop_notification_seconds=cfg.desktop_notification_seconds,
    )


if __name__ == "__main__":
    unittest.main()
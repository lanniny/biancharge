import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from trading_dashboard import create_live_order_request, latest_jsonl, load_status, read_jsonl_tail


class TradingDashboardTests(unittest.TestCase):
    def test_read_jsonl_tail_skips_bad_lines(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "items.jsonl"
            path.write_text('{"a":1}\nnot-json\n{"a":2}\n', encoding="utf-8")

            rows = read_jsonl_tail(path, 5)

            self.assertEqual(rows, [{"a": 1}, {"a": 2}])
            self.assertEqual(latest_jsonl(path), {"a": 2})

    def test_live_order_request_is_blocked_when_data_unavailable(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            logs.mkdir()
            (logs / "realtime-supervisor.jsonl").write_text(
                json.dumps(
                    {
                        "createdAt": "2026-06-19T13:00:00+08:00",
                        "autonomy": {"primaryAction": "DATA_UNAVAILABLE"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (logs / "realtime-supervisor-model-reviews.jsonl").write_text(
                json.dumps({"eventId": "evt-1", "verdict": "block", "userMessage": "数据不可用"}) + "\n",
                encoding="utf-8",
            )

            result = create_live_order_request(
                {
                    "market": "spot",
                    "symbol": "BTCUSDT",
                    "side": "买入",
                    "orderType": "限价",
                    "quoteAmountUSDT": "5",
                },
                root,
            )

            self.assertTrue(Path(result["path"]).exists())
            self.assertEqual(result["ticket"]["status"], "blocked_by_risk_gate")
            self.assertFalse(result["ticket"]["realOrderAllowed"])
            self.assertTrue(result["ticket"]["notSubmittedToExchange"])

    def test_load_status_returns_latest_cycle_and_approvals(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            approvals = root / "approvals"
            logs.mkdir()
            approvals.mkdir()
            (logs / "realtime-supervisor.jsonl").write_text('{"createdAt":"now"}\n', encoding="utf-8")
            (approvals / "ticket.json").write_text("{}", encoding="utf-8")

            status = load_status(root)

            self.assertEqual(status["latestCycle"]["createdAt"], "now")
            # Use os.path.join so the assertion matches the platform separator
            # (was hardcoded to a Windows backslash, failing on Linux/VPS).
            self.assertEqual(status["approvals"][0]["name"], os.path.join("approvals", "ticket.json"))


if __name__ == "__main__":
    unittest.main()
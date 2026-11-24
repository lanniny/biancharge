"""Forward paper behavior without a live socket.

These tests do not claim a real-time paper session is running.
"""

from __future__ import annotations

import json
import unittest
from decimal import Decimal
from pathlib import Path

from paper_forward import (
    PaperForwardError,
    apply_feed_event,
    load_paper_config,
    new_state,
    on_book,
    on_closed_bar,
    on_mark,
    on_next_open,
    status_report,
)
from paper_ws import build_client_frame, parse_server_frames


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "paper_forward.example.json"


def _config() -> dict:
    cfg = load_paper_config(CONFIG)
    cfg["stale_mark_seconds"] = 30
    cfg["stale_book_seconds"] = 15
    return cfg


def _rising_bars(count: int, start_ms: int) -> list[dict]:
    bars = []
    for index in range(count):
        close = Decimal("100") + Decimal(index)
        bars.append(
            {
                "open_ms": start_ms + index * 86_400_000,
                "close_ms": start_ms + (index + 1) * 86_400_000 - 1,
                "close": close,
            }
        )
    return bars


class FeedFrameTests(unittest.TestCase):
    def test_server_text_frame_round_trip_parse(self) -> None:
        payload = b'{"e":"markPriceUpdate"}'
        raw = bytes([0x81, len(payload)]) + payload
        frames, rest = parse_server_frames(raw)
        self.assertEqual(rest, b"")
        self.assertEqual(frames, [(0x1, payload)])
        client = build_client_frame(0x1, payload)
        self.assertTrue(client[1] & 0x80)

    def test_public_config_rejects_live_and_credentials(self) -> None:
        load_paper_config(CONFIG)
        with self.assertRaises(PaperForwardError):
            load_paper_config_payload = json.loads(CONFIG.read_text(encoding="utf-8"))
            load_paper_config_payload["mode"] = "live"
            path = CONFIG.with_name("paper_forward.live.json")
            path.write_text(json.dumps(load_paper_config_payload), encoding="utf-8")
            try:
                load_paper_config(path)
            finally:
                path.unlink()
        secret = "SUPERSECRET"
        payload = json.loads(CONFIG.read_text(encoding="utf-8"))
        payload["api_key"] = secret
        path = CONFIG.with_name("paper_forward.secret.json")
        path.write_text(json.dumps(payload), encoding="utf-8")
        try:
            with self.assertRaises(PaperForwardError) as raised:
                load_paper_config(path)
            self.assertNotIn(secret, str(raised.exception))
        finally:
            path.unlink()

    def test_source_does_not_import_order_clients(self) -> None:
        text = (ROOT / "paper_forward.py").read_text(encoding="utf-8") + (ROOT / "paper_ws.py").read_text(encoding="utf-8")
        self.assertNotIn("market_autotrader", text)
        self.assertNotIn("/fapi/v1/order", text)
        self.assertNotIn("signed_binance_request", text)


class ForwardAccountingTests(unittest.TestCase):
    def test_warmup_does_not_create_a_forward_fill(self) -> None:
        state = new_state(_config())
        config = _config()
        for bar in _rising_bars(60, 1_700_000_000_000):
            events = on_closed_bar(state, config, bar, live=False)
            self.assertTrue(events)
            self.assertTrue(all(item["type"] == "warmup_bar" for item in events))
        self.assertIsNone(state["position"])
        self.assertIsNone(state["pending"])

    def test_signal_waits_for_a_later_quote_and_is_idempotent(self) -> None:
        config = _config()
        state = new_state(config)
        bars = _rising_bars(60, 1_700_000_000_000)
        for bar in bars[:-1]:
            on_closed_bar(state, config, bar, live=False)
        latest = bars[-1]
        now = latest["close_ms"]
        on_mark(
            state,
            config,
            {"mark": Decimal(latest["close"]), "funding_rate": Decimal("0.0001"), "event_ms": now - 1000, "index": Decimal("1")},
        )
        events = on_closed_bar(state, config, latest, live=True)
        self.assertTrue(any(item["type"] == "signal" for item in events))
        self.assertIsNone(state["position"])
        early = on_book(state, config, {"bid": Decimal("159"), "ask": Decimal("160"), "event_ms": latest["close_ms"] - 10})
        self.assertEqual(early, [])
        self.assertIsNone(state["position"])
        quote_ms = latest["close_ms"] + 1000
        # Keep the quote inside the freshness window relative to the test clock by using a current timestamp.
        import paper_forward

        quote_ms = paper_forward._now_ms()
        state["pending"]["signal_bar_close_ms"] = quote_ms - 1000
        filled = on_book(state, config, {"bid": Decimal("159"), "ask": Decimal("160"), "event_ms": quote_ms})
        self.assertEqual(filled[0]["action"], "open_long")
        self.assertEqual(filled[0]["reason"], "first_quote_after_signal")
        self.assertGreater(Decimal(filled[0]["fill_price"]), Decimal("160"))
        self.assertEqual(filled[0]["orders_sent"], 0)
        again = on_book(state, config, {"bid": Decimal("159"), "ask": Decimal("161"), "event_ms": quote_ms + 10})
        self.assertEqual(again, [])
        self.assertEqual(len(state["filled_signal_open_ms"]), 1)

    def test_next_open_fills_when_no_quote_arrived(self) -> None:
        config = _config()
        state = new_state(config)
        for bar in _rising_bars(59, 1_700_000_000_000):
            on_closed_bar(state, config, bar, live=False)
        signal = _rising_bars(60, 1_700_000_000_000)[-1]
        on_mark(state, config, {"mark": signal["close"], "funding_rate": Decimal("0.0001"), "event_ms": signal["close_ms"] - 500, "index": None})
        on_closed_bar(state, config, signal, live=True)
        opened = on_next_open(
            state,
            config,
            {"open_ms": signal["open_ms"] + 86_400_000, "open": Decimal("170"), "event_ms": signal["close_ms"] + 1000},
        )
        self.assertEqual(opened[0]["reason"], "next_bar_open")
        self.assertEqual(Decimal(opened[0]["raw_price"]), Decimal("170"))

    def test_missing_funding_rate_is_not_zero(self) -> None:
        config = _config()
        state = new_state(config)
        state["position"] = {"side": "long", "qty": "1", "entry": "100", "stop": "92", "margin": "100", "entry_ms": 1, "signal_bar_open_ms": 1}
        state["cash"] = "9900"
        start = 1_700_000_000_000
        on_mark(state, config, {"mark": Decimal("100"), "funding_rate": Decimal("0.0001"), "event_ms": start, "index": None})
        before = state["funding_pnl"]
        events = on_mark(
            state,
            config,
            {"mark": Decimal("100"), "funding_rate": None, "event_ms": start + 8 * 60 * 60 * 1000, "index": None},
        )
        self.assertEqual(state["funding_pnl"], before)
        self.assertTrue(any(item["reason"] == "funding_missing_not_zeroed" for item in events))
        self.assertEqual(status_report(state, config, now_ms=start + 8 * 60 * 60 * 1000)["valuation"], "incomplete_missed_funding")

    def test_observed_funding_is_charged_once(self) -> None:
        config = _config()
        state = new_state(config)
        state["position"] = {"side": "long", "qty": "2", "entry": "100", "stop": "92", "margin": "200", "entry_ms": 1, "signal_bar_open_ms": 1}
        state["cash"] = "9800"
        start = 1_700_000_000_000
        on_mark(state, config, {"mark": Decimal("100"), "funding_rate": Decimal("0.001"), "event_ms": start, "index": None})
        boundary = ((start // (8 * 60 * 60 * 1000)) + 1) * 8 * 60 * 60 * 1000
        events = on_mark(state, config, {"mark": Decimal("110"), "funding_rate": Decimal("0.001"), "event_ms": boundary, "index": Decimal("109")})
        self.assertEqual(events[-1]["type"], "funding")
        self.assertNotEqual(events[-1]["cashflow"], "0")
        again = on_mark(state, config, {"mark": Decimal("110"), "funding_rate": Decimal("0.001"), "event_ms": boundary + 1000, "index": None})
        self.assertFalse(any(item.get("type") == "funding" for item in again))

    def test_stop_gap_uses_the_observed_mark(self) -> None:
        config = _config()
        state = new_state(config)
        state["position"] = {"side": "long", "qty": "1", "entry": "100", "stop": "92", "margin": "100", "entry_ms": 1, "signal_bar_open_ms": 1}
        state["cash"] = "9900"
        on_mark(state, config, {"mark": Decimal("105"), "funding_rate": Decimal("0.0001"), "event_ms": 1_000, "index": None})
        events = on_mark(state, config, {"mark": Decimal("80"), "funding_rate": Decimal("0.0001"), "event_ms": 2_000, "index": None})
        close = next(item for item in events if item["type"] == "fill")
        self.assertEqual(close["reason"], "stop_gap")
        self.assertEqual(Decimal(close["raw_price"]), Decimal("80"))
        self.assertLess(Decimal(close["raw_price"]), Decimal("92"))

    def test_stale_mark_blocks_a_new_entry(self) -> None:
        config = _config()
        state = new_state(config)
        for bar in _rising_bars(59, 1_700_000_000_000):
            on_closed_bar(state, config, bar, live=False)
        latest = _rising_bars(60, 1_700_000_000_000)[-1]
        on_mark(state, config, {"mark": latest["close"], "funding_rate": Decimal("0.0001"), "event_ms": latest["close_ms"] - 120_000, "index": None})
        events = on_closed_bar(state, config, latest, live=True)
        self.assertTrue(any(item.get("reason") == "incomplete_stale_mark" for item in events))
        self.assertIsNone(state["pending"])

    def test_wrong_symbol_and_intraday_kline_are_rejected(self) -> None:
        with self.assertRaises(PaperForwardError):
            normalize = __import__("paper_forward", fromlist=["normalize_stream"]).normalize_stream
            normalize({"data": {"e": "bookTicker", "s": "ETHUSDT", "b": "1", "a": "2", "E": 1}})
        with self.assertRaises(PaperForwardError):
            apply_feed_event(
                new_state(_config()),
                _config(),
                {"data": {"e": "kline", "E": 1, "k": {"s": "BTCUSDT", "i": "1h", "t": 1, "T": 2, "o": "1", "h": "1", "l": "1", "c": "1", "x": True}}},
            )

    def test_status_without_a_fresh_heartbeat_is_not_running(self) -> None:
        state = new_state(_config())
        state["running"] = True
        state["heartbeat_ms"] = 1
        report = status_report(state, _config(), now_ms=10_000_000)
        self.assertEqual(report["process"], "not_running")
        self.assertEqual(report["orders_sent"], 0)


if __name__ == "__main__":
    unittest.main()

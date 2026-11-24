"""A dead public socket is logged and retried. It does not stop the paper process."""

from __future__ import annotations

import io
import json
import tempfile
import threading
import time
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from paper_alt_forward import cmd_start as alt_start
from paper_alt_forward import portfolio_status
from paper_forward import entries_blocked, load_paper_config, load_warmup_bars, new_state, on_closed_bar, status_report


ROOT = Path(__file__).resolve().parents[1]


class QuietSocket:
    def __init__(self, fail_first: bool = False) -> None:
        self.fail_first = fail_first
        self.closed = False
        self.connects = 0

    def connect(self) -> str:
        self.connects += 1
        if self.fail_first and self.connects == 1:
            raise ConnectionError("reset by peer")
        return "HTTP/1.1 101 Switching Protocols"

    def read_text(self, timeout: float) -> str | None:
        if self.closed:
            raise ConnectionError("closed for reconnect")
        time.sleep(min(timeout, 0.02))
        return None

    def close(self) -> None:
        self.closed = True


def _run_until_stop(start, config, factory, seconds: float) -> str:
    threading.Timer(seconds, lambda: Path(config["stop_path"]).write_text("stop\n", encoding="utf-8")).start()
    code = start(config, feed_factory=factory, warmup_loader=lambda *_args: [])
    if code != 0:
        raise AssertionError(code)
    return Path(config["log_path"]).read_text(encoding="utf-8")


class FeedReconnectTests(unittest.TestCase):
    def test_socket_error_is_logged_and_does_not_exit(self) -> None:
        sockets: list[QuietSocket] = []

        def factory(_url: str) -> QuietSocket:
            sock = QuietSocket(fail_first=not sockets)
            sockets.append(sock)
            return sock

        log = _run_until_stop(self._btc_start, self._btc_config(), factory, 1.2)
        self.assertIn("reset by peer", log)
        self.assertIn('"type": "feed_error"', log)
        self.assertIn('"ts":', log)
        self.assertLess(log.index("feed_error"), log.rindex('"type": "stopped"'))
        self.assertGreaterEqual(sum(sock.connects for sock in sockets), 2)

    def test_stale_quote_reconnects_without_stopping(self) -> None:
        created: list[QuietSocket] = []

        def factory(_url: str) -> QuietSocket:
            sock = QuietSocket()
            created.append(sock)
            return sock

        config = self._btc_config()
        config["stale_mark_seconds"] = 1
        config["stale_book_seconds"] = 1
        config["reconnect_initial_seconds"] = 0.05
        log = _run_until_stop(self._btc_start, config, factory, 2.4)
        self.assertIn("stale_quote", log)
        self.assertIn('"type": "feed_disconnect"', log)
        self.assertGreater(len(created), 2)

    def test_alt_socket_error_is_logged_too(self) -> None:
        sockets: list[QuietSocket] = []

        def factory(_url: str) -> QuietSocket:
            sock = QuietSocket(fail_first=not sockets)
            sockets.append(sock)
            return sock

        log = _run_until_stop(alt_start, self._alt_config(), factory, 1.2)
        self.assertIn("reset by peer", log)
        self.assertIn('"type": "feed_error"', log)
        self.assertIn('"ts":', log)
        self.assertLess(log.index("feed_error"), log.rindex('"type": "stopped"'))

    def test_fresh_heartbeat_with_an_old_mark_is_stale(self) -> None:
        config = load_paper_config(ROOT / "paper_forward.example.json")
        state = new_state(config)
        now = 1_700_000_000_000
        state["running"] = True
        state["heartbeat_ms"] = now
        state["last_mark"] = {"price": "100", "event_ms": now - 120_000}
        state["last_book"] = {"bid": "100", "ask": "101", "event_ms": now - 120_000}
        report = status_report(state, config, now_ms=now)
        self.assertEqual(report["process"], "stale")
        self.assertNotEqual(report["process"], "heartbeat_fresh")
        self.assertEqual(report["last_mark_time"], "2023-11-14T22:11:20Z")
        self.assertIn("ts", report)
        state["feed_down"] = True
        blocked = entries_blocked(state, now, config)
        self.assertEqual(blocked, "feed_down")

    def test_alt_status_lists_each_symbol(self) -> None:
        config = load_paper_config(ROOT / "paper_forward.alts.example.json")
        from paper_alt_forward import new_portfolio_state

        portfolio = new_portfolio_state(config)
        now = 1_700_000_000_000
        portfolio["running"] = True
        portfolio["heartbeat_ms"] = now
        eth = portfolio["symbols"]["ETHUSDT"]
        eth["running"] = True
        eth["heartbeat_ms"] = now
        eth["last_mark"] = {"price": "2000", "event_ms": now - 120_000}
        eth["cash"] = "10000"
        eth["forward_fills"] = 0
        report = portfolio_status(portfolio, config, now)
        self.assertEqual(report["process"], "stale")
        self.assertEqual(report["symbols"]["ETHUSDT"]["mark"], "2000")
        self.assertEqual(report["symbols"]["ETHUSDT"]["cash"], "10000")
        self.assertIsNone(report["symbols"]["ETHUSDT"]["position"])
        self.assertEqual(report["symbols"]["SOLUSDT"]["forward_fills"], 0)
        self.assertEqual(set(report["symbols"]), {"ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"})

    def test_four_hour_warmup_keeps_today_closed_bars(self) -> None:
        day = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
        closed = int(datetime(2026, 11, 24, 4, 0, tzinfo=timezone.utc).timestamp() * 1000)
        still_open = int(datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)
        text = f"{closed},10,11,9,10,1,0,0,0,0,0,0\n{still_open},10,11,9,10,1,0,0,0,0,0,0\n"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("ETHUSDT-4h.csv", text)
        payload = buffer.getvalue()

        def fetcher(url: str, _cache: Path):
            if url.endswith("ETHUSDT-4h-2026-11-24.zip"):
                return payload, {}
            raise OSError("missing")

        bars = load_warmup_bars(now=day, cache_root=Path(tempfile.mkdtemp()), fetcher=fetcher, interval="4h", symbol="ETHUSDT")
        opens = {bar["open_ms"] for bar in bars}
        self.assertIn(closed, opens)
        self.assertNotIn(still_open, opens)

    def test_old_flat_state_is_still_readable(self) -> None:
        state = new_state(load_paper_config(ROOT / "paper_forward.example.json"))
        self.assertEqual(state["cash"], "10000")
        self.assertIsNone(state["position"])
        on_closed_bar(state, load_paper_config(ROOT / "paper_forward.example.json"), {"open_ms": 1, "close_ms": 2, "close": "100"}, live=False)
        self.assertEqual(state["cash"], "10000")

    def _btc_config(self) -> dict:
        return self._copy(ROOT / "paper_forward.example.json")

    def _alt_config(self) -> dict:
        return self._copy(ROOT / "paper_forward.alts.example.json")

    def _copy(self, path: Path) -> dict:
        payload = json.loads(path.read_text(encoding="utf-8"))
        root = Path(tempfile.mkdtemp())
        payload["state_path"] = str(root / "state.json")
        payload["log_path"] = str(root / "log.jsonl")
        payload["stop_path"] = str(root / "STOP")
        payload["heartbeat_seconds"] = 30
        payload["reconnect_initial_seconds"] = 0.05
        payload["stale_mark_seconds"] = 30
        payload["stale_book_seconds"] = 15
        config_path = root / "config.json"
        config_path.write_text(json.dumps(payload), encoding="utf-8")
        return load_paper_config(config_path)

    def _btc_start(self, config, feed_factory, warmup_loader):
        from paper_forward import cmd_start

        return cmd_start(config, feed_factory=feed_factory, warmup_loader=warmup_loader)

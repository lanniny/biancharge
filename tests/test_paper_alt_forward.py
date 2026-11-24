"""The four-coin paper process stays off the running 1x daily ledger."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path

from paper_alt_forward import apply_alt_event, cmd_start, new_portfolio_state
from paper_forward import PaperForwardError, load_paper_config, normalize_stream
from research_alt_validation import FROZEN_PRESET


ROOT = Path(__file__).resolve().parents[1]


def _bars(count: int) -> list[dict]:
    bars = []
    start = 1_700_000_000_000
    for index in range(count):
        close = Decimal("100") + Decimal(index)
        bars.append(
            {
                "open_ms": start + index * 14_400_000,
                "close_ms": start + (index + 1) * 14_400_000 - 1,
                "close": close,
                "high": close + Decimal("1"),
                "low": close - Decimal("1"),
            }
        )
    return bars


class AltPaperTests(unittest.TestCase):
    def test_daily_config_stays_long_only_and_alt_config_is_separate(self) -> None:
        daily = load_paper_config(ROOT / "paper_forward.example.json")
        self.assertNotIn("portfolio", daily)
        self.assertEqual(daily["strategy"]["name"], "baseline_sma_momentum")
        self.assertNotIn("allow_short", daily["strategy"])
        alt = load_paper_config(ROOT / "paper_forward.alts.example.json")
        self.assertEqual(alt["portfolio"], "alt_slow_momentum")
        self.assertEqual(tuple(alt["symbols"]), ("ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"))
        self.assertNotEqual(Path(alt["state_path"]).name, "btcusdt-paper-state.json")
        self.assertTrue(alt["strategy"]["preset"]["allow_short"])
        self.assertEqual(alt["strategy"]["preset"]["filter"], FROZEN_PRESET["filter"])
        with self.assertRaises(PaperForwardError):
            normalize_stream({"data": {"e": "bookTicker", "s": "ETHUSDT", "b": "1", "a": "2", "E": 1}})

    def test_a_changed_preset_or_the_daily_state_path_is_rejected(self) -> None:
        payload = json.loads((ROOT / "paper_forward.alts.example.json").read_text(encoding="utf-8"))
        payload["strategy"]["preset"]["ema"] = 34
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(payload, handle)
            changed = handle.name
        with self.assertRaises(PaperForwardError):
            load_paper_config(changed)
        payload = json.loads((ROOT / "paper_forward.alts.example.json").read_text(encoding="utf-8"))
        payload["state_path"] = "paper-state/btcusdt-paper-state.json"
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(payload, handle)
            blocked = handle.name
        with self.assertRaises(PaperForwardError):
            load_paper_config(blocked)

    def test_one_symbol_fill_does_not_spend_another_sleeve(self) -> None:
        config = load_paper_config(ROOT / "paper_forward.alts.example.json")
        portfolio = new_portfolio_state(config)
        import paper_forward

        now = paper_forward._now_ms()
        portfolio["symbols"]["ETHUSDT"]["pending"] = {
            "signal_bar_open_ms": 10,
            "signal_bar_close_ms": now - 1000,
            "side": "short",
            "stop_distance": "0.02",
        }
        sol_cash = portfolio["symbols"]["SOLUSDT"]["cash"]
        events = apply_alt_event(
            portfolio,
            config,
            {"data": {"e": "bookTicker", "s": "ETHUSDT", "b": "100", "a": "100.1", "E": now}},
        )
        self.assertEqual(events[0]["action"], "open_short")
        self.assertEqual(events[0]["symbol"], "ETHUSDT")
        eth = portfolio["symbols"]["ETHUSDT"]["position"]
        self.assertEqual(eth["side"], "short")
        self.assertGreater(Decimal(eth["liquidation_price"]), Decimal(eth["stop"]))
        self.assertEqual(portfolio["symbols"]["SOLUSDT"]["cash"], sol_cash)
        self.assertIsNone(portfolio["symbols"]["SOLUSDT"]["position"])
        self.assertEqual(portfolio["orders_sent"], 0)

    def test_start_warms_up_without_forward_fills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = json.loads((ROOT / "paper_forward.alts.example.json").read_text(encoding="utf-8"))
            payload["state_path"] = str(root / "state.json")
            payload["log_path"] = str(root / "log.jsonl")
            payload["stop_path"] = str(root / "STOP")
            payload["heartbeat_seconds"] = 1
            path = root / "alt.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            config = load_paper_config(path)

            class Socket:
                def connect(self) -> str:
                    return "HTTP/1.1 101 Switching Protocols"

                def read_text(self, timeout: float) -> None:
                    import time

                    time.sleep(min(timeout, 0.05))
                    return None

                def close(self) -> None:
                    return None

            threading.Timer(1.2, lambda: Path(payload["stop_path"]).write_text("stop\n", encoding="utf-8")).start()
            code = cmd_start(config, feed_factory=lambda _url: Socket(), warmup_loader=lambda _symbol: _bars(60))
            self.assertEqual(code, 0)
            state = json.loads(Path(payload["state_path"]).read_text(encoding="utf-8"))
            self.assertEqual(state["orders_sent"], 0)
            self.assertFalse((root / "btcusdt-paper-state.json").exists())
            for symbol, sleeve in state["symbols"].items():
                self.assertIsNone(sleeve["position"])
                self.assertEqual(sleeve["cash"], "10000")
                self.assertGreaterEqual(len(sleeve["closed_bars"]), 50)
                self.assertEqual(sleeve["symbol"], symbol)
                self.assertEqual(sleeve["orders_sent"], 0)

"""Tests for supervisor_hints bridge."""

import json
import tempfile
import unittest
from pathlib import Path

from supervisor_hints import load_supervisor_hints


class SupervisorHintsTests(unittest.TestCase):
    def test_load_recent_reduce_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "action": "REDUCE_POSITION",
                        "symbol": "SPCXUSDT",
                        "market": "futures",
                        "priority": "critical",
                        "reason": "below buffer",
                        "createdAtEpoch": 1_700_000_000,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            hints = load_supervisor_hints(path, max_age_seconds=3600, now=1_700_000_100)
            self.assertIn("SPCXUSDT", hints)
            self.assertEqual(hints["SPCXUSDT"]["action"], "REDUCE_POSITION")

    def test_stale_events_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "action": "REDUCE_POSITION",
                        "symbol": "BTCUSDT",
                        "priority": "high",
                        "createdAtEpoch": 1_000_000_000,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            hints = load_supervisor_hints(path, max_age_seconds=900, now=1_700_000_000)
            self.assertEqual(hints, {})


if __name__ == "__main__":
    unittest.main()

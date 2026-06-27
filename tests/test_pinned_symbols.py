import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pinned_symbols import add_pin, load_pins, merge_config_pins, remove_pin


class PinnedSymbolsTests(unittest.TestCase):
    def test_add_and_remove_pin(self) -> None:
        with TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "pins.json")
            add_pin("ETHUSDT", "binance_futures", path)
            pins = load_pins(path)
            self.assertEqual(len(pins), 1)
            self.assertEqual(pins[0]["symbol"], "ETHUSDT")
            remove_pin("ETHUSDT", "binance_futures", path)
            self.assertEqual(load_pins(path), [])

    def test_merge_config_pins_with_store(self) -> None:
        with TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "pins.json")
            Path(path).write_text(
                json.dumps({"symbols": [{"symbol": "SOLUSDT", "market": "binance_futures"}]}),
                encoding="utf-8",
            )
            merged = merge_config_pins(({"symbol": "SPCXUSDT", "market": "binance_futures"},), path)
            symbols = {item["symbol"] for item in merged}
            self.assertEqual(symbols, {"SPCXUSDT", "SOLUSDT"})


if __name__ == "__main__":
    unittest.main()

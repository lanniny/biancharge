"""Repair false-positive external_sl_tp outcomes and rebuild trade-learning snapshot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trade_outcomes import repair_trade_outcomes_file, trade_learning_from_config


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair trade-outcomes false positives.")
    parser.add_argument("--config", default="market_autotrader.growth.example.json")
    parser.add_argument("--live-state", default="logs/live-trading-state.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config_path = Path(args.config)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    learning_cfg = trade_learning_from_config(raw.get("trade_learning"))
    report = repair_trade_outcomes_file(
        learning_cfg,
        live_state_path=args.live_state,
        dry_run=args.dry_run,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Summarize last --once stdout or ledger tail."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    if not sys.stdin.isatty() and sys.stdin.readable():
        text = sys.stdin.read()
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "action" in obj:
                rows.append(obj)
    else:
        ledger = ROOT / "logs" / "market-autotrader-live-decisions.jsonl"
        lines = ledger.read_text(encoding="utf-8").splitlines()
        rows = [json.loads(l) for l in lines[-45:] if l.strip()]

    halt = [r for r in rows if any("Daily drag" in str(b) for b in r.get("blocked_reasons", []))]
    print(f"decisions={len(rows)} daily_drag_halt={len(halt)}")
    for r in rows[-5:]:
        tr = r.get("trade_rationale") or {}
        gm = tr.get("growthMetrics") or {}
        blocked = r.get("blocked_reasons") or []
        print(
            f"{r.get('symbol')} {r.get('action')} {r.get('status')} "
            f"drag={gm.get('dailyDrag')} cap={gm.get('effectiveMaxDailyLoss')} "
            f"halt={'Daily drag' in str(blocked)} "
            f"holding={'positionHolding' in tr}"
        )


if __name__ == "__main__":
    main()

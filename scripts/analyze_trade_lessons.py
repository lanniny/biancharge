#!/usr/bin/env python3
"""Backfill openContext on outcomes from ledger, refresh trade-lessons.json."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trade_lessons import refresh_trade_lessons, trade_lessons_from_config
from trade_outcomes import TradeLearningConfig, load_outcomes_jsonl, trade_learning_from_config, write_outcomes_jsonl


def _load_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _find_last_open(symbol: str, ledger_path: Path) -> dict | None:
    symbol = symbol.upper()
    if not ledger_path.exists():
        return None
    with ledger_path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - 6_000_000))
        chunk = handle.read().decode("utf-8", errors="ignore")
    best: dict | None = None
    best_ts = -1
    for line in chunk.splitlines():
        line = line.strip()
        if not line or symbol not in line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("symbol") != symbol:
            continue
        ex = row.get("execution_details") or {}
        if not ex.get("response") or ex.get("error"):
            continue
        kind = str(ex.get("intentKind", ""))
        if "open" not in kind:
            continue
        ts = int(row.get("timestamp") or 0)
        if ts <= best_ts:
            continue
        tr = row.get("trade_rationale") or {}
        md = tr.get("marketDiscovery") or {}
        ind = row.get("indicators") or {}
        best_ts = ts
        best = {
            "regime": ind.get("regime"),
            "bucket": md.get("bucket") or md.get("source"),
            "source": md.get("source"),
            "priceChangePct24h": str(ind.get("price_change_pct_24h") or md.get("priceChangePct24h") or "0"),
            "confidence": str(tr.get("confidence") or "0"),
            "rsi": str(ind.get("rsi", "0")),
            "momentum": str(ind.get("momentum", "0")),
            "fusionBullPct": str(ind.get("fusion_bull_pct", "")),
            "openTimestamp": ts,
            "intentKind": kind,
        }
    return best


def enrich_outcomes(cfg: TradeLearningConfig, ledger_path: Path, *, dry_run: bool) -> dict:
    rows = load_outcomes_jsonl(cfg.outcomes_path)
    enriched = 0
    for row in rows:
        if row.get("openContext"):
            continue
        ctx = _find_last_open(str(row.get("symbol", "")), ledger_path)
        if ctx:
            row["openContext"] = ctx
            enriched += 1
    if not dry_run and enriched:
        write_outcomes_jsonl(cfg.outcomes_path, rows)
    return {"enriched": enriched, "total": len(rows)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze trade outcomes and refresh lessons.")
    parser.add_argument("--config", default="market_autotrader.growth.example.json")
    parser.add_argument("--ledger", default="logs/market-autotrader-live-decisions.jsonl")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    raw = _load_config(ROOT / args.config)
    learning_cfg = trade_learning_from_config(raw.get("trade_learning"))
    lessons_cfg = trade_lessons_from_config(raw.get("trade_lessons"))

    enrich_report = enrich_outcomes(learning_cfg, ROOT / args.ledger, dry_run=args.dry_run)
    doc = refresh_trade_lessons(lessons_cfg, learning_cfg.outcomes_path)
    print(
        json.dumps(
            {"enrich": enrich_report, "lessons": doc.get("summary"), "losses": doc.get("losses"), "wins": doc.get("wins")},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

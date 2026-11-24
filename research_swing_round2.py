"""Second 4h long/short pass. Rules in ROUND2_GRIDS are already frozen.

This reruns the same walk-forward gates on the same stitched window. That is a
second look, so a pass here is not a clean out-of-sample result. The reserved
segment from 2026-08-01 is scored only when one family survives the gates.
"""

from __future__ import annotations

import json
from pathlib import Path

from research_swing_study import run_swing_study
from strategy_candidates import ROUND2_GRIDS, preset_matches


def main() -> int:
    config = json.loads(Path("leverage_study.example.json").read_text(encoding="utf-8"))
    report = run_swing_study(config, ROUND2_GRIDS, iteration=2)
    reason = report["recommendation"]["reason"]
    if reason == "no_candidate_survived_walk_forward_and_cost_stress" and report["recommendation"]["holdout"] is not None:
        raise RuntimeError("the reserved segment was scored even though no candidate passed")
    target = Path("artifacts/research/swing-walkforward-round2.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["recommendation"], ensure_ascii=False, indent=2))
    for name, family in report["families"].items():
        row = family["walk_forward"]
        stress = report["cost_stress"][name]
        print(
            name,
            "net", row["net_pnl"],
            "sharpe", row["sharpe"],
            "dd", row["max_drawdown"],
            "trades", row["trades"],
            "liq", row["liquidations"],
            "stress", stress["net_pnl"],
            "long", row["by_side"]["long"],
            "short", row["by_side"]["short"],
        )
    swing_path = Path("paper_forward.swing.example.json")
    winner = report["recommendation"]["strategy"]
    if winner is None:
        if swing_path.exists():
            raise RuntimeError("no round-2 winner, so a startable swing config must not be present")
        return 0
    preset = report["recommendation"]["holdout"]["preset"]
    if preset.get("allow_short") is not True:
        raise RuntimeError("recommended paper preset must allow shorts")
    payload = {
        "mode": "paper",
        "symbol": "BTCUSDT",
        "product": "binance_usdt_perp",
        "starting_equity": "10000",
        "leverage_policy": "risk_bounded_dynamic",
        "max_contract_leverage": 50,
        "bar_interval": "4h",
        "fee_bps": "5",
        "slippage_bps": "2",
        "risk_per_trade_pct": "0.01",
        "maintenance_rate": "0.004",
        "liquidation_fee_rate": "0.005",
        "extra_buffer": "0.001",
        "stale_mark_seconds": 30,
        "stale_book_seconds": 15,
        "heartbeat_seconds": 30,
        "state_path": "paper-state/btcusdt-swing-state.json",
        "log_path": "paper-state/btcusdt-swing.jsonl",
        "stop_path": "paper-state/btcusdt-swing.stop",
        "strategy": {"name": preset["name"], "preset": preset},
        "feed": {
            "type": "binance_usdt_m_public_ws",
            "mark_kline_url": "wss://fstream.binance.com/market/stream?streams=btcusdt@markPrice@1s/btcusdt@kline_4h",
            "book_url": "wss://fstream.binance.com/ws/btcusdt@bookTicker",
        },
        "notes": [
            "第二套纸面进程。不读取、不覆盖 paper-state/btcusdt-paper-state.json。",
            "做多和做空都允许。止损距离来自信号当时的 ATR，强平价必须留在止损之外。",
            "这是同一段走步样本外上的第二次迭代，不能当成未见过的样本。",
        ],
    }
    if not any(preset_matches(item, preset) for item in ROUND2_GRIDS[winner]):
        raise RuntimeError("winner preset is not in the frozen round-2 grid")
    swing_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("wrote", swing_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

import json
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from market_autotrader import TradingMemory
from trading_pipeline import (
    DecisionHandoff,
    DecisionWorker,
    ExecutionWorker,
    MarketPoller,
    PollerHandoff,
    TradingPipeline,
    decision_from_dict,
    decision_to_dict,
    pipeline_from_config,
    signal_to_dict,
    should_override_discovery_gate,
    snapshot_from_dict,
    snapshot_to_dict,
)
from market_autotrader import (
    BUY,
    ExecutionRecord,
    PaperPortfolio,
    Position,
    AssetConfig,
    MarketBar,
    MarketSnapshot,
    OrderIntent,
    RiskDecision,
    Signal,
    decimal_from,
)


STATIC_CONFIG = {
    "ledger_path": "logs/test-pipeline.jsonl",
    "portfolio": {"cash": {"USDT": "100"}, "positions": {}},
    "risk": {"mode": "paper", "min_confidence": "0.5", "require_reason_count": 2},
    "execution": {"mode": "paper"},
    "strategy": {"min_buy_confidence": "0.55"},
    "market_discovery": {"enabled": False},
    "assets": [
        {
            "symbol": "TESTUSDT",
            "market": "crypto_spot_demo",
            "base_asset": "TEST",
            "quote_asset": "USDT",
            "provider": {
                "type": "static",
                "bars": [
                    {
                        "timestamp": idx,
                        "open": "100",
                        "high": "101",
                        "low": "99",
                        "close": str(100 + idx * 0.1),
                        "volume": "1000",
                    }
                    for idx in range(40)
                ],
            },
        }
    ],
}


class TradingPipelineTests(unittest.TestCase):
    def test_pipeline_from_config_defaults_disabled(self) -> None:
        self.assertFalse(pipeline_from_config({}).enabled)

    def test_snapshot_roundtrip(self) -> None:
        asset = AssetConfig(
            symbol="ABCUSDT",
            market="binance_futures",
            base_asset="ABC",
            quote_asset="USDT",
            provider={"type": "static"},
        )
        bars = [
            MarketBar(1, decimal_from("1"), decimal_from("2"), decimal_from("0.5"), decimal_from("1.5"), decimal_from("10"))
        ]
        snap = MarketSnapshot(asset=asset, bars=bars, observed_at=123)
        restored = snapshot_from_dict(snapshot_to_dict(snap))
        self.assertEqual(restored.asset.symbol, "ABCUSDT")
        self.assertEqual(len(restored.bars), 1)

    def test_full_pipeline_cycle_writes_handoffs(self) -> None:
        with TemporaryDirectory() as tmp:
            handoff_dir = Path(tmp) / "pipeline"
            config = dict(STATIC_CONFIG)
            config["pipeline"] = {
                "enabled": True,
                "handoff_dir": str(handoff_dir),
                "persist_handoffs": True,
                "state_path": str(handoff_dir / "state.json"),
            }
            config["ledger_path"] = str(Path(tmp) / "ledger.jsonl")

            records = TradingPipeline(config, memory=TradingMemory()).run_cycle()

            self.assertTrue((handoff_dir / "latest-poller.json").exists())
            self.assertTrue((handoff_dir / "latest-decisions.json").exists())
            self.assertTrue((handoff_dir / "latest-execution.json").exists())
            self.assertGreaterEqual(len(records), 1)

    def test_standalone_workers(self) -> None:
        with TemporaryDirectory() as tmp:
            handoff_dir = Path(tmp) / "pipeline"
            config = dict(STATIC_CONFIG)
            config["pipeline"] = {
                "enabled": True,
                "handoff_dir": str(handoff_dir),
                "persist_handoffs": True,
                "state_path": str(handoff_dir / "state.json"),
            }
            config["ledger_path"] = str(Path(tmp) / "ledger.jsonl")
            pipeline = TradingPipeline(config, memory=TradingMemory())

            poller_out = pipeline.run_poller_only()
            self.assertEqual(poller_out.asset_count, 1)

            decision_out = pipeline.run_decision_only(poller_out.cycle_id)
            self.assertEqual(len(decision_out.pending), 1)

            records = pipeline.run_execution_only(poller_out.cycle_id)
            self.assertEqual(len(records), 1)

    def test_decision_handoff_preserves_effective_risk_for_growth_scheduler(self) -> None:
        with TemporaryDirectory() as tmp:
            handoff_dir = Path(tmp) / "pipeline"
            config = dict(STATIC_CONFIG)
            config["pipeline"] = {
                "enabled": True,
                "handoff_dir": str(handoff_dir),
                "persist_handoffs": True,
                "state_path": str(handoff_dir / "state.json"),
            }
            config["portfolio"] = {"cash": {"USDT_FUTURES": "200"}, "positions": {}}
            config["risk"] = {
                "mode": "paper",
                "min_confidence": "0.5",
                "require_reason_count": 1,
                "max_concurrent_positions": 8,
                "max_daily_trades": 0,
                "cooldown_seconds": 600,
            }
            config["capital_scaling"] = {
                "enabled": True,
                "growth_scheduler": {"enabled": True, "min_sample": 10},
            }
            config["trade_learning"] = {
                "enabled": True,
                "state_path": str(Path(tmp) / "learning.json"),
                "outcomes_path": str(Path(tmp) / "outcomes.jsonl"),
                "shadow_outcomes_path": str(Path(tmp) / "shadow.jsonl"),
            }
            outcomes = Path(config["trade_learning"]["outcomes_path"])
            rows = [
                {"symbol": f"WIN{idx}USDT", "realizedPnl": "0.10", "closedAt": idx, "bucket": "configured"}
                for idx in range(5)
            ]
            rows.extend(
                {"symbol": f"LOSS{idx}USDT", "realizedPnl": "-0.20", "closedAt": idx + 5, "bucket": "configured"}
                for idx in range(15)
            )
            outcomes.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

            pipeline = TradingPipeline(config, memory=TradingMemory())
            poller_out = pipeline.run_poller_only()
            decision_out = pipeline.run_decision_only(poller_out.cycle_id)
            decision_payload = decision_out.pending[0]["decision"]
            decision = decision_from_dict(decision_payload)
            roundtripped = decision_from_dict(decision_to_dict(decision))

            self.assertIsNotNone(roundtripped.effective_risk)
            self.assertEqual(roundtripped.effective_risk.max_daily_trades, 30)
            self.assertEqual(roundtripped.effective_risk.cooldown_seconds, 900)
            guardrails = decision_out.pending[0]["rationale"]["emotionGuardrails"]
            self.assertIn("Daily trade cap 30", guardrails[0])
            self.assertIn("Cooldown 900s", guardrails[1])

    def test_strong_discovery_candidate_can_override_analysis_only_gate(self) -> None:
        signal = Signal("BUY", Decimal("0.99"), ["strong"], [], {})
        decision = RiskDecision(
            True,
            "BUY",
            ["strong"],
            [],
            OrderIntent("BUY", "TESTUSDT", "binance_futures", "USDT", Decimal("50"), Decimal("1")),
        )
        self.assertTrue(
            should_override_discovery_gate(
                decision=decision,
                signal=signal,
                watch_entry={
                    "source": "discovery:futuresGainers",
                    "bucket": "futuresGainers",
                    "regime_pass": False,
                },
                trade_learning={"shadowFirstBuckets": [], "bucketLiveModes": {"futuresGainers": "live"}},
            )
        )

    def test_entry_timing_strong_continuation_can_override_discovery_gate(self) -> None:
        signal = Signal(
            "BUY",
            Decimal("0.96"),
            ["strong"],
            [],
            {
                "entry_timing_strong_continuation": "true",
                "entry_timing_strong_continuation_score": "0.82",
            },
        )
        decision = RiskDecision(
            True,
            "BUY",
            ["strong"],
            [],
            OrderIntent("BUY", "TESTUSDT", "binance_futures", "USDT", Decimal("50"), Decimal("1")),
        )
        self.assertTrue(
            should_override_discovery_gate(
                decision=decision,
                signal=signal,
                watch_entry={
                    "source": "discovery:futuresGainers",
                    "bucket": "futuresGainers",
                    "regime_pass": False,
                },
                trade_learning={"shadowFirstBuckets": [], "bucketLiveModes": {"futuresGainers": "live"}},
            )
        )

    def test_discovery_override_does_not_bypass_other_blocks_or_shadow(self) -> None:
        signal = Signal("BUY", Decimal("0.99"), ["strong"], [], {})
        order = OrderIntent("BUY", "TESTUSDT", "binance_futures", "USDT", Decimal("50"), Decimal("1"))
        blocked = RiskDecision(False, "BLOCKED", ["strong"], ["Entry timing: wait"], order)
        self.assertFalse(
            should_override_discovery_gate(
                decision=blocked,
                signal=signal,
                watch_entry={"source": "discovery:futuresGainers", "bucket": "futuresGainers"},
                trade_learning={"shadowFirstBuckets": [], "bucketLiveModes": {"futuresGainers": "live"}},
            )
        )
        approved = RiskDecision(True, "BUY", ["strong"], [], order)
        self.assertFalse(
            should_override_discovery_gate(
                decision=approved,
                signal=signal,
                watch_entry={"source": "discovery:futuresLosers", "bucket": "futuresLosers"},
                trade_learning={
                    "shadowFirstBuckets": ["futuresLosers"],
                    "bucketLiveModes": {"futuresLosers": "shadow_first"},
                },
            )
        )

    def test_live_execution_preflight_rechecks_heat_after_prior_fill(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            arm_path = tmp_path / "armed"
            arm_path.write_text("armed", encoding="utf-8")
            config = {
                "ledger_path": str(tmp_path / "ledger.jsonl"),
                "risk": {
                    "mode": "live",
                    "allow_live_trading": True,
                    "live_arm_path": str(arm_path),
                    "kill_switch_path": str(tmp_path / "kill"),
                    "min_confidence": "0.5",
                    "require_reason_count": 1,
                    "max_trade_quote": "50",
                    "min_trade_quote": "1",
                    "reserve_futures_available_usdt": "0",
                    "max_portfolio_heat_pct": "0.50",
                    "max_daily_trades": 0,
                },
                "execution": {"mode": "live", "state_path": str(tmp_path / "state.json")},
                "strategy": {"buy_quote_fraction": "0.10", "min_buy_confidence": "0.50"},
                "auto_execution": {"default_leverage": 5, "max_leverage": 5},
                "market_discovery": {"enabled": False},
                "assets": [],
            }
            low = PaperPortfolio(cash={"USDT_FUTURES": Decimal("100")}, wallet_balance=Decimal("100"))
            hot = PaperPortfolio(
                cash={"USDT_FUTURES": Decimal("40")},
                positions={
                    "FILLEDUSDT": Position(
                        quantity=Decimal("1"),
                        average_price=Decimal("300"),
                        initial_margin=Decimal("60"),
                        notional=Decimal("300"),
                        leverage=5,
                    )
                },
                wallet_balance=Decimal("100"),
            )
            asset_a = AssetConfig("AAAUSDT", "binance_futures", "AAA", "USDT", {"type": "static"})
            asset_b = AssetConfig("BBBUSDT", "binance_futures", "BBB", "USDT", {"type": "static"})
            snap_a = MarketSnapshot(
                asset_a,
                [MarketBar(1, Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1"), Decimal("100"))],
                observed_at=1_000,
            )
            snap_b = MarketSnapshot(
                asset_b,
                [MarketBar(1, Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1"), Decimal("100"))],
                observed_at=1_001,
            )
            signal = Signal(
                BUY,
                Decimal("0.90"),
                ["strong"],
                [],
                {"volatility": "0", "drawdown": "0", "atr_pct": "0.01"},
            )

            def pending_item(snapshot: MarketSnapshot) -> dict[str, object]:
                order = OrderIntent(
                    BUY,
                    snapshot.asset.symbol,
                    "binance_futures",
                    "USDT",
                    Decimal("50"),
                    Decimal("1"),
                    leverage=5,
                    intent_kind="open_long",
                )
                decision = RiskDecision(True, BUY, ["strong"], [], order)
                return {
                    "snapshot": snapshot_to_dict(snapshot),
                    "signal": signal_to_dict(signal),
                    "decision": decision_to_dict(decision),
                    "rationale": {},
                }

            poller = PollerHandoff(
                cycle_id="cycle-test",
                polled_at=1_000,
                portfolio=low.snapshot(),
                snapshots=[snapshot_to_dict(snap_a), snapshot_to_dict(snap_b)],
                asset_count=2,
            )
            decisions = DecisionHandoff(
                cycle_id="cycle-test",
                decided_at=1_000,
                portfolio=low.snapshot(),
                pending=[pending_item(snap_a), pending_item(snap_b)],
            )

            def fake_execute(snapshot, decision, signal, strategy, risk, portfolio, memory, execution, rationale, **kwargs):
                return ExecutionRecord(
                    status="executed_live" if decision.approved else "blocked",
                    mode="live",
                    symbol=snapshot.asset.symbol,
                    action=decision.action,
                    quantity=Decimal("1") if decision.approved else Decimal("0"),
                    quote_amount=decision.order.quote_amount if decision.order else Decimal("0"),
                    price=snapshot.price,
                    reasons=decision.reasons,
                    blocked_reasons=decision.blocked_reasons,
                    indicators=signal.indicators,
                    portfolio=portfolio.snapshot(),
                    timestamp=snapshot.observed_at,
                )

            worker = ExecutionWorker(config, TradingMemory())
            with (
                patch("trading_pipeline.fetch_live_portfolio", side_effect=[low, hot, hot, hot]) as refresh,
                patch("trading_pipeline.execute_decision", side_effect=fake_execute),
                patch(
                    "market_autotrader.fetch_symbol_filters",
                    return_value={"step_size": Decimal("1"), "min_qty": Decimal("1"), "min_notional": Decimal("1")},
                ),
            ):
                records = worker.run(poller, decisions)

            self.assertEqual([record.status for record in records], ["executed_live", "blocked"])
            self.assertGreaterEqual(refresh.call_count, 3)
            self.assertIn("Execution preflight", records[1].blocked_reasons[0])
            self.assertTrue(any("Portfolio heat" in reason for reason in records[1].blocked_reasons))


if __name__ == "__main__":
    unittest.main()

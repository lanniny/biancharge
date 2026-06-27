"""Tests for trade_outcomes learning module."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from trade_outcomes import (
    apply_bucket_sizing_factor,
    apply_sizing_factor,
    compute_trade_learning_snapshot,
    record_trade_outcome,
    required_confidence_with_learning,
    resolve_discovery_live_mode,
    resolve_exit_quality_factor,
    trade_learning_block_reason,
    trade_learning_discovery_shadow_first,
    trade_learning_from_config,
)


def test_apply_sizing_factor_scales_down():
    snapshot = {"enabled": True, "sizingFactor": "0.70"}
    result = apply_sizing_factor(Decimal("100"), snapshot)
    assert result == Decimal("70.00")


def test_apply_bucket_sizing_factor_scales_bucket():
    snapshot = {"enabled": True, "bucketSizingFactors": {"configured": "1.10", "pinned": "0.65"}}
    result, factor = apply_bucket_sizing_factor(
        Decimal("100"), snapshot, bucket="configured", source="configured"
    )
    assert result == Decimal("110.00")
    assert factor == Decimal("1.10")
    pinned, pinned_factor = apply_bucket_sizing_factor(
        Decimal("100"), snapshot, bucket="", source="pinned"
    )
    assert pinned == Decimal("65.00")
    assert pinned_factor == Decimal("0.65")


def test_required_confidence_bump():
    snapshot = {"enabled": True, "confidenceBump": "0.05"}
    base = Decimal("0.65")
    assert required_confidence_with_learning(base, snapshot) == Decimal("0.70")


def test_loss_streak_blocks_new_opens(tmp_path: Path):
    outcomes = tmp_path / "outcomes.jsonl"
    cfg = trade_learning_from_config(
        {
            "enabled": True,
            "outcomes_path": str(outcomes),
            "state_path": str(tmp_path / "state.json"),
            "loss_streak_cooldown": 2,
        }
    )
    for _ in range(2):
        record_trade_outcome(
            cfg,
            symbol="TESTUSDT",
            side="SELL",
            quantity=Decimal("1"),
            exit_price=Decimal("9"),
            entry_price=Decimal("10"),
            position_side="LONG",
            regime="range",
            session="亚盘",
            rationale_summary="test",
        )
    snapshot = compute_trade_learning_snapshot(cfg)
    reason = trade_learning_block_reason(snapshot, symbol="TESTUSDT", is_reduce_only=False, cfg=cfg)
    assert reason is not None
    assert "consecutive losses" in reason


def test_winning_close_unblocks(tmp_path: Path):
    outcomes = tmp_path / "outcomes.jsonl"
    cfg = trade_learning_from_config(
        {
            "enabled": True,
            "outcomes_path": str(outcomes),
            "state_path": str(tmp_path / "state.json"),
            "loss_streak_cooldown": 3,
        }
    )
    record_trade_outcome(
        cfg,
        symbol="WINUSDT",
        side="SELL",
        quantity=Decimal("1"),
        exit_price=Decimal("11"),
        entry_price=Decimal("10"),
        position_side="LONG",
        regime="trend_up",
        session=None,
        rationale_summary=None,
    )
    snapshot = compute_trade_learning_snapshot(cfg)
    assert snapshot["wins"] == 1
    reason = trade_learning_block_reason(snapshot, symbol="WINUSDT", is_reduce_only=False, cfg=cfg)
    assert reason is None


def test_short_pnl_sign(tmp_path: Path):
    outcomes = tmp_path / "out.jsonl"
    cfg = trade_learning_from_config({"enabled": True, "outcomes_path": str(outcomes)})
    row = record_trade_outcome(
        cfg,
        symbol="XUSDT",
        side="BUY",
        quantity=Decimal("2"),
        exit_price=Decimal("95"),
        entry_price=Decimal("100"),
        position_side="SHORT",
        regime=None,
        session=None,
        rationale_summary=None,
    )
    assert Decimal(row["realizedPnl"]) == Decimal("10.0")


def _record_with_bucket(
    cfg,
    *,
    symbol: str,
    pnl: Decimal,
    bucket: str,
) -> None:
    entry = Decimal("10")
    exit_price = entry + pnl
    record_trade_outcome(
        cfg,
        symbol=symbol,
        side="SELL",
        quantity=Decimal("1"),
        exit_price=exit_price,
        entry_price=entry,
        position_side="LONG",
        regime="range",
        session=None,
        rationale_summary=None,
        open_context={"bucket": bucket, "source": f"discovery:{bucket}"},
    )


def test_low_win_rate_uses_shadow_first_not_hard_block(tmp_path: Path):
    outcomes = tmp_path / "outcomes.jsonl"
    cfg = trade_learning_from_config(
        {
            "enabled": True,
            "outcomes_path": str(outcomes),
            "state_path": str(tmp_path / "state.json"),
            "min_win_rate_block": "0.35",
            "min_sample_for_win_rate_block": 8,
        }
    )
    for i in range(8):
        pnl = Decimal("1") if i == 0 else Decimal("-0.5")
        _record_with_bucket(cfg, symbol=f"SYM{i}USDT", pnl=pnl, bucket="futuresGainers")
    snapshot = compute_trade_learning_snapshot(cfg)
    reason = trade_learning_block_reason(
        snapshot, symbol="NEWUSDT", is_reduce_only=False, cfg=cfg, is_discovery_open=True
    )
    assert reason is None
    shadow, shadow_reason = trade_learning_discovery_shadow_first(
        snapshot,
        cfg=cfg,
        bucket="futuresGainers",
        is_discovery_open=True,
    )
    assert shadow is True
    assert shadow_reason is not None
    assert "shadow paper" in shadow_reason


def test_low_win_rate_does_not_block_non_discovery(tmp_path: Path):
    outcomes = tmp_path / "outcomes.jsonl"
    cfg = trade_learning_from_config(
        {
            "enabled": True,
            "outcomes_path": str(outcomes),
            "state_path": str(tmp_path / "state.json"),
            "min_win_rate_block": "0.35",
            "min_sample_for_win_rate_block": 8,
        }
    )
    for i in range(8):
        record_trade_outcome(
            cfg,
            symbol=f"SYM{i}USDT",
            side="SELL",
            quantity=Decimal("1"),
            exit_price=Decimal("9"),
            entry_price=Decimal("10"),
            position_side="LONG",
            regime="range",
            session=None,
            rationale_summary=None,
        )
    snapshot = compute_trade_learning_snapshot(cfg)
    reason = trade_learning_block_reason(
        snapshot, symbol="NEWUSDT", is_reduce_only=False, cfg=cfg, is_discovery_open=False
    )
    assert reason is None
    shadow, _ = trade_learning_discovery_shadow_first(
        snapshot, cfg=cfg, bucket="", is_discovery_open=False
    )
    assert shadow is False


def test_bucket_shadow_only_for_weak_bucket(tmp_path: Path):
    outcomes = tmp_path / "outcomes.jsonl"
    cfg = trade_learning_from_config(
        {
            "enabled": True,
            "outcomes_path": str(outcomes),
            "state_path": str(tmp_path / "state.json"),
            "min_win_rate_block": "0.35",
            "min_win_rate_unblock": "0.40",
            "min_sample_for_win_rate_block": 20,
            "min_bucket_sample": 4,
            "bucket_win_rate_enabled": True,
        }
    )
    for i in range(4):
        _record_with_bucket(cfg, symbol=f"BAD{i}USDT", pnl=Decimal("-1"), bucket="futuresLosers")
    for i in range(2):
        _record_with_bucket(cfg, symbol=f"GOOD{i}USDT", pnl=Decimal("2"), bucket="futuresGainers")
    snapshot = compute_trade_learning_snapshot(cfg)
    assert snapshot["discoveryLiveMode"] == "live"
    assert snapshot["bucketLiveModes"]["futuresLosers"] == "shadow_first"
    assert snapshot["bucketLiveModes"]["futuresGainers"] == "live"

    losers_shadow, losers_reason = trade_learning_discovery_shadow_first(
        snapshot, cfg=cfg, bucket="futuresLosers", is_discovery_open=True
    )
    gainers_shadow, _ = trade_learning_discovery_shadow_first(
        snapshot, cfg=cfg, bucket="futuresGainers", is_discovery_open=True
    )
    assert losers_shadow is True
    assert losers_reason is not None
    assert "futuresLosers" in losers_reason
    assert gainers_shadow is False


def test_low_win_rate_profitable_bucket_stays_live_and_unshrunk(tmp_path: Path):
    outcomes = tmp_path / "outcomes.jsonl"
    cfg = trade_learning_from_config(
        {
            "enabled": True,
            "outcomes_path": str(outcomes),
            "state_path": str(tmp_path / "state.json"),
            "min_win_rate_block": "0.35",
            "min_win_rate_unblock": "0.40",
            "min_bucket_sample": 6,
            "min_bucket_sample_for_sizing": 4,
            "bucket_win_rate_enabled": True,
            "bucket_sizing_enabled": True,
        }
    )
    for i in range(2):
        _record_with_bucket(cfg, symbol=f"BIGWIN{i}USDT", pnl=Decimal("1.50"), bucket="futuresTopVolume")
    for i in range(9):
        _record_with_bucket(cfg, symbol=f"SMALLLOSS{i}USDT", pnl=Decimal("-0.10"), bucket="futuresTopVolume")
    snapshot = compute_trade_learning_snapshot(cfg)

    assert snapshot["bucketStats"]["futuresTopVolume"]["winRate"] == "0.18"
    assert Decimal(snapshot["bucketStats"]["futuresTopVolume"]["profitFactor"]) > Decimal("1.20")
    assert snapshot["bucketLiveModes"]["futuresTopVolume"] == "live"
    assert "futuresTopVolume" not in snapshot["bucketSizingFactors"]


def test_global_low_win_rate_positive_expectancy_not_shrunk_or_bumped(tmp_path: Path):
    outcomes = tmp_path / "outcomes.jsonl"
    cfg = trade_learning_from_config(
        {
            "enabled": True,
            "outcomes_path": str(outcomes),
            "state_path": str(tmp_path / "state.json"),
            "lookback_trades": 8,
            "global_loss_rate_block": "0.65",
            "sizing_penalty_per_loss": "0.10",
            "min_sizing_factor": "0.75",
            "min_confidence_bump": "0.02",
        }
    )
    for i, pnl in enumerate(["3", "3", "-0.5", "-0.5", "-0.5", "-0.5", "-0.5", "-0.5"]):
        _record_with_bucket(cfg, symbol=f"EDGE{i}USDT", pnl=Decimal(pnl), bucket="futuresLosers")

    snapshot = compute_trade_learning_snapshot(cfg)
    assert snapshot["winRate"] == "0.25"
    assert Decimal(snapshot["profitFactor"]) > Decimal("1.20")
    assert snapshot["sizingFactor"] == "1.00"
    assert snapshot["confidenceBump"] == "0"


def test_hysteresis_band_keeps_shadow_until_unblock_threshold():
    cfg = trade_learning_from_config(
        {
            "min_win_rate_block": "0.35",
            "min_win_rate_unblock": "0.40",
            "min_sample_for_win_rate_block": 10,
        }
    )
    assert (
        resolve_discovery_live_mode(
            win_rate=Decimal("0.37"),
            sample=10,
            cfg=cfg,
            previous_mode="shadow_first",
        )
        == "shadow_first"
    )
    assert (
        resolve_discovery_live_mode(
            win_rate=Decimal("0.42"),
            sample=10,
            cfg=cfg,
            previous_mode="shadow_first",
        )
        == "live"
    )


def test_shadow_graduation_restores_bucket_live(tmp_path: Path):
    outcomes = tmp_path / "live-outcomes.jsonl"
    shadow_outcomes = tmp_path / "shadow-outcomes.jsonl"
    cfg = trade_learning_from_config(
        {
            "enabled": True,
            "outcomes_path": str(outcomes),
            "state_path": str(tmp_path / "state.json"),
            "shadow_outcomes_path": str(shadow_outcomes),
            "min_win_rate_block": "0.35",
            "min_win_rate_unblock": "0.40",
            "min_sample_for_win_rate_block": 20,
            "min_bucket_sample": 2,
            "min_shadow_bucket_sample": 3,
            "shadow_graduation_enabled": True,
        }
    )
    for _ in range(2):
        record_trade_outcome(
            cfg,
            symbol="BAD1USDT",
            side="SELL",
            quantity=Decimal("1"),
            exit_price=Decimal("9"),
            entry_price=Decimal("10"),
            position_side="LONG",
            regime="range",
            session=None,
            rationale_summary=None,
            open_context={"bucket": "futuresLosers", "source": "discovery:futuresLosers"},
        )
    from shadow_paper import append_shadow_trade

    for i in range(3):
        append_shadow_trade(
            str(shadow_outcomes),
            {
                "symbol": f"SH{i}USDT",
                "realizedPnl": "0.8" if i < 2 else "-0.2",
                "bucket": "futuresLosers",
                "closedAt": 100 + i,
            },
        )
    # seed shadow summary via evaluate isn't needed - summarize reads outcomes directly
    # but our shadow outcomes need to match summarize_shadow_outcomes format
    snapshot = compute_trade_learning_snapshot(cfg)
    assert snapshot["bucketLiveModes"]["futuresLosers"] == "live"
    assert "futuresLosers" in snapshot.get("bucketGraduations", {})


def test_shadow_graduation_uses_profitable_expectancy_even_with_low_win_rate(tmp_path: Path):
    outcomes = tmp_path / "live-outcomes.jsonl"
    shadow_outcomes = tmp_path / "shadow-outcomes.jsonl"
    cfg = trade_learning_from_config(
        {
            "enabled": True,
            "outcomes_path": str(outcomes),
            "state_path": str(tmp_path / "state.json"),
            "shadow_outcomes_path": str(shadow_outcomes),
            "min_win_rate_block": "0.35",
            "min_win_rate_unblock": "0.50",
            "min_bucket_sample": 2,
            "min_shadow_bucket_sample": 6,
            "shadow_graduation_enabled": True,
        }
    )
    for _ in range(2):
        _record_with_bucket(cfg, symbol="BADLIVEUSDT", pnl=Decimal("-1"), bucket="futuresLosers")

    from shadow_paper import append_shadow_trade

    for i, pnl in enumerate(["3", "3", "-1", "-1", "-1", "-1"]):
        append_shadow_trade(
            str(shadow_outcomes),
            {
                "symbol": f"SH{i}USDT",
                "realizedPnl": pnl,
                "bucket": "futuresLosers",
                "closedAt": 100 + i,
            },
        )

    snapshot = compute_trade_learning_snapshot(cfg)
    assert snapshot["bucketLiveModes"]["futuresLosers"] == "live"
    note = snapshot.get("bucketGraduations", {}).get("futuresLosers", "")
    assert "PF" in note
    assert "pnl" in note


def test_shadow_only_bucket_can_graduate_without_live_bucket_stats(tmp_path: Path):
    outcomes = tmp_path / "live-outcomes.jsonl"
    shadow_outcomes = tmp_path / "shadow-outcomes.jsonl"
    cfg = trade_learning_from_config(
        {
            "enabled": True,
            "outcomes_path": str(outcomes),
            "state_path": str(tmp_path / "state.json"),
            "shadow_outcomes_path": str(shadow_outcomes),
            "min_win_rate_unblock": "0.38",
            "min_shadow_bucket_sample": 4,
            "shadow_graduation_enabled": True,
        }
    )
    from shadow_paper import append_shadow_trade

    for i, pnl in enumerate(["1", "1", "-0.2", "-0.2"]):
        append_shadow_trade(
            str(shadow_outcomes),
            {
                "symbol": f"SHONLY{i}USDT",
                "realizedPnl": pnl,
                "bucket": "futuresLosers",
                "closedAt": 100 + i,
            },
        )

    snapshot = compute_trade_learning_snapshot(cfg)
    assert snapshot["bucketLiveModes"]["futuresLosers"] == "live"
    assert "futuresLosers" in snapshot.get("bucketGraduations", {})


def test_shadow_graduation_does_not_override_weak_live_bucket(tmp_path: Path):
    outcomes = tmp_path / "live-outcomes.jsonl"
    shadow_outcomes = tmp_path / "shadow-outcomes.jsonl"
    cfg = trade_learning_from_config(
        {
            "enabled": True,
            "outcomes_path": str(outcomes),
            "state_path": str(tmp_path / "state.json"),
            "shadow_outcomes_path": str(shadow_outcomes),
            "min_win_rate_block": "0.35",
            "min_win_rate_unblock": "0.40",
            "min_bucket_sample": 4,
            "min_shadow_bucket_sample": 3,
            "shadow_graduation_enabled": True,
        }
    )
    for i in range(4):
        _record_with_bucket(cfg, symbol=f"BAD{i}USDT", pnl=Decimal("-1"), bucket="futuresTopVolume")

    from shadow_paper import append_shadow_trade

    for i in range(4):
        append_shadow_trade(
            str(shadow_outcomes),
            {
                "symbol": f"SH{i}USDT",
                "realizedPnl": "1",
                "bucket": "futuresTopVolume",
                "closedAt": 100 + i,
            },
        )

    snapshot = compute_trade_learning_snapshot(cfg)
    assert snapshot["bucketLiveModes"]["futuresTopVolume"] == "shadow_first"
    assert "live lock" in snapshot.get("bucketGraduations", {}).get("futuresTopVolume", "")


def test_shadow_graduation_not_live_locked_when_live_bucket_is_profitable_edge(tmp_path: Path):
    outcomes = tmp_path / "live-outcomes.jsonl"
    shadow_outcomes = tmp_path / "shadow-outcomes.jsonl"
    cfg = trade_learning_from_config(
        {
            "enabled": True,
            "outcomes_path": str(outcomes),
            "state_path": str(tmp_path / "state.json"),
            "shadow_outcomes_path": str(shadow_outcomes),
            "min_win_rate_block": "0.35",
            "min_win_rate_unblock": "0.50",
            "min_bucket_sample": 4,
            "min_shadow_bucket_sample": 4,
            "shadow_graduation_enabled": True,
        }
    )
    for i, pnl in enumerate(["3", "-0.5", "-0.5", "-0.5"]):
        _record_with_bucket(cfg, symbol=f"LIVEEDGE{i}USDT", pnl=Decimal(pnl), bucket="futuresTopVolume")

    from shadow_paper import append_shadow_trade

    for i, pnl in enumerate(["1", "1", "-0.2", "-0.2"]):
        append_shadow_trade(
            str(shadow_outcomes),
            {
                "symbol": f"SHEDGE{i}USDT",
                "realizedPnl": pnl,
                "bucket": "futuresTopVolume",
                "closedAt": 100 + i,
            },
        )

    snapshot = compute_trade_learning_snapshot(cfg)
    assert snapshot["bucketStats"]["futuresTopVolume"]["winRate"] == "0.25"
    assert Decimal(snapshot["bucketStats"]["futuresTopVolume"]["profitFactor"]) > Decimal("1.20")
    assert snapshot["bucketLiveModes"]["futuresTopVolume"] == "live"
    assert "live lock" not in snapshot.get("bucketGraduations", {}).get("futuresTopVolume", "")


def test_bucket_shadow_even_when_global_sample_below_threshold(tmp_path: Path):
    outcomes = tmp_path / "outcomes.jsonl"
    cfg = trade_learning_from_config(
        {
            "enabled": True,
            "outcomes_path": str(outcomes),
            "state_path": str(tmp_path / "state.json"),
            "min_sample_for_win_rate_block": 15,
            "min_bucket_sample": 4,
        }
    )
    for i in range(8):
        _record_with_bucket(cfg, symbol=f"SYM{i}USDT", pnl=Decimal("-1"), bucket="futuresLosers")
    snapshot = compute_trade_learning_snapshot(cfg)
    assert snapshot["discoveryLiveMode"] == "live"
    shadow, reason = trade_learning_discovery_shadow_first(
        snapshot, cfg=cfg, bucket="futuresLosers", is_discovery_open=True
    )
    assert shadow is True
    assert reason is not None
    assert "futuresLosers" in reason


def test_bucket_live_mode_overrides_force_shadow_lane(tmp_path: Path):
    outcomes = tmp_path / "outcomes.jsonl"
    cfg = trade_learning_from_config(
        {
            "enabled": True,
            "outcomes_path": str(outcomes),
            "state_path": str(tmp_path / "state.json"),
            "min_bucket_sample": 4,
            "bucket_live_mode_overrides": {
                "futuresLosers": "shadow_first",
                "futuresGainers": "live",
            },
        }
    )
    for i in range(2):
        _record_with_bucket(cfg, symbol=f"WIN{i}USDT", pnl=Decimal("1"), bucket="futuresGainers")
    snapshot = compute_trade_learning_snapshot(cfg)
    assert snapshot["bucketLiveModes"]["futuresLosers"] == "shadow_first"
    assert snapshot["bucketLiveModes"]["futuresGainers"] == "live"
    losers_shadow, _ = trade_learning_discovery_shadow_first(
        snapshot, cfg=cfg, bucket="futuresLosers", is_discovery_open=True
    )
    gainers_shadow, _ = trade_learning_discovery_shadow_first(
        snapshot, cfg=cfg, bucket="futuresGainers", is_discovery_open=True
    )
    assert losers_shadow is True
    assert gainers_shadow is False


class TradeOutcomeUnittestCoverage(unittest.TestCase):
    def test_low_symbol_win_rate_blocks_even_without_loss_streak(self) -> None:
        snapshot = {
            "enabled": True,
            "symbolStats": {
                "TSLAUSDT": {"recentTrades": 5, "lossStreak": 1, "winRate": "0.20"}
            },
        }
        cfg = trade_learning_from_config(
            {
                "enabled": True,
                "min_symbol_sample_for_block": 4,
                "min_symbol_win_rate_block": "0.30",
            }
        )
        reason = trade_learning_block_reason(
            snapshot, symbol="TSLAUSDT", is_reduce_only=False, cfg=cfg
        )
        self.assertIsNotNone(reason)
        self.assertIn("recent win rate", reason)

    def test_low_symbol_win_rate_does_not_block_reduce_only(self) -> None:
        snapshot = {
            "enabled": True,
            "symbolStats": {
                "TSLAUSDT": {"recentTrades": 5, "lossStreak": 1, "winRate": "0.20"}
            },
        }
        cfg = trade_learning_from_config({"enabled": True})
        self.assertIsNone(
            trade_learning_block_reason(snapshot, symbol="TSLAUSDT", is_reduce_only=True, cfg=cfg)
        )

    def test_shadow_graduation_does_not_override_weak_live_bucket(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            outcomes = tmp_path / "live-outcomes.jsonl"
            shadow_outcomes = tmp_path / "shadow-outcomes.jsonl"
            cfg = trade_learning_from_config(
                {
                    "enabled": True,
                    "outcomes_path": str(outcomes),
                    "state_path": str(tmp_path / "state.json"),
                    "shadow_outcomes_path": str(shadow_outcomes),
                    "min_win_rate_block": "0.35",
                    "min_win_rate_unblock": "0.40",
                    "min_bucket_sample": 4,
                    "min_shadow_bucket_sample": 3,
                    "shadow_graduation_enabled": True,
                }
            )
            for i in range(4):
                _record_with_bucket(cfg, symbol=f"BAD{i}USDT", pnl=Decimal("-1"), bucket="futuresTopVolume")

            from shadow_paper import append_shadow_trade

            for i in range(4):
                append_shadow_trade(
                    str(shadow_outcomes),
                    {
                        "symbol": f"SH{i}USDT",
                        "realizedPnl": "1",
                        "bucket": "futuresTopVolume",
                        "closedAt": 100 + i,
                    },
                )

            snapshot = compute_trade_learning_snapshot(cfg)
            self.assertEqual(snapshot["bucketLiveModes"]["futuresTopVolume"], "shadow_first")
            self.assertIn("live lock", snapshot.get("bucketGraduations", {}).get("futuresTopVolume", ""))

            shadow, reason = trade_learning_discovery_shadow_first(
                snapshot, cfg=cfg, bucket="futuresTopVolume", is_discovery_open=True
            )
            self.assertTrue(shadow)
            self.assertIsNotNone(reason)

    def test_shadow_only_bucket_can_graduate_without_live_bucket_stats(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            outcomes = tmp_path / "live-outcomes.jsonl"
            shadow_outcomes = tmp_path / "shadow-outcomes.jsonl"
            cfg = trade_learning_from_config(
                {
                    "enabled": True,
                    "outcomes_path": str(outcomes),
                    "state_path": str(tmp_path / "state.json"),
                    "shadow_outcomes_path": str(shadow_outcomes),
                    "min_win_rate_unblock": "0.38",
                    "min_shadow_bucket_sample": 4,
                    "shadow_graduation_enabled": True,
                }
            )
            from shadow_paper import append_shadow_trade

            for i, pnl in enumerate(["1", "1", "-0.2", "-0.2"]):
                append_shadow_trade(
                    str(shadow_outcomes),
                    {
                        "symbol": f"SHONLY{i}USDT",
                        "realizedPnl": pnl,
                        "bucket": "futuresLosers",
                        "closedAt": 100 + i,
                    },
                )

            snapshot = compute_trade_learning_snapshot(cfg)
            self.assertEqual(snapshot["bucketLiveModes"]["futuresLosers"], "live")
            self.assertIn("futuresLosers", snapshot.get("bucketGraduations", {}))

    def test_bucket_sizing_factors_follow_live_expectancy(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            outcomes = tmp_path / "outcomes.jsonl"
            cfg = trade_learning_from_config(
                {
                    "enabled": True,
                    "outcomes_path": str(outcomes),
                    "state_path": str(tmp_path / "state.json"),
                    "min_bucket_sample_for_sizing": 4,
                    "bucket_sizing_enabled": True,
                    "bucket_win_sizing_mult": "1.10",
                    "bucket_loss_sizing_mult": "0.65",
                }
            )
            for i in range(4):
                _record_with_bucket(cfg, symbol=f"WIN{i}USDT", pnl=Decimal("1"), bucket="configured")
            for i in range(4):
                _record_with_bucket(cfg, symbol=f"LOSS{i}USDT", pnl=Decimal("-1"), bucket="pinned")

            snapshot = compute_trade_learning_snapshot(cfg)
            self.assertEqual(snapshot["sizingFactor"], "0.50")
            self.assertEqual(snapshot["bucketSizingFactors"]["configured"], "1.10")
            self.assertEqual(snapshot["bucketSizingFactors"]["pinned"], "0.65")
            self.assertEqual(snapshot["bucketStats"]["configured"]["totalPnl"], "4.0000")
            self.assertEqual(snapshot["bucketStats"]["pinned"]["profitFactor"], "0.00")

    def test_exit_quality_stats_use_mfe_mae_from_open_context(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            outcomes = tmp_path / "outcomes.jsonl"
            cfg = trade_learning_from_config(
                {
                    "enabled": True,
                    "outcomes_path": str(outcomes),
                    "state_path": str(tmp_path / "state.json"),
                }
            )
            record_trade_outcome(
                cfg,
                symbol="GOODUSDT",
                side="SELL",
                quantity=Decimal("1"),
                exit_price=Decimal("104"),
                entry_price=Decimal("100"),
                position_side="LONG",
                regime="trend_up",
                session=None,
                rationale_summary=None,
                open_context={
                    "bucket": "configured",
                    "source": "configured",
                    "mfePct": "0.1000",
                    "maePct": "-0.0200",
                },
            )
            snapshot = compute_trade_learning_snapshot(cfg)
            self.assertEqual(snapshot["exitQuality"]["sampleSize"], 1)
            self.assertEqual(snapshot["exitQuality"]["avgMfePct"], "0.1000")
            self.assertEqual(snapshot["exitQuality"]["avgMaePct"], "-0.0200")
            self.assertEqual(snapshot["exitQuality"]["avgMissedPct"], "0.0600")
            self.assertEqual(snapshot["exitQuality"]["avgCaptureRatio"], "0.40")
            self.assertEqual(
                snapshot["bucketStats"]["configured"]["exitQuality"]["avgCaptureRatio"],
                "0.40",
            )

    def test_exit_quality_factors_follow_bucket_capture_efficiency(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            outcomes = tmp_path / "outcomes.jsonl"
            cfg = trade_learning_from_config(
                {
                    "enabled": True,
                    "outcomes_path": str(outcomes),
                    "state_path": str(tmp_path / "state.json"),
                    "min_exit_quality_sample": 4,
                    "low_capture_exit_mult": "0.80",
                    "high_capture_exit_mult": "1.10",
                }
            )
            for i in range(4):
                record_trade_outcome(
                    cfg,
                    symbol=f"LOW{i}USDT",
                    side="SELL",
                    quantity=Decimal("1"),
                    exit_price=Decimal("104"),
                    entry_price=Decimal("100"),
                    position_side="LONG",
                    regime="range",
                    session=None,
                    rationale_summary=None,
                    open_context={"bucket": "lowCapture", "mfePct": "0.1000", "maePct": "-0.0200"},
                )
                record_trade_outcome(
                    cfg,
                    symbol=f"HIGH{i}USDT",
                    side="SELL",
                    quantity=Decimal("1"),
                    exit_price=Decimal("108"),
                    entry_price=Decimal("100"),
                    position_side="LONG",
                    regime="trend_up",
                    session=None,
                    rationale_summary=None,
                    open_context={"bucket": "highCapture", "mfePct": "0.1000", "maePct": "-0.0100"},
                )

            snapshot = compute_trade_learning_snapshot(cfg)
            self.assertEqual(snapshot["bucketExitQualityFactors"]["lowCapture"], "0.80")
            self.assertEqual(snapshot["bucketExitQualityFactors"]["highCapture"], "1.10")

    def test_exit_quality_factor_respects_sample_floor(self) -> None:
        cfg = trade_learning_from_config({"enabled": True, "min_exit_quality_sample": 4})
        factor = resolve_exit_quality_factor(
            {"sampleSize": 3, "avgCaptureRatio": "0.20", "avgMfePct": "0.10"},
            cfg,
        )
        self.assertEqual(factor, Decimal("1"))

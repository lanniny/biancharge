"""Tests for shadow paper evaluation loop."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from shadow_paper import (
    evaluate_shadow_positions,
    load_shadow_summary,
    open_shadow_position,
    record_shadow_decision,
    shadow_paper_from_config,
    shadow_realized_pnl,
    summarize_shadow_outcomes,
)


class _Order:
    quote_amount = Decimal("20")


class _Decision:
    action = "BUY"
    approved = True
    order = _Order()
    reasons = ["test"]
    blocked_reasons = []


class _Signal:
    confidence = Decimal("0.8")
    indicators = {"discovery_source": "discovery:futuresGainers", "discovery_bucket": "futuresGainers"}


class _Asset:
    symbol = "TESTUSDT"
    market = "binance_futures"


class _Snapshot:
    asset = _Asset()
    observed_at = 1_000_000
    price = Decimal("100")


def test_shadow_realized_pnl_long():
    pnl = shadow_realized_pnl(
        entry_price=Decimal("100"),
        exit_price=Decimal("102"),
        quote_amount=Decimal("50"),
        position_side="LONG",
    )
    assert pnl == Decimal("1.0000")


def test_open_and_close_shadow_take_profit(tmp_path: Path):
    cfg = shadow_paper_from_config(
        {
            "enabled": True,
            "ledger_path": str(tmp_path / "ledger.jsonl"),
            "state_path": str(tmp_path / "state.json"),
            "outcomes_path": str(tmp_path / "outcomes.jsonl"),
            "take_profit_pct": "0.02",
            "stop_loss_pct": "0.015",
        }
    )
    opened = open_shadow_position(
        cfg,
        {
            "symbol": "ABCUSDT",
            "action": "BUY",
            "price": "100",
            "timestamp": 999_000,
            "quoteAmount": "25",
            "bucket": "futuresGainers",
            "source": "discovery:futuresGainers",
            "trigger": "trade_learning",
        },
    )
    assert opened is True
    closed = evaluate_shadow_positions(
        cfg,
        prices={"ABCUSDT": Decimal("102.5")},
        observed_at=1_000_000,
    )
    assert len(closed) == 1
    assert closed[0]["closeReason"] == "take_profit"
    assert Decimal(closed[0]["realizedPnl"]) > 0
    summary = load_shadow_summary(cfg)
    assert summary["closedCount"] == 1
    assert summary["openCount"] == 0


def test_shadow_short_stop_loss(tmp_path: Path):
    cfg = shadow_paper_from_config(
        {
            "enabled": True,
            "state_path": str(tmp_path / "state.json"),
            "outcomes_path": str(tmp_path / "outcomes.jsonl"),
            "take_profit_pct": "0.02",
            "stop_loss_pct": "0.01",
        }
    )
    open_shadow_position(
        cfg,
        {
            "symbol": "XYZUSDT",
            "action": "SELL",
            "price": "100",
            "timestamp": 999_000,
            "quoteAmount": "30",
            "bucket": "futuresLosers",
        },
    )
    closed = evaluate_shadow_positions(
        cfg,
        prices={"XYZUSDT": Decimal("101.5")},
        observed_at=1_000_000,
    )
    assert closed[0]["closeReason"] == "stop_loss"
    assert Decimal(closed[0]["realizedPnl"]) < 0


def test_record_shadow_decision_opens_position(tmp_path: Path):
    cfg = shadow_paper_from_config(
        {
            "enabled": True,
            "ledger_path": str(tmp_path / "ledger.jsonl"),
            "state_path": str(tmp_path / "state.json"),
            "outcomes_path": str(tmp_path / "outcomes.jsonl"),
        }
    )
    record_shadow_decision(cfg, snapshot=_Snapshot(), signal=_Signal(), decision=_Decision())
    summary = load_shadow_summary(cfg)
    assert summary["count"] == 1
    assert summary["openCount"] == 1


def test_summarize_shadow_bucket_stats():
    outcomes = [
        {"realizedPnl": "1", "bucket": "futuresGainers"},
        {"realizedPnl": "-0.5", "bucket": "futuresLosers"},
        {"realizedPnl": "-0.5", "bucket": "futuresLosers"},
    ]
    stats = summarize_shadow_outcomes(outcomes)
    assert stats["closedCount"] == 3
    assert stats["bucketStats"]["futuresGainers"]["wins"] == 1
    assert stats["bucketStats"]["futuresLosers"]["losses"] == 2
    assert stats["profitFactor"] == "1.00"
    assert stats["avgWin"] == "1.0000"
    assert stats["avgLoss"] == "0.5000"
    assert stats["bucketStats"]["futuresLosers"]["profitFactor"] == "0.00"


def test_summarize_shadow_low_win_high_profit_factor():
    outcomes = [
        {"realizedPnl": "3", "bucket": "futuresLosers"},
        {"realizedPnl": "3", "bucket": "futuresLosers"},
        {"realizedPnl": "-1", "bucket": "futuresLosers"},
        {"realizedPnl": "-1", "bucket": "futuresLosers"},
        {"realizedPnl": "-1", "bucket": "futuresLosers"},
        {"realizedPnl": "-1", "bucket": "futuresLosers"},
    ]
    stats = summarize_shadow_outcomes(outcomes)
    bucket = stats["bucketStats"]["futuresLosers"]
    assert bucket["winRate"] == "0.33"
    assert bucket["totalPnl"] == "2.0000"
    assert bucket["profitFactor"] == "1.50"

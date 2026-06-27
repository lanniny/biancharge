"""Tests for performance_metrics."""

from __future__ import annotations

from performance_metrics import summarize_performance


def test_summarize_performance_basic():
    outcomes = [
        {"realizedPnl": "3.0", "positionSide": "LONG", "closeSource": "agent_reduce"},
        {"realizedPnl": "-0.5", "positionSide": "SHORT", "closeSource": "external_sl_tp"},
    ]
    summary = summarize_performance(outcomes)
    assert summary["closedTrades"] == 2
    assert summary["wins"] == 1
    assert summary["losses"] == 1
    assert summary["winRate"] == "0.50"

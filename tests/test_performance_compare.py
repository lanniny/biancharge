"""Tests for live vs shadow performance comparison."""

from __future__ import annotations

from performance_metrics import compare_live_vs_shadow


def test_compare_live_vs_shadow_buckets():
    live = [
        {
            "realizedPnl": "1",
            "openContext": {"bucket": "futuresGainers", "source": "discovery:futuresGainers"},
        },
        {
            "realizedPnl": "-0.5",
            "openContext": {"bucket": "futuresLosers", "source": "discovery:futuresLosers"},
        },
    ]
    shadow = {
        "closedCount": 3,
        "winRate": "0.67",
        "totalRealizedPnl": "2.5",
        "bucketStats": {
            "futuresLosers": {
                "sampleSize": 3,
                "winRate": "0.67",
                "totalPnl": "2.5",
            }
        },
    }
    result = compare_live_vs_shadow(live, shadow)
    assert result["global"]["live"]["closedTrades"] == 2
    assert result["global"]["shadow"]["closedTrades"] == 3
    assert "futuresGainers" in result["buckets"]
    assert "futuresLosers" in result["buckets"]

"""Tests for the Stage 4 sentiment engine (network mocked)."""

import os
import tempfile
import unittest
from decimal import Decimal

import sentiment_engine as se
from persona_council import VERDICT_DOWNGRADE, VERDICT_VETO, evaluate_council, persona_council_from_config
from sentiment_engine import (
    evaluate_sentiment,
    fetch_fng_trend,
    sentiment_from_config,
    sentiment_symbol_block_reason,
)


class FngTrendTests(unittest.TestCase):
    def setUp(self):
        self._orig = se._http_json
        se._CACHE.clear()

    def tearDown(self):
        se._http_json = self._orig
        se._CACHE.clear()

    def test_extreme_fear_is_negative_with_reversal_flag(self):
        se._http_json = lambda url, timeout=8: {"data": [{"value": "10"}, {"value": "20"}]}
        r = fetch_fng_trend(2)
        self.assertLess(r["score"], 0)
        self.assertEqual(r["bias"], "extreme_fear")
        self.assertEqual(r["contrarian"], "extreme_fear_reversal_watch")

    def test_extreme_greed_is_positive(self):
        se._http_json = lambda url, timeout=8: {"data": [{"value": "90"}, {"value": "80"}]}
        r = fetch_fng_trend(2)
        self.assertGreater(r["score"], 0)
        self.assertEqual(r["bias"], "extreme_greed")

    def test_fng_failure_degrades_to_zero_score(self):
        def boom(url, timeout=8):
            raise OSError("network down")

        se._http_json = boom
        r = fetch_fng_trend(2)
        self.assertEqual(r["score"], 0.0)
        self.assertIn("error", r)


class ListingDiffTests(unittest.TestCase):
    def setUp(self):
        self._orig = se._http_json
        se._CACHE.clear()
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "listings.json")

    def tearDown(self):
        se._http_json = self._orig
        se._CACHE.clear()

    def _cfg(self):
        return sentiment_from_config(
            {"enabled": True, "listings_state_path": self.state, "fng_enabled": False}
        )

    def test_first_run_suppresses_noise_then_detects_new(self):
        se._http_json = lambda url, timeout=8: {
            "symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]
        }
        r1 = se.detect_listing_changes(self._cfg())
        self.assertTrue(r1["firstRun"])
        self.assertEqual(r1["newListings"], [])

        # New symbol appears on the second poll.
        se._http_json = lambda url, timeout=8: {
            "symbols": [
                {"symbol": "BTCUSDT", "status": "TRADING"},
                {"symbol": "FRESHUSDT", "status": "TRADING"},
            ]
        }
        r2 = se.detect_listing_changes(self._cfg())
        self.assertIn("FRESHUSDT", r2["newListings"])

    def test_detects_delisting(self):
        se._http_json = lambda url, timeout=8: {
            "symbols": [
                {"symbol": "BTCUSDT", "status": "TRADING"},
                {"symbol": "OLDUSDT", "status": "TRADING"},
            ]
        }
        se.detect_listing_changes(self._cfg())  # baseline
        se._http_json = lambda url, timeout=8: {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]}
        r = se.detect_listing_changes(self._cfg())
        self.assertIn("OLDUSDT", r["delistings"])


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self._orig = se._http_json
        se._CACHE.clear()
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        se._http_json = self._orig
        se._CACHE.clear()

    def test_aggregate_label_bearish_on_extreme_fear(self):
        se._http_json = lambda url, timeout=8: (
            {"data": [{"value": "8"}]}
            if "fng" in url
            else {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]}
        )
        cfg = sentiment_from_config(
            {
                "enabled": True,
                "listings_state_path": os.path.join(self.tmp, "l.json"),
                "cryptopanic_enabled": False,
                "social_enabled": False,
            }
        )
        r = evaluate_sentiment(cfg, timestamp=1000)
        self.assertEqual(r["label"], "bearish")
        self.assertLess(r["score"], 0)

    def test_disabled_returns_disabled(self):
        cfg = sentiment_from_config({"enabled": False})
        self.assertFalse(evaluate_sentiment(cfg)["enabled"])


class DelistBlockTests(unittest.TestCase):
    def test_delisting_symbol_is_blocked(self):
        sentiment = {"enabled": True, "sources": {"listings": {"delistings": ["XYZUSDT"]}}}
        self.assertIsNotNone(sentiment_symbol_block_reason(sentiment, "XYZUSDT"))
        self.assertIsNone(sentiment_symbol_block_reason(sentiment, "BTCUSDT"))


class CouncilSentimentTests(unittest.TestCase):
    """The council should react to a sentiment score in context."""

    BASE = {
        "fusion_bull_pct": "0.66",
        "adx": "24",
        "mtf_1m": "bullish",
        "mtf_5m": "bullish",
        "mtf_15m": "neutral",
        "rsi": "60",
        "volatility": "0.02",
        "drawdown": "0.03",
        "price_change_pct_24h": "0.02",
        "volume_ratio": "1.3",
        "regime": "uptrend",
        "fusion_votes": {"sma": "bull", "momentum": "bull", "rsi": "neutral", "volume": "bull"},
    }

    def setUp(self):
        self.cfg = persona_council_from_config({"enabled": True})

    def test_opposing_sentiment_lowers_aggregate(self):
        ctx_bear = {"sentiment": {"enabled": True, "score": -0.8, "label": "bearish"}}
        ctx_none = None
        v_bear = evaluate_council("BUY", dict(self.BASE), self.cfg, context=ctx_bear)
        v_none = evaluate_council("BUY", dict(self.BASE), self.cfg, context=ctx_none)
        self.assertLess(v_bear.aggregate_score, v_none.aggregate_score)


if __name__ == "__main__":
    unittest.main()

"""Tests for the multi-persona hedging review council (Stage 3)."""

import unittest
from decimal import Decimal

from persona_council import (
    CONSERVATIVE,
    PRUDENT,
    VERDICT_CONFIRM,
    VERDICT_DOWNGRADE,
    VERDICT_VETO,
    evaluate_council,
    persona_council_from_config,
)


STRONG_LONG = {
    "fusion_bull_pct": "0.85",
    "adx": "30",
    "mtf_1m": "bullish",
    "mtf_5m": "bullish",
    "mtf_15m": "bullish",
    "rsi": "58",
    "volatility": "0.02",
    "drawdown": "0.03",
    "price_change_pct_24h": "0.03",
    "volume_ratio": "1.5",
    "regime": "uptrend",
    "fusion_votes": {"sma": "bull", "momentum": "bull", "rsi": "bull", "volume": "bull"},
}


class PersonaCouncilTests(unittest.TestCase):
    def setUp(self):
        self.cfg = persona_council_from_config({"enabled": True})

    def test_strong_long_confirms_at_full_size(self):
        v = evaluate_council("BUY", STRONG_LONG, self.cfg)
        self.assertEqual(v.verdict, VERDICT_CONFIRM)
        self.assertEqual(v.size_multiplier, Decimal("1"))
        self.assertGreaterEqual(v.aggregate_score, self.cfg.downgrade_below)

    def test_overbought_buy_is_vetoed_by_prudent(self):
        ind = dict(STRONG_LONG, rsi="85")
        v = evaluate_council("BUY", ind, self.cfg)
        self.assertEqual(v.verdict, VERDICT_VETO)
        self.assertIn(PRUDENT, [x.persona for x in v.votes if x.veto])

    def test_high_volatility_is_vetoed_by_conservative(self):
        ind = dict(STRONG_LONG, volatility="0.15")
        v = evaluate_council("BUY", ind, self.cfg)
        self.assertEqual(v.verdict, VERDICT_VETO)
        self.assertIn(CONSERVATIVE, [x.persona for x in v.votes if x.veto])

    def test_pump_chase_is_vetoed(self):
        ind = dict(STRONG_LONG, price_change_pct_24h="0.30")
        v = evaluate_council("BUY", ind, self.cfg)
        self.assertEqual(v.verdict, VERDICT_VETO)

    def test_minority_veto_overrides_high_aggregate(self):
        # Even a strong-looking trade is killed if a single cautious persona vetoes.
        ind = dict(STRONG_LONG, rsi="88")  # prudent veto, everything else bullish
        v = evaluate_council("BUY", ind, self.cfg)
        self.assertEqual(v.verdict, VERDICT_VETO)

    def test_mediocre_signal_downgrades(self):
        # Mixed confluence, no veto trigger -> aggregate lands in downgrade band.
        ind = {
            "fusion_bull_pct": "0.52",
            "adx": "18",
            "mtf_1m": "neutral",
            "mtf_5m": "bearish",
            "mtf_15m": "neutral",
            "rsi": "55",
            "volatility": "0.03",
            "drawdown": "0.04",
            "price_change_pct_24h": "0.01",
            "volume_ratio": "0.6",
            "regime": "range",
            "fusion_votes": {"sma": "bull", "momentum": "bear", "rsi": "neutral", "volume": "bear"},
        }
        v = evaluate_council("BUY", ind, self.cfg)
        self.assertIn(v.verdict, {VERDICT_DOWNGRADE, VERDICT_VETO})
        if v.verdict == VERDICT_DOWNGRADE:
            self.assertLess(v.size_multiplier, Decimal("1"))

    def test_verdict_serializes_for_ledger(self):
        v = evaluate_council("BUY", STRONG_LONG, self.cfg)
        d = v.to_dict()
        self.assertIn("verdict", d)
        self.assertIn("votes", d)
        self.assertEqual(len(d["votes"]), 4)
        for vote in d["votes"]:
            self.assertIn("persona", vote)
            self.assertIn("score", vote)

    def test_disabled_config_default(self):
        cfg = persona_council_from_config(None)
        self.assertFalse(cfg.enabled)

    def test_short_side_uses_bear_conviction(self):
        # A strong DOWN setup should let a SELL confirm.
        ind = {
            "fusion_bull_pct": "0.15",
            "adx": "30",
            "mtf_1m": "bearish",
            "mtf_5m": "bearish",
            "mtf_15m": "bearish",
            "rsi": "42",
            "volatility": "0.02",
            "drawdown": "0.03",
            "price_change_pct_24h": "-0.03",
            "volume_ratio": "1.4",
            "regime": "downtrend",
            "fusion_votes": {"sma": "bear", "momentum": "bear", "rsi": "bear", "volume": "bear"},
        }
        v = evaluate_council("SELL", ind, self.cfg)
        self.assertEqual(v.verdict, VERDICT_CONFIRM)

    def test_project_regime_labels_count_as_directional_support(self):
        long_vote = evaluate_council("BUY", dict(STRONG_LONG, regime="trend_up"), self.cfg)
        self.assertTrue(
            any("regime supports long" in reason for vote in long_vote.votes for reason in vote.reasons)
        )

        short_ind = dict(
            STRONG_LONG,
            fusion_bull_pct="0.15",
            mtf_1m="bearish",
            mtf_5m="bearish",
            mtf_15m="bearish",
            rsi="42",
            price_change_pct_24h="-0.03",
            regime="trend_down",
            fusion_votes={"sma": "bear", "momentum": "bear", "rsi": "bear", "volume": "bear"},
        )
        short_vote = evaluate_council("SELL", short_ind, self.cfg)
        self.assertTrue(
            any("regime supports short" in reason for vote in short_vote.votes for reason in vote.reasons)
        )


if __name__ == "__main__":
    unittest.main()

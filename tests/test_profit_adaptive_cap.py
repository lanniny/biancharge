"""Tests for the profit-adaptive daily trade cap.

The cap is raised only when today's net realized PnL (UTC) is strictly positive;
net-loss/flat days stay at the base cap. The daily-loss circuit breaker is a
separate path that this feature must never weaken.
"""

import os
import tempfile
import unittest
from decimal import Decimal

os.environ.setdefault("AUTOTRADER_SOCKS5_DISABLED", "1")

from market_autotrader import (
    ProfitAdaptiveCapConfig,
    RiskConfig,
    TradingMemory,
    daily_drag_blocked_reason,
    profit_adaptive_effective_cap,
    risk_from_config,
    utc_today_key,
)


def _cfg(**kw):
    base = dict(enabled=True, step_usdt=Decimal("0.5"), extra_trades_per_step=5, hard_ceiling=60)
    base.update(kw)
    return ProfitAdaptiveCapConfig(**base)


class ProfitAdaptiveCapFormulaTest(unittest.TestCase):
    def test_profit_raises_cap_by_formula(self):
        # 1.20 net / 0.5 step = 2 steps * 5 = +10 -> 15 + 10 = 25
        self.assertEqual(profit_adaptive_effective_cap(15, Decimal("1.20"), _cfg()), (25, 10))

    def test_single_step_profit(self):
        self.assertEqual(profit_adaptive_effective_cap(15, Decimal("0.5"), _cfg()), (20, 5))

    def test_subthreshold_profit_no_bonus(self):
        # below one full step: noise must not unlock churn
        self.assertEqual(profit_adaptive_effective_cap(15, Decimal("0.49"), _cfg()), (15, 0))

    def test_net_loss_keeps_base(self):
        self.assertEqual(profit_adaptive_effective_cap(15, Decimal("-2.0"), _cfg()), (15, 0))

    def test_flat_keeps_base(self):
        self.assertEqual(profit_adaptive_effective_cap(15, Decimal("0"), _cfg()), (15, 0))

    def test_hard_ceiling_clamps_total(self):
        eff, bonus = profit_adaptive_effective_cap(15, Decimal("100"), _cfg())
        self.assertEqual(eff, 60)
        self.assertEqual(bonus, 45)  # 60 - 15

    def test_disabled_no_op(self):
        self.assertEqual(profit_adaptive_effective_cap(15, Decimal("5"), _cfg(enabled=False)), (15, 0))

    def test_base_cap_zero_unlimited_untouched(self):
        # 0 means "no cap" upstream; must stay 0 (never gets a bonus that turns it into a finite cap)
        self.assertEqual(profit_adaptive_effective_cap(0, Decimal("5"), _cfg()), (0, 0))


class MemoryNetMirrorTest(unittest.TestCase):
    def test_net_mirror_signed_tracks_gains_and_losses(self):
        m = TradingMemory()
        today = utc_today_key()
        m.record_realized_pnl(Decimal("0.8"), today)   # gain
        m.record_realized_pnl(Decimal("-0.3"), today)  # loss
        self.assertEqual(m.daily_realized_net_today(), Decimal("0.5"))
        # loss breaker still sees ONLY the loss portion (unchanged behavior)
        self.assertEqual(m.daily_loss_today(), Decimal("0.3"))

    def test_prior_day_pnl_not_counted_today(self):
        m = TradingMemory()
        m.record_realized_pnl(Decimal("5.0"), "2020-01-01")
        self.assertEqual(m.daily_realized_net_today(), Decimal("0"))

    def test_persistence_round_trip(self):
        m = TradingMemory()
        m.record_realized_pnl(Decimal("0.7"), utc_today_key())
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "state.json")
            m.save(p)
            m2 = TradingMemory.load(p)
        self.assertEqual(m2.daily_realized_net_today(), Decimal("0.7"))


class CircuitBreakerIndependenceTest(unittest.TestCase):
    def test_daily_loss_cap_blocks_regardless_of_raised_cap(self):
        # Profitable earlier, then a big loss past the 3 USDT daily-loss cap. Even with
        # profit-adaptive enabled, the SEPARATE loss breaker must still fire.
        m = TradingMemory()
        today = utc_today_key()
        m.record_realized_pnl(Decimal("2.0"), today)    # net +2 (would raise the trade cap)
        m.record_realized_pnl(Decimal("-4.0"), today)   # loss 4 > 3 cap
        risk = RiskConfig(
            max_daily_loss_quote=Decimal("3"),
            daily_drag_scope="loss_only",
            profit_adaptive_daily_cap=ProfitAdaptiveCapConfig(enabled=True),
        )
        reason = daily_drag_blocked_reason(risk, m, Decimal("14.66"), is_reduce_only=False)
        self.assertIsNotNone(reason)
        self.assertIn("cap", reason)


class ConfigParsingTest(unittest.TestCase):
    def test_default_disabled(self):
        risk = risk_from_config({"max_daily_trades": 15})
        self.assertFalse(risk.profit_adaptive_daily_cap.enabled)

    def test_parsing_reads_subconfig(self):
        risk = risk_from_config({
            "max_daily_trades": 15,
            "profit_adaptive_daily_cap": {
                "enabled": True, "step_usdt": "0.5",
                "extra_trades_per_step": 5, "hard_ceiling": 60,
            },
        })
        self.assertTrue(risk.profit_adaptive_daily_cap.enabled)
        self.assertEqual(risk.profit_adaptive_daily_cap.extra_trades_per_step, 5)
        self.assertEqual(risk.profit_adaptive_daily_cap.hard_ceiling, 60)


if __name__ == "__main__":
    unittest.main()

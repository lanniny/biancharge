"""Equity-adaptive risk/sizing scaling.

The base config is tuned for a large target account (e.g. 300k USDT). On a small
account those absolute floors (min_trade_quote=50, min_margin_per_trade=10,
reserve=5) and the tiny risk_per_trade_pct (1.5% of 14 USDT = 0.22 USDT) make it
impossible to place any order. This module rescales a handful of risk/sizing
parameters to the CURRENT equity each cycle, so one config works across orders
of magnitude of account size — the autonomous "adapt to capital" requirement.

It is intentionally conservative for micro accounts: low leverage cap, low trade
frequency, a hard daily-loss fraction, and it never lowers a floor below what the
exchange physically allows (some perps have a 5 USDT min notional).

Returns a *new* RiskConfig (via dataclasses.replace) plus a leverage cap; it
never mutates the input. Disabled by default — opt in with capital_scaling.enabled.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_DOWN
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from market_autotrader import RiskConfig


def _dec(value: Any, default: str = "0") -> Decimal:
    try:
        if value is None or value == "":
            return Decimal(default)
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


# Equity tiers (USDT). Each tier carries the scaled parameters. The first tier
# whose `max_equity` is >= current equity wins; the last (None) is the catch-all.
@dataclass(frozen=True)
class CapitalTier:
    name: str
    max_equity: Decimal | None  # upper bound (inclusive); None = no bound
    min_trade_quote: Decimal
    max_trade_quote: Decimal
    min_margin_per_trade: Decimal
    reserve: Decimal
    risk_per_trade_pct: Decimal
    max_position_quote: Decimal
    leverage_cap: int
    max_daily_loss_pct: Decimal
    max_daily_loss_quote: Decimal
    max_daily_trades: int
    cooldown_seconds: int
    max_concurrent_positions: int  # cap simultaneous open positions for the tier


# Conservative ladder. Micro accounts trade only low-min-notional perps at low
# leverage and low frequency; caps grow with equity toward the base config.
DEFAULT_TIERS: list[CapitalTier] = [
    CapitalTier(
        name="micro",  # ~ up to 50 USDT
        max_equity=Decimal("50"),
        min_trade_quote=Decimal("5"),  # exchange floor for many perps (DOGE/SOL)
        # 18 not 10: at 14.74 USDT / 3x the account can carry ~22 USDT notional, so
        # a 10 cap was wrongly killing valid 11-12.5 USDT candidates ("exceeds max
        # trade quote"). 18 admits them while staying within 3x affordability.
        max_trade_quote=Decimal("18"),
        min_margin_per_trade=Decimal("2"),
        reserve=Decimal("3"),  # 3 not 1: leave a margin buffer so the account is not
        # pinned at ~92% used after a couple of opens (caused -2019 on every new candidate).
        risk_per_trade_pct=Decimal("0.5"),  # allow notional up to ~50% equity-cap
        max_position_quote=Decimal("25"),
        leverage_cap=3,
        max_daily_loss_pct=Decimal("0.15"),
        max_daily_loss_quote=Decimal("3"),
        # 40 not 15: user wants to be able to keep trading when opportunities exist
        # (RC1 now filters the bottom-shorts that drove the early losses). Still a
        # hard ceiling so a losing day can't churn the ~14 USDT account to death —
        # the independent max_daily_loss_quote=3 circuit breaker stops the day first.
        # The profit-adaptive cap (risk.profit_adaptive_daily_cap) can raise it above
        # 40 only when today is net-profitable.
        max_daily_trades=40,
        cooldown_seconds=300,  # 5 min (was 900); more active per user request
        # 2 not 8: 14 USDT cannot margin more than ~2 positions; cap proactively
        # instead of letting margin exhaustion reject every new candidate (-2019).
        max_concurrent_positions=2,
    ),
    CapitalTier(
        name="small",  # 50 - 300 USDT
        max_equity=Decimal("300"),
        min_trade_quote=Decimal("10"),
        max_trade_quote=Decimal("90"),
        min_margin_per_trade=Decimal("5"),
        reserve=Decimal("5"),
        # Target about 50% total margin use when five positions are open at 5x:
        # notional ~= 50% equity per position -> margin ~= 10% equity each.
        risk_per_trade_pct=Decimal("0.50"),
        max_position_quote=Decimal("180"),
        leverage_cap=5,
        max_daily_loss_pct=Decimal("0.08"),
        max_daily_loss_quote=Decimal("15"),
        # 60 not 12:升档后日限必须单调不减(micro是40)。12对一个能开5并发的122U
        # 账户太小,升档反而比micro少是设计缺陷。600s冷却已防过度交易
        # (60笔/天≈平均24min一笔,远低于冷却频率)。日亏15U熔断仍独立兜底。
        max_daily_trades=60,
        cooldown_seconds=600,
        # 5 not 3: margin is not the binding constraint on this account (each position
        # uses ~2% of equity), so 3 left ~94% of capital idle and blocked many entries.
        # Raised to 5 as a risk-preference choice — it permits more simultaneous,
        # correlated perp positions; each carries its own stop, and total margin use
        # stays low. NOT a "dynamic" optimization (margin never binds here); just a
        # higher fixed concurrency ceiling.
        max_concurrent_positions=5,
    ),
    CapitalTier(
        name="mid",  # 300 - 3000 USDT
        max_equity=Decimal("3000"),
        min_trade_quote=Decimal("20"),
        max_trade_quote=Decimal("300"),
        min_margin_per_trade=Decimal("8"),
        reserve=Decimal("15"),
        risk_per_trade_pct=Decimal("0.03"),
        max_position_quote=Decimal("1500"),
        leverage_cap=8,
        max_daily_loss_pct=Decimal("0.06"),
        max_daily_loss_quote=Decimal("120"),
        # 80 not 20: keep the ladder monotonic (small=60). A larger account must never
        # get FEWER daily trades than a smaller one. 300s cooldown bounds frequency.
        max_daily_trades=80,
        cooldown_seconds=300,
        max_concurrent_positions=5,
    ),
    CapitalTier(
        name="large",  # 3000+ USDT -> falls back close to base config
        max_equity=None,
        min_trade_quote=Decimal("50"),
        max_trade_quote=Decimal("800"),
        min_margin_per_trade=Decimal("10"),
        reserve=Decimal("30"),
        risk_per_trade_pct=Decimal("0.015"),
        max_position_quote=Decimal("5000"),
        leverage_cap=20,
        max_daily_loss_pct=Decimal("0.05"),
        max_daily_loss_quote=Decimal("400"),
        max_daily_trades=0,  # 0 = use base / unlimited
        cooldown_seconds=600,
        max_concurrent_positions=8,
    ),
]


@dataclass
class GrowthSchedulerConfig:
    enabled: bool = False
    min_sample: int = 12
    positive_win_rate: Decimal = Decimal("0.45")
    negative_win_rate: Decimal = Decimal("0.35")
    positive_profit_factor: Decimal = Decimal("1.20")
    negative_profit_factor: Decimal = Decimal("0.85")
    max_concurrency_boost: int = 2
    max_daily_trade_boost: int = 20
    min_cooldown_mult: Decimal = Decimal("0.50")
    loss_cooldown_mult: Decimal = Decimal("1.50")
    min_cooldown_seconds: int = 120
    max_cooldown_seconds: int = 1800
    min_daily_trades: int = 4
    max_daily_trades: int = 120
    daily_profit_trade_step_usdt: Decimal = Decimal("0.5")
    daily_profit_extra_trades_per_step: int = 2
    daily_loss_throttle_ratio: Decimal = Decimal("0.50")
    drawdown_heat_throttle: Decimal = Decimal("0.80")


@dataclass
class CapitalScalingConfig:
    enabled: bool = False
    tiers: list[CapitalTier] = None  # type: ignore[assignment]
    growth_scheduler: GrowthSchedulerConfig = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.tiers is None:
            self.tiers = DEFAULT_TIERS
        if self.growth_scheduler is None:
            self.growth_scheduler = GrowthSchedulerConfig()


def capital_scaling_from_config(raw: dict[str, Any] | None) -> CapitalScalingConfig:
    raw = raw or {}
    tiers = DEFAULT_TIERS
    custom = raw.get("tiers")
    if isinstance(custom, list) and custom:
        parsed: list[CapitalTier] = []
        for t in custom:
            parsed.append(
                CapitalTier(
                    name=str(t.get("name", "tier")),
                    max_equity=(None if t.get("max_equity") in (None, "", "none") else _dec(t.get("max_equity"))),
                    min_trade_quote=_dec(t.get("min_trade_quote", "5"), "5"),
                    max_trade_quote=_dec(t.get("max_trade_quote", "10"), "10"),
                    min_margin_per_trade=_dec(t.get("min_margin_per_trade", "2"), "2"),
                    reserve=_dec(t.get("reserve", "1"), "1"),
                    risk_per_trade_pct=_dec(t.get("risk_per_trade_pct", "0.5"), "0.5"),
                    max_position_quote=_dec(t.get("max_position_quote", "15"), "15"),
                    leverage_cap=int(t.get("leverage_cap", 3)),
                    max_daily_loss_pct=_dec(t.get("max_daily_loss_pct", "0.15"), "0.15"),
                    max_daily_loss_quote=_dec(t.get("max_daily_loss_quote", "3"), "3"),
                    max_daily_trades=int(t.get("max_daily_trades", 8)),
                    cooldown_seconds=int(t.get("cooldown_seconds", 900)),
                    max_concurrent_positions=int(t.get("max_concurrent_positions", 2)),
                )
            )
        tiers = parsed
    sched_raw = raw.get("growth_scheduler") or {}
    scheduler = GrowthSchedulerConfig(
        enabled=bool(sched_raw.get("enabled", False)),
        min_sample=int(sched_raw.get("min_sample", 12)),
        positive_win_rate=_dec(sched_raw.get("positive_win_rate", "0.45"), "0.45"),
        negative_win_rate=_dec(sched_raw.get("negative_win_rate", "0.35"), "0.35"),
        positive_profit_factor=_dec(sched_raw.get("positive_profit_factor", "1.20"), "1.20"),
        negative_profit_factor=_dec(sched_raw.get("negative_profit_factor", "0.85"), "0.85"),
        max_concurrency_boost=int(sched_raw.get("max_concurrency_boost", 2)),
        max_daily_trade_boost=int(sched_raw.get("max_daily_trade_boost", 20)),
        min_cooldown_mult=_dec(sched_raw.get("min_cooldown_mult", "0.50"), "0.50"),
        loss_cooldown_mult=_dec(sched_raw.get("loss_cooldown_mult", "1.50"), "1.50"),
        min_cooldown_seconds=int(sched_raw.get("min_cooldown_seconds", 120)),
        max_cooldown_seconds=int(sched_raw.get("max_cooldown_seconds", 1800)),
        min_daily_trades=int(sched_raw.get("min_daily_trades", 4)),
        max_daily_trades=int(sched_raw.get("max_daily_trades", 120)),
        daily_profit_trade_step_usdt=_dec(
            sched_raw.get("daily_profit_trade_step_usdt", "0.5"), "0.5"
        ),
        daily_profit_extra_trades_per_step=int(sched_raw.get("daily_profit_extra_trades_per_step", 2)),
        daily_loss_throttle_ratio=_dec(sched_raw.get("daily_loss_throttle_ratio", "0.50"), "0.50"),
        drawdown_heat_throttle=_dec(sched_raw.get("drawdown_heat_throttle", "0.80"), "0.80"),
    )
    return CapitalScalingConfig(
        enabled=bool(raw.get("enabled", False)),
        tiers=tiers,
        growth_scheduler=scheduler,
    )


def select_tier(cfg: CapitalScalingConfig, equity: Decimal) -> CapitalTier:
    for tier in cfg.tiers:
        if tier.max_equity is None or equity <= tier.max_equity:
            return tier
    return cfg.tiers[-1]


def scale_risk_for_equity(
    cfg: CapitalScalingConfig,
    risk: "RiskConfig",
    equity: Decimal,
) -> tuple["RiskConfig", dict[str, Any]]:
    """Return (scaled_risk, info). info documents the chosen tier for the ledger.

    Only known sizing/risk floors are overridden; everything else on the original
    RiskConfig is preserved. Never raises.
    """
    if not cfg.enabled or equity <= 0:
        return risk, {"enabled": False}
    tier = select_tier(cfg, equity)
    scaled = replace(
        risk,
        min_trade_quote=tier.min_trade_quote,
        max_trade_quote=tier.max_trade_quote,
        min_margin_per_trade=tier.min_margin_per_trade,
        reserve_futures_available_usdt=tier.reserve,
        risk_per_trade_pct=tier.risk_per_trade_pct,
        max_position_quote=tier.max_position_quote,
        max_daily_loss_pct=tier.max_daily_loss_pct,
        max_daily_loss_quote=tier.max_daily_loss_quote,
        max_daily_trades=tier.max_daily_trades,
        cooldown_seconds=tier.cooldown_seconds,
        max_concurrent_positions=tier.max_concurrent_positions,
        scale_sizing_with_equity=True,
    )
    info = {
        "enabled": True,
        "tier": tier.name,
        "equity": str(equity.quantize(Decimal("0.01"))),
        "leverageCap": tier.leverage_cap,
        "minTradeQuote": str(tier.min_trade_quote),
        "maxTradeQuote": str(tier.max_trade_quote),
        "riskPerTradePct": str(tier.risk_per_trade_pct),
        "reserve": str(tier.reserve),
        "maxConcurrentPositions": tier.max_concurrent_positions,
    }
    return scaled, info


def scaled_leverage_cap(cfg: CapitalScalingConfig, equity: Decimal) -> int | None:
    """Leverage ceiling for the current equity tier (None when disabled)."""
    if not cfg.enabled or equity <= 0:
        return None
    return select_tier(cfg, equity).leverage_cap


def _bounded_int(value: int, lower: int, upper: int) -> int:
    if upper > 0:
        value = min(value, upper)
    return max(value, lower)


def _trade_learning_edge_score(snapshot: dict[str, Any] | None, cfg: GrowthSchedulerConfig) -> tuple[str, Decimal, dict[str, Any]]:
    data = snapshot or {}
    sample = int(data.get("sampleSize", 0) or 0)
    win_rate = _dec(data.get("winRate", "0"))
    profit_factor = _dec(data.get("profitFactor", "0"))
    total_pnl = _dec(data.get("totalRealizedPnl", "0"))
    if sample < cfg.min_sample:
        return "sample_wait", Decimal("0"), {
            "sampleSize": sample,
            "winRate": str(win_rate),
            "profitFactor": str(profit_factor),
            "totalRealizedPnl": str(total_pnl),
        }
    if total_pnl > 0 and win_rate >= cfg.positive_win_rate and profit_factor >= cfg.positive_profit_factor:
        win_span = max(Decimal("1") - cfg.positive_win_rate, Decimal("0.01"))
        pf_span = max(cfg.positive_profit_factor, Decimal("0.01"))
        win_score = min((win_rate - cfg.positive_win_rate) / win_span, Decimal("1"))
        pf_score = min((profit_factor - cfg.positive_profit_factor) / pf_span, Decimal("1"))
        score = max(Decimal("0.25"), min((win_score + pf_score) / Decimal("2"), Decimal("1")))
        return "expand", score, {
            "sampleSize": sample,
            "winRate": str(win_rate),
            "profitFactor": str(profit_factor),
            "totalRealizedPnl": str(total_pnl),
        }
    if total_pnl > 0 and profit_factor >= cfg.positive_profit_factor:
        return "neutral", Decimal("0"), {
            "sampleSize": sample,
            "winRate": str(win_rate),
            "profitFactor": str(profit_factor),
            "totalRealizedPnl": str(total_pnl),
            "expectancyProfile": "positive_pnl_low_win_rate",
        }
    if total_pnl < 0 or profit_factor < cfg.negative_profit_factor:
        return "throttle", Decimal("-1"), {
            "sampleSize": sample,
            "winRate": str(win_rate),
            "profitFactor": str(profit_factor),
            "totalRealizedPnl": str(total_pnl),
        }
    if win_rate < cfg.negative_win_rate and total_pnl <= 0:
        return "throttle", Decimal("-1"), {
            "sampleSize": sample,
            "winRate": str(win_rate),
            "profitFactor": str(profit_factor),
            "totalRealizedPnl": str(total_pnl),
        }
    return "neutral", Decimal("0"), {
        "sampleSize": sample,
        "winRate": str(win_rate),
        "profitFactor": str(profit_factor),
        "totalRealizedPnl": str(total_pnl),
    }


def apply_growth_scheduler(
    cfg: CapitalScalingConfig,
    risk: "RiskConfig",
    *,
    equity: Decimal,
    trade_learning: dict[str, Any] | None,
    today_net: Decimal,
    daily_drag: Decimal,
    effective_daily_loss_cap: Decimal,
    portfolio_heat: Decimal,
) -> tuple["RiskConfig", dict[str, Any]]:
    """Adjust frequency/concurrency for geometric growth under drawdown constraints."""
    sched = cfg.growth_scheduler
    if not cfg.enabled or not sched.enabled or equity <= 0:
        return risk, {"enabled": False}

    mode, edge_score, edge_info = _trade_learning_edge_score(trade_learning, sched)
    base_concurrency = risk.max_concurrent_positions
    base_daily = risk.max_daily_trades
    base_cooldown = risk.cooldown_seconds
    concurrency = base_concurrency
    daily_trades = base_daily
    cooldown = base_cooldown
    reasons: list[str] = [f"edge={mode}"]
    daily_changed = False

    if mode == "expand":
        boost = int((Decimal(sched.max_concurrency_boost) * edge_score).to_integral_value(rounding=ROUND_DOWN))
        concurrency = max(base_concurrency, base_concurrency + boost)
        daily_boost = int((Decimal(sched.max_daily_trade_boost) * edge_score).to_integral_value(rounding=ROUND_DOWN))
        daily_trades = (base_daily or sched.min_daily_trades) + daily_boost
        if sched.daily_profit_trade_step_usdt > 0 and today_net > 0:
            steps = int(today_net / sched.daily_profit_trade_step_usdt)
            daily_trades += steps * sched.daily_profit_extra_trades_per_step
        cooldown = int(Decimal(base_cooldown) * max(sched.min_cooldown_mult, Decimal("0.01")))
        reasons.append(f"positive expectancy score={edge_score}")
        daily_changed = True
    elif mode == "throttle":
        concurrency = max(1, base_concurrency - 1) if base_concurrency > 0 else 1
        daily_trades = max(sched.min_daily_trades, int((base_daily or sched.min_daily_trades) * 0.5))
        cooldown = int(Decimal(base_cooldown) * sched.loss_cooldown_mult)
        reasons.append("recent expectancy weak")
        daily_changed = True

    if effective_daily_loss_cap > 0 and daily_drag >= effective_daily_loss_cap * sched.daily_loss_throttle_ratio:
        concurrency = max(1, min(concurrency, base_concurrency))
        daily_trades = max(sched.min_daily_trades, min(daily_trades, base_daily or daily_trades))
        cooldown = max(cooldown, int(Decimal(base_cooldown) * sched.loss_cooldown_mult))
        reasons.append("daily drag throttle")
        daily_changed = True
    if portfolio_heat >= sched.drawdown_heat_throttle:
        concurrency = max(1, min(concurrency, base_concurrency))
        cooldown = max(cooldown, base_cooldown)
        reasons.append("portfolio heat throttle")

    if base_concurrency > 0:
        concurrency = min(concurrency, base_concurrency + max(sched.max_concurrency_boost, 0))
    if daily_changed or daily_trades > 0:
        daily_trades = _bounded_int(daily_trades, sched.min_daily_trades, sched.max_daily_trades)
    cooldown = _bounded_int(cooldown, sched.min_cooldown_seconds, sched.max_cooldown_seconds)

    adjusted = replace(
        risk,
        max_concurrent_positions=concurrency,
        max_daily_trades=daily_trades,
        cooldown_seconds=cooldown,
    )
    info = {
        "enabled": True,
        "mode": mode,
        "edgeScore": str(edge_score),
        "baseMaxConcurrentPositions": base_concurrency,
        "maxConcurrentPositions": concurrency,
        "baseMaxDailyTrades": base_daily,
        "maxDailyTrades": daily_trades,
        "baseCooldownSeconds": base_cooldown,
        "cooldownSeconds": cooldown,
        "todayNetRealized": str(today_net),
        "dailyDrag": str(daily_drag),
        "effectiveDailyLossCap": str(effective_daily_loss_cap),
        "portfolioHeat": str(portfolio_heat),
        "reasons": reasons,
        **edge_info,
    }
    return adjusted, info

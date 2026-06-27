"""Profitability tuning — regime soft-pass, bucket sizing, daily loss budget."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


def decimal_from(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


@dataclass(frozen=True)
class ProfitabilityConfig:
    enabled: bool = True
    regime_filter_mode: str = "soft"
    daily_loss_floor_fraction_of_cap: Decimal = Decimal("0.25")
    force_exit_fraction: Decimal = Decimal("0.75")
    deep_loss_momentum_cut_mult: Decimal = Decimal("1.5")
    skip_5m_exit_confirm_loss_mult: Decimal = Decimal("1.5")
    gainers_allow_range: bool = True
    gainers_allow_squeeze: bool = True
    gainers_allow_trend_down: bool = True
    losers_allow_range: bool = True
    losers_allow_squeeze: bool = True
    losers_allow_trend_up: bool = True


def profitability_from_config(raw: dict[str, Any] | None) -> ProfitabilityConfig:
    if not raw:
        return ProfitabilityConfig(enabled=False)
    return ProfitabilityConfig(
        enabled=bool(raw.get("enabled", True)),
        regime_filter_mode=str(raw.get("regime_filter_mode", "soft")).lower(),
        daily_loss_floor_fraction_of_cap=decimal_from(
            raw.get("daily_loss_floor_fraction_of_cap", "0.25")
        ),
        force_exit_fraction=decimal_from(raw.get("force_exit_fraction", "0.75")),
        deep_loss_momentum_cut_mult=decimal_from(raw.get("deep_loss_momentum_cut_mult", "1.5")),
        skip_5m_exit_confirm_loss_mult=decimal_from(
            raw.get("skip_5m_exit_confirm_loss_mult", "1.5")
        ),
        gainers_allow_range=bool(raw.get("gainers_allow_range", True)),
        gainers_allow_squeeze=bool(raw.get("gainers_allow_squeeze", True)),
        gainers_allow_trend_down=bool(raw.get("gainers_allow_trend_down", True)),
        losers_allow_range=bool(raw.get("losers_allow_range", True)),
        losers_allow_squeeze=bool(raw.get("losers_allow_squeeze", True)),
        losers_allow_trend_up=bool(raw.get("losers_allow_trend_up", True)),
    )


def bucket_regime_match_soft(
    bucket: str,
    regime_kind: str,
    cfg: ProfitabilityConfig,
) -> bool:
    """Extend strict bucket×regime matching so discovery is not over-filtered."""
    if not cfg.enabled or cfg.regime_filter_mode != "soft":
        from market_discovery import bucket_regime_match

        return bucket_regime_match(bucket, regime_kind)

    kind = str(regime_kind or "").lower()
    if bucket.endswith("TradFi") or bucket == "futuresTradFi":
        return kind in {"trend_up", "trend_down", "range", "squeeze"}
    if bucket.endswith("Gainers"):
        if kind == "trend_up":
            return True
        if kind == "trend_down" and cfg.gainers_allow_trend_down:
            return True
        if kind == "range" and cfg.gainers_allow_range:
            return True
        if kind == "squeeze" and cfg.gainers_allow_squeeze:
            return True
        return False
    if bucket.endswith("Losers"):
        if kind == "trend_down":
            return True
        if kind == "trend_up" and cfg.losers_allow_trend_up:
            return True
        if kind == "range" and cfg.losers_allow_range:
            return True
        if kind == "squeeze" and cfg.losers_allow_squeeze:
            return True
        return False
    if bucket.endswith("TopVolume"):
        return kind in {"trend_up", "trend_down", "range", "squeeze"}
    return True


def effective_max_daily_loss_profit_aware(
    risk: Any,
    equity: Decimal,
    *,
    profit_cfg: ProfitabilityConfig | None = None,
) -> Decimal:
    """Pct-first daily loss budget; small accounts get a fraction of max cap, not stuck at min floor only."""
    fixed = risk.max_daily_loss_quote if risk.max_daily_loss_quote > 0 else Decimal("0")
    pct_loss = Decimal("0")
    if risk.max_daily_loss_pct > 0 and equity > 0:
        pct_loss = (equity * risk.max_daily_loss_pct).quantize(Decimal("0.00000001"))

    if not risk.scale_sizing_with_equity:
        if fixed > 0:
            return fixed
        return pct_loss

    candidate = pct_loss
    if profit_cfg and profit_cfg.enabled and fixed > 0:
        floor_from_cap = (fixed * profit_cfg.daily_loss_floor_fraction_of_cap).quantize(
            Decimal("0.00000001")
        )
        candidate = max(candidate, floor_from_cap)
    if risk.min_daily_loss_cap_quote > 0:
        candidate = max(candidate, risk.min_daily_loss_cap_quote)
    if fixed > 0:
        return min(candidate, fixed)
    return candidate

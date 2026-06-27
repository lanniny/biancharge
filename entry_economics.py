"""Fee-aware entry gates — block opens where expected edge cannot cover round-trip costs."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

DEFAULT_TAKER_FEE_RATE = Decimal("0.0004")


def decimal_from(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def round_trip_fee_pct(*, fee_rate: Decimal | None = None) -> Decimal:
    rate = fee_rate if fee_rate is not None else DEFAULT_TAKER_FEE_RATE
    return (rate * Decimal("2")).quantize(Decimal("0.0000001"))


def expected_favorable_move_pct(
    *,
    atr_pct: Decimal,
    take_profit_pct: Decimal,
    atr_tp_multiplier: Decimal,
    use_atr: bool,
) -> Decimal:
    if use_atr and atr_pct > 0:
        return (atr_pct * atr_tp_multiplier).quantize(Decimal("0.0000001"))
    return take_profit_pct


def entry_economics_block_reason(
    *,
    enabled: bool,
    is_new_open: bool,
    quote_amount: Decimal,
    atr_pct: Decimal,
    min_edge_fee_multiple: Decimal,
    take_profit_pct: Decimal,
    atr_tp_multiplier: Decimal,
    use_atr_stops: bool,
    fee_rate: Decimal | None = None,
) -> str | None:
    if not enabled or not is_new_open or quote_amount <= 0:
        return None
    if min_edge_fee_multiple <= 0:
        return None
    fee_pct = round_trip_fee_pct(fee_rate=fee_rate)
    min_move = fee_pct * min_edge_fee_multiple
    expected = expected_favorable_move_pct(
        atr_pct=atr_pct,
        take_profit_pct=take_profit_pct,
        atr_tp_multiplier=atr_tp_multiplier,
        use_atr=use_atr_stops,
    )
    if expected < min_move:
        return (
            f"Entry economics: expected move {expected:.2%} < "
            f"{min_edge_fee_multiple}x round-trip fee ({min_move:.2%}); edge too thin."
        )
    return None


def discovery_short_block_reason(
    *,
    discovery_short_mode: str = "off",
    allow_discovery_shorts: bool = False,
    order_action: str,
    is_discovery_open: bool,
    bucket: str = "",
    regime: str = "",
    entry_quadrant: str = "",
    profitability_raw: dict[str, Any] | None = None,
) -> str | None:
    if entry_quadrant == "reversal_short":
        return None
    if not is_discovery_open or order_action != "SELL":
        return None

    mode = str(discovery_short_mode or "off").strip().lower()
    if allow_discovery_shorts and mode == "off":
        mode = "all"
    if mode == "all":
        return None
    if mode == "off":
        return (
            "Discovery short opens disabled (discovery_short_mode=off); "
            "short book had 0% win rate in live sample."
        )

    bucket_name = str(bucket or "").strip()
    if bucket_name.startswith("discovery:"):
        bucket_name = bucket_name.split(":", 1)[1]
    regime_kind = str(regime or "").lower()

    if mode == "losers_only":
        if bucket_name.endswith("Losers") or bucket_name == "futuresLosers":
            return None
        return (
            f"Discovery short policy (losers_only): bucket {bucket_name or 'unknown'} "
            "not eligible for new shorts."
        )

    if mode == "losers_trend_down":
        losers_ok = bucket_name.endswith("Losers") or bucket_name == "futuresLosers"
        if losers_ok and regime_kind == "trend_down":
            return None
        return (
            f"Discovery short policy (losers_trend_down): requires futuresLosers + trend_down "
            f"(got bucket={bucket_name or 'unknown'}, regime={regime_kind or 'unknown'})."
        )

    if mode == "losers_soft":
        losers_ok = bucket_name.endswith("Losers") or bucket_name == "futuresLosers"
        if not losers_ok:
            return (
                f"Discovery short policy (losers_soft): bucket {bucket_name or 'unknown'} "
                "not eligible for new shorts."
            )
        from profitability import bucket_regime_match_soft, profitability_from_config

        profit_cfg = profitability_from_config(profitability_raw or {"enabled": True, "regime_filter_mode": "soft"})
        if bucket_regime_match_soft(bucket_name, regime_kind, profit_cfg):
            return None
        return (
            f"Discovery short policy (losers_soft): regime {regime_kind or 'unknown'} "
            f"not aligned with {bucket_name} under soft filter."
        )

    return f"Discovery short policy: unknown mode {mode!r}."


def suppress_ineligible_discovery_short(
    signal: Any,
    *,
    discovery_short_mode: str = "off",
    allow_discovery_shorts: bool = False,
    profitability_raw: dict[str, Any] | None = None,
    hold_action: str = "HOLD",
) -> Any:
    """Downgrade discovery SELL that policy would reject — avoids noisy blocked cycles."""
    if signal is None or str(signal.action).upper() != "SELL":
        return signal
    indicators = dict(signal.indicators or {})
    discovery_source = str(indicators.get("discovery_source", "") or "")
    if not discovery_source.startswith("discovery:"):
        return signal
    block = discovery_short_block_reason(
        discovery_short_mode=discovery_short_mode,
        allow_discovery_shorts=allow_discovery_shorts,
        order_action="SELL",
        is_discovery_open=True,
        bucket=str(indicators.get("discovery_bucket", "") or ""),
        regime=str(indicators.get("regime", "") or ""),
        entry_quadrant=str(indicators.get("entry_quadrant", "") or ""),
        profitability_raw=profitability_raw,
    )
    if not block:
        return signal
    reasons = list(signal.reasons) + [
        f"Discovery short guard: suppressed SELL ({block.split('(')[0].strip()})."
    ]
    return signal.__class__(
        action=hold_action,
        confidence=signal.confidence,
        reasons=reasons,
        warnings=list(signal.warnings),
        indicators=indicators,
    )

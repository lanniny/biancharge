"""Four-quadrant entry modes — trend continuation vs reversal, with live/shadow routing."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

QUADRANT_TREND_LONG = "trend_long"
QUADRANT_TREND_SHORT = "trend_short"
QUADRANT_REVERSAL_SHORT = "reversal_short"
QUADRANT_REVERSAL_LONG = "reversal_long"

BUY = "BUY"
SELL = "SELL"
HOLD = "HOLD"


def decimal_from(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


@dataclass(frozen=True)
class QuadrantStrategyConfig:
    enabled: bool = True
    reversal_short_enabled: bool = True
    reversal_short_min_rsi: Decimal = Decimal("80")
    reversal_short_require_mtf_5m_bearish: bool = True
    reversal_short_allowed_buckets: tuple[str, ...] = (
        "futuresTradFi",
        "futuresTopVolume",
        "configured",
        "pinned",
    )
    reversal_short_execution_mode: str = "shadow"
    reversal_short_min_confidence: Decimal = Decimal("0.72")
    reversal_short_quote_fraction_mult: Decimal = Decimal("0.50")
    reversal_long_enabled: bool = True
    reversal_long_max_rsi: Decimal = Decimal("30")
    reversal_long_require_mtf_5m_bullish: bool = True
    reversal_long_allowed_buckets: tuple[str, ...] = (
        "futuresTradFi",
        "futuresTopVolume",
        "futuresLosers",
        "configured",
        "pinned",
    )
    reversal_long_execution_mode: str = "shadow"
    reversal_long_min_confidence: Decimal = Decimal("0.72")
    reversal_long_quote_fraction_mult: Decimal = Decimal("0.50")
    macro_block_gainer_long_on_risk_off: bool = True


def quadrant_strategy_from_config(raw: dict[str, Any] | None) -> QuadrantStrategyConfig:
    if not raw:
        return QuadrantStrategyConfig(enabled=False)

    def _buckets(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        items = raw.get(key, list(default))
        if not isinstance(items, (list, tuple)):
            return default
        return tuple(str(item) for item in items)

    return QuadrantStrategyConfig(
        enabled=bool(raw.get("enabled", True)),
        reversal_short_enabled=bool(raw.get("reversal_short_enabled", True)),
        reversal_short_min_rsi=decimal_from(raw.get("reversal_short_min_rsi", "80")),
        reversal_short_require_mtf_5m_bearish=bool(raw.get("reversal_short_require_mtf_5m_bearish", True)),
        reversal_short_allowed_buckets=_buckets(
            "reversal_short_allowed_buckets",
            QuadrantStrategyConfig.reversal_short_allowed_buckets,
        ),
        reversal_short_execution_mode=str(raw.get("reversal_short_execution_mode", "shadow")).lower(),
        reversal_short_min_confidence=decimal_from(raw.get("reversal_short_min_confidence", "0.72")),
        reversal_short_quote_fraction_mult=decimal_from(raw.get("reversal_short_quote_fraction_mult", "0.50")),
        reversal_long_enabled=bool(raw.get("reversal_long_enabled", True)),
        reversal_long_max_rsi=decimal_from(raw.get("reversal_long_max_rsi", "30")),
        reversal_long_require_mtf_5m_bullish=bool(raw.get("reversal_long_require_mtf_5m_bullish", True)),
        reversal_long_allowed_buckets=_buckets(
            "reversal_long_allowed_buckets",
            QuadrantStrategyConfig.reversal_long_allowed_buckets,
        ),
        reversal_long_execution_mode=str(raw.get("reversal_long_execution_mode", "shadow")).lower(),
        reversal_long_min_confidence=decimal_from(raw.get("reversal_long_min_confidence", "0.72")),
        reversal_long_quote_fraction_mult=decimal_from(raw.get("reversal_long_quote_fraction_mult", "0.50")),
        macro_block_gainer_long_on_risk_off=bool(raw.get("macro_block_gainer_long_on_risk_off", True)),
    )


def normalize_entry_bucket(bucket: str, source: str = "") -> str:
    raw = str(bucket or source or "").strip()
    if raw.startswith("discovery:"):
        return raw.split(":", 1)[1]
    if raw.startswith("holding"):
        return "holding"
    if raw.startswith("pinned"):
        return "pinned"
    return raw


def bucket_allowed(bucket: str, source: str, allowed: tuple[str, ...]) -> bool:
    name = normalize_entry_bucket(bucket, source)
    if not name:
        return False
    if name in allowed:
        return True
    return any(name.endswith(suffix) for suffix in allowed if suffix.startswith("futures"))


def classify_trend_quadrant(action: str, regime: str) -> str | None:
    regime_kind = str(regime or "").lower()
    if action == BUY and regime_kind == "trend_up":
        return QUADRANT_TREND_LONG
    if action == SELL and regime_kind == "trend_down":
        return QUADRANT_TREND_SHORT
    return None


def trend_short_allowed_bucket(bucket: str, source: str = "") -> bool:
    name = normalize_entry_bucket(bucket, source)
    return name.endswith("Losers") or name == "futuresLosers"


def reversal_short_candidate(
    *,
    cfg: QuadrantStrategyConfig,
    indicators: dict[str, str],
    bucket: str,
    source: str,
    current_action: str,
    confidence: Decimal,
) -> tuple[bool, list[str]]:
    if not cfg.enabled or not cfg.reversal_short_enabled:
        return False, []
    if cfg.reversal_short_execution_mode == "off":
        return False, []
    if not bucket_allowed(bucket, source, cfg.reversal_short_allowed_buckets):
        return False, []
    bucket_name = normalize_entry_bucket(bucket, source)
    if "Gainers" in bucket_name or bucket_name == "futuresGainers":
        return False, []

    regime = str(indicators.get("regime", "") or "").lower()
    if regime not in {"trend_up", "range"}:
        return False, []

    rsi = decimal_from(indicators.get("rsi", "0"))
    if rsi < cfg.reversal_short_min_rsi:
        return False, []

    mtf_5m = str(indicators.get("mtf_5m", "") or "")
    if cfg.reversal_short_require_mtf_5m_bearish and mtf_5m != "bearish":
        return False, []

    momentum = decimal_from(indicators.get("momentum", "0"))
    if momentum > Decimal("0.01"):
        return False, []

    reasons = [
        f"Quadrant reversal_short: RSI {rsi:.1f} overheated in {regime} regime.",
        f"5m MTF={mtf_5m or 'neutral'} with non-positive momentum {momentum:.2%}.",
        "Top-fade short on allowed bucket (not gainers).",
    ]
    if current_action == HOLD or confidence < cfg.reversal_short_min_confidence:
        confidence = max(confidence, cfg.reversal_short_min_confidence)
    return True, reasons


def reversal_long_candidate(
    *,
    cfg: QuadrantStrategyConfig,
    indicators: dict[str, str],
    bucket: str,
    source: str,
    current_action: str,
    confidence: Decimal,
) -> tuple[bool, list[str]]:
    if not cfg.enabled or not cfg.reversal_long_enabled:
        return False, []
    if cfg.reversal_long_execution_mode == "off":
        return False, []
    if not bucket_allowed(bucket, source, cfg.reversal_long_allowed_buckets):
        return False, []

    regime = str(indicators.get("regime", "") or "").lower()
    if regime != "trend_down":
        return False, []

    rsi = decimal_from(indicators.get("rsi", "0"))
    if rsi <= 0 or rsi > cfg.reversal_long_max_rsi:
        return False, []

    mtf_5m = str(indicators.get("mtf_5m", "") or "")
    if cfg.reversal_long_require_mtf_5m_bullish and mtf_5m != "bullish":
        return False, []

    momentum = decimal_from(indicators.get("momentum", "0"))
    if momentum < Decimal("-0.05"):
        return False, []

    reasons = [
        f"Quadrant reversal_long: RSI {rsi:.1f} oversold in downtrend.",
        f"5m MTF={mtf_5m or 'neutral'} suggests bounce attempt.",
        "Counter-trend dip-buy on allowed bucket with reduced size.",
    ]
    if current_action == HOLD or confidence < cfg.reversal_long_min_confidence:
        confidence = max(confidence, cfg.reversal_long_min_confidence)
    return True, reasons


def apply_quadrant_signal(
    signal: Any,
    cfg: QuadrantStrategyConfig,
    *,
    bucket: str = "",
    source: str = "",
) -> Any:
    if not cfg.enabled or signal is None:
        return signal

    indicators = dict(signal.indicators or {})
    bucket = bucket or str(indicators.get("discovery_bucket", "") or "")
    source = source or str(indicators.get("discovery_source", "") or "")
    regime = str(indicators.get("regime", "") or "")
    conf = decimal_from(signal.confidence)

    trend_q = classify_trend_quadrant(signal.action, regime)
    if (
        trend_q == QUADRANT_TREND_SHORT
        and not trend_short_allowed_bucket(bucket, source)
    ):
        trend_q = None

    ok_short, short_reasons = reversal_short_candidate(
        cfg=cfg,
        indicators=indicators,
        bucket=bucket,
        source=source,
        current_action=signal.action,
        confidence=conf,
    )
    if ok_short:
        new_conf = max(conf, cfg.reversal_short_min_confidence)
        reasons = list(signal.reasons) + short_reasons
        warnings = list(signal.warnings)
        if "overbought_rsi" not in warnings:
            warnings.append("overbought_rsi")
        indicators["entry_quadrant"] = QUADRANT_REVERSAL_SHORT
        indicators["entry_quadrant_mode"] = cfg.reversal_short_execution_mode
        return signal.__class__(
            action=SELL,
            confidence=new_conf,
            reasons=reasons,
            warnings=warnings,
            indicators=indicators,
        )

    ok_long, long_reasons = reversal_long_candidate(
        cfg=cfg,
        indicators=indicators,
        bucket=bucket,
        source=source,
        current_action=signal.action,
        confidence=conf,
    )
    if ok_long:
        new_conf = max(conf, cfg.reversal_long_min_confidence)
        reasons = list(signal.reasons) + long_reasons
        warnings = [w for w in signal.warnings if w != "downtrend_regime"]
        indicators["entry_quadrant"] = QUADRANT_REVERSAL_LONG
        indicators["entry_quadrant_mode"] = cfg.reversal_long_execution_mode
        return signal.__class__(
            action=BUY,
            confidence=new_conf,
            reasons=reasons,
            warnings=warnings,
            indicators=indicators,
        )

    if trend_q and signal.action in {BUY, SELL}:
        indicators["entry_quadrant"] = trend_q
        indicators["entry_quadrant_mode"] = "live"
        return signal.__class__(
            action=signal.action,
            confidence=signal.confidence,
            reasons=list(signal.reasons),
            warnings=list(signal.warnings),
            indicators=indicators,
        )

    if signal.action in {BUY, SELL}:
        indicators["entry_quadrant"] = trend_q or "unclassified"
        indicators["entry_quadrant_mode"] = "live"
    return signal.__class__(
        action=signal.action,
        confidence=signal.confidence,
        reasons=list(signal.reasons),
        warnings=list(signal.warnings),
        indicators=indicators,
    )


def entry_quadrant(indicators: dict[str, str] | None) -> str:
    return str((indicators or {}).get("entry_quadrant", "") or "")


def quadrant_quote_fraction_mult(indicators: dict[str, str] | None, cfg: QuadrantStrategyConfig) -> Decimal:
    quad = entry_quadrant(indicators)
    if quad == QUADRANT_REVERSAL_SHORT:
        return cfg.reversal_short_quote_fraction_mult
    if quad == QUADRANT_REVERSAL_LONG:
        return cfg.reversal_long_quote_fraction_mult
    return Decimal("1")


def quadrant_should_shadow(indicators: dict[str, str] | None, cfg: QuadrantStrategyConfig) -> tuple[bool, str | None]:
    quad = entry_quadrant(indicators)
    mode = str((indicators or {}).get("entry_quadrant_mode", "") or "").lower()
    if quad == QUADRANT_REVERSAL_SHORT and mode == "shadow":
        return True, "Quadrant reversal_short routed to shadow paper (graduation required for live)."
    if quad == QUADRANT_REVERSAL_LONG and mode == "shadow":
        return True, "Quadrant reversal_long routed to shadow paper (graduation required for live)."
    return False, None


def bypass_downtrend_long_block(indicators: dict[str, str] | None) -> bool:
    return entry_quadrant(indicators) == QUADRANT_REVERSAL_LONG


def bypass_overbought_long_block(indicators: dict[str, str] | None) -> bool:
    return entry_quadrant(indicators) == QUADRANT_REVERSAL_SHORT


def bypass_discovery_short_block(indicators: dict[str, str] | None) -> bool:
    return entry_quadrant(indicators) == QUADRANT_REVERSAL_SHORT


def skip_gainers_short_lessons(indicators: dict[str, str] | None) -> bool:
    return entry_quadrant(indicators) == QUADRANT_REVERSAL_SHORT


def macro_gainer_long_block_reason(
    ctx: dict[str, Any],
    *,
    order_action: str,
    bucket: str,
    source: str,
    cfg: QuadrantStrategyConfig,
) -> str | None:
    if not cfg.enabled or not cfg.macro_block_gainer_long_on_risk_off:
        return None
    if order_action != BUY:
        return None
    macro = ctx.get("macro") or {}
    if macro.get("bias") != "risk_off":
        return None
    bucket_name = normalize_entry_bucket(bucket, source)
    if "Gainers" in bucket_name or bucket_name == "futuresGainers":
        return (
            "Macro guard: BTC risk_off — block new long on futures gainers "
            "until BTC 1h bias improves."
        )
    return None

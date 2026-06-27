"""Per-discovery-bucket entry policies — confidence, MTF alignment, sizing."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

BUY = "BUY"
SELL = "SELL"


def decimal_from(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


@dataclass(frozen=True)
class BucketPolicy:
    min_confidence_bump: Decimal = Decimal("0")
    require_mtf_5m_align: bool = False
    require_mtf_5m_align_short: bool = False
    short_allow_neutral_5m: bool = False
    min_fusion_bull_pct: Decimal | None = None
    min_fusion_bear_pct: Decimal | None = None
    quote_fraction_mult: Decimal = Decimal("1")


DEFAULT_POLICIES: dict[str, BucketPolicy] = {
    "futuresGainers": BucketPolicy(
        min_confidence_bump=Decimal("0.05"),
        require_mtf_5m_align=True,
        min_fusion_bull_pct=Decimal("0.55"),
        quote_fraction_mult=Decimal("0.85"),
    ),
    "futuresLosers": BucketPolicy(
        min_confidence_bump=Decimal("0.03"),
        require_mtf_5m_align=False,
        require_mtf_5m_align_short=True,
        min_fusion_bear_pct=Decimal("0.48"),
        quote_fraction_mult=Decimal("0.90"),
    ),
    "futuresTradFi": BucketPolicy(
        min_confidence_bump=Decimal("0.02"),
        require_mtf_5m_align=True,
        require_mtf_5m_align_short=True,
        min_fusion_bull_pct=Decimal("0.50"),
        min_fusion_bear_pct=Decimal("0.50"),
        quote_fraction_mult=Decimal("0.80"),
    ),
    "futuresTopVolume": BucketPolicy(
        min_confidence_bump=Decimal("0"),
        require_mtf_5m_align=True,
        quote_fraction_mult=Decimal("1.0"),
    ),
    "holding": BucketPolicy(),
    "pinned": BucketPolicy(),
}


@dataclass(frozen=True)
class BucketStrategyConfig:
    enabled: bool = True
    policies: dict[str, BucketPolicy] = field(default_factory=lambda: dict(DEFAULT_POLICIES))


def bucket_strategy_from_config(raw: dict[str, Any] | None) -> BucketStrategyConfig:
    if not raw:
        return BucketStrategyConfig(enabled=False)
    policies = dict(DEFAULT_POLICIES)
    overrides = raw.get("policies") or {}
    if isinstance(overrides, dict):
        for name, spec in overrides.items():
            if not isinstance(spec, dict):
                continue
            base = policies.get(name, BucketPolicy())
            policies[name] = BucketPolicy(
                min_confidence_bump=decimal_from(spec.get("min_confidence_bump", base.min_confidence_bump)),
                require_mtf_5m_align=bool(spec.get("require_mtf_5m_align", base.require_mtf_5m_align)),
                require_mtf_5m_align_short=bool(
                    spec.get("require_mtf_5m_align_short", base.require_mtf_5m_align_short)
                ),
                short_allow_neutral_5m=bool(
                    spec.get("short_allow_neutral_5m", base.short_allow_neutral_5m)
                ),
                min_fusion_bull_pct=(
                    decimal_from(spec["min_fusion_bull_pct"])
                    if spec.get("min_fusion_bull_pct") is not None
                    else base.min_fusion_bull_pct
                ),
                min_fusion_bear_pct=(
                    decimal_from(spec["min_fusion_bear_pct"])
                    if spec.get("min_fusion_bear_pct") is not None
                    else base.min_fusion_bear_pct
                ),
                quote_fraction_mult=decimal_from(spec.get("quote_fraction_mult", base.quote_fraction_mult)),
            )
    return BucketStrategyConfig(enabled=bool(raw.get("enabled", True)), policies=policies)


def normalize_bucket(bucket: str, source: str = "") -> str:
    raw = str(bucket or source or "").strip()
    if raw.startswith("discovery:"):
        raw = raw.split(":", 1)[1]
    if raw.startswith("holding"):
        return "holding"
    if raw.startswith("pinned"):
        return "pinned"
    return raw


def resolve_bucket_policy(cfg: BucketStrategyConfig, bucket: str, source: str = "") -> BucketPolicy | None:
    if not cfg.enabled:
        return None
    name = normalize_bucket(bucket, source)
    return cfg.policies.get(name)


def bucket_open_block_reasons(
    *,
    cfg: BucketStrategyConfig,
    bucket: str,
    source: str,
    order_action: str,
    reduce_only: bool,
    indicators: dict[str, str],
    confidence: Decimal,
    base_min_confidence: Decimal,
) -> list[str]:
    if reduce_only or not cfg.enabled:
        return []
    policy = resolve_bucket_policy(cfg, bucket, source)
    if policy is None:
        return []
    blocked: list[str] = []
    bucket_name = normalize_bucket(bucket, source)
    mtf_5m = str(indicators.get("mtf_5m", "") or "")
    mtf_1m = str(indicators.get("mtf_1m", "") or "")
    fusion = decimal_from(indicators.get("fusion_bull_pct", "0") or "0")
    fusion_bear = Decimal("1") - fusion

    required = base_min_confidence + policy.min_confidence_bump
    if confidence < required:
        blocked.append(
            f"Bucket {bucket_name}: confidence {confidence} below bucket minimum {required}."
        )
    if policy.require_mtf_5m_align:
        if order_action == BUY and mtf_5m != "bullish":
            blocked.append(f"Bucket {bucket_name}: long entry requires 5m bullish (got {mtf_5m or 'neutral'}).")
        if order_action == SELL and mtf_5m != "bearish":
            blocked.append(f"Bucket {bucket_name}: short entry requires 5m bearish (got {mtf_5m or 'neutral'}).")
    elif policy.require_mtf_5m_align_short and order_action == SELL:
        short_5m_ok = mtf_5m == "bearish"
        if not short_5m_ok and policy.short_allow_neutral_5m and mtf_5m in {"", "neutral"}:
            short_5m_ok = mtf_1m in {"bearish", "neutral"}
        if not short_5m_ok:
            blocked.append(
                f"Bucket {bucket_name}: short entry requires 5m bearish (got {mtf_5m or 'neutral'})."
            )
    if policy.min_fusion_bull_pct is not None and order_action == BUY and fusion < policy.min_fusion_bull_pct:
        blocked.append(
            f"Bucket {bucket_name}: fusion bull {fusion} below bucket min {policy.min_fusion_bull_pct}."
        )
    if policy.min_fusion_bear_pct is not None and order_action == SELL and fusion_bear < policy.min_fusion_bear_pct:
        blocked.append(
            f"Bucket {bucket_name}: fusion bear {fusion_bear} below bucket min {policy.min_fusion_bear_pct}."
        )
    return blocked


def bucket_quote_fraction_mult(cfg: BucketStrategyConfig, bucket: str, source: str = "") -> Decimal:
    if not cfg.enabled:
        return Decimal("1")
    policy = resolve_bucket_policy(cfg, bucket, source)
    if policy is None:
        return Decimal("1")
    mult = policy.quote_fraction_mult
    if mult <= 0:
        return Decimal("1")
    return mult

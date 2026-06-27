"""High-quality recovery probes for bad recent expectancy periods."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
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
class RecoveryModeConfig:
    enabled: bool = False
    allowed_buckets: tuple[str, ...] = ("configured", "pinned", "futuresTradFi")
    allowed_sessions: tuple[str, ...] = ("europe", "overlap_europe_us")
    min_learning_sample: int = 15
    max_learning_win_rate: Decimal = Decimal("0.25")
    max_learning_profit_factor: Decimal = Decimal("0.60")
    max_learning_total_pnl: Decimal = Decimal("0")
    min_confidence: Decimal = Decimal("0.95")
    min_fusion_pct: Decimal = Decimal("0.80")
    min_volume_ratio: Decimal = Decimal("1.00")
    max_abs_momentum: Decimal = Decimal("0.018")
    max_abs_24h_change: Decimal = Decimal("0.12")
    long_min_rsi: Decimal = Decimal("45")
    long_max_rsi: Decimal = Decimal("68")
    short_min_rsi: Decimal = Decimal("32")
    short_max_rsi: Decimal = Decimal("55")
    require_1m_not_against: bool = True
    size_factor: Decimal = Decimal("0.25")
    max_daily_probes: int = 2
    max_session_daily_probes: int = 1


@dataclass(frozen=True)
class RecoveryProbeDecision:
    approved: bool
    reason: str
    size_factor: Decimal = Decimal("1")
    session: str = ""
    bucket: str = ""


def recovery_mode_from_config(raw: dict[str, Any] | None) -> RecoveryModeConfig:
    raw = raw or {}
    return RecoveryModeConfig(
        enabled=bool(raw.get("enabled", False)),
        allowed_buckets=tuple(str(v) for v in raw.get("allowed_buckets", ["configured", "pinned", "futuresTradFi"])),
        allowed_sessions=tuple(str(v) for v in raw.get("allowed_sessions", ["europe", "overlap_europe_us"])),
        min_learning_sample=int(raw.get("min_learning_sample", 15)),
        max_learning_win_rate=decimal_from(raw.get("max_learning_win_rate", "0.25")),
        max_learning_profit_factor=decimal_from(raw.get("max_learning_profit_factor", "0.60")),
        max_learning_total_pnl=decimal_from(raw.get("max_learning_total_pnl", "0")),
        min_confidence=decimal_from(raw.get("min_confidence", "0.95")),
        min_fusion_pct=decimal_from(raw.get("min_fusion_pct", "0.80")),
        min_volume_ratio=decimal_from(raw.get("min_volume_ratio", "1.00")),
        max_abs_momentum=decimal_from(raw.get("max_abs_momentum", "0.018")),
        max_abs_24h_change=decimal_from(raw.get("max_abs_24h_change", "0.12")),
        long_min_rsi=decimal_from(raw.get("long_min_rsi", "45")),
        long_max_rsi=decimal_from(raw.get("long_max_rsi", "68")),
        short_min_rsi=decimal_from(raw.get("short_min_rsi", "32")),
        short_max_rsi=decimal_from(raw.get("short_max_rsi", "55")),
        require_1m_not_against=bool(raw.get("require_1m_not_against", True)),
        size_factor=decimal_from(raw.get("size_factor", "0.25")),
        max_daily_probes=int(raw.get("max_daily_probes", 2)),
        max_session_daily_probes=int(raw.get("max_session_daily_probes", 1)),
    )


def _bucket_from_indicators(indicators: dict[str, Any]) -> str:
    bucket = str(indicators.get("discovery_bucket") or "").strip()
    source = str(indicators.get("discovery_source") or "").strip()
    if bucket:
        return bucket
    if source.startswith("discovery:"):
        return source.split(":", 1)[1]
    if source.startswith("pinned"):
        return "pinned"
    if source:
        return source
    return "configured"


def _learning_is_in_recovery(snapshot: dict[str, Any] | None, cfg: RecoveryModeConfig) -> tuple[bool, str]:
    if not snapshot or not snapshot.get("enabled"):
        return False, "trade learning snapshot unavailable"
    sample = int(snapshot.get("sampleSize", 0))
    win_rate = decimal_from(snapshot.get("winRate", "0"))
    profit_factor = decimal_from(snapshot.get("profitFactor", "0"))
    total_pnl = decimal_from(snapshot.get("totalRealizedPnl", "0"))
    if sample < cfg.min_learning_sample:
        return False, f"learning sample {sample} < {cfg.min_learning_sample}"
    if (
        win_rate <= cfg.max_learning_win_rate
        or profit_factor <= cfg.max_learning_profit_factor
        or total_pnl <= cfg.max_learning_total_pnl
    ):
        return True, (
            f"learning recovery active: sample={sample}, WR={win_rate:.0%}, "
            f"PF={profit_factor}, pnl={total_pnl}"
        )
    return False, f"recent expectancy not weak enough for recovery probe: WR={win_rate:.0%}, PF={profit_factor}"


def _direction_quality(
    *,
    action: str,
    indicators: dict[str, Any],
    cfg: RecoveryModeConfig,
) -> tuple[bool, str]:
    mtf_1m = str(indicators.get("mtf_1m") or "").lower()
    mtf_5m = str(indicators.get("mtf_5m") or "").lower()
    mtf_15m = str(indicators.get("mtf_15m") or "").lower()
    regime = str(indicators.get("regime") or "").lower()
    rsi = decimal_from(indicators.get("rsi", "0"))
    momentum = decimal_from(indicators.get("momentum", "0"))
    volume_ratio = decimal_from(indicators.get("volume_ratio", "0"))
    change_24h = decimal_from(indicators.get("price_change_pct_24h", "0"))

    if abs(momentum) > cfg.max_abs_momentum:
        return False, f"momentum {momentum:.2%} is too extended"
    if abs(change_24h) > cfg.max_abs_24h_change:
        return False, f"24h change {change_24h:.2%} is too extended"
    if volume_ratio < cfg.min_volume_ratio:
        return False, f"volume ratio {volume_ratio} < {cfg.min_volume_ratio}"

    if action == BUY:
        fusion = decimal_from(indicators.get("fusion_bull_pct", "0"))
        if fusion < cfg.min_fusion_pct:
            return False, f"bull fusion {fusion} < {cfg.min_fusion_pct}"
        if mtf_5m != "bullish" or mtf_15m != "bullish":
            return False, f"long requires 5m+15m bullish (got 5m={mtf_5m}, 15m={mtf_15m})"
        if cfg.require_1m_not_against and mtf_1m == "bearish":
            return False, "long recovery probe blocked by bearish 1m"
        if regime != "trend_up":
            return False, f"long requires trend_up regime (got {regime})"
        if not (cfg.long_min_rsi <= rsi <= cfg.long_max_rsi):
            return False, f"long RSI {rsi} outside {cfg.long_min_rsi}-{cfg.long_max_rsi}"
        if momentum <= 0:
            return False, f"long momentum {momentum:.2%} is not positive"
        return True, "long quality confirmed by 5m+15m trend, fusion, volume, and non-extended momentum"

    if action == SELL:
        fusion = decimal_from(indicators.get("fusion_bear_pct", "0"))
        if fusion < cfg.min_fusion_pct:
            return False, f"bear fusion {fusion} < {cfg.min_fusion_pct}"
        if mtf_5m != "bearish" or mtf_15m != "bearish":
            return False, f"short requires 5m+15m bearish (got 5m={mtf_5m}, 15m={mtf_15m})"
        if cfg.require_1m_not_against and mtf_1m == "bullish":
            return False, "short recovery probe blocked by bullish 1m"
        if regime != "trend_down":
            return False, f"short requires trend_down regime (got {regime})"
        if not (cfg.short_min_rsi <= rsi <= cfg.short_max_rsi):
            return False, f"short RSI {rsi} outside {cfg.short_min_rsi}-{cfg.short_max_rsi}"
        if momentum >= 0:
            return False, f"short momentum {momentum:.2%} is not negative"
        return True, "short quality confirmed by 5m+15m trend, fusion, volume, and non-extended momentum"

    return False, f"unsupported recovery action {action}"


def evaluate_recovery_probe(
    *,
    cfg: RecoveryModeConfig,
    market_context: dict[str, Any] | None,
    trade_learning: dict[str, Any] | None,
    indicators: dict[str, Any],
    action: str,
    confidence: Decimal,
    memory: Any,
) -> RecoveryProbeDecision:
    if not cfg.enabled:
        return RecoveryProbeDecision(False, "recovery mode disabled")
    ctx = market_context or {}
    session = str(((ctx.get("session") or {}).get("primary")) or "")
    if session not in set(cfg.allowed_sessions):
        return RecoveryProbeDecision(False, f"session {session or 'unknown'} not enabled for recovery probes")
    funding = ctx.get("funding") or {}
    if funding.get("phase") == "pre":
        return RecoveryProbeDecision(False, "funding settlement window is not eligible for recovery probes")

    bucket = _bucket_from_indicators(indicators)
    if bucket not in set(cfg.allowed_buckets):
        return RecoveryProbeDecision(False, f"bucket {bucket} not enabled for recovery probes")

    learning_ok, learning_reason = _learning_is_in_recovery(trade_learning, cfg)
    if not learning_ok:
        return RecoveryProbeDecision(False, learning_reason)

    if confidence < cfg.min_confidence:
        return RecoveryProbeDecision(False, f"confidence {confidence} < recovery minimum {cfg.min_confidence}")

    daily_count = int(getattr(memory, "recovery_probe_count_today", lambda *_: 0)())
    if cfg.max_daily_probes >= 0 and daily_count >= cfg.max_daily_probes:
        return RecoveryProbeDecision(False, f"daily recovery probe cap {cfg.max_daily_probes} reached")
    session_count = int(getattr(memory, "recovery_probe_count_today", lambda *_: 0)(session))
    if cfg.max_session_daily_probes >= 0 and session_count >= cfg.max_session_daily_probes:
        return RecoveryProbeDecision(False, f"session recovery probe cap {cfg.max_session_daily_probes} reached for {session}")

    direction_ok, direction_reason = _direction_quality(action=action, indicators=indicators, cfg=cfg)
    if not direction_ok:
        return RecoveryProbeDecision(False, direction_reason)

    factor = min(max(cfg.size_factor, Decimal("0.05")), Decimal("1"))
    factor = factor.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    return RecoveryProbeDecision(True, f"{learning_reason}; {direction_reason}", factor, session, bucket)

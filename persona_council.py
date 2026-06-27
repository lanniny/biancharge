"""Multi-persona hedging review council for the autotrader.

Four deterministic personas independently score a candidate trade from the
signal indicators + context, then a weighted vote plus a minority-veto produces
a verdict. This is a META review layer that runs AFTER the normal risk gates
produce a RiskDecision: it can confirm, downgrade (shrink size / lower trust),
or veto an otherwise-approved trade. It never turns a blocked trade into an
approved one.

Design notes (why this shape):
- Pure functions + a *_from_config loader, matching the rest of the codebase, so
  it is fully unit-testable with no network and fully reproducible in backtests.
- The verdict and every persona's score/reasons are written to
  trade_rationale.personaCouncil so each decision is auditable in the ledger.
- Safety-critical: the Conservative and Prudent personas hold a UNILATERAL veto
  (minority-veto), because majority voting inherits an "agreeable" bias that is
  poor at rejecting bad trades. A single risk-focused persona can stop a trade.

Inspired by the risk-management debate structure in multi-agent trading research
(aggressive / conservative / neutral debators), reduced to a rule engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

# Verdict outcomes.
VERDICT_CONFIRM = "confirm"
VERDICT_DOWNGRADE = "downgrade"
VERDICT_VETO = "veto"

AGGRESSIVE = "aggressive"
CONSERVATIVE = "conservative"
ANALYST = "analyst"
PRUDENT = "prudent"


def _dec(value: Any, default: str = "0") -> Decimal:
    try:
        if value is None or value == "":
            return Decimal(default)
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


@dataclass
class PersonaCouncilConfig:
    enabled: bool = False
    # Vote weights per persona (relative). Conservative/Prudent also hold veto.
    weight_aggressive: Decimal = Decimal("1.0")
    weight_conservative: Decimal = Decimal("1.0")
    weight_analyst: Decimal = Decimal("1.0")
    weight_prudent: Decimal = Decimal("1.0")
    # Aggregate score (0..1) below which an open is downgraded (size shrunk).
    downgrade_below: Decimal = Decimal("0.50")
    # Aggregate score below which an open is vetoed outright.
    veto_below: Decimal = Decimal("0.30")
    # Size multiplier applied when verdict is downgrade.
    downgrade_size_mult: Decimal = Decimal("0.5")
    # Risk thresholds the cautious personas react to.
    high_volatility: Decimal = Decimal("0.10")
    high_drawdown: Decimal = Decimal("0.15")
    overbought_rsi: Decimal = Decimal("78")
    oversold_rsi: Decimal = Decimal("22")
    pump_chase_24h_pct: Decimal = Decimal("0.20")
    # Only review opening trades; reduce-only exits are always allowed through.
    review_reduce_only: bool = False


def persona_council_from_config(raw: dict[str, Any] | None) -> PersonaCouncilConfig:
    raw = raw or {}
    return PersonaCouncilConfig(
        enabled=bool(raw.get("enabled", False)),
        weight_aggressive=_dec(raw.get("weight_aggressive", "1.0"), "1.0"),
        weight_conservative=_dec(raw.get("weight_conservative", "1.0"), "1.0"),
        weight_analyst=_dec(raw.get("weight_analyst", "1.0"), "1.0"),
        weight_prudent=_dec(raw.get("weight_prudent", "1.0"), "1.0"),
        downgrade_below=_dec(raw.get("downgrade_below", "0.50"), "0.50"),
        veto_below=_dec(raw.get("veto_below", "0.30"), "0.30"),
        downgrade_size_mult=_dec(raw.get("downgrade_size_mult", "0.5"), "0.5"),
        high_volatility=_dec(raw.get("high_volatility", "0.10"), "0.10"),
        high_drawdown=_dec(raw.get("high_drawdown", "0.15"), "0.15"),
        overbought_rsi=_dec(raw.get("overbought_rsi", "78"), "78"),
        oversold_rsi=_dec(raw.get("oversold_rsi", "22"), "22"),
        pump_chase_24h_pct=_dec(raw.get("pump_chase_24h_pct", "0.20"), "0.20"),
        review_reduce_only=bool(raw.get("review_reduce_only", False)),
    )


@dataclass
class PersonaVote:
    persona: str
    score: Decimal  # 0..1, higher = more in favor of the trade
    veto: bool = False
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "persona": self.persona,
            "score": str(self.score.quantize(Decimal("0.0001"))),
            "veto": self.veto,
            "reasons": self.reasons,
        }


@dataclass
class CouncilVerdict:
    verdict: str
    aggregate_score: Decimal
    size_multiplier: Decimal
    votes: list[PersonaVote]
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "aggregateScore": str(self.aggregate_score.quantize(Decimal("0.0001"))),
            "sizeMultiplier": str(self.size_multiplier.quantize(Decimal("0.0001"))),
            "votes": [v.to_dict() for v in self.votes],
            "reasons": self.reasons,
        }


def _mtf_alignment(indicators: dict[str, Any], side: str) -> tuple[int, int]:
    """Return (aligned, total) timeframes that agree with the trade side."""
    want = "bullish" if side == "BUY" else "bearish"
    frames = [indicators.get("mtf_1m"), indicators.get("mtf_5m"), indicators.get("mtf_15m")]
    frames = [f for f in frames if f]
    aligned = sum(1 for f in frames if str(f) == want)
    return aligned, len(frames)


def _clamp01(x: Decimal) -> Decimal:
    if x < 0:
        return Decimal("0")
    if x > 1:
        return Decimal("1")
    return x


def aggressive_vote(side: str, indicators: dict[str, Any], cfg: PersonaCouncilConfig) -> PersonaVote:
    """Momentum/breakout seeker: rewards strong directional conviction."""
    reasons: list[str] = []
    score = Decimal("0.5")
    fusion = _dec(indicators.get("fusion_bull_pct"), "0.5")
    # For shorts, conviction is the bear share (1 - bull share).
    conviction = fusion if side == "BUY" else (Decimal("1") - fusion)
    score += (conviction - Decimal("0.5")) * Decimal("0.8")
    reasons.append(f"fusion conviction {conviction.quantize(Decimal('0.01'))}")
    adx = _dec(indicators.get("adx"), "0")
    if adx >= 25:
        score += Decimal("0.15")
        reasons.append(f"strong trend adx={adx.quantize(Decimal('0.1'))}")
    aligned, total = _mtf_alignment(indicators, side)
    if total:
        score += (Decimal(aligned) / Decimal(total) - Decimal("0.5")) * Decimal("0.3")
        reasons.append(f"mtf {aligned}/{total} aligned")
    return PersonaVote(AGGRESSIVE, _clamp01(score), reasons=reasons)


def conservative_vote(side: str, indicators: dict[str, Any], cfg: PersonaCouncilConfig) -> PersonaVote:
    """Capital-preservation focus: punishes volatility/drawdown, holds a veto."""
    reasons: list[str] = []
    score = Decimal("0.6")
    veto = False
    vol = _dec(indicators.get("volatility"), "0")
    if vol >= cfg.high_volatility:
        score -= Decimal("0.4")
        reasons.append(f"volatility {vol.quantize(Decimal('0.0001'))} >= {cfg.high_volatility}")
        veto = True
    dd = _dec(indicators.get("drawdown"), "0")
    if dd >= cfg.high_drawdown:
        score -= Decimal("0.3")
        reasons.append(f"drawdown {dd.quantize(Decimal('0.0001'))} >= {cfg.high_drawdown}")
        veto = True
    bb = _dec(indicators.get("bb_width"), "0")
    if str(indicators.get("regime")) == "squeeze":
        score -= Decimal("0.15")
        reasons.append("squeeze regime: breakout direction unconfirmed")
    if not reasons:
        reasons.append("risk metrics within conservative limits")
    return PersonaVote(CONSERVATIVE, _clamp01(score), veto=veto, reasons=reasons)


def analyst_vote(side: str, indicators: dict[str, Any], cfg: PersonaCouncilConfig) -> PersonaVote:
    """Confluence checker: rewards multi-factor agreement and regime fit."""
    reasons: list[str] = []
    score = Decimal("0.5")
    votes = indicators.get("fusion_votes") or {}
    # build_signal stores fusion_votes as a JSON string in the live path; accept both.
    if isinstance(votes, str):
        try:
            import json as _json

            votes = _json.loads(votes)
        except Exception:
            votes = {}
    if isinstance(votes, dict) and votes:
        want = "bull" if side == "BUY" else "bear"
        agree = sum(1 for v in votes.values() if str(v) == want)
        disagree = sum(1 for v in votes.values() if str(v) not in (want, "neutral"))
        total = len(votes)
        score += (Decimal(agree - disagree) / Decimal(total)) * Decimal("0.4")
        reasons.append(f"factor confluence {agree} agree / {disagree} against of {total}")
    # Stage 5: candlestick pattern read (positive = bullish geometry).
    kline = _dec(indicators.get("kline_pattern_score"), "0")
    if kline != 0:
        directional = kline if side == "BUY" else -kline
        score += directional * Decimal("0.2")
        reasons.append(f"kline {indicators.get('kline_pattern_label', '?')} ({kline})")
    regime = str(indicators.get("regime", ""))
    if side == "BUY" and regime in {"trend_up", "uptrend", "trending_up"}:
        score += Decimal("0.1")
        reasons.append("regime supports long")
    elif side == "SELL" and regime in {"trend_down", "downtrend", "trending_down"}:
        score += Decimal("0.1")
        reasons.append("regime supports short")
    vr = _dec(indicators.get("volume_ratio"), "1")
    if vr >= Decimal("1.2"):
        score += Decimal("0.1")
        reasons.append(f"volume confirms ({vr.quantize(Decimal('0.01'))}x)")
    elif vr < Decimal("0.7"):
        score -= Decimal("0.1")
        reasons.append(f"thin volume ({vr.quantize(Decimal('0.01'))}x)")
    return PersonaVote(ANALYST, _clamp01(score), reasons=reasons)


def prudent_vote(side: str, indicators: dict[str, Any], cfg: PersonaCouncilConfig) -> PersonaVote:
    """Compliance/last-line persona: vetoes chasing extremes; holds a veto."""
    reasons: list[str] = []
    score = Decimal("0.6")
    veto = False
    rsi = _dec(indicators.get("rsi"), "50")
    if side == "BUY" and rsi >= cfg.overbought_rsi:
        score -= Decimal("0.4")
        reasons.append(f"overbought rsi={rsi.quantize(Decimal('0.1'))}; no chasing")
        veto = True
    if side == "SELL" and rsi <= cfg.oversold_rsi:
        score -= Decimal("0.4")
        reasons.append(f"oversold rsi={rsi.quantize(Decimal('0.1'))}; no chasing")
        veto = True
    change = _dec(indicators.get("price_change_pct_24h"), "0")
    if side == "BUY" and change >= cfg.pump_chase_24h_pct:
        score -= Decimal("0.3")
        reasons.append(f"pump chase: +{(change*100).quantize(Decimal('0.1'))}% in 24h")
        veto = True
    if side == "SELL" and change <= -cfg.pump_chase_24h_pct:
        score -= Decimal("0.3")
        reasons.append(f"dump chase: {(change*100).quantize(Decimal('0.1'))}% in 24h")
        veto = True
    # Counter-trend entry against a clearly opposing higher timeframe.
    aligned, total = _mtf_alignment(indicators, side)
    if total >= 2 and aligned == 0:
        score -= Decimal("0.25")
        reasons.append("counter-trend vs all higher timeframes")
    if not reasons:
        reasons.append("no compliance red flags")
    return PersonaVote(PRUDENT, _clamp01(score), veto=veto, reasons=reasons)


def evaluate_council(
    side: str,
    indicators: dict[str, Any],
    cfg: PersonaCouncilConfig,
    *,
    context: dict[str, Any] | None = None,
) -> CouncilVerdict:
    """Run all four personas and aggregate into a verdict.

    side is the trade direction ("BUY"/"SELL"). Returns a CouncilVerdict whose
    size_multiplier the caller multiplies into the order notional (1.0 = full,
    0.0 = veto -> caller should block).
    """
    votes = [
        aggressive_vote(side, indicators, cfg),
        conservative_vote(side, indicators, cfg),
        analyst_vote(side, indicators, cfg),
        prudent_vote(side, indicators, cfg),
    ]
    weights = {
        AGGRESSIVE: cfg.weight_aggressive,
        CONSERVATIVE: cfg.weight_conservative,
        ANALYST: cfg.weight_analyst,
        PRUDENT: cfg.weight_prudent,
    }
    total_weight = sum(weights.values()) or Decimal("1")
    aggregate = sum((v.score * weights[v.persona] for v in votes), Decimal("0")) / total_weight
    aggregate = _clamp01(aggregate)

    reasons: list[str] = []

    # Stage 4: sentiment alignment nudge. If the market sentiment score (-1..1)
    # strongly opposes the trade direction, shave the aggregate; if it aligns,
    # give a small boost. Sentiment is advisory here (it never vetoes on its own).
    sent = (context or {}).get("sentiment") if context else None
    if isinstance(sent, dict) and sent.get("enabled") and "score" in sent:
        s = _dec(sent.get("score"), "0")
        directional = s if side == "BUY" else -s  # how much sentiment favors this side
        if directional <= Decimal("-0.5"):
            aggregate = _clamp01(aggregate - Decimal("0.15"))
            reasons.append(f"sentiment opposes trade ({sent.get('label')}, score {s}); -0.15")
        elif directional >= Decimal("0.5"):
            aggregate = _clamp01(aggregate + Decimal("0.05"))
            reasons.append(f"sentiment confirms ({sent.get('label')}); +0.05")

    vetoers = [v.persona for v in votes if v.veto]

    if vetoers:
        reasons.append(f"unilateral veto by {', '.join(vetoers)}")
        return CouncilVerdict(VERDICT_VETO, aggregate, Decimal("0"), votes, reasons)
    if aggregate < cfg.veto_below:
        reasons.append(f"aggregate score {aggregate.quantize(Decimal('0.01'))} below veto floor {cfg.veto_below}")
        return CouncilVerdict(VERDICT_VETO, aggregate, Decimal("0"), votes, reasons)
    if aggregate < cfg.downgrade_below:
        reasons.append(
            f"aggregate score {aggregate.quantize(Decimal('0.01'))} below downgrade line {cfg.downgrade_below}"
        )
        return CouncilVerdict(VERDICT_DOWNGRADE, aggregate, cfg.downgrade_size_mult, votes, reasons)
    reasons.append(f"council confirms (score {aggregate.quantize(Decimal('0.01'))})")
    return CouncilVerdict(VERDICT_CONFIRM, aggregate, Decimal("1"), votes, reasons)

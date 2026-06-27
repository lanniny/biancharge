"""Symbol asset-class profiles — TradFi vs crypto, leverage ceilings, tiered sizing."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from market_autotrader import AutoExecutionConfig, Signal

BUY = "BUY"
SELL = "SELL"

ASSET_CRYPTO = "crypto"
ASSET_TRADFI_STOCK = "tradfi_stock"
ASSET_TRADFI_COMMODITY = "tradfi_commodity"
ASSET_TRADFI_INDEX = "tradfi_index"

# Binance USDT-M TradFi perpetuals (stock / commodity / index tokens).
TRADFI_STOCK_BASES = frozenset(
    {
        "AAPL",
        "AMZN",
        "COIN",
        "GOOG",
        "META",
        "MSFT",
        "MSTR",
        "NFLX",
        "NVDA",
        "TSLA",
        "BABA",
        "DIS",
        "INTC",
        "AMD",
        "PYPL",
        "SQ",
        "UBER",
        "ABNB",
        "CRM",
        "ORCL",
    }
)
TRADFI_COMMODITY_BASES = frozenset({"XAU", "XAG", "PAXG"})
TRADFI_INDEX_BASES = frozenset({"SPX", "US500", "NAS100", "US30", "HK50"})


def split_usdt_base(symbol: str) -> str:
    normalized = str(symbol or "").upper().strip()
    for quote in ("USDT", "USDC", "FDUSD", "BUSD"):
        if normalized.endswith(quote):
            return normalized[: -len(quote)]
    return normalized


def classify_asset(symbol: str) -> str:
    base = split_usdt_base(symbol)
    if base in TRADFI_STOCK_BASES:
        return ASSET_TRADFI_STOCK
    if base in TRADFI_COMMODITY_BASES:
        return ASSET_TRADFI_COMMODITY
    if base in TRADFI_INDEX_BASES:
        return ASSET_TRADFI_INDEX
    return ASSET_CRYPTO


def is_tradfi_symbol(symbol: str) -> bool:
    return classify_asset(symbol) != ASSET_CRYPTO


def asset_class_label(asset_class: str) -> str:
    return {
        ASSET_CRYPTO: "crypto",
        ASSET_TRADFI_STOCK: "tradfi_stock",
        ASSET_TRADFI_COMMODITY: "tradfi_commodity",
        ASSET_TRADFI_INDEX: "tradfi_index",
    }.get(asset_class, "crypto")


def asset_class_max_leverage(asset_class: str, auto_exec: AutoExecutionConfig) -> int:
    caps = {
        ASSET_CRYPTO: int(auto_exec.max_leverage_crypto),
        ASSET_TRADFI_STOCK: int(auto_exec.max_leverage_tradfi_stock),
        ASSET_TRADFI_COMMODITY: int(auto_exec.max_leverage_tradfi_commodity),
        ASSET_TRADFI_INDEX: int(auto_exec.max_leverage_tradfi_index),
    }
    global_cap = int(auto_exec.max_leverage)
    return min(global_cap, caps.get(asset_class, global_cap))


def _mtf_aligned_count(indicators: dict[str, str], side: str) -> int:
    target = "bullish" if side == "bull" else "bearish"
    return sum(1 for key in ("mtf_1m", "mtf_5m", "mtf_15m") if indicators.get(key) == target)


def _volatility_penalty_steps(atr_pct: Decimal, auto_exec: AutoExecutionConfig) -> int:
    threshold = auto_exec.leverage_atr_penalty_threshold
    if threshold <= 0 or atr_pct <= threshold:
        return 0
    excess = atr_pct - threshold
    step_size = threshold if threshold > 0 else Decimal("0.01")
    return min(int(excess / step_size) + 1, int(auto_exec.leverage_atr_penalty_max_steps))


def resolve_entry_leverage(
    auto_exec: AutoExecutionConfig | None,
    signal: Signal | None = None,
    indicators: dict[str, str] | None = None,
    symbol: str | None = None,
) -> int:
    if auto_exec is None or not auto_exec.auto_leverage:
        return 1

    asset_class = classify_asset(symbol or "")
    class_ceiling = asset_class_max_leverage(asset_class, auto_exec)
    ceiling = min(int(auto_exec.max_leverage), class_ceiling)
    if int(auto_exec.account_max_leverage) > 0:
        ceiling = min(ceiling, int(auto_exec.account_max_leverage))
    base = min(int(auto_exec.default_leverage), ceiling)

    if signal is None or indicators is None:
        return base

    conf = signal.confidence
    regime = str(indicators.get("regime", "") or "")
    atr_pct = Decimal(str(indicators.get("atr_pct", "0") or "0"))
    action = signal.action

    aligned_bull = _mtf_aligned_count(indicators, "bull")
    aligned_bear = _mtf_aligned_count(indicators, "bear")

    tiers: list[tuple[Decimal, int, int]] = [
        (auto_exec.leverage_extreme_confidence, int(auto_exec.leverage_extreme_mtf_min), int(auto_exec.leverage_extreme)),
        (auto_exec.leverage_high_confidence, 2, int(auto_exec.leverage_high)),
        (auto_exec.leverage_mid_confidence, 2, int(auto_exec.leverage_mid)),
    ]

    chosen = base
    if action == BUY and regime == "trend_up":
        aligned = aligned_bull
        for min_conf, min_mtf, lev in tiers:
            if conf >= min_conf and aligned >= min_mtf:
                chosen = max(chosen, lev)
                break
    elif action == SELL and regime == "trend_down":
        aligned = aligned_bear
        for min_conf, min_mtf, lev in tiers:
            if conf >= min_conf and aligned >= min_mtf:
                chosen = max(chosen, lev)
                break
    elif action == BUY and asset_class != ASSET_CRYPTO and regime in {"trend_up", "range"}:
        if conf >= auto_exec.leverage_mid_confidence and aligned_bull >= 2:
            chosen = max(chosen, int(auto_exec.leverage_mid))
    elif action == SELL and asset_class != ASSET_CRYPTO and regime == "trend_down":
        if conf >= auto_exec.leverage_mid_confidence and aligned_bear >= 2:
            chosen = max(chosen, int(auto_exec.leverage_mid))

    if regime in {"squeeze", "range"} and chosen > int(auto_exec.leverage_mid):
        chosen = int(auto_exec.leverage_mid)

    if asset_class != ASSET_CRYPTO and chosen > class_ceiling:
        chosen = class_ceiling

    penalty = _volatility_penalty_steps(atr_pct, auto_exec)
    if penalty > 0:
        step_down = int(auto_exec.leverage_volatility_step_down)
        chosen = max(base, chosen - penalty * step_down)

    return min(max(int(chosen), 1), ceiling)


def leverage_rationale(
    auto_exec: AutoExecutionConfig | None,
    signal: Signal | None,
    indicators: dict[str, str] | None,
    symbol: str | None,
    resolved: int,
) -> dict[str, Any]:
    asset_class = classify_asset(symbol or "")
    return {
        "leverage": resolved,
        "assetClass": asset_class_label(asset_class),
        "classCeiling": asset_class_max_leverage(asset_class, auto_exec) if auto_exec else None,
        "regime": (indicators or {}).get("regime"),
        "confidence": str(signal.confidence) if signal else None,
        "action": signal.action if signal else None,
    }

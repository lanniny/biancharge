"""Binance universe scan: volume leaders, movers, and dynamic watchlists."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from market_autotrader import (
    AssetConfig,
    CASH_USDT_FUTURES,
    CASH_USDT_SPOT,
    PaperPortfolio,
    decimal_from,
    normalize_symbol,
)

SPOT = "spot"
FUTURES = "futures"
MARKET_SPOT = "binance_spot"
MARKET_FUTURES = "binance_futures"

SPOT_BASE_URLS = ("https://api.binance.com", "https://api.binance.us")
FUTURES_BASE_URL = "https://fapi.binance.com"

STABLECOIN_BASES = frozenset({"USDC", "USDT", "USD1", "FDUSD", "BUSD", "DAI", "TUSD", "USDP", "EUR", "TRY"})
LEVERAGED_MARKERS = ("UP", "DOWN", "BULL", "BEAR")


@dataclass(frozen=True)
class MarketDiscoveryConfig:
    enabled: bool = True
    min_quote_volume_usdt: Decimal = Decimal("5000000")
    spot_top_by_volume: int = 25
    futures_top_by_volume: int = 25
    max_gainers: int = 12
    max_losers: int = 12
    max_analyze_per_cycle: int = 12
    include_holdings: bool = True
    trade_discovered: bool = False
    exclude_stable_pairs: bool = True
    exclude_leveraged_tokens: bool = True
    snapshot_path: str = "logs/market-discovery-latest.json"
    pinned_store_path: str = "config/pinned-symbols.json"
    spot_base_urls: tuple[str, ...] = SPOT_BASE_URLS
    futures_base_url: str = FUTURES_BASE_URL
    timeout_seconds: int = 12
    pinned: tuple[dict[str, str], ...] = ()
    regime_filter_enabled: bool = True
    regime_filter_mode: str = "strict"
    max_discovered_trades_per_cycle: int = 2
    prefer_futures_only: bool = True
    min_discovery_score: Decimal = Decimal("0")
    regime_cache_seconds: int = 300
    kline_limit: int = 500
    include_tradfi: bool = True
    tradfi_min_quote_volume_usdt: Decimal = Decimal("1000000")
    max_tradfi_symbols: int = 8
    tradfi_always_include: tuple[str, ...] = (
        "TSLAUSDT",
        "AAPLUSDT",
        "NVDAUSDT",
        "XAUUSDT",
        "XAGUSDT",
    )


@dataclass(frozen=True)
class TickerRow:
    symbol: str
    market: str
    last_price: Decimal
    price_change_pct: Decimal
    quote_volume: Decimal
    high_price: Decimal
    low_price: Decimal
    trade_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "market": self.market,
            "lastPrice": str(self.last_price),
            "priceChangePercent": str(self.price_change_pct),
            "quoteVolume": str(self.quote_volume),
            "highPrice": str(self.high_price),
            "lowPrice": str(self.low_price),
            "rangePosition24h": str(range_position_24h(self.last_price, self.low_price, self.high_price)),
            "count": self.trade_count,
        }


@dataclass
class WatchlistEntry:
    symbol: str
    market: str
    source: str
    quote_volume: str = "0"
    price_change_pct: str = "0"
    executable: bool = False
    bucket: str = ""
    regime_kind: str = "unknown"
    regime_pass: bool = True
    discovery_score: str = "0"
    block_reason: str = ""
    last_price: str = "0"
    high_price: str = "0"
    low_price: str = "0"
    range_position_24h: str = ""


def discovery_from_config(raw: dict[str, Any] | None) -> MarketDiscoveryConfig:
    raw = raw or {}
    pinned_raw = raw.get("pinned_symbols", raw.get("pinned", []))
    pinned: list[dict[str, str]] = []
    for item in pinned_raw:
        if isinstance(item, str):
            pinned.append({"symbol": normalize_symbol(item), "market": MARKET_FUTURES})
        elif isinstance(item, dict):
            pinned.append(
                {
                    "symbol": normalize_symbol(item["symbol"]),
                    "market": str(item.get("market", MARKET_SPOT)),
                }
            )
    pinned_store_path = str(raw.get("pinned_store_path", "config/pinned-symbols.json"))
    try:
        from pinned_symbols import merge_config_pins

        merged_pins = merge_config_pins(tuple(pinned), pinned_store_path)
    except Exception:
        merged_pins = tuple(pinned)
    return MarketDiscoveryConfig(
        enabled=bool(raw.get("enabled", False)),
        min_quote_volume_usdt=decimal_from(raw.get("min_quote_volume_usdt", "5000000")),
        spot_top_by_volume=int(raw.get("spot_top_by_volume", 25)),
        futures_top_by_volume=int(raw.get("futures_top_by_volume", 25)),
        max_gainers=int(raw.get("max_gainers", 12)),
        max_losers=int(raw.get("max_losers", 12)),
        max_analyze_per_cycle=int(raw.get("max_analyze_per_cycle", 12)),
        include_holdings=bool(raw.get("include_holdings", True)),
        trade_discovered=bool(raw.get("trade_discovered", False)),
        exclude_stable_pairs=bool(raw.get("exclude_stable_pairs", True)),
        exclude_leveraged_tokens=bool(raw.get("exclude_leveraged_tokens", True)),
        snapshot_path=str(raw.get("snapshot_path", "logs/market-discovery-latest.json")),
        pinned_store_path=pinned_store_path,
        spot_base_urls=tuple(raw.get("spot_base_urls", list(SPOT_BASE_URLS))),
        futures_base_url=str(raw.get("futures_base_url", FUTURES_BASE_URL)),
        timeout_seconds=int(raw.get("timeout_seconds", 12)),
        pinned=merged_pins,
        regime_filter_enabled=bool(raw.get("regime_filter_enabled", True)),
        regime_filter_mode=str(raw.get("regime_filter_mode", raw.get("regime_filter", "strict"))).lower(),
        max_discovered_trades_per_cycle=int(raw.get("max_discovered_trades_per_cycle", 2)),
        prefer_futures_only=bool(raw.get("prefer_futures_only", True)),
        min_discovery_score=decimal_from(raw.get("min_discovery_score", "0")),
        regime_cache_seconds=int(raw.get("regime_cache_seconds", 300)),
        kline_limit=int(raw.get("kline_limit", 500)),
        include_tradfi=bool(raw.get("include_tradfi", True)),
        tradfi_min_quote_volume_usdt=decimal_from(raw.get("tradfi_min_quote_volume_usdt", "1000000")),
        max_tradfi_symbols=int(raw.get("max_tradfi_symbols", 8)),
        tradfi_always_include=tuple(
            normalize_symbol(item)
            for item in raw.get(
                "tradfi_always_include",
                ["TSLAUSDT", "AAPLUSDT", "NVDAUSDT", "XAUUSDT", "XAGUSDT"],
            )
        ),
    )


def split_usdt_symbol(symbol: str) -> tuple[str, str]:
    normalized = normalize_symbol(symbol)
    for quote in ("USDT", "USDC", "FDUSD", "BUSD"):
        if normalized.endswith(quote):
            return normalized[: -len(quote)], quote
    return normalized, "USDT"


def is_tradable_ticker(symbol: str, config: MarketDiscoveryConfig) -> bool:
    if not symbol.endswith("USDT"):
        return False
    if "_" in symbol or "-" in symbol:
        return False
    try:
        base, _ = split_usdt_symbol(symbol)
    except ValueError:
        return False
    if config.exclude_stable_pairs and base in STABLECOIN_BASES:
        return False
    if config.exclude_leveraged_tokens:
        upper = base.upper()
        for marker in LEVERAGED_MARKERS:
            if upper.endswith(marker) and len(base) > len(marker) + 2:
                return False
    return True


def fetch_json(url: str, timeout_seconds: int, retries: int = 3) -> Any:
    from proxy_http import urlopen as proxy_urlopen

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "market-discovery/1.0"})
            with proxy_urlopen(request, timeout_seconds=timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except (TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"fetch_json failed for {url}")


def fetch_tickers_24hr(market: str, config: MarketDiscoveryConfig) -> list[TickerRow]:
    if market == SPOT:
        path = "/api/v3/ticker/24hr"
        base_urls = config.spot_base_urls
    else:
        path = "/fapi/v1/ticker/24hr"
        base_urls = (config.futures_base_url,)
    last_error: Exception | None = None
    for base_url in base_urls:
        url = f"{base_url.rstrip('/')}{path}"
        try:
            payload = fetch_json(url, config.timeout_seconds)
            if not isinstance(payload, list):
                raise RuntimeError(f"Unexpected ticker payload from {url}")
            rows: list[TickerRow] = []
            for item in payload:
                raw_symbol = str(item.get("symbol", ""))
                if not is_tradable_ticker(raw_symbol.upper(), config):
                    continue
                try:
                    symbol = normalize_symbol(raw_symbol)
                except ValueError:
                    continue
                quote_volume = decimal_from(item.get("quoteVolume", "0"))
                if quote_volume < config.min_quote_volume_usdt:
                    continue
                rows.append(
                    TickerRow(
                        symbol=symbol,
                        market=market,
                        last_price=decimal_from(item.get("lastPrice", "0")),
                        price_change_pct=decimal_from(item.get("priceChangePercent", "0")),
                        quote_volume=quote_volume,
                        high_price=decimal_from(item.get("highPrice", "0")),
                        low_price=decimal_from(item.get("lowPrice", "0")),
                        trade_count=int(item.get("count", 0) or 0),
                    )
                )
            return rows
        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc
    raise RuntimeError(f"Failed to fetch {market} 24hr tickers: {last_error}")


def rank_by_volume(rows: list[TickerRow], limit: int) -> list[TickerRow]:
    return sorted(rows, key=lambda row: row.quote_volume, reverse=True)[:limit]


def rank_by_change(rows: list[TickerRow], limit: int, *, gainers: bool) -> list[TickerRow]:
    ordered = sorted(rows, key=lambda row: row.price_change_pct, reverse=gainers)
    return ordered[:limit]


def market_kind_to_asset_market(market: str) -> str:
    return MARKET_FUTURES if market == FUTURES else MARKET_SPOT


def default_provider(market: str, config: MarketDiscoveryConfig) -> dict[str, Any]:
    if market == FUTURES:
        return {
            "type": "binance",
            "base_url": config.futures_base_url,
            "path": "/fapi/v1/klines",
            "interval": "1m",
            "limit": config.kline_limit,
            "timeout_seconds": config.timeout_seconds,
        }
    return {
        "type": "binance",
        "base_url": config.spot_base_urls[0],
        "fallback_base_urls": list(config.spot_base_urls[1:]),
        "path": "/api/v3/klines",
        "interval": "1m",
        "limit": config.kline_limit,
        "timeout_seconds": config.timeout_seconds,
    }


def asset_config_for(symbol: str, market: str, config: MarketDiscoveryConfig) -> AssetConfig:
    base, quote = split_usdt_symbol(symbol)
    asset_market = market if market.startswith("binance") else market_kind_to_asset_market(market)
    provider_market = FUTURES if asset_market == MARKET_FUTURES else SPOT
    return AssetConfig(
        symbol=normalize_symbol(symbol),
        market=asset_market,
        base_asset=base,
        quote_asset=quote,
        provider=default_provider(provider_market, config),
    )


LEDGER_CASH_KEYS = frozenset({"USDT", CASH_USDT_SPOT, CASH_USDT_FUTURES})


def holdings_symbols(portfolio: PaperPortfolio | None) -> list[tuple[str, str]]:
    if portfolio is None:
        return []
    result: list[tuple[str, str]] = []
    for symbol, position in portfolio.positions.items():
        if position.quantity != 0:
            result.append((normalize_symbol(symbol), MARKET_FUTURES))
    for asset, amount in portfolio.cash.items():
        asset_key = asset.upper()
        if asset_key in LEDGER_CASH_KEYS or amount <= 0:
            continue
        result.append((f"{asset_key}USDT", MARKET_SPOT))
    return result


def bucket_name_from_source(source: str) -> str:
    if source.startswith("discovery:"):
        return source.split(":", 1)[1]
    return source


def collect_tradfi_rows(rows: list[TickerRow], config: MarketDiscoveryConfig) -> list[TickerRow]:
    from asset_profile import is_tradfi_symbol

    selected: dict[str, TickerRow] = {}
    for row in rows:
        if not is_tradfi_symbol(row.symbol):
            continue
        if row.quote_volume < config.tradfi_min_quote_volume_usdt:
            continue
        selected[row.symbol] = row
    for symbol in config.tradfi_always_include:
        for row in rows:
            if row.symbol == symbol:
                selected[symbol] = row
                break
    ranked = sorted(selected.values(), key=lambda item: item.quote_volume, reverse=True)
    return ranked[: config.max_tradfi_symbols]


def bucket_regime_match(bucket: str, regime_kind: str) -> bool:
    if bucket.endswith("TradFi") or bucket == "futuresTradFi":
        return regime_kind in {"trend_up", "trend_down", "range"}
    if bucket.endswith("Gainers"):
        return regime_kind == "trend_up"
    if bucket.endswith("Losers"):
        return regime_kind == "trend_down"
    if bucket.endswith("TopVolume"):
        return regime_kind in {"trend_up", "trend_down", "range"}
    return True


def range_position_24h(last_price: Decimal, low_price: Decimal, high_price: Decimal) -> Decimal:
    span = high_price - low_price
    if last_price <= 0 or low_price <= 0 or high_price <= 0 or span <= 0:
        return Decimal("0")
    pos = (last_price - low_price) / span
    if pos < 0:
        return Decimal("0")
    if pos > 1:
        return Decimal("1")
    return pos.quantize(Decimal("0.0001"))


def discovery_entry_score(entry: WatchlistEntry) -> Decimal:
    volume = decimal_from(entry.quote_volume or "0")
    change = decimal_from(entry.price_change_pct or "0")
    abs_change = abs(change)
    regime_bonus = Decimal("1.25") if entry.regime_kind in {"trend_up", "trend_down"} else Decimal("1.0")
    bucket = bucket_name_from_source(entry.bucket or entry.source)
    direction_bonus = Decimal("1.0")
    if bucket.endswith("Gainers") and entry.regime_kind == "trend_up" and change > 0:
        direction_bonus = Decimal("1.12")
    elif bucket.endswith("Losers") and entry.regime_kind == "trend_down" and change < 0:
        direction_bonus = Decimal("1.12")
    elif bucket.endswith("TopVolume") and entry.regime_kind in {"trend_up", "trend_down"}:
        direction_bonus = Decimal("1.05")

    range_position_raw = entry.range_position_24h
    range_position = Decimal("0") if range_position_raw in ("", None) else decimal_from(range_position_raw)
    overextension_mult = Decimal("1.0")
    if range_position_raw not in ("", None):
        if bucket.endswith("Gainers") and range_position >= Decimal("0.92"):
            overextension_mult = Decimal("0.72")
        elif bucket.endswith("Gainers") and range_position >= Decimal("0.82"):
            overextension_mult = Decimal("0.88")
        elif bucket.endswith("Losers") and range_position <= Decimal("0.08"):
            overextension_mult = Decimal("0.72")
        elif bucket.endswith("Losers") and range_position <= Decimal("0.18"):
            overextension_mult = Decimal("0.88")
        elif bucket.endswith("Gainers") and Decimal("0.35") <= range_position <= Decimal("0.78"):
            overextension_mult = Decimal("1.08")
        elif bucket.endswith("Losers") and Decimal("0.22") <= range_position <= Decimal("0.65"):
            overextension_mult = Decimal("1.08")

    return (
        volume
        * (Decimal("1") + abs_change / Decimal("100"))
        * regime_bonus
        * direction_bonus
        * overextension_mult
    ).quantize(Decimal("0.01"))


def _discovery_rank_key(entry: WatchlistEntry) -> tuple[int, int, Decimal, Decimal]:
    executable_rank = 0 if entry.executable and entry.regime_pass else 1
    source_rank = 0 if entry.source.startswith("discovery") else 1
    return (
        executable_rank,
        source_rank,
        -decimal_from(entry.discovery_score or "0"),
        -decimal_from(entry.quote_volume or "0"),
    )


def trim_discovery_analysis_entries(entries: list[WatchlistEntry], config: MarketDiscoveryConfig) -> list[WatchlistEntry]:
    if config.max_analyze_per_cycle <= 0:
        return entries
    discovery = [entry for entry in entries if entry.source.startswith("discovery")]
    if len(discovery) <= config.max_analyze_per_cycle:
        return entries
    kept_discovery = set(
        (entry.symbol, entry.market)
        for entry in sorted(discovery, key=_discovery_rank_key)[: config.max_analyze_per_cycle]
    )
    result: list[WatchlistEntry] = []
    for entry in entries:
        if entry.source.startswith("discovery") and (entry.symbol, entry.market) not in kept_discovery:
            continue
        result.append(entry)
    return result


def probe_regime_kind(symbol: str, market: str, config: MarketDiscoveryConfig, strategy_raw: dict[str, Any]) -> str:
    from regime_cache import get_cached_regime, set_cached_regime

    ttl = int(getattr(config, "regime_cache_seconds", 300))
    cached = get_cached_regime(symbol, market, ttl)
    if cached:
        return cached

    from market_autotrader import build_provider, detect_regime, strategy_from_config

    asset = asset_config_for(symbol, market, config)
    try:
        provider = build_provider(asset)
        bars = provider.get_bars(asset)
        if len(bars) < 30:
            return "unknown"
        strategy = strategy_from_config(strategy_raw)
        regime = detect_regime(bars, strategy)
        set_cached_regime(symbol, market, regime.kind)
        return regime.kind
    except Exception:
        return "unknown"


def finalize_watchlist(
    entries: list[WatchlistEntry],
    config: MarketDiscoveryConfig,
    strategy_raw: dict[str, Any] | None = None,
    *,
    max_trade_quote: Decimal | None = None,
    execution_raw: dict[str, Any] | None = None,
    buy_quote_fraction: Decimal | None = None,
    available_futures_usdt: Decimal | None = None,
    default_leverage: int = 1,
    profitability_raw: dict[str, Any] | None = None,
) -> list[WatchlistEntry]:
    strategy_raw = strategy_raw or {}
    execution: Any | None = None
    if max_trade_quote is not None and execution_raw is not None:
        from market_autotrader import execution_from_config, open_quote_meets_min_notional

        execution = execution_from_config(execution_raw)
    finalized: list[WatchlistEntry] = []
    for entry in entries:
        bucket = bucket_name_from_source(entry.source)
        regime_kind = entry.regime_kind
        regime_pass = entry.regime_pass
        if entry.source.startswith("discovery") and config.regime_filter_enabled:
            regime_kind = probe_regime_kind(entry.symbol, entry.market, config, strategy_raw)
            if config.regime_filter_mode == "soft":
                from profitability import profitability_from_config, bucket_regime_match_soft

                profit_cfg = profitability_from_config(profitability_raw or {"enabled": True, "regime_filter_mode": "soft"})
                regime_pass = bucket_regime_match_soft(bucket, regime_kind, profit_cfg)
            else:
                regime_pass = bucket_regime_match(bucket, regime_kind)
        score = discovery_entry_score(
            WatchlistEntry(
                entry.symbol,
                entry.market,
                entry.source,
                entry.quote_volume,
                entry.price_change_pct,
                entry.executable,
                bucket,
                regime_kind,
                regime_pass,
                last_price=entry.last_price,
                high_price=entry.high_price,
                low_price=entry.low_price,
                range_position_24h=entry.range_position_24h,
            )
        )
        executable = entry.executable
        block_reason = entry.block_reason
        if entry.source.startswith("discovery"):
            if not config.trade_discovered:
                executable = False
            elif config.regime_filter_enabled and not regime_pass:
                executable = False
            elif score < config.min_discovery_score:
                executable = False
            elif (
                executable
                and execution is not None
                and max_trade_quote is not None
            ):
                from market_autotrader import effective_open_quote

                open_quote = effective_open_quote(
                    max_trade_quote,
                    buy_quote_fraction or Decimal("1"),
                    available_futures_usdt if entry.market == MARKET_FUTURES else None,
                    leverage=default_leverage,
                )
                try:
                    meets_min_notional = open_quote_meets_min_notional(
                        entry.symbol, entry.market, open_quote, execution
                    )
                except Exception as exc:
                    meets_min_notional = False
                    block_reason = f"min_notional_check_failed:{exc}"
                if not meets_min_notional:
                    executable = False
                    if not block_reason:
                        block_reason = "min_notional_exceeds_max_trade_quote"
        finalized.append(
            WatchlistEntry(
                symbol=entry.symbol,
                market=entry.market,
                source=entry.source,
                quote_volume=entry.quote_volume,
                price_change_pct=entry.price_change_pct,
                executable=executable,
                bucket=bucket,
                regime_kind=regime_kind,
                regime_pass=regime_pass,
                discovery_score=str(score),
                block_reason=block_reason,
                last_price=entry.last_price,
                high_price=entry.high_price,
                low_price=entry.low_price,
                range_position_24h=entry.range_position_24h,
            )
        )

    discovery_tradeable = [
        entry
        for entry in finalized
        if entry.source.startswith("discovery") and entry.executable and entry.regime_pass
    ]
    discovery_tradeable.sort(key=lambda item: decimal_from(item.discovery_score), reverse=True)
    allowed = { (e.symbol, e.market) for e in discovery_tradeable[: config.max_discovered_trades_per_cycle] }

    result: list[WatchlistEntry] = []
    for entry in finalized:
        if entry.source.startswith("discovery") and config.trade_discovered:
            trade_ok = (entry.symbol, entry.market) in allowed
            result.append(
                WatchlistEntry(
                    symbol=entry.symbol,
                    market=entry.market,
                    source=entry.source,
                    quote_volume=entry.quote_volume,
                    price_change_pct=entry.price_change_pct,
                    executable=trade_ok and entry.regime_pass,
                    bucket=entry.bucket,
                    regime_kind=entry.regime_kind,
                    regime_pass=entry.regime_pass,
                    discovery_score=entry.discovery_score,
                    block_reason=entry.block_reason,
                    last_price=entry.last_price,
                    high_price=entry.high_price,
                    low_price=entry.low_price,
                    range_position_24h=entry.range_position_24h,
                )
            )
        else:
            result.append(entry)
    return trim_discovery_analysis_entries(result, config)


def build_watchlist(
    config: MarketDiscoveryConfig,
    scan: dict[str, Any],
    portfolio: PaperPortfolio | None = None,
    static_assets: list[AssetConfig] | None = None,
) -> list[WatchlistEntry]:
    ticker_index: dict[tuple[str, str], TickerRow] = {}
    for bucket in ("spotTopVolume", "futuresTopVolume", "spotGainers", "spotLosers", "futuresGainers", "futuresLosers", "futuresTradFi"):
        for item in scan.get(bucket, []):
            row_market = SPOT if item.get("market") == SPOT else FUTURES
            key = (item["symbol"], row_market)
            if key not in ticker_index:
                ticker_index[key] = TickerRow(
                    symbol=item["symbol"],
                    market=row_market,
                    last_price=decimal_from(item.get("lastPrice", "0")),
                    price_change_pct=decimal_from(item.get("priceChangePercent", "0")),
                    quote_volume=decimal_from(item.get("quoteVolume", "0")),
                    high_price=decimal_from(item.get("highPrice", "0")),
                    low_price=decimal_from(item.get("lowPrice", "0")),
                    trade_count=int(item.get("count", 0) or 0),
                )

    entries: dict[tuple[str, str], WatchlistEntry] = {}

    def add(symbol: str, market: str, source: str, executable: bool, bucket: str = "") -> None:
        asset_market = market if market.startswith("binance") else market_kind_to_asset_market(market)
        provider_market = FUTURES if asset_market == MARKET_FUTURES else SPOT
        key = (normalize_symbol(symbol), provider_market)
        ticker = ticker_index.get(key)
        prior = entries.get(key)
        entries[key] = WatchlistEntry(
            symbol=key[0],
            market=asset_market,
            source=source,
            quote_volume=str(ticker.quote_volume) if ticker else "0",
            price_change_pct=str(ticker.price_change_pct) if ticker else "0",
            executable=(executable if prior is None else (executable or prior.executable)),
            bucket=bucket or bucket_name_from_source(source),
            last_price=str(ticker.last_price) if ticker else "0",
            high_price=str(ticker.high_price) if ticker else "0",
            low_price=str(ticker.low_price) if ticker else "0",
            range_position_24h=str(
                range_position_24h(ticker.last_price, ticker.low_price, ticker.high_price)
            ) if ticker else "",
        )

    for pin in config.pinned:
        add(pin["symbol"], pin["market"], "pinned", True)

    if static_assets:
        for asset in static_assets:
            add(asset.symbol, asset.market, "configured", True)

    if config.include_holdings and portfolio:
        for symbol, market in holdings_symbols(portfolio):
            add(symbol, market, "holding", True)

    discovery_buckets = ["futuresGainers", "futuresLosers", "futuresTopVolume"]
    if config.include_tradfi:
        discovery_buckets.append("futuresTradFi")
    if not config.prefer_futures_only:
        discovery_buckets.extend(["spotGainers", "spotLosers", "spotTopVolume"])

    discovery_candidate_cap = max(config.max_analyze_per_cycle, 1) * 2
    discovery_slots = 0
    max_bucket_len = max((len(scan.get(bucket, [])) for bucket in discovery_buckets), default=0)
    for index in range(max_bucket_len):
        if discovery_slots >= discovery_candidate_cap:
            break
        for bucket in discovery_buckets:
            if discovery_slots >= discovery_candidate_cap:
                break
            bucket_items = scan.get(bucket, [])
            if index >= len(bucket_items):
                continue
            item = bucket_items[index]
            market = item.get("market", SPOT)
            key = (item["symbol"], SPOT if market == SPOT else FUTURES)
            if key in entries:
                continue
            add(item["symbol"], market, f"discovery:{bucket}", config.trade_discovered, bucket=bucket)
            discovery_slots += 1

    ordered = sorted(
        entries.values(),
        key=lambda entry: (
            0 if entry.source == "pinned" else 1 if entry.source == "holding" else 2 if entry.source == "configured" else 3,
            -decimal_from(entry.quote_volume or "0"),
        ),
    )
    cap = config.max_analyze_per_cycle + sum(1 for entry in ordered if entry.executable)
    return ordered[: max(cap, config.max_analyze_per_cycle)]


def run_market_scan(config: MarketDiscoveryConfig) -> dict[str, Any]:
    spot_rows = fetch_tickers_24hr(SPOT, config)
    futures_rows = fetch_tickers_24hr(FUTURES, config)
    report = {
        "scannedAt": int(time.time()),
        "scannedAtIso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "filters": {
            "minQuoteVolumeUSDT": str(config.min_quote_volume_usdt),
            "spotUniverse": len(spot_rows),
            "futuresUniverse": len(futures_rows),
        },
        "spotTopVolume": [row.to_dict() for row in rank_by_volume(spot_rows, config.spot_top_by_volume)],
        "futuresTopVolume": [row.to_dict() for row in rank_by_volume(futures_rows, config.futures_top_by_volume)],
        "spotGainers": [row.to_dict() for row in rank_by_change(spot_rows, config.max_gainers, gainers=True)],
        "spotLosers": [row.to_dict() for row in rank_by_change(spot_rows, config.max_losers, gainers=False)],
        "futuresGainers": [row.to_dict() for row in rank_by_change(futures_rows, config.max_gainers, gainers=True)],
        "futuresLosers": [row.to_dict() for row in rank_by_change(futures_rows, config.max_losers, gainers=False)],
    }
    if config.include_tradfi:
        report["futuresTradFi"] = [row.to_dict() for row in collect_tradfi_rows(futures_rows, config)]
    return report


def save_discovery_snapshot(path: str, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_discovery_snapshot(path: str) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def portfolio_from_supervisor_snapshot(snapshot: dict[str, Any]) -> PaperPortfolio:
    cash: dict[str, Decimal] = {}
    positions: dict[str, Any] = {}
    futures = snapshot.get("futuresAccount") or {}
    if futures.get("availableBalance") is not None:
        cash["USDT"] = decimal_from(futures.get("availableBalance", "0"))
    for balance in snapshot.get("spotNonZeroBalances") or []:
        asset = str(balance.get("asset", "")).upper()
        total = decimal_from(balance.get("total") or balance.get("free") or "0")
        if asset:
            cash[asset] = total
    for position in snapshot.get("futuresNonZeroPositions") or []:
        symbol = normalize_symbol(str(position.get("symbol", "")))
        quantity = decimal_from(position.get("positionAmt", "0"))
        if quantity:
            positions[symbol] = {"quantity": str(quantity), "average_price": position.get("entryPrice", "0")}
    return PaperPortfolio.from_config({"cash": {k: str(v) for k, v in cash.items()}, "positions": positions})


def supervisor_static_assets(futures_symbols: list[str], spot_symbols: list[str], config: MarketDiscoveryConfig) -> list[AssetConfig]:
    assets: list[AssetConfig] = []
    for symbol in futures_symbols:
        assets.append(asset_config_for(symbol, MARKET_FUTURES, config))
    for symbol in spot_symbols:
        assets.append(asset_config_for(symbol, MARKET_SPOT, config))
    return assets


def resolve_supervisor_universe(
    market_discovery_raw: dict[str, Any],
    snapshot: dict[str, Any],
    futures_symbols: list[str],
    spot_symbols: list[str],
) -> tuple[list[str], list[str], dict[str, Any]]:
    discovery_cfg = discovery_from_config(market_discovery_raw)
    if not discovery_cfg.enabled:
        return futures_symbols, spot_symbols, {"enabled": False}

    portfolio = portfolio_from_supervisor_snapshot(snapshot)
    static_assets = supervisor_static_assets(futures_symbols, spot_symbols, discovery_cfg)
    try:
        scan = run_market_scan(discovery_cfg)
    except Exception as exc:
        cached = load_discovery_snapshot(discovery_cfg.snapshot_path)
        if cached:
            cached["scanError"] = str(exc)
            scan = cached
        else:
            return futures_symbols, spot_symbols, {"enabled": True, "error": str(exc)}

    watchlist = build_watchlist(discovery_cfg, scan, portfolio=portfolio, static_assets=static_assets)
    watchlist = finalize_watchlist(watchlist, discovery_cfg, config.get("strategy", {}))
    scan["watchlist"] = [asdict(entry) for entry in watchlist]
    scan["enabled"] = True
    save_discovery_snapshot(discovery_cfg.snapshot_path, scan)

    futures_out: list[str] = []
    spot_out: list[str] = []
    seen_f: set[str] = set()
    seen_s: set[str] = set()
    for entry in watchlist:
        if entry.market == MARKET_FUTURES:
            if entry.symbol not in seen_f:
                seen_f.add(entry.symbol)
                futures_out.append(entry.symbol)
        else:
            if entry.symbol not in seen_s:
                seen_s.add(entry.symbol)
                spot_out.append(entry.symbol)
    if not futures_out:
        futures_out = list(futures_symbols)
    if not spot_out:
        spot_out = list(spot_symbols)
    return futures_out, spot_out, scan


def resolve_trading_universe(
    config: dict[str, Any],
    portfolio: PaperPortfolio | None,
    static_assets: list[AssetConfig],
) -> tuple[list[AssetConfig], dict[str, Any]]:
    discovery_cfg = discovery_from_config(config.get("market_discovery", {}))
    if not discovery_cfg.enabled:
        return static_assets, {"enabled": False}

    scan = run_market_scan(discovery_cfg)
    watchlist = build_watchlist(discovery_cfg, scan, portfolio=portfolio, static_assets=static_assets)
    risk_raw = config.get("risk", {})
    strategy_raw = config.get("strategy", {})
    max_trade_quote = (
        decimal_from(risk_raw["max_trade_quote"])
        if risk_raw.get("max_trade_quote") is not None
        else None
    )
    buy_quote_fraction = decimal_from(strategy_raw.get("buy_quote_fraction", "1"))
    auto_raw = config.get("auto_execution", {})
    default_leverage = min(
        int(auto_raw.get("default_leverage", 5)),
        int(auto_raw.get("max_leverage", 10)),
    )
    available_futures_usdt = None
    if portfolio is not None:
        available_futures_usdt = portfolio.available_cash("USDT", MARKET_FUTURES)
    watchlist = finalize_watchlist(
        watchlist,
        discovery_cfg,
        strategy_raw,
        max_trade_quote=max_trade_quote,
        execution_raw=config.get("execution", {}),
        buy_quote_fraction=buy_quote_fraction,
        available_futures_usdt=available_futures_usdt,
        default_leverage=default_leverage,
        profitability_raw=config.get("profitability"),
    )
    scan["watchlist"] = [asdict(entry) for entry in watchlist]
    scan["enabled"] = True
    save_discovery_snapshot(discovery_cfg.snapshot_path, scan)

    assets: list[AssetConfig] = []
    seen: set[tuple[str, str]] = set()
    for entry in watchlist:
        key = (entry.symbol, entry.market)
        if key in seen:
            continue
        seen.add(key)
        assets.append(asset_config_for(entry.symbol, entry.market, discovery_cfg))

    if not assets and static_assets:
        return static_assets, scan
    return assets, scan


def executable_symbol(entry: WatchlistEntry) -> bool:
    return entry.executable


def discovery_recommendations(scan: dict[str, Any], limit: int = 6) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    for bucket, label in (
        ("futuresGainers", "FUTURES_MOMENTUM_UP"),
        ("futuresLosers", "FUTURES_MOMENTUM_DOWN"),
        ("spotGainers", "SPOT_MOMENTUM_UP"),
        ("spotLosers", "SPOT_MOMENTUM_DOWN"),
    ):
        for item in scan.get(bucket, [])[:3]:
            recommendations.append(
                {
                    "action": label,
                    "symbol": item["symbol"],
                    "market": item["market"],
                    "priority": "medium",
                    "reason": (
                        f"{item['symbol']} {item['market']} moved {item['priceChangePercent']}% in 24h; "
                        f"quote volume {item['quoteVolume']} USDT."
                    ),
                    "details": {"bucket": bucket, "ticker": item},
                }
            )
            if len(recommendations) >= limit:
                return recommendations
    return recommendations

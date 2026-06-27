from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import statistics
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Protocol


BUY = "BUY"
SELL = "SELL"
HOLD = "HOLD"
BLOCKED = "BLOCKED"
PAPER = "paper"
LIVE = "live"
APPROVAL_REQUIRED = "approval_required"
BINANCE_ORDER_TEST = "binance_order_test"
FUTURES = "futures"
SPOT = "spot"
BINANCE_FUTURES_BASE = "https://fapi.binance.com"
DOTENV_PATH = ".env"

ORDER_MARKET = "MARKET"
ORDER_LIMIT = "LIMIT"
ORDER_STOP_MARKET = "STOP_MARKET"
ORDER_TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"

INTENT_OPEN_LONG = "open_long"
INTENT_OPEN_SHORT = "open_short"
INTENT_REDUCE_LONG = "reduce_long"
INTENT_REDUCE_SHORT = "reduce_short"
INTENT_CLOSE_LONG = "close_long"
INTENT_CLOSE_SHORT = "close_short"
INTENT_BUY_SPOT = "buy_spot"
INTENT_SELL_SPOT = "sell_spot"

CASH_USDT_SPOT = "USDT_SPOT"
CASH_USDT_FUTURES = "USDT_FUTURES"


def decimal_from(value: Any) -> Decimal:
    return Decimal(str(value))


def read_dotenv_value(name: str, *, path: str | None = None) -> str:
    path = path or DOTENV_PATH
    try:
        with open(path, "r", encoding="utf-8") as file:
            lines = file.readlines()
    except OSError:
        return ""
    prefix = f"{name}="
    export_prefix = f"export {name}="
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(export_prefix):
            value = line[len(export_prefix) :]
        elif line.startswith(prefix):
            value = line[len(prefix) :]
        else:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value
    return ""


def env_value(name: str) -> str:
    return os.environ.get(name, "").strip() or read_dotenv_value(name).strip()


def normalize_symbol(symbol: str) -> str:
    value = symbol.strip().upper().replace("/", "").replace("-", "")
    normalized_for_check = value.replace(".", "")
    if not value or not normalized_for_check.isascii() or not normalized_for_check.isalnum():
        raise ValueError(f"Invalid symbol: {symbol!r}")
    return value


@dataclass(frozen=True)
class MarketBar:
    timestamp: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class AssetConfig:
    symbol: str
    market: str
    base_asset: str
    quote_asset: str
    provider: dict[str, Any]


def utc_today_key() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def atomic_write_json(path: "str | Path", payload: Any) -> None:
    """Write JSON atomically: serialize to a temp file in the same dir, then
    os.replace() it into place. A crash mid-write can no longer truncate or
    corrupt a live state file (CRASH-02) — readers always see either the old
    complete file or the new complete file, never a half-written one.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp-{os.getpid()}")
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


@dataclass(frozen=True)
class StrategyConfig:
    fast_window: int = 8
    slow_window: int = 21
    momentum_window: int = 6
    rsi_window: int = 14
    atr_window: int = 14
    adx_window: int = 14
    bb_window: int = 20
    bb_std: Decimal = Decimal("2")
    bb_squeeze_width: Decimal = Decimal("0.04")
    adx_range_max: Decimal = Decimal("22")
    mtf_resample_5m: int = 5
    mtf_resample_15m: int = 15
    min_buy_confidence: Decimal = Decimal("0.68")
    min_sell_confidence: Decimal = Decimal("0.62")
    holding_reduce_loss_pct: Decimal = Decimal("0.02")
    holding_take_profit_pct: Decimal = Decimal("0.04")
    holding_trailing_activate_pct: Decimal = Decimal("0.025")
    holding_peak_giveback_pct: Decimal = Decimal("0.025")
    holding_early_take_pct: Decimal = Decimal("0.03")
    holding_early_take_fraction: Decimal = Decimal("0.30")
    holding_trend_up_giveback_mult: Decimal = Decimal("1.8")
    holding_adverse_momentum_cut_mult: Decimal = Decimal("0.75")
    allow_discovery_shorts: bool = False
    discovery_short_mode: str = "off"
    entry_min_edge_fee_multiple: Decimal = Decimal("3")
    allow_pyramid_adds: bool = False
    max_adds_per_symbol_per_day: int = 1
    pump_guard_min_change_24h_pct: Decimal = Decimal("0.30")
    pump_guard_block_new_open_pct: Decimal = Decimal("0.50")
    protection_breakeven_activate_pct: Decimal = Decimal("0.03")
    protection_breakeven_buffer_pct: Decimal = Decimal("0.001")
    buy_quote_fraction: Decimal = Decimal("0.10")
    sell_position_fraction: Decimal = Decimal("1.0")
    min_fusion_bull_pct: Decimal = Decimal("0.52")
    primary_signal_bars: int = 0
    regime_adaptive_signal: bool = False
    short_in_downtrend_boost: bool = False
    confidence_scale_sizing: bool = False
    rsrs_enabled: bool = True
    rsrs_window: int = 18
    rsrs_min_buy: Decimal = Decimal("0.6")
    trade_horizon: str = "scalp"
    swing_take_profit_pct: Decimal = Decimal("0.08")
    swing_stop_loss_pct: Decimal = Decimal("0.035")
    swing_sell_position_fraction: Decimal = Decimal("0.25")
    holding_max_hours: int = 0
    holding_require_5m_exit_confirm: bool = True


@dataclass(frozen=True)
class ProfitAdaptiveCapConfig:
    """Profit-adaptive daily trade cap. When today's net realized PnL (UTC) is
    strictly positive, the daily trade cap is raised by
    floor(net / step_usdt) * extra_trades_per_step, clamped so the EFFECTIVE total
    cap never exceeds hard_ceiling. Net-loss or flat days are unchanged. This NEVER
    reads or weakens the daily-loss circuit breaker (max_daily_loss_quote)."""

    enabled: bool = False
    step_usdt: Decimal = Decimal("0.5")
    extra_trades_per_step: int = 5
    hard_ceiling: int = 60


@dataclass(frozen=True)
class RiskConfig:
    mode: str = PAPER
    allow_live_trading: bool = False
    max_trade_quote: Decimal = Decimal("250")
    max_position_quote: Decimal = Decimal("1000")
    min_confidence: Decimal = Decimal("0.60")
    max_volatility: Decimal = Decimal("0.08")
    max_drawdown: Decimal = Decimal("0.12")
    max_daily_trades: int = 0
    max_daily_loss_quote: Decimal = Decimal("50")
    cooldown_seconds: int = 300
    require_reason_count: int = 3
    kill_switch_path: str = "logs/live-trading.kill"
    live_arm_path: str = "logs/live-trading.armed"
    reserve_futures_available_usdt: Decimal = Decimal("30")
    allow_futures_open: bool = True
    allowed_live_markets: tuple[str, ...] = (SPOT, FUTURES)
    risk_per_trade_pct: Decimal = Decimal("0")
    scale_sizing_with_equity: bool = False
    min_trade_quote: Decimal = Decimal("0")
    max_portfolio_heat_pct: Decimal = Decimal("1")
    target_equity_quote: Decimal = Decimal("300000")
    max_daily_loss_pct: Decimal = Decimal("0")
    min_daily_loss_cap_quote: Decimal = Decimal("0")
    daily_drag_scope: str = "loss_and_costs"
    daily_drag_blocks: str = "new_opens_only"
    max_concurrent_positions: int = 0
    min_margin_per_trade: Decimal = Decimal("0")
    max_position_loss_pct: Decimal = Decimal("0")
    profit_adaptive_daily_cap: ProfitAdaptiveCapConfig = field(default_factory=ProfitAdaptiveCapConfig)


@dataclass(frozen=True)
class ExecutionConfig:
    mode: str = PAPER
    approval_dir: str = "approvals"
    binance_base_url: str = "https://api.binance.com"
    api_key: str = ""
    api_secret: str = ""
    recv_window: int = 5000
    order_type: str = "MARKET"
    timeout_seconds: int = 10
    binance_futures_base_url: str = BINANCE_FUTURES_BASE
    allowed_live_markets: tuple[str, ...] = (SPOT, FUTURES)
    state_path: str = "logs/live-trading-state.json"


@dataclass(frozen=True)
class AutoExecutionConfig:
    entry_order_type: str = ORDER_LIMIT
    limit_offset_pct: Decimal = Decimal("0.001")
    time_in_force: str = "GTC"
    auto_leverage: bool = True
    default_leverage: int = 5
    max_leverage: int = 10
    leverage_mid: int = 6
    leverage_high: int = 8
    leverage_extreme: int = 50
    leverage_mid_confidence: Decimal = Decimal("0.75")
    leverage_high_confidence: Decimal = Decimal("0.85")
    leverage_extreme_confidence: Decimal = Decimal("0.92")
    leverage_extreme_mtf_min: int = 3
    max_leverage_crypto: int = 50
    max_leverage_tradfi_stock: int = 20
    max_leverage_tradfi_commodity: int = 25
    max_leverage_tradfi_index: int = 15
    leverage_atr_penalty_threshold: Decimal = Decimal("0.06")
    leverage_atr_penalty_max_steps: int = 3
    leverage_volatility_step_down: int = 5
    auto_take_profit_stop_loss: bool = True
    take_profit_pct: Decimal = Decimal("0.02")
    stop_loss_pct: Decimal = Decimal("0.015")
    use_atr_stops: bool = False
    atr_stop_multiplier: Decimal = Decimal("2")
    atr_tp_multiplier: Decimal = Decimal("3")
    working_type: str = "MARK_PRICE"
    full_close_threshold: Decimal = Decimal("0.99")
    account_max_leverage: int = 0
    # BIN-008: reject a MARKET entry whose live mark price has moved more than this
    # many basis points from the price the decision was sized on (0 = disabled).
    # Exits are never slippage-blocked — getting out is always allowed.
    max_slippage_bps: Decimal = Decimal("0")


@dataclass
class Position:
    quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")
    initial_margin: Decimal = Decimal("0")
    notional: Decimal = Decimal("0")
    leverage: int = 0


@dataclass
class PaperPortfolio:
    cash: dict[str, Decimal]
    positions: dict[str, Position] = field(default_factory=dict)
    wallet_balance: Decimal | None = None

    @classmethod
    def from_config(cls, payload: dict[str, Any]) -> "PaperPortfolio":
        cash = {asset.upper(): decimal_from(amount) for asset, amount in payload.get("cash", {}).items()}
        positions: dict[str, Position] = {}
        for symbol, raw_position in payload.get("positions", {}).items():
            positions[normalize_symbol(symbol)] = Position(
                quantity=decimal_from(raw_position.get("quantity", "0")),
                average_price=decimal_from(raw_position.get("average_price", "0")),
            )
        return cls(cash=cash, positions=positions)

    def available_cash(self, asset: str, market: str | None = None) -> Decimal:
        asset = asset.upper()
        if market is not None:
            key = quote_cash_key(asset, market)
            if key in self.cash:
                return self.cash[key]
        return self.cash.get(asset, Decimal("0"))

    def position(self, symbol: str) -> Position:
        return self.positions.setdefault(normalize_symbol(symbol), Position())

    def position_value(self, symbol: str, price: Decimal) -> Decimal:
        return (abs(self.position(symbol).quantity) * price).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)

    def buy(self, asset: AssetConfig, price: Decimal, quote_amount: Decimal) -> Decimal:
        quote_asset = asset.quote_asset.upper()
        symbol = normalize_symbol(asset.symbol)
        cash_key = quote_cash_key(quote_asset, asset.market)
        spend = min(self.cash.get(cash_key, self.available_cash(quote_asset, asset.market)), quote_amount)
        if spend <= 0 or price <= 0:
            return Decimal("0")
        quantity = (spend / price).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        if quantity <= 0:
            return Decimal("0")
        actual_spend = (quantity * price).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        position = self.position(symbol)
        current_cost = position.quantity * position.average_price
        new_quantity = position.quantity + quantity
        position.average_price = ((current_cost + actual_spend) / new_quantity).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        position.quantity = new_quantity
        self.cash[cash_key] = self.cash.get(cash_key, Decimal("0")) - actual_spend
        return quantity

    def sell(self, asset: AssetConfig, price: Decimal, fraction: Decimal) -> Decimal:
        symbol = normalize_symbol(asset.symbol)
        quote_asset = asset.quote_asset.upper()
        cash_key = quote_cash_key(quote_asset, asset.market)
        position = self.position(symbol)
        fraction = min(max(fraction, Decimal("0")), Decimal("1"))
        quantity = (position.quantity * fraction).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        if quantity <= 0 or price <= 0:
            return Decimal("0")
        proceeds = (quantity * price).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        position.quantity -= quantity
        if position.quantity <= 0:
            position.average_price = Decimal("0")
        self.cash[cash_key] = self.cash.get(cash_key, Decimal("0")) + proceeds
        return quantity

    def snapshot(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "cash": {asset: str(amount.normalize()) for asset, amount in sorted(self.cash.items())},
            "positions": {
                symbol: {
                    "quantity": str(pos.quantity.normalize()),
                    "average_price": str(pos.average_price.normalize()),
                    **(
                        {"initial_margin": str(pos.initial_margin.normalize()), "notional": str(pos.notional.normalize()), "leverage": pos.leverage}
                        if pos.initial_margin > 0 or pos.notional > 0 or pos.leverage > 0
                        else {}
                    ),
                }
                for symbol, pos in sorted(self.positions.items())
                if pos.quantity != 0
            },
        }
        if self.wallet_balance is not None:
            payload["walletBalance"] = str(self.wallet_balance.normalize())
        return payload


@dataclass(frozen=True)
class MarketSnapshot:
    asset: AssetConfig
    bars: list[MarketBar]
    observed_at: int

    @property
    def price(self) -> Decimal:
        if not self.bars:
            raise ValueError("Snapshot has no bars.")
        return self.bars[-1].close


@dataclass(frozen=True)
class Signal:
    action: str
    confidence: Decimal
    reasons: list[str]
    warnings: list[str]
    indicators: dict[str, str]


@dataclass(frozen=True)
class OrderIntent:
    action: str
    symbol: str
    market: str
    quote_asset: str
    quote_amount: Decimal
    estimated_price: Decimal
    reduce_only: bool = False
    quantity: Decimal | None = None
    order_type: str = ORDER_MARKET
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    leverage: int | None = None
    intent_kind: str = "entry"
    time_in_force: str = "GTC"


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    action: str
    reasons: list[str]
    blocked_reasons: list[str]
    order: OrderIntent | None
    effective_risk: RiskConfig | None = None


@dataclass(frozen=True)
class ExecutionRecord:
    status: str
    mode: str
    symbol: str
    action: str
    quantity: Decimal
    quote_amount: Decimal
    price: Decimal
    reasons: list[str]
    blocked_reasons: list[str]
    indicators: dict[str, str]
    portfolio: dict[str, Any]
    timestamp: int
    execution_details: dict[str, Any] = field(default_factory=dict)
    trade_rationale: dict[str, Any] = field(default_factory=dict)


class MarketDataProvider(Protocol):
    def get_bars(self, asset: AssetConfig) -> list[MarketBar]:
        ...


class StaticBarsProvider:
    def get_bars(self, asset: AssetConfig) -> list[MarketBar]:
        raw_bars = asset.provider.get("bars", [])
        return [
            MarketBar(
                timestamp=int(item.get("timestamp", index)),
                open=decimal_from(item["open"]),
                high=decimal_from(item["high"]),
                low=decimal_from(item["low"]),
                close=decimal_from(item["close"]),
                volume=decimal_from(item.get("volume", "0")),
            )
            for index, item in enumerate(raw_bars)
        ]


class SyntheticBarsProvider:
    def get_bars(self, asset: AssetConfig) -> list[MarketBar]:
        count = int(asset.provider.get("count", 80))
        start = decimal_from(asset.provider.get("start", "100"))
        step = decimal_from(asset.provider.get("step", "1"))
        volume = decimal_from(asset.provider.get("volume", "1000"))
        volume_step = decimal_from(asset.provider.get("volume_step", "10"))
        trend = str(asset.provider.get("trend", "up")).lower()
        direction = Decimal("-1") if trend == "down" else Decimal("0") if trend == "flat" else Decimal("1")
        pullback_every = int(asset.provider.get("pullback_every", 0))
        pullback_step = decimal_from(asset.provider.get("pullback_step", "0"))
        bars: list[MarketBar] = []
        price = start
        for index in range(count):
            change = step * direction
            if pullback_every > 0 and index > 0 and index % pullback_every == 0:
                change -= pullback_step * direction
            price = max(Decimal("0.00000001"), price + change)
            current_volume = max(Decimal("0"), volume + (volume_step * Decimal(index)))
            bars.append(
                MarketBar(
                    timestamp=index,
                    open=price - (step / Decimal("2")),
                    high=price + step,
                    low=max(Decimal("0.00000001"), price - step),
                    close=price,
                    volume=current_volume,
                )
            )
        return bars


class BinanceKlineProvider:
    def get_bars(self, asset: AssetConfig) -> list[MarketBar]:
        primary = asset.provider.get("base_url", "https://api.binance.com").rstrip("/")
        fallbacks = asset.provider.get("fallback_base_urls", ["https://api.binance.us"])
        base_urls = [primary] + [url.rstrip("/") for url in fallbacks if url.rstrip("/") != primary]
        path = asset.provider.get("path", "/api/v3/klines")
        params = {
            "symbol": normalize_symbol(asset.symbol),
            "interval": asset.provider.get("interval", "1m"),
            "limit": int(asset.provider.get("limit", 120)),
        }
        timeout = int(asset.provider.get("timeout_seconds", 10))
        query = urllib.parse.urlencode(params)
        last_error: Exception | None = None
        for base_url in base_urls:
            url = f"{base_url}{path}?{query}"
            try:
                from proxy_http import urlopen as proxy_urlopen

                request = urllib.request.Request(url, headers={"User-Agent": "market-autotrader/1.0"})
                with proxy_urlopen(request, timeout_seconds=timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                return [
                    MarketBar(
                        timestamp=int(item[0]),
                        open=decimal_from(item[1]),
                        high=decimal_from(item[2]),
                        low=decimal_from(item[3]),
                        close=decimal_from(item[4]),
                        volume=decimal_from(item[5]),
                    )
                    for item in payload
                ]
            except urllib.error.HTTPError as exc:
                last_error = RuntimeError(f"Binance public market data failed with HTTP {exc.code}")
            except urllib.error.URLError as exc:
                last_error = RuntimeError(f"Binance public market data failed: {exc.reason}")
        raise last_error or RuntimeError("Binance public market data failed on all endpoints.")


class AlpacaBarsProvider:
    def get_bars(self, asset: AssetConfig) -> list[MarketBar]:
        headers = {
            "APCA-API-KEY-ID": asset.provider.get("api_key", ""),
            "APCA-API-SECRET-KEY": asset.provider.get("api_secret", ""),
        }
        if not headers["APCA-API-KEY-ID"] or not headers["APCA-API-SECRET-KEY"]:
            raise RuntimeError("Alpaca market data requires api_key and api_secret in local config or environment expansion.")

        base_url = asset.provider.get("data_base_url", "https://data.alpaca.markets").rstrip("/")
        timeframe = asset.provider.get("timeframe", "1Min")
        limit = int(asset.provider.get("limit", 120))
        params = urllib.parse.urlencode({"symbols": normalize_symbol(asset.symbol), "timeframe": timeframe, "limit": limit})
        request = urllib.request.Request(f"{base_url}/v2/stocks/bars?{params}", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=int(asset.provider.get("timeout_seconds", 10))) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Alpaca market data failed with HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Alpaca market data failed: {exc.reason}") from exc

        rows = payload.get("bars", {}).get(normalize_symbol(asset.symbol), [])
        return [
            MarketBar(
                timestamp=index,
                open=decimal_from(item["o"]),
                high=decimal_from(item["h"]),
                low=decimal_from(item["l"]),
                close=decimal_from(item["c"]),
                volume=decimal_from(item.get("v", "0")),
            )
            for index, item in enumerate(rows)
        ]


def build_provider(asset: AssetConfig) -> MarketDataProvider:
    provider_type = str(asset.provider.get("type", "static")).lower()
    if provider_type == "static":
        return StaticBarsProvider()
    if provider_type == "synthetic":
        return SyntheticBarsProvider()
    if provider_type == "binance":
        return BinanceKlineProvider()
    if provider_type == "alpaca":
        return AlpacaBarsProvider()
    raise ValueError(f"Unsupported provider type: {provider_type}")


def simple_moving_average(values: list[Decimal], window: int) -> Decimal:
    if len(values) < window or window <= 0:
        raise ValueError("Not enough values for moving average.")
    return sum(values[-window:]) / Decimal(window)


def pct_change(old: Decimal, new: Decimal) -> Decimal:
    if old == 0:
        return Decimal("0")
    return (new - old) / old


def volatility(closes: list[Decimal], window: int) -> Decimal:
    if len(closes) <= window:
        return Decimal("0")
    returns = [float(pct_change(closes[index - 1], closes[index])) for index in range(len(closes) - window, len(closes))]
    if len(returns) < 2:
        return Decimal("0")
    return decimal_from(statistics.pstdev(returns))


def rsi(closes: list[Decimal], window: int) -> Decimal:
    if len(closes) <= window:
        return Decimal("50")
    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for index in range(len(closes) - window, len(closes)):
        change = closes[index] - closes[index - 1]
        if change >= 0:
            gains.append(change)
            losses.append(Decimal("0"))
        else:
            gains.append(Decimal("0"))
            losses.append(abs(change))
    average_gain = sum(gains) / Decimal(window)
    average_loss = sum(losses) / Decimal(window)
    if average_gain == 0 and average_loss == 0:
        return Decimal("50")
    if average_loss == 0:
        return Decimal("100")
    relative_strength = average_gain / average_loss
    return Decimal("100") - (Decimal("100") / (Decimal("1") + relative_strength))


def max_drawdown(closes: list[Decimal], window: int) -> Decimal:
    if not closes:
        return Decimal("0")
    relevant = closes[-window:] if len(closes) >= window else closes
    peak = relevant[0]
    worst = Decimal("0")
    for close in relevant:
        peak = max(peak, close)
        if peak > 0:
            worst = min(worst, (close - peak) / peak)
    return abs(worst)


def volume_ratio(volumes: list[Decimal], window: int) -> Decimal:
    if len(volumes) < window + 1:
        return Decimal("1")
    baseline = sum(volumes[-window - 1 : -1]) / Decimal(window)
    if baseline == 0:
        return Decimal("1")
    return volumes[-1] / baseline


def ema(values: list[Decimal], window: int) -> Decimal:
    if len(values) < window or window <= 0:
        raise ValueError("Not enough values for EMA.")
    multiplier = Decimal("2") / Decimal(window + 1)
    current = sum(values[:window]) / Decimal(window)
    for value in values[window:]:
        current = (value - current) * multiplier + current
    return current


def true_range(bars: list[MarketBar], index: int) -> Decimal:
    bar = bars[index]
    if index == 0:
        return bar.high - bar.low
    previous_close = bars[index - 1].close
    return max(bar.high - bar.low, abs(bar.high - previous_close), abs(bar.low - previous_close))


def average_true_range(bars: list[MarketBar], window: int) -> Decimal:
    if len(bars) < window + 1:
        return Decimal("0")
    ranges = [true_range(bars, index) for index in range(len(bars) - window, len(bars))]
    return sum(ranges) / Decimal(len(ranges))


def directional_movement(bars: list[MarketBar], window: int) -> tuple[Decimal, Decimal, Decimal]:
    if len(bars) < window + 2:
        return Decimal("0"), Decimal("0"), Decimal("0")

    plus_dm_values: list[Decimal] = []
    minus_dm_values: list[Decimal] = []
    tr_values: list[Decimal] = []
    for index in range(1, len(bars)):
        up_move = bars[index].high - bars[index - 1].high
        down_move = bars[index - 1].low - bars[index].low
        plus_dm = up_move if up_move > down_move and up_move > 0 else Decimal("0")
        minus_dm = down_move if down_move > up_move and down_move > 0 else Decimal("0")
        plus_dm_values.append(plus_dm)
        minus_dm_values.append(minus_dm)
        tr_values.append(true_range(bars, index))

    recent_plus = plus_dm_values[-window:]
    recent_minus = minus_dm_values[-window:]
    recent_tr = tr_values[-window:]
    tr_sum = sum(recent_tr)
    if tr_sum == 0:
        return Decimal("0"), Decimal("0"), Decimal("0")

    plus_di = (Decimal("100") * sum(recent_plus) / tr_sum).quantize(Decimal("0.01"))
    minus_di = (Decimal("100") * sum(recent_minus) / tr_sum).quantize(Decimal("0.01"))
    di_sum = plus_di + minus_di
    if di_sum == 0:
        return plus_di, minus_di, Decimal("0")
    dx = (Decimal("100") * abs(plus_di - minus_di) / di_sum).quantize(Decimal("0.01"))

    dx_values: list[Decimal] = []
    for offset in range(window, len(bars)):
        chunk_plus = plus_dm_values[offset - window : offset]
        chunk_minus = minus_dm_values[offset - window : offset]
        chunk_tr = tr_values[offset - window : offset]
        chunk_tr_sum = sum(chunk_tr)
        if chunk_tr_sum == 0:
            continue
        chunk_plus_di = Decimal("100") * sum(chunk_plus) / chunk_tr_sum
        chunk_minus_di = Decimal("100") * sum(chunk_minus) / chunk_tr_sum
        chunk_di_sum = chunk_plus_di + chunk_minus_di
        if chunk_di_sum == 0:
            continue
        dx_values.append(Decimal("100") * abs(chunk_plus_di - chunk_minus_di) / chunk_di_sum)

    adx = (sum(dx_values[-window:]) / Decimal(min(len(dx_values[-window:]), window))).quantize(Decimal("0.01")) if dx_values else dx
    return plus_di, minus_di, adx


def bollinger_bands(closes: list[Decimal], window: int, num_std: Decimal) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    middle = simple_moving_average(closes, window)
    subset = closes[-window:]
    if len(subset) < 2:
        return middle, middle, middle, Decimal("0")
    std = decimal_from(statistics.pstdev([float(value) for value in subset]))
    upper = middle + (num_std * decimal_from(std))
    lower = middle - (num_std * decimal_from(std))
    width = (upper - lower) / middle if middle > 0 else Decimal("0")
    return upper, middle, lower, width


def resample_bars(bars: list[MarketBar], factor: int) -> list[MarketBar]:
    if factor <= 1:
        return list(bars)
    resampled: list[MarketBar] = []
    for index in range(0, len(bars) - factor + 1, factor):
        chunk = bars[index : index + factor]
        resampled.append(
            MarketBar(
                timestamp=chunk[0].timestamp,
                open=chunk[0].open,
                high=max(item.high for item in chunk),
                low=min(item.low for item in chunk),
                close=chunk[-1].close,
                volume=sum(item.volume for item in chunk),
            )
        )
    return resampled


@dataclass(frozen=True)
class MarketRegime:
    kind: str
    adx: Decimal
    plus_di: Decimal
    minus_di: Decimal
    bb_width: Decimal
    atr: Decimal
    atr_pct: Decimal


def detect_regime(bars: list[MarketBar], strategy: StrategyConfig) -> MarketRegime:
    closes = [bar.close for bar in bars]
    plus_di, minus_di, adx = directional_movement(bars, strategy.adx_window)
    _, _, _, width = bollinger_bands(closes, strategy.bb_window, strategy.bb_std)
    atr = average_true_range(bars, strategy.atr_window)
    price = closes[-1]
    atr_pct = (atr / price) if price > 0 else Decimal("0")

    # Direction comes from the DI sign; ADX only needs to clear range_max to confirm trend.
    if width <= strategy.bb_squeeze_width and adx < strategy.adx_range_max:
        kind = "squeeze"
    elif adx >= strategy.adx_range_max:
        kind = "trend_up" if plus_di >= minus_di else "trend_down"
    else:
        kind = "range"

    return MarketRegime(
        kind=kind,
        adx=adx,
        plus_di=plus_di,
        minus_di=minus_di,
        bb_width=width,
        atr=atr,
        atr_pct=atr_pct,
    )


def timeframe_trend_label(bars: list[MarketBar], strategy: StrategyConfig) -> str:
    min_bars = max(strategy.slow_window, strategy.fast_window) + 1
    if len(bars) < min_bars:
        return "insufficient"
    closes = [bar.close for bar in bars]
    fast = simple_moving_average(closes, strategy.fast_window)
    slow = simple_moving_average(closes, strategy.slow_window)
    if fast > slow * Decimal("1.002"):
        return "bullish"
    if fast < slow * Decimal("0.998"):
        return "bearish"
    return "neutral"


def multi_timeframe_trend(bars: list[MarketBar], strategy: StrategyConfig) -> dict[str, str]:
    return {
        "1m": timeframe_trend_label(bars, strategy),
        "5m": timeframe_trend_label(resample_bars(bars, strategy.mtf_resample_5m), strategy),
        "15m": timeframe_trend_label(resample_bars(bars, strategy.mtf_resample_15m), strategy),
    }


def build_signal(snapshot: MarketSnapshot, strategy: StrategyConfig) -> Signal:
    raw_bars = snapshot.bars
    mtf = multi_timeframe_trend(raw_bars, strategy)
    bars = raw_bars
    if strategy.primary_signal_bars > 1:
        bars = resample_bars(bars, strategy.primary_signal_bars)
    min_bars = max(
        strategy.slow_window,
        strategy.rsi_window,
        strategy.momentum_window,
        strategy.atr_window + 1,
        strategy.adx_window + 2,
        strategy.bb_window,
    ) + 1
    if len(bars) < min_bars:
        return Signal(
            action=HOLD,
            confidence=Decimal("0"),
            reasons=[f"Only {len(bars)} bars available; need at least {min_bars} for a stable decision."],
            warnings=["insufficient_market_history"],
            indicators={},
        )

    closes = [bar.close for bar in bars]
    volumes = [bar.volume for bar in bars]
    price = closes[-1]
    fast_sma = simple_moving_average(closes, strategy.fast_window)
    slow_sma = simple_moving_average(closes, strategy.slow_window)
    momentum = pct_change(closes[-strategy.momentum_window], price)
    current_rsi = rsi(closes, strategy.rsi_window)
    current_volatility = volatility(closes, strategy.slow_window)
    current_drawdown = max_drawdown(closes, strategy.slow_window)
    current_volume_ratio = volume_ratio(volumes, strategy.slow_window)
    regime = detect_regime(bars, strategy)

    reasons: list[str] = []
    warnings: list[str] = []
    score = Decimal("0.35")
    fusion_votes: list[tuple[str, str, Decimal]] = []

    def vote(name: str, side: str, weight: Decimal, delta: Decimal) -> None:
        fusion_votes.append((name, side, weight))
        nonlocal score
        score += delta

    if fast_sma > slow_sma:
        vote("sma", "bull", Decimal("0.20"), Decimal("0.20"))
        reasons.append(f"Fast SMA {fast_sma:.4f} is above slow SMA {slow_sma:.4f}, showing trend alignment.")
    else:
        vote("sma", "bear", Decimal("0.20"), Decimal("-0.18"))
        reasons.append(f"Fast SMA {fast_sma:.4f} is below slow SMA {slow_sma:.4f}, showing weak trend alignment.")

    if momentum > Decimal("0.01"):
        vote("momentum", "bull", Decimal("0.18"), Decimal("0.18"))
        reasons.append(f"Momentum over {strategy.momentum_window} bars is positive at {momentum:.4%}.")
    elif momentum < Decimal("-0.01"):
        vote("momentum", "bear", Decimal("0.18"), Decimal("-0.18"))
        reasons.append(f"Momentum over {strategy.momentum_window} bars is negative at {momentum:.4%}.")
    else:
        vote("momentum", "neutral", Decimal("0.10"), Decimal("0"))
        reasons.append(f"Momentum over {strategy.momentum_window} bars is flat at {momentum:.4%}.")

    if Decimal("45") <= current_rsi <= Decimal("72"):
        vote("rsi", "bull", Decimal("0.12"), Decimal("0.12"))
        reasons.append(f"RSI {current_rsi:.2f} is constructive without being extremely overbought.")
    elif current_rsi > Decimal("80"):
        vote("rsi", "bear", Decimal("0.12"), Decimal("-0.20"))
        warnings.append("overbought_rsi")
        reasons.append(f"RSI {current_rsi:.2f} is overheated.")
    elif current_rsi < Decimal("35"):
        vote("rsi", "bear", Decimal("0.12"), Decimal("-0.08"))
        warnings.append("weak_rsi")
        reasons.append(f"RSI {current_rsi:.2f} shows weak demand.")
    else:
        vote("rsi", "neutral", Decimal("0.08"), Decimal("0"))
        reasons.append(f"RSI {current_rsi:.2f} is neutral.")

    if current_volume_ratio >= Decimal("1.10"):
        vote("volume", "bull", Decimal("0.10"), Decimal("0.10"))
        reasons.append(f"Latest volume is {current_volume_ratio:.2f}x the recent average, confirming participation.")
    elif current_volume_ratio < Decimal("0.70"):
        vote("volume", "bear", Decimal("0.10"), Decimal("-0.08"))
        warnings.append("thin_volume")
        reasons.append(f"Latest volume is only {current_volume_ratio:.2f}x the recent average.")
    else:
        vote("volume", "neutral", Decimal("0.08"), Decimal("0"))
        reasons.append(f"Latest volume is {current_volume_ratio:.2f}x the recent average.")

    if current_volatility > Decimal("0.05"):
        vote("volatility", "bear", Decimal("0.10"), Decimal("-0.10"))
        warnings.append("elevated_volatility")
        reasons.append(f"Recent volatility {current_volatility:.4%} is elevated.")
    else:
        vote("volatility", "bull", Decimal("0.08"), Decimal("0.05"))
        reasons.append(f"Recent volatility {current_volatility:.4%} is controlled.")

    if regime.kind == "trend_up":
        vote("regime", "bull", Decimal("0.12"), Decimal("0.08"))
        reasons.append(f"Market regime is uptrend (ADX={regime.adx:.1f}, +DI>{regime.minus_di:.1f} -DI).")
    elif regime.kind == "trend_down":
        vote("regime", "bear", Decimal("0.12"), Decimal("-0.12"))
        warnings.append("downtrend_regime")
        reasons.append(f"Market regime is downtrend (ADX={regime.adx:.1f}, -DI>{regime.plus_di:.1f} +DI).")
    elif regime.kind == "squeeze":
        vote("regime", "bear", Decimal("0.10"), Decimal("-0.05"))
        warnings.append("squeeze_regime")
        reasons.append(f"Bollinger squeeze detected (width={regime.bb_width:.4%}); wait for directional breakout.")
    else:
        vote("regime", "neutral", Decimal("0.08"), Decimal("0"))
        reasons.append(f"Range-bound market (ADX={regime.adx:.1f}); trend signals are discounted.")

    bullish_count = sum(1 for label in mtf.values() if label == "bullish")
    bearish_count = sum(1 for label in mtf.values() if label == "bearish")
    if bullish_count >= 2:
        vote("mtf", "bull", Decimal("0.12"), Decimal("0.10"))
        reasons.append(f"Multi-timeframe alignment is bullish: 1m={mtf['1m']}, 5m={mtf['5m']}, 15m={mtf['15m']}.")
    elif bearish_count >= 2:
        vote("mtf", "bear", Decimal("0.12"), Decimal("-0.10"))
        reasons.append(f"Multi-timeframe alignment is bearish: 1m={mtf['1m']}, 5m={mtf['5m']}, 15m={mtf['15m']}.")
    else:
        vote("mtf", "neutral", Decimal("0.08"), Decimal("0"))
        reasons.append(f"Multi-timeframe mixed: 1m={mtf['1m']}, 5m={mtf['5m']}, 15m={mtf['15m']}.")

    rsrs_value = Decimal("0")
    if strategy.rsrs_enabled:
        from rsrs import rsrs_score

        rsrs_value = rsrs_score(bars, strategy.rsrs_window)
        if rsrs_value >= strategy.rsrs_min_buy:
            vote("rsrs", "bull", Decimal("0.10"), Decimal("0.06"))
            reasons.append(f"RSRS strength {rsrs_value:.4f} supports trend continuation.")
        elif rsrs_value <= -strategy.rsrs_min_buy:
            vote("rsrs", "bear", Decimal("0.10"), Decimal("-0.06"))
            reasons.append(f"RSRS strength {rsrs_value:.4f} is weak/bearish.")
        else:
            vote("rsrs", "neutral", Decimal("0.08"), Decimal("0"))
            reasons.append(f"RSRS strength {rsrs_value:.4f} is neutral.")

    total_weight = sum(weight for _, _, weight in fusion_votes) or Decimal("1")
    bull_weight = sum(weight for _, side, weight in fusion_votes if side == "bull")
    fusion_bull_pct = (bull_weight / total_weight).quantize(Decimal("0.0001"))
    score = min(max(score, Decimal("0")), Decimal("0.99"))
    if score >= strategy.min_buy_confidence and fusion_bull_pct < strategy.min_fusion_bull_pct:
        warnings.append("weak_fusion")
        reasons.append(
            f"Fusion bull weight {fusion_bull_pct:.2%} below required {strategy.min_fusion_bull_pct:.2%}; BUY downgraded."
        )
        score = min(score, strategy.min_buy_confidence - Decimal("0.01"))
    if strategy.regime_adaptive_signal:
        if regime.kind == "trend_up":
            score = min(score + Decimal("0.05"), Decimal("0.99"))
            reasons.append("Regime-adaptive boost: uptrend favors long bias (+5% score).")
        elif regime.kind == "trend_down":
            score = max(score - Decimal("0.05"), Decimal("0"))
            reasons.append("Regime-adaptive penalty: downtrend reduces long bias (-5% score).")
        elif regime.kind == "squeeze":
            score = max(score - Decimal("0.03"), Decimal("0"))
            reasons.append("Regime-adaptive caution: squeeze compresses conviction (-3% score).")
    # Stage 5 (computed BEFORE the action decision so it can veto counter-trend
    # entries): candlestick pattern + structure read on the native timeframe.
    # Best-effort and side-effect free; failures must never break signal building
    # and must leave kline_label="neutral" so the veto below is a no-op.
    kline_score = Decimal("0")
    kline_label = "neutral"
    kline_patterns_detected: list[str] = []
    try:
        from kline_patterns import detect_patterns

        kp = detect_patterns(raw_bars)
        kline_score = decimal_from(kp.get("patternScore", "0"))
        kline_label = kp.get("patternLabel", "neutral")
        if kp.get("patterns"):
            kline_patterns_detected = [h["name"] for h in kp["patterns"]]
    except Exception:
        kp = {}

    # A bullish reversal candle (e.g. hammer / bullish engulfing at a downtrend
    # bottom) is exactly the signal that should stop us shorting INTO a turn.
    # Used only to VETO a short (downgrade SELL -> HOLD); it never creates a trade,
    # so it cannot increase exposure. Threshold mirrors detect_patterns' bullish
    # cutoff (>=0.4) but we require >=0.5 to act, to avoid vetoing on a lone doji.
    kline_bullish_reversal = kline_label == "bullish" or kline_score >= Decimal("0.5")

    if score >= strategy.min_buy_confidence:
        action = BUY
    elif (score <= Decimal("0.32") and momentum < 0) or momentum < Decimal("-0.025"):
        if kline_bullish_reversal:
            action = HOLD
            reasons.append(
                f"Bullish reversal candle ({kline_label}, score={kline_score}) vetoes strong-momentum short."
            )
        else:
            action = SELL
            score = max(Decimal("1") - score, strategy.min_sell_confidence)
    elif (
        strategy.short_in_downtrend_boost
        and regime.kind == "trend_down"
        and bearish_count >= 2
        and score <= Decimal("0.45")
    ):
        if kline_bullish_reversal:
            action = HOLD
            reasons.append(
                f"Bullish reversal candle ({kline_label}, score={kline_score}) vetoes downtrend+MTF short at a possible bottom."
            )
        else:
            action = SELL
            score = max(Decimal("1") - score, strategy.min_sell_confidence)
            reasons.append("Downtrend + multi-timeframe bearish alignment triggers short/reduce bias.")
    else:
        action = HOLD

    indicators = {
        "price": str(price),
        "fast_sma": str(fast_sma),
        "slow_sma": str(slow_sma),
        "momentum": str(momentum),
        "rsi": str(current_rsi),
        "volatility": str(current_volatility),
        "drawdown": str(current_drawdown),
        "volume_ratio": str(current_volume_ratio),
        "regime": regime.kind,
        "adx": str(regime.adx),
        "plus_di": str(regime.plus_di),
        "minus_di": str(regime.minus_di),
        "bb_width": str(regime.bb_width),
        "atr_pct": str(regime.atr_pct),
        "mtf_1m": mtf["1m"],
        "mtf_5m": mtf["5m"],
        "mtf_15m": mtf["15m"],
        "fusion_bull_pct": str(fusion_bull_pct),
        "fusion_votes": json.dumps({name: side for name, side, _ in fusion_votes}),
        "rsrs": str(rsrs_value),
        "kline_pattern_score": str(kline_score),
        "kline_pattern_label": kline_label,
    }
    if kline_patterns_detected:
        indicators["kline_patterns"] = json.dumps(kline_patterns_detected)
    return Signal(action=action, confidence=score, reasons=reasons, warnings=warnings, indicators=indicators)


class TradingMemory:
    def __init__(self) -> None:
        self.daily_trade_counts: dict[str, int] = {}
        self.last_trade_at: dict[str, int] = {}
        self.position_opened_at: dict[str, int] = {}
        self.position_peak_price: dict[str, str] = {}
        self.position_exit_tiers: dict[str, list[str]] = {}
        self.position_open_context: dict[str, dict[str, Any]] = {}
        self.position_excursion: dict[str, dict[str, str]] = {}
        self.daily_symbol_add_counts: dict[str, int] = {}
        self.last_open_positions: dict[str, dict[str, Any]] = {}
        self.daily_loss_quote: dict[str, Decimal] = {}
        self.daily_funding_fee_quote: dict[str, Decimal] = {}
        self.daily_commission_quote: dict[str, Decimal] = {}
        # Signed net realized PnL per UTC day (gains +ve, losses -ve). Mirrors the
        # REALIZED_PNL income rows that feed daily_loss_quote, but keeps the sign so
        # the profit-adaptive trade cap can ask "is today net-profitable?". Accounting
        # only — it MUST NOT influence the daily-loss circuit breaker.
        self.daily_realized_pnl_net: dict[str, Decimal] = {}

    def count_for_today(self) -> int:
        return self.daily_trade_counts.get(utc_today_key(), 0)

    def daily_loss_today(self) -> Decimal:
        return self.daily_loss_quote.get(utc_today_key(), Decimal("0"))

    def daily_realized_net_today(self) -> Decimal:
        """Signed net realized PnL booked to the CURRENT UTC day (gains +ve)."""
        return self.daily_realized_pnl_net.get(utc_today_key(), Decimal("0"))

    def daily_funding_fee_today(self) -> Decimal:
        return self.daily_funding_fee_quote.get(utc_today_key(), Decimal("0"))

    def daily_commission_today(self) -> Decimal:
        return self.daily_commission_quote.get(utc_today_key(), Decimal("0"))

    def daily_costs_today(self) -> Decimal:
        return self.daily_funding_fee_today() + self.daily_commission_today()

    def total_daily_drag_today(self) -> Decimal:
        return self.daily_loss_today() + self.daily_costs_today()

    def record_trade(
        self, symbol: str, timestamp: int, *, is_open: bool = False, is_add: bool = False
    ) -> None:
        # Day-key MUST match the UTC key used by every read accessor
        # (count_for_today / daily_*_today). Using local date here previously
        # desynced caps for the 8h window where local and UTC dates differ.
        today = utc_today_key()
        self.daily_trade_counts[today] = self.daily_trade_counts.get(today, 0) + 1
        self.last_trade_at[normalize_symbol(symbol)] = timestamp
        # An "add" is a follow-on entry on an existing position. The per-symbol
        # add cap (max_adds_per_symbol_per_day) reads daily_symbol_add_counts but
        # nothing incremented it before, so the cap was inert.
        if is_add:
            # Key format MUST match the read side in apply_risk_controls:
            # f"{symbol}:{utc_today_key()}" with a normalized symbol.
            add_key = f"{normalize_symbol(symbol)}:{today}"
            self.daily_symbol_add_counts[add_key] = self.daily_symbol_add_counts.get(add_key, 0) + 1
        elif is_open:
            self.position_opened_at[normalize_symbol(symbol)] = timestamp

    def record_loss(self, amount: Decimal) -> None:
        if amount <= 0:
            return
        today = utc_today_key()
        self.daily_loss_quote[today] = self.daily_loss_quote.get(today, Decimal("0")) + amount

    def record_realized_pnl(self, amount: Decimal, day_key: str | None = None) -> None:
        """Record exchange-authoritative realized PnL into the daily-loss cap.

        Only the loss portion (negative PnL) feeds daily_loss_quote, which is what
        the daily-loss circuit breaker reads. This is the single source of truth for
        realized losses: it comes from /fapi/v1/income REALIZED_PNL rows (deduped by
        tranId), so it must NOT be combined with the estimate-based record_loss on the
        live close path or losses would be double-counted.

        The full signed amount is ALSO mirrored into daily_realized_pnl_net (gains and
        losses) under the SAME day_key, so the profit-adaptive trade cap can read
        today's net. A deduped row dated to a prior UTC day lands on that day, never
        today. This mirror is accounting-only and never affects the loss breaker.
        """
        today = day_key or utc_today_key()
        self.daily_realized_pnl_net[today] = (
            self.daily_realized_pnl_net.get(today, Decimal("0")) + amount
        )
        if amount >= 0:
            return
        self.daily_loss_quote[today] = self.daily_loss_quote.get(today, Decimal("0")) + (-amount)

    def record_income(self, income_type: str, amount: Decimal, day_key: str | None = None) -> None:
        if amount >= 0:
            return
        today = day_key or utc_today_key()
        cost = -amount
        if income_type == "FUNDING_FEE":
            self.daily_funding_fee_quote[today] = self.daily_funding_fee_quote.get(today, Decimal("0")) + cost
        elif income_type == "COMMISSION":
            self.daily_commission_quote[today] = self.daily_commission_quote.get(today, Decimal("0")) + cost

    def record_commission(self, amount: Decimal) -> None:
        if amount <= 0:
            return
        today = utc_today_key()
        self.daily_commission_quote[today] = self.daily_commission_quote.get(today, Decimal("0")) + amount

    @classmethod
    def load(cls, path: str | None) -> "TradingMemory":
        memory = cls()
        if not path:
            return memory
        state_path = Path(path)
        if not state_path.exists():
            return memory
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return memory
        memory.daily_trade_counts = {str(k): int(v) for k, v in payload.get("daily_trade_counts", {}).items()}
        memory.last_trade_at = {str(k): int(v) for k, v in payload.get("last_trade_at", {}).items()}
        memory.position_opened_at = {
            str(k): int(v) for k, v in payload.get("position_opened_at", {}).items()
        }
        memory.position_open_context = {
            str(k): dict(v) for k, v in payload.get("position_open_context", {}).items()
        }
        memory.position_excursion = {
            str(k): {
                "mfePct": str((v or {}).get("mfePct", "0")),
                "maePct": str((v or {}).get("maePct", "0")),
            }
            for k, v in payload.get("position_excursion", {}).items()
            if isinstance(v, dict)
        }
        memory.daily_loss_quote = {str(k): decimal_from(v) for k, v in payload.get("daily_loss_quote", {}).items()}
        memory.daily_realized_pnl_net = {
            str(k): decimal_from(v) for k, v in payload.get("daily_realized_pnl_net", {}).items()
        }
        memory.daily_funding_fee_quote = {
            str(k): decimal_from(v) for k, v in payload.get("daily_funding_fee_quote", {}).items()
        }
        memory.daily_commission_quote = {
            str(k): decimal_from(v) for k, v in payload.get("daily_commission_quote", {}).items()
        }
        memory.position_peak_price = {
            str(k): str(v) for k, v in payload.get("position_peak_price", {}).items()
        }
        memory.position_exit_tiers = {
            str(k): list(v) for k, v in payload.get("position_exit_tiers", {}).items()
        }
        memory.daily_symbol_add_counts = {
            str(k): int(v) for k, v in payload.get("daily_symbol_add_counts", {}).items()
        }
        memory.last_open_positions = {
            str(k): dict(v) if isinstance(v, dict) else {}
            for k, v in payload.get("last_open_positions", {}).items()
        }
        return memory

    def save(self, path: str | None) -> None:
        if not path:
            return
        state_path = Path(path)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "daily_trade_counts": self.daily_trade_counts,
            "last_trade_at": self.last_trade_at,
            "position_opened_at": self.position_opened_at,
            "position_open_context": self.position_open_context,
            "position_excursion": self.position_excursion,
            "daily_loss_quote": {key: str(value) for key, value in self.daily_loss_quote.items()},
            "daily_realized_pnl_net": {key: str(value) for key, value in self.daily_realized_pnl_net.items()},
            "daily_funding_fee_quote": {key: str(value) for key, value in self.daily_funding_fee_quote.items()},
            "daily_commission_quote": {key: str(value) for key, value in self.daily_commission_quote.items()},
            "position_peak_price": self.position_peak_price,
            "position_exit_tiers": self.position_exit_tiers,
            "daily_symbol_add_counts": self.daily_symbol_add_counts,
            "last_open_positions": self.last_open_positions,
        }
        atomic_write_json(state_path, payload)


def build_order_intent(
    snapshot: MarketSnapshot,
    signal: Signal,
    strategy: StrategyConfig,
    risk: RiskConfig,
    portfolio: PaperPortfolio,
    auto_exec: AutoExecutionConfig | None = None,
    entry_timing_cfg: Any = None,
    execution: ExecutionConfig | None = None,
) -> OrderIntent | None:
    from growth_sizing import compute_open_notional, mark_prices_from_portfolio, portfolio_equity

    asset = snapshot.asset
    price = snapshot.price
    symbol = normalize_symbol(asset.symbol)
    marks = mark_prices_from_portfolio(portfolio, symbol, price)
    equity = portfolio_equity(portfolio, marks)
    order: OrderIntent | None
    if is_futures_market(asset.market):
        order = build_futures_order_intent(
            asset,
            symbol,
            price,
            signal,
            strategy,
            risk,
            portfolio,
            auto_exec=auto_exec,
            equity=equity,
            execution=execution,
        )
    elif signal.action == BUY:
        available_cash = portfolio.available_cash(asset.quote_asset, asset.market)
        current_value = portfolio.position_value(symbol, price)
        quote_amount = compute_open_notional(
            risk,
            strategy,
            equity=equity,
            available_margin=available_cash,
            signal_confidence=signal.confidence,
            current_position_value=current_value,
            min_margin_per_trade=risk.min_margin_per_trade,
        )
        order = OrderIntent(
            BUY, symbol, asset.market, asset.quote_asset.upper(), quote_amount, price, intent_kind=INTENT_BUY_SPOT
        )
    elif signal.action == SELL:
        position_value = portfolio.position_value(symbol, price)
        quote_amount = position_value * strategy.sell_position_fraction
        order = OrderIntent(
            SELL, symbol, asset.market, asset.quote_asset.upper(), quote_amount, price, intent_kind=INTENT_SELL_SPOT
        )
    else:
        order = None
    if order and auto_exec:
        from entry_timing import enrich_order_intent

        atr_pct = decimal_from(signal.indicators.get("atr_pct", "0") or "0")
        return enrich_order_intent(
            order,
            auto_exec,
            strategy,
            asset,
            atr_pct=atr_pct,
            signal=signal,
            entry_timing_cfg=entry_timing_cfg,
            profitability_raw=None,
        )
    return order


def resize_order_quote(order: OrderIntent, quote_amount: Decimal) -> OrderIntent:
    return replace(order, quote_amount=quote_amount.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN))


def is_full_close(strategy: StrategyConfig, auto_exec: AutoExecutionConfig, close_qty: Decimal, position_qty: Decimal) -> bool:
    if position_qty <= 0:
        return False
    if strategy.sell_position_fraction >= auto_exec.full_close_threshold:
        return True
    return close_qty >= position_qty * auto_exec.full_close_threshold


def is_futures_market(market: str) -> bool:
    normalized = market.lower()
    return "futures" in normalized or normalized in {"binance_usdt_futures", "usdt_futures", "fapi"}


def is_spot_market(market: str) -> bool:
    normalized = market.lower()
    return "spot" in normalized or normalized in {"binance_spot", "crypto_spot", "crypto_spot_demo"}


def quote_cash_key(quote_asset: str, market: str) -> str:
    quote = quote_asset.upper()
    if quote == "USDT":
        if is_futures_market(market):
            return CASH_USDT_FUTURES
        if is_spot_market(market):
            return CASH_USDT_SPOT
    return quote


def daily_drag_amount(memory: TradingMemory, scope: str) -> Decimal:
    if scope == "loss_only":
        return memory.daily_loss_today()
    return memory.total_daily_drag_today()


def daily_drag_blocked_reason(
    risk: RiskConfig,
    memory: TradingMemory,
    equity: Decimal,
    *,
    is_reduce_only: bool = False,
) -> str | None:
    from growth_sizing import effective_max_daily_loss

    cap = effective_max_daily_loss(risk, equity)
    drag = daily_drag_amount(memory, risk.daily_drag_scope)
    if drag < cap:
        return None
    if risk.daily_drag_blocks == "new_opens_only" and is_reduce_only:
        return None
    return (
        f"Daily drag {drag} (loss {memory.daily_loss_today()} + "
        f"costs {memory.daily_costs_today()}, scope={risk.daily_drag_scope}) "
        f"reached cap {cap}; "
        f"{'new opens blocked' if risk.daily_drag_blocks == 'new_opens_only' else 'live trading halted'}."
    )


def resolve_entry_leverage(
    auto_exec: AutoExecutionConfig | None,
    signal: Signal | None = None,
    indicators: dict[str, str] | None = None,
    symbol: str | None = None,
) -> int:
    from asset_profile import resolve_entry_leverage as _resolve

    return _resolve(auto_exec, signal, indicators, symbol)


def effective_stop_take_profit_pcts(strategy: StrategyConfig, auto_exec: AutoExecutionConfig) -> tuple[Decimal, Decimal]:
    if strategy.trade_horizon == "swing":
        return strategy.swing_stop_loss_pct, strategy.swing_take_profit_pct
    return auto_exec.stop_loss_pct, auto_exec.take_profit_pct


def effective_sell_fraction(
    strategy: StrategyConfig,
    indicators: dict[str, str] | None = None,
    *,
    force_fraction: Decimal | None = None,
) -> Decimal:
    indicators = indicators or {}
    tier = indicators.get("exit_tier", "")
    if tier == "force":
        return force_fraction if force_fraction is not None else Decimal("0.75")
    if tier == "early":
        return strategy.holding_early_take_fraction
    if strategy.trade_horizon == "swing":
        return strategy.swing_sell_position_fraction
    return strategy.sell_position_fraction


def position_exit_tiers_taken(memory: TradingMemory | None, symbol: str) -> set[str]:
    if memory is None:
        return set()
    return set(memory.position_exit_tiers.get(normalize_symbol(symbol), []))


def mark_position_exit_tier(memory: TradingMemory, symbol: str, tier: str) -> None:
    sym = normalize_symbol(symbol)
    tiers = list(memory.position_exit_tiers.get(sym, []))
    if tier not in tiers:
        tiers.append(tier)
    memory.position_exit_tiers[sym] = tiers


def clear_position_exit_tiers(memory: TradingMemory, symbol: str) -> None:
    memory.position_exit_tiers.pop(normalize_symbol(symbol), None)


def count_open_positions(portfolio: PaperPortfolio) -> int:
    return sum(1 for pos in portfolio.positions.values() if pos.quantity != 0)


def normalize_price_change_pct_24h(value: Any) -> Decimal:
    """Binance priceChangePercent → decimal ratio (+88.02 points → 0.8802)."""
    return decimal_from(value or "0") / Decimal("100")


def effective_price_change_pct_24h(indicators: dict[str, str]) -> Decimal:
    """Read 24h change ratio from indicators (already normalized when injected)."""
    raw = indicators.get("price_change_pct_24h", "0") or "0"
    parsed = decimal_from(raw)
    if parsed == 0:
        return Decimal("0")
    # Legacy rows may still carry Binance percent points (>1.5).
    if abs(parsed) > Decimal("1.5"):
        return parsed / Decimal("100")
    return parsed


def format_pct_ratio(value: Decimal) -> str:
    return f"{value * Decimal('100'):+.2f}%"


def fetch_exchange_position_qty(execution: ExecutionConfig, symbol: str) -> Decimal | None:
    """Return signed futures position qty, 0 if flat, None if API failed."""
    try:
        sym = normalize_symbol(symbol)
        for row in fetch_futures_positions(execution):
            if normalize_symbol(str(row.get("symbol", ""))) != sym:
                continue
            return decimal_from(row.get("positionAmt", "0"))
        return Decimal("0")
    except Exception:
        return None


def position_age_seconds(memory: TradingMemory, symbol: str, now: int) -> int | None:
    opened = memory.position_opened_at.get(normalize_symbol(symbol))
    if opened:
        return max(0, now - opened)
    last = memory.last_trade_at.get(normalize_symbol(symbol))
    if last:
        return max(0, now - last)
    return None


from exit_engine import (
    apply_holding_priority_signal,
    exit_quality_threshold_mult,
    holding_thresholds_for_regime,
    peak_giveback_exit_reason,
    position_peak_key,
    sync_position_peak,
)


def fetch_futures_user_trades(
    execution: ExecutionConfig,
    symbol: str,
    *,
    start_time_ms: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"symbol": normalize_symbol(symbol), "limit": int(limit)}
    if start_time_ms is not None:
        params["startTime"] = int(start_time_ms)
    payload = signed_binance_request(
        execution,
        "GET",
        "/fapi/v1/userTrades",
        params,
        market="binance_futures",
    )
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected userTrades payload: {payload!r}")
    return payload


def resolve_closure_exit_price(
    execution: ExecutionConfig | None,
    symbol: str,
    position_side: str,
    snap: dict[str, Any],
) -> Decimal:
    entry = decimal_from(snap.get("entryPrice", "0"))
    fallback = decimal_from(snap.get("lastMarkPrice", "0"))
    if fallback <= 0:
        fallback = entry
    if execution is None:
        return fallback
    close_side = SELL if str(position_side).upper() == "LONG" else BUY
    started_at = int(snap.get("snapshotAt", 0) or 0)
    start_ms = started_at * 1000 if started_at > 0 else None
    try:
        trades = fetch_futures_user_trades(execution, symbol, start_time_ms=start_ms, limit=100)
        matched = [row for row in trades if str(row.get("side", "")).upper() == close_side]
        if not matched:
            return fallback
        total_qty = Decimal("0")
        total_quote = Decimal("0")
        for row in matched:
            qty = decimal_from(row.get("qty", "0"))
            price = decimal_from(row.get("price", "0"))
            if qty <= 0 or price <= 0:
                continue
            total_qty += qty
            total_quote += qty * price
        if total_qty <= 0:
            return fallback
        return total_quote / total_qty
    except Exception:
        return fallback


def _clear_position_tracking(memory: TradingMemory, symbol: str) -> None:
    sym = normalize_symbol(symbol)
    memory.position_opened_at.pop(sym, None)
    memory.position_open_context.pop(sym, None)
    memory.position_excursion.pop(sym, None)
    memory.position_peak_price.pop(f"{sym}:LONG", None)
    memory.position_peak_price.pop(f"{sym}:SHORT", None)
    clear_position_exit_tiers(memory, sym)


def update_position_excursion(
    memory: TradingMemory,
    symbol: str,
    *,
    entry: Decimal,
    mark: Decimal,
    side_long: bool,
) -> dict[str, str]:
    sym = normalize_symbol(symbol)
    if entry <= 0 or mark <= 0:
        return memory.position_excursion.get(sym, {"mfePct": "0", "maePct": "0"})
    pnl_pct = (mark - entry) / entry if side_long else (entry - mark) / entry
    prev = memory.position_excursion.get(sym, {})
    mfe = max(decimal_from(prev.get("mfePct", "0")), pnl_pct)
    mae = min(decimal_from(prev.get("maePct", "0")), pnl_pct)
    payload = {
        "mfePct": str(mfe.quantize(Decimal("0.0001"))),
        "maePct": str(mae.quantize(Decimal("0.0001"))),
    }
    memory.position_excursion[sym] = payload
    return payload


def open_context_with_excursion(
    memory: TradingMemory,
    symbol: str,
) -> dict[str, Any]:
    sym = normalize_symbol(symbol)
    ctx = dict(memory.position_open_context.get(sym) or {})
    excursion = memory.position_excursion.get(sym) or {}
    if excursion:
        ctx["mfePct"] = str(excursion.get("mfePct", "0"))
        ctx["maePct"] = str(excursion.get("maePct", "0"))
    return ctx


def ensure_position_open_time(memory: TradingMemory, symbol: str, observed_at: int) -> None:
    sym = normalize_symbol(symbol)
    if memory.position_opened_at.get(sym):
        return
    ctx = memory.position_open_context.get(sym) or {}
    candidates = [
        ctx.get("capturedAt"),
        memory.last_trade_at.get(sym),
        observed_at,
    ]
    for candidate in candidates:
        try:
            opened_at = int(candidate)
        except (TypeError, ValueError):
            continue
        if opened_at > 0:
            memory.position_opened_at[sym] = opened_at
            return


def detect_external_position_closures(
    memory: TradingMemory,
    portfolio: PaperPortfolio,
    learning_cfg: Any,
    execution: ExecutionConfig | None = None,
) -> list[dict[str, Any]]:
    """Record trade outcomes when a symbol disappeared from the book (SL/TP/manual close)."""
    from trade_outcomes import load_recent_outcomes, record_trade_outcome

    if not learning_cfg or not getattr(learning_cfg, "enabled", False):
        return []
    outcomes: list[dict[str, Any]] = []
    now = int(time.time())
    for sym, snap in list(memory.last_open_positions.items()):
        prev_qty = decimal_from(snap.get("quantity", "0"))
        if prev_qty <= 0:
            continue
        pos = portfolio.positions.get(sym)
        now_qty = pos.quantity if pos is not None else Decimal("0")
        if now_qty != 0:
            continue
        if execution is not None:
            live_qty = fetch_exchange_position_qty(execution, sym)
            if live_qty is None:
                continue
            if live_qty != 0:
                continue
        elif learning_cfg and getattr(learning_cfg, "enabled", False):
            continue
        recent_rows = load_recent_outcomes(learning_cfg.outcomes_path, limit=12)
        if any(
            normalize_symbol(str(row.get("symbol", ""))) == sym
            and row.get("closeSource") == "external_sl_tp"
            and now - int(row.get("closedAt", 0) or 0) < 180
            for row in recent_rows
        ):
            continue
        position_side = str(snap.get("positionSide", "LONG")).upper()
        entry = decimal_from(snap.get("entryPrice", "0"))
        if entry <= 0:
            continue
        exit_price = resolve_closure_exit_price(execution, sym, position_side, snap)
        close_side = SELL if position_side == "LONG" else BUY
        row = record_trade_outcome(
            learning_cfg,
            symbol=sym,
            side=close_side,
            quantity=prev_qty,
            exit_price=exit_price,
            entry_price=entry,
            position_side=position_side,
            regime=snap.get("regime"),
            session=snap.get("session"),
            rationale_summary="External close detected (SL/TP/manual)",
            close_source="external_sl_tp",
            open_context=open_context_with_excursion(memory, sym) or {
                "regime": snap.get("regime"),
                "source": "unknown",
                "capturedAt": snap.get("snapshotAt"),
            },
        )
        if row:
            outcomes.append(row)
            _clear_position_tracking(memory, sym)
            memory.last_open_positions.pop(sym, None)
    return outcomes


def refresh_last_open_positions_snapshot(
    memory: TradingMemory,
    portfolio: PaperPortfolio,
    *,
    observed_at: int,
    mark_prices: dict[str, Decimal] | None = None,
    symbol_meta: dict[str, dict[str, Any]] | None = None,
    execution: ExecutionConfig | None = None,
) -> None:
    if execution is not None:
        current: dict[str, dict[str, Any]] = {}
        try:
            for row in fetch_futures_positions(execution):
                amt = decimal_from(row.get("positionAmt", "0"))
                if amt == 0:
                    continue
                sym = normalize_symbol(str(row.get("symbol", "")))
                side = "LONG" if amt > 0 else "SHORT"
                meta = (symbol_meta or {}).get(sym, {})
                mark = decimal_from(row.get("markPrice", "0"))
                entry = decimal_from(row.get("entryPrice", "0"))
                if mark <= 0:
                    mark = (mark_prices or {}).get(sym) or entry
                if entry > 0 and mark > 0:
                    update_position_excursion(memory, sym, entry=entry, mark=mark, side_long=amt > 0)
                ensure_position_open_time(memory, sym, observed_at)
                current[sym] = {
                    "quantity": str(abs(amt)),
                    "entryPrice": str(entry),
                    "positionSide": side,
                    "lastMarkPrice": str(mark),
                    "regime": meta.get("regime"),
                    "session": meta.get("session"),
                    "snapshotAt": observed_at,
                }
            memory.last_open_positions = current
            return
        except Exception:
            pass
    current = {}
    for sym, pos in portfolio.positions.items():
        if pos.quantity == 0:
            continue
        side = "LONG" if pos.quantity > 0 else "SHORT"
        meta = (symbol_meta or {}).get(sym, {})
        mark = (mark_prices or {}).get(sym) or decimal_from(meta.get("lastMarkPrice", "0"))
        if mark <= 0:
            mark = pos.average_price
        if pos.average_price > 0 and mark > 0:
            update_position_excursion(
                memory,
                sym,
                entry=pos.average_price,
                mark=mark,
                side_long=pos.quantity > 0,
            )
        ensure_position_open_time(memory, sym, observed_at)
        current[sym] = {
            "quantity": str(abs(pos.quantity)),
            "entryPrice": str(pos.average_price),
            "positionSide": side,
            "lastMarkPrice": str(mark),
            "regime": meta.get("regime"),
            "session": meta.get("session"),
            "snapshotAt": observed_at,
        }
    memory.last_open_positions = current


def compute_breakeven_stop_price(
    entry: Decimal,
    *,
    side_long: bool,
    buffer_pct: Decimal,
    tick: Decimal,
) -> Decimal:
    if side_long:
        return quantize_price(entry * (Decimal("1") + buffer_pct), tick)
    return quantize_price(entry * (Decimal("1") - buffer_pct), tick)


def effective_breakeven_activate_pct(
    strategy: StrategyConfig,
    *,
    activate_multiplier: Decimal | None = None,
) -> Decimal:
    activate = strategy.protection_breakeven_activate_pct
    if strategy.trade_horizon == "swing":
        activate *= Decimal("1.5")
    if activate_multiplier is not None and Decimal("0") < activate_multiplier < Decimal("1"):
        activate *= max(activate_multiplier, Decimal("0.50"))
    return activate


def _learning_bucket_from_open_context(open_context: dict[str, Any] | None) -> str:
    ctx = open_context or {}
    bucket = str(ctx.get("bucket") or "").strip()
    if bucket.startswith("discovery:"):
        bucket = bucket.split(":", 1)[1]
    if bucket:
        return "pinned" if bucket.startswith("pinned") else bucket
    source = str(ctx.get("source") or "").strip()
    if source.startswith("discovery:"):
        return source.split(":", 1)[1]
    if source.startswith("pinned"):
        return "pinned"
    return source


def protection_breakeven_activate_multiplier(
    trade_learning: dict[str, Any] | None,
    open_context: dict[str, Any] | None,
) -> Decimal:
    if not trade_learning or not trade_learning.get("enabled"):
        return Decimal("1")
    bucket = _learning_bucket_from_open_context(open_context)
    if not bucket:
        return Decimal("1")
    mult = Decimal("1")
    sizing_factor = decimal_from((trade_learning.get("bucketSizingFactors") or {}).get(bucket, "1"))
    if Decimal("0") < sizing_factor < mult:
        mult = sizing_factor
    if (trade_learning.get("bucketLiveModes") or {}).get(bucket) == "shadow_first":
        mult = min(mult, Decimal("0.65"))
    return max(mult, Decimal("0.50"))


def _protection_sl_needs_breakeven_tighten(
    *,
    side_long: bool,
    current_sl: Decimal,
    target_sl: Decimal,
) -> bool:
    if side_long:
        return current_sl < target_sl
    return current_sl > target_sl


def maybe_tighten_futures_stop_to_breakeven(
    symbol: str,
    execution: ExecutionConfig,
    auto_exec: AutoExecutionConfig,
    strategy: StrategyConfig,
    *,
    entry: Decimal,
    mark: Decimal,
    side_long: bool,
    atr_pct: Decimal | None = None,
    activate_multiplier: Decimal | None = None,
    trade_learning: dict[str, Any] | None = None,
    open_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Raise futures SL to entry+buffer when unrealized profit crosses activation threshold."""
    if not auto_exec.auto_take_profit_stop_loss or entry <= 0 or mark <= 0:
        return {"symbol": symbol, "status": "skipped", "reason": "protection disabled or invalid prices"}
    activate = effective_breakeven_activate_pct(strategy, activate_multiplier=activate_multiplier)
    buffer = strategy.protection_breakeven_buffer_pct
    pnl_pct = (mark - entry) / entry if side_long else (entry - mark) / entry
    if pnl_pct < activate:
        return {
            "symbol": symbol,
            "status": "breakeven_skip",
            "reason": f"unrealized {pnl_pct:.2%} below activate {activate:.2%}",
        }
    filters = fetch_symbol_filters(symbol, execution, "binance_futures")
    tick = filters.get("tick_size", Decimal("0.01"))
    target_sl = compute_breakeven_stop_price(entry, side_long=side_long, buffer_pct=buffer, tick=tick)
    open_orders = fetch_futures_open_algo_orders(execution, symbol)
    sl_rows = [
        row
        for row in filter_position_protection_orders(open_orders, side_long=side_long)
        if protection_algo_kind(row) == "stop_loss"
    ]
    if sl_rows:
        triggers = [decimal_from(row.get("triggerPrice", "0")) for row in sl_rows]
        triggers = [price for price in triggers if price > 0]
        if triggers:
            current_sl = max(triggers) if side_long else min(triggers)
            if not _protection_sl_needs_breakeven_tighten(
                side_long=side_long, current_sl=current_sl, target_sl=target_sl
            ):
                return {
                    "symbol": symbol,
                    "status": "breakeven_skip",
                    "reason": "stop already at/above breakeven",
                    "currentSl": str(current_sl),
                    "targetSl": str(target_sl),
                }
    return replace_futures_position_protection(
        symbol,
        execution,
        auto_exec,
        atr_pct=atr_pct,
        stop_price_override=target_sl,
        trade_learning=trade_learning,
        open_context=open_context,
    )


def maybe_trail_futures_take_profit(
    symbol: str,
    execution: ExecutionConfig,
    auto_exec: AutoExecutionConfig,
    strategy: StrategyConfig,
    memory: TradingMemory,
    *,
    entry: Decimal,
    mark: Decimal,
    side_long: bool,
    atr_pct: Decimal | None = None,
    activate_multiplier: Decimal | None = None,
) -> dict[str, Any]:
    """Ratchet exchange TP toward peak minus giveback when position is in profit."""
    if not auto_exec.auto_take_profit_stop_loss or entry <= 0 or mark <= 0:
        return {"symbol": symbol, "status": "skipped", "reason": "protection disabled or invalid prices"}
    peak_key = f"{normalize_symbol(symbol)}:{'LONG' if side_long else 'SHORT'}"
    peak_raw = memory.position_peak_price.get(peak_key)
    if not peak_raw:
        return {"symbol": symbol, "status": "trail_tp_skip", "reason": "no peak tracked"}
    peak = decimal_from(peak_raw)
    _, _, trail_pct, giveback_pct = holding_thresholds_for_regime(
        strategy, "trend_up" if side_long else "trend_down"
    )
    if activate_multiplier is not None and activate_multiplier > 0:
        trail_pct *= min(max(activate_multiplier, Decimal("0.50")), Decimal("1.50"))
    peak_pnl = (peak - entry) / entry if side_long else (entry - peak) / entry
    if peak_pnl < trail_pct:
        return {
            "symbol": symbol,
            "status": "trail_tp_skip",
            "reason": f"peak pnl {peak_pnl:.2%} below trail activate {trail_pct:.2%}",
        }
    filters = fetch_symbol_filters(symbol, execution, "binance_futures")
    tick = filters.get("tick_size", Decimal("0.01"))
    if side_long:
        target_tp = quantize_price(peak * (Decimal("1") - giveback_pct), tick)
        if target_tp <= entry:
            return {"symbol": symbol, "status": "trail_tp_skip", "reason": "target tp not above entry"}
    else:
        target_tp = quantize_price(peak * (Decimal("1") + giveback_pct), tick)
        if target_tp >= entry:
            return {"symbol": symbol, "status": "trail_tp_skip", "reason": "target tp not below entry"}
    open_orders = fetch_futures_open_algo_orders(execution, symbol)
    tp_rows = [
        row
        for row in filter_position_protection_orders(open_orders, side_long=side_long)
        if protection_algo_kind(row) == "take_profit"
    ]
    if tp_rows:
        triggers = [decimal_from(row.get("triggerPrice", "0")) for row in tp_rows]
        triggers = [price for price in triggers if price > 0]
        if triggers:
            current_tp = max(triggers) if side_long else min(triggers)
            if side_long and current_tp >= target_tp:
                return {
                    "symbol": symbol,
                    "status": "trail_tp_skip",
                    "reason": "take profit already at/above target",
                    "currentTp": str(current_tp),
                    "targetTp": str(target_tp),
                }
            if not side_long and current_tp <= target_tp:
                return {
                    "symbol": symbol,
                    "status": "trail_tp_skip",
                    "reason": "take profit already at/below target",
                    "currentTp": str(current_tp),
                    "targetTp": str(target_tp),
                }
    return replace_futures_position_protection(
        symbol,
        execution,
        auto_exec,
        atr_pct=atr_pct,
        take_profit_price_override=target_tp,
    )


def tighten_futures_protection_for_portfolio(
    portfolio: PaperPortfolio,
    execution: ExecutionConfig,
    auto_exec: AutoExecutionConfig,
    strategy: StrategyConfig,
    memory: TradingMemory | None = None,
    trade_learning: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    try:
        rows = fetch_futures_positions(execution)
    except Exception as exc:
        return [{"status": "failed", "error": str(exc)}]
    for row in rows:
        amt = decimal_from(row.get("positionAmt", "0"))
        if amt == 0:
            continue
        symbol = normalize_symbol(str(row.get("symbol", "")))
        entry = decimal_from(row.get("entryPrice", "0"))
        mark = decimal_from(row.get("markPrice", "0"))
        if entry <= 0 or mark <= 0:
            continue
        side_long = amt > 0
        pos = portfolio.positions.get(symbol)
        if pos is None or pos.quantity == 0:
            continue
        try:
            results.append(
                maybe_tighten_futures_stop_to_breakeven(
                    symbol,
                    execution,
                    auto_exec,
                    strategy,
                    entry=entry,
                    mark=mark,
                    side_long=side_long,
                    activate_multiplier=protection_breakeven_activate_multiplier(
                        trade_learning,
                        memory.position_open_context.get(symbol) if memory is not None else None,
                    ),
                    trade_learning=trade_learning,
                    open_context=memory.position_open_context.get(symbol) if memory is not None else None,
                )
            )
            if memory is not None:
                sym = normalize_symbol(symbol)
                qty = amt if side_long else -abs(amt)
                sync_position_peak(memory, sym, mark, qty)
                results.append(
                    maybe_trail_futures_take_profit(
                        symbol,
                        execution,
                        auto_exec,
                        strategy,
                        memory,
                        entry=entry,
                        mark=mark,
                        side_long=side_long,
                        activate_multiplier=exit_quality_threshold_mult(
                            trade_learning,
                            memory.position_open_context.get(symbol),
                        ),
                    )
                )
        except Exception as exc:
            results.append({"symbol": symbol, "status": "failed", "error": str(exc)})
    return results


def process_live_position_closures_at_cycle_start(
    memory: TradingMemory,
    portfolio: PaperPortfolio,
    learning_cfg: Any,
    execution: ExecutionConfig | None,
    *,
    lessons_cfg: Any = None,
) -> list[dict[str, Any]]:
    rows = detect_external_position_closures(memory, portfolio, learning_cfg, execution)
    if rows and lessons_cfg is not None and getattr(lessons_cfg, "enabled", False):
        from trade_lessons import refresh_trade_lessons

        refresh_trade_lessons(lessons_cfg, learning_cfg.outcomes_path)
    return rows


def process_live_position_tracking_at_cycle_end(
    memory: TradingMemory,
    portfolio: PaperPortfolio,
    execution: ExecutionConfig,
    auto_exec: AutoExecutionConfig,
    strategy: StrategyConfig,
    *,
    symbol_meta: dict[str, dict[str, Any]] | None = None,
    trade_learning: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mark_prices: dict[str, Decimal] = {}
    try:
        for row in fetch_futures_positions(execution):
            amt = decimal_from(row.get("positionAmt", "0"))
            if amt == 0:
                continue
            sym = normalize_symbol(str(row.get("symbol", "")))
            mark = decimal_from(row.get("markPrice", "0"))
            if mark > 0:
                mark_prices[sym] = mark
    except Exception:
        pass
    refresh_last_open_positions_snapshot(
        memory,
        portfolio,
        observed_at=int(time.time()),
        mark_prices=mark_prices,
        symbol_meta=symbol_meta,
        execution=execution,
    )
    breakeven_results = tighten_futures_protection_for_portfolio(
        portfolio, execution, auto_exec, strategy, memory, trade_learning=trade_learning
    )
    cleanup_result: dict[str, Any] | None = None
    if auto_exec.auto_take_profit_stop_loss:
        try:
            cleanup_result = cleanup_futures_protection_orders(
                execution,
                auto_exec,
                dry_run=False,
                reattach=True,
            )
        except Exception as exc:
            cleanup_result = {"status": "failed", "error": str(exc)}
    payload = {
        "updatedAt": int(time.time()),
        "refreshedPositions": len(memory.last_open_positions),
        "breakevenTighten": breakeven_results,
        "protectionCleanup": cleanup_result,
    }
    tweak_path = Path(execution.state_path).parent / "protection-tweak-latest.json"
    try:
        tweak_path.parent.mkdir(parents=True, exist_ok=True)
        tweak_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return payload


def effective_open_quote(
    max_trade_quote: Decimal,
    buy_quote_fraction: Decimal,
    available_quote: Decimal | None,
    leverage: int = 1,
) -> Decimal:
    """Max open **notional** from available margin budget and leverage."""
    if available_quote is None or available_quote <= 0:
        return max_trade_quote
    lev = max(int(leverage), 1)
    notional_cap = available_quote * buy_quote_fraction * Decimal(lev)
    return min(max_trade_quote, notional_cap)


def validate_risk_execution_mode_alignment(risk: RiskConfig, execution: ExecutionConfig) -> None:
    if risk.mode != execution.mode:
        raise ValueError(
            f"risk.mode ({risk.mode}) must match execution.mode ({execution.mode}); "
            "misaligned modes can bypass live safety gates."
        )


def live_safety_blocked_reasons(
    risk: RiskConfig,
    *,
    snapshot: MarketSnapshot | None = None,
    memory: TradingMemory | None = None,
    equity: Decimal | None = None,
) -> list[str]:
    blocked: list[str] = []
    if not risk.allow_live_trading:
        blocked.append(
            "Unattended live matching-engine trading is not enabled without explicit risk.allow_live_trading."
        )
    if kill_switch_active(risk.kill_switch_path):
        blocked.append(f"Kill switch active at {risk.kill_switch_path}; all live orders blocked.")
    arm_status = live_trading_arm_status(risk.live_arm_path)
    if not arm_status.get("armed"):
        if arm_status.get("reason") == "expired":
            blocked.append(f"Live trading arm expired at {arm_status.get('expiresAt')}; re-arm after manual review.")
        else:
            blocked.append(f"Live trading not armed; create {risk.live_arm_path} after manual review.")
        blocked.append("Binance matching-engine live orders are not supported until armed.")
    if memory is not None:
        if risk.daily_drag_blocks == "all":
            drag_reason = daily_drag_blocked_reason(
                risk, memory, equity or Decimal("0"), is_reduce_only=False
            )
            if drag_reason:
                blocked.append(drag_reason)
    if snapshot is not None:
        try:
            market_kind = live_market_kind(snapshot.asset.market)
        except ValueError as exc:
            blocked.append(str(exc))
        else:
            if market_kind not in risk.allowed_live_markets:
                blocked.append(
                    f"Live market {market_kind} is not enabled; allowed={','.join(risk.allowed_live_markets)}."
                )
    return blocked


def open_notional_blocked_reason(order: OrderIntent, execution: ExecutionConfig) -> str | None:
    if order.reduce_only:
        return None
    try:
        order_quantity_from_intent(order, execution)
    except ValueError as exc:
        return str(exc)
    return None


def live_market_kind(market: str) -> str:
    if is_futures_market(market):
        return FUTURES
    if is_spot_market(market):
        return SPOT
    raise ValueError(f"Unsupported live market type: {market}")


def _entry_leverage(
    auto_exec: AutoExecutionConfig | None,
    signal: Signal | None = None,
    symbol: str | None = None,
) -> int:
    indicators = signal.indicators if signal else None
    return resolve_entry_leverage(auto_exec, signal, indicators, symbol)


def build_futures_order_intent(
    asset: AssetConfig,
    symbol: str,
    price: Decimal,
    signal: Signal,
    strategy: StrategyConfig,
    risk: RiskConfig,
    portfolio: PaperPortfolio,
    auto_exec: AutoExecutionConfig | None = None,
    equity: Decimal | None = None,
    execution: ExecutionConfig | None = None,
) -> OrderIntent | None:
    from growth_sizing import compute_open_notional, mark_prices_from_portfolio, portfolio_equity

    position_qty = portfolio.position(symbol).quantity
    available_margin = portfolio.available_cash(asset.quote_asset, asset.market)
    # reserve fix: size new opens against collateral AFTER setting aside the
    # reserve, so sizing and the reserve floor agree. Previously notional was sized
    # on 100% of available while a separate gate blocked when available < reserve.
    sizing_margin = max(available_margin - risk.reserve_futures_available_usdt, Decimal("0"))
    if equity is None:
        equity = portfolio_equity(portfolio, mark_prices_from_portfolio(portfolio, symbol, price))
    current_position_value = portfolio.position_value(symbol, price)
    # F1-leverage: capture the sizing leverage ONCE and propagate it onto the
    # OrderIntent below. Previously the notional was sized with this tiered
    # leverage but the intent carried leverage=None, so enrich_order_intent later
    # recomputed a different (smaller) leverage and set_futures_leverage applied
    # THAT — sizing and applied leverage diverged, over-sizing every open.
    entry_lev = _entry_leverage(auto_exec, signal, symbol)
    max_quote = compute_open_notional(
        risk,
        strategy,
        equity=equity,
        available_margin=sizing_margin,
        signal_confidence=signal.confidence,
        current_position_value=current_position_value,
        leverage=entry_lev,
        min_margin_per_trade=risk.min_margin_per_trade,
    )
    sell_fraction = effective_sell_fraction(strategy, signal.indicators)
    full_close_threshold = auto_exec.full_close_threshold if auto_exec else Decimal("0.99")

    def _quantize_close_qty(raw_qty: Decimal) -> Decimal:
        try:
            filter_execution = execution or ExecutionConfig()
            filters = fetch_symbol_filters(symbol, filter_execution, asset.market)
            step = filters.get("step_size", Decimal("0.00000001"))
        except Exception:
            step = Decimal("0.00000001")
        return quantize_to_step(raw_qty, step)

    def _safe_close_qty(position_abs: Decimal, fraction: Decimal) -> Decimal:
        raw = position_abs * fraction
        qty = _quantize_close_qty(raw)
        if qty <= 0 and position_abs > 0:
            qty = _quantize_close_qty(position_abs)
        if qty <= 0 and position_abs > 0:
            qty = position_abs
        return qty

    if signal.action == BUY:
        if position_qty < 0:
            close_qty = _safe_close_qty(abs(position_qty), sell_fraction)
            quote_amount = (close_qty * price).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            intent_kind = (
                INTENT_CLOSE_SHORT
                if sell_fraction >= full_close_threshold or close_qty >= abs(position_qty) * full_close_threshold
                else INTENT_REDUCE_SHORT
            )
            return OrderIntent(
                BUY,
                symbol,
                asset.market,
                asset.quote_asset.upper(),
                quote_amount,
                price,
                reduce_only=True,
                quantity=close_qty,
                intent_kind=intent_kind,
            )
        intent_kind = INTENT_OPEN_LONG if position_qty == 0 else INTENT_OPEN_LONG
        quote_amount = max_quote
        return OrderIntent(
            BUY,
            symbol,
            asset.market,
            asset.quote_asset.upper(),
            quote_amount,
            price,
            leverage=entry_lev,
            intent_kind=intent_kind,
        )

    if signal.action == SELL:
        if position_qty > 0:
            close_qty = _safe_close_qty(position_qty, sell_fraction)
            quote_amount = (close_qty * price).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            intent_kind = (
                INTENT_CLOSE_LONG
                if sell_fraction >= full_close_threshold or close_qty >= position_qty * full_close_threshold
                else INTENT_REDUCE_LONG
            )
            return OrderIntent(
                SELL,
                symbol,
                asset.market,
                asset.quote_asset.upper(),
                quote_amount,
                price,
                reduce_only=True,
                quantity=close_qty,
                intent_kind=intent_kind,
            )
        if position_qty < 0:
            return None
        return OrderIntent(
            SELL,
            symbol,
            asset.market,
            asset.quote_asset.upper(),
            max_quote,
            price,
            leverage=entry_lev,
            intent_kind=INTENT_OPEN_SHORT,
        )
    return None


def enrich_order_intent(
    order: OrderIntent,
    auto_exec: AutoExecutionConfig,
    strategy: StrategyConfig,
    asset: AssetConfig,
    *,
    atr_pct: Decimal | None = None,
    signal: Any = None,
    entry_timing_cfg: Any = None,
    profitability_raw: dict[str, Any] | None = None,
) -> OrderIntent:
    _ = (strategy, profitability_raw)
    working = order
    if entry_timing_cfg is not None and getattr(entry_timing_cfg, "enabled", False):
        from entry_timing import enrich_order_intent as timing_enrich

        working = timing_enrich(
            working,
            auto_exec,
            strategy,
            asset,
            atr_pct=atr_pct or Decimal("0"),
            signal=signal,
            entry_timing_cfg=entry_timing_cfg,
            profitability_raw=profitability_raw,
        )
    else:
        order_type = working.order_type if working.order_type != ORDER_MARKET else auto_exec.entry_order_type
        limit_price = working.limit_price
        if order_type == ORDER_LIMIT and limit_price is None:
            if working.action == BUY:
                limit_price = working.estimated_price * (Decimal("1") - auto_exec.limit_offset_pct)
            else:
                limit_price = working.estimated_price * (Decimal("1") + auto_exec.limit_offset_pct)
        working = OrderIntent(
            action=working.action,
            symbol=working.symbol,
            market=working.market,
            quote_asset=working.quote_asset,
            quote_amount=working.quote_amount,
            estimated_price=working.estimated_price,
            reduce_only=working.reduce_only,
            quantity=working.quantity,
            order_type=ORDER_MARKET if working.reduce_only else order_type,
            limit_price=limit_price,
            stop_price=working.stop_price,
            take_profit_price=working.take_profit_price,
            leverage=working.leverage,
            intent_kind=working.intent_kind,
            time_in_force=working.time_in_force or auto_exec.time_in_force,
        )

    order_type = ORDER_MARKET if working.reduce_only else working.order_type
    leverage = working.leverage
    if (
        is_futures_market(asset.market)
        and auto_exec.auto_leverage
        and leverage is None
        and not working.reduce_only
    ):
        leverage = min(auto_exec.default_leverage, auto_exec.max_leverage)
    limit_price = working.limit_price
    stop_price = working.stop_price
    take_profit_price = working.take_profit_price
    if auto_exec.auto_take_profit_stop_loss and not working.reduce_only and working.intent_kind in {
        INTENT_OPEN_LONG,
        INTENT_OPEN_SHORT,
        INTENT_BUY_SPOT,
    }:
        reference = limit_price or working.estimated_price
        if working.intent_kind in {INTENT_OPEN_LONG, INTENT_BUY_SPOT}:
            take_profit_price = reference * (Decimal("1") + auto_exec.take_profit_pct)
            stop_price = reference * (Decimal("1") - auto_exec.stop_loss_pct)
        elif working.intent_kind == INTENT_OPEN_SHORT:
            take_profit_price = reference * (Decimal("1") - auto_exec.take_profit_pct)
            stop_price = reference * (Decimal("1") + auto_exec.stop_loss_pct)
    return OrderIntent(
        action=working.action,
        symbol=working.symbol,
        market=working.market,
        quote_asset=working.quote_asset,
        quote_amount=working.quote_amount,
        estimated_price=working.estimated_price,
        reduce_only=working.reduce_only,
        quantity=working.quantity,
        order_type=order_type,
        limit_price=limit_price,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        leverage=leverage,
        intent_kind=working.intent_kind,
        time_in_force=working.time_in_force or auto_exec.time_in_force,
    )


def profit_adaptive_effective_cap(
    base_cap: int, today_net: Decimal, cfg: "ProfitAdaptiveCapConfig"
) -> tuple[int, int]:
    """Return (effective_cap, bonus). Raises the cap only on strictly-positive net
    realized PnL; never lowers it; clamps the effective TOTAL cap to hard_ceiling.
    base_cap == 0 means 'unlimited' upstream and is returned untouched. Disabled or
    net <= 0 -> (base_cap, 0), i.e. identical to today."""
    if cfg is None or not cfg.enabled or base_cap <= 0 or today_net <= 0 or cfg.step_usdt <= 0:
        return base_cap, 0
    steps = int(today_net / cfg.step_usdt)  # Decimal division floors toward zero; net > 0
    bonus = steps * int(cfg.extra_trades_per_step)
    headroom = max(0, int(cfg.hard_ceiling) - base_cap)
    bonus = min(bonus, headroom)
    return base_cap + bonus, bonus


def apply_risk_controls(
    snapshot: MarketSnapshot,
    signal: Signal,
    strategy: StrategyConfig,
    risk: RiskConfig,
    portfolio: PaperPortfolio,
    memory: TradingMemory,
    auto_exec: AutoExecutionConfig | None = None,
    benchmark_gate: dict[str, Any] | None = None,
    execution: ExecutionConfig | None = None,
    market_context: dict[str, Any] | None = None,
    trade_learning: dict[str, Any] | None = None,
    trade_learning_cfg: Any = None,
    trade_lessons: dict[str, Any] | None = None,
    trade_lessons_cfg: Any = None,
    market_context_cfg: Any = None,
    bucket_strategy_cfg: Any = None,
    quadrant_strategy_cfg: Any = None,
    entry_timing_cfg: Any = None,
    profitability_raw: dict[str, Any] | None = None,
    persona_council_cfg: Any = None,
    capital_scaling_cfg: Any = None,
) -> RiskDecision:
    from growth_sizing import effective_max_daily_loss, mark_prices_from_portfolio, portfolio_equity, portfolio_heat
    from entry_timing import entry_timing_defer_reason, entry_timing_from_config
    from quadrant_strategy import (
        bypass_downtrend_long_block,
        bypass_discovery_short_block,
        macro_gainer_long_block_reason,
        quadrant_quote_fraction_mult,
        quadrant_strategy_from_config,
        skip_gainers_short_lessons,
        QUADRANT_REVERSAL_SHORT,
        entry_quadrant,
    )
    from trade_outcomes import (
        apply_bucket_sizing_factor,
        apply_shadow_canary_factor,
        apply_sizing_factor,
        required_confidence_with_learning,
        trade_learning_block_reason,
        trade_learning_discovery_shadow_first,
    )
    from trade_lessons import trade_lesson_block_reasons, trade_lessons_from_config
    from market_context import market_context_block_reason

    quadrant_cfg = quadrant_strategy_cfg if quadrant_strategy_cfg is not None else quadrant_strategy_from_config({})
    timing_cfg = entry_timing_cfg if entry_timing_cfg is not None else entry_timing_from_config({})
    execution = execution or ExecutionConfig()

    reasons = list(signal.reasons)
    blocked: list[str] = []
    symbol = normalize_symbol(snapshot.asset.symbol)
    indicators = signal.indicators
    marks = mark_prices_from_portfolio(portfolio, symbol, snapshot.price)
    equity = portfolio_equity(portfolio, marks)
    heat = portfolio_heat(portfolio, marks)

    # Capital-adaptive scaling: rescale risk/sizing floors to the CURRENT equity so
    # one config works from a 14 USDT micro account up to a large one. Applied
    # before sizing (build_order_intent) and before the leverage cap below.
    capital_info: dict[str, Any] | None = None
    if capital_scaling_cfg is not None and getattr(capital_scaling_cfg, "enabled", False):
        from capital_scaling import apply_growth_scheduler, scale_risk_for_equity, scaled_leverage_cap
        from growth_sizing import effective_max_daily_loss

        risk, capital_info = scale_risk_for_equity(capital_scaling_cfg, risk, equity)
        risk, growth_schedule = apply_growth_scheduler(
            capital_scaling_cfg,
            risk,
            equity=equity,
            trade_learning=trade_learning,
            today_net=memory.daily_realized_net_today(),
            daily_drag=memory.total_daily_drag_today(),
            effective_daily_loss_cap=effective_max_daily_loss(risk, equity),
            portfolio_heat=heat,
        )
        if capital_info is not None:
            capital_info["growthScheduler"] = growth_schedule
            capital_info["maxDailyTrades"] = risk.max_daily_trades
            capital_info["cooldownSeconds"] = risk.cooldown_seconds
            capital_info["maxConcurrentPositions"] = risk.max_concurrent_positions
        lev_cap = scaled_leverage_cap(capital_scaling_cfg, equity)
        if lev_cap is not None and auto_exec is not None:
            from dataclasses import replace as _replace

            # Clamp every leverage knob to the tier cap so sizing AND the applied
            # leverage both respect it.
            auto_exec = _replace(
                auto_exec,
                max_leverage=min(auto_exec.max_leverage, lev_cap),
                default_leverage=min(auto_exec.default_leverage, lev_cap),
                leverage_mid=min(auto_exec.leverage_mid, lev_cap),
                leverage_high=min(auto_exec.leverage_high, lev_cap),
                leverage_extreme=min(auto_exec.leverage_extreme, lev_cap),
                max_leverage_crypto=min(auto_exec.max_leverage_crypto, lev_cap),
            )
        try:
            indicators["capitalScaling"] = capital_info
        except Exception:
            pass

    live_blocked: list[str] = []
    if risk.mode == LIVE:
        live_blocked = live_safety_blocked_reasons(risk, snapshot=snapshot, memory=memory, equity=equity)
        blocked.extend(live_blocked)

    order = build_order_intent(
        snapshot,
        signal,
        strategy,
        risk,
        portfolio,
        auto_exec=auto_exec,
        entry_timing_cfg=timing_cfg,
        execution=execution,
    )
    if order is None:
        if signal.action == HOLD:
            blocked.append("Strategy chose HOLD; no order should be created.")
        return RiskDecision(False, signal.action, reasons, blocked, None, risk)

    is_reduce = order.reduce_only
    exit_tier = str(indicators.get("exit_tier", ""))

    if not is_reduce:
        defer_reason = entry_timing_defer_reason(
            action=order.action,
            indicators=indicators,
            cfg=timing_cfg,
            reduce_only=is_reduce,
            confidence=signal.confidence,
        )
        if defer_reason:
            blocked.append(defer_reason)
        elif indicators.get("entry_timing_chase_override_reason"):
            reasons.append(str(indicators["entry_timing_chase_override_reason"]))

    if signal.action == HOLD and not is_reduce:
        blocked.append("Strategy chose HOLD; no order should be created.")
    if not is_reduce and signal.action == BUY and "overbought_rsi" in signal.warnings:
        blocked.append("RSI is overheated; BUY is blocked to avoid chasing an extended move.")
    if not is_reduce and signal.action == BUY and "squeeze_regime" in signal.warnings:
        blocked.append("Bollinger squeeze active; BUY blocked until directional breakout confirms.")
    if not is_reduce and signal.action == BUY and "downtrend_regime" in signal.warnings:
        if not bypass_downtrend_long_block(indicators):
            blocked.append("Downtrend regime detected; BUY blocked to avoid counter-trend entries.")
    if not is_reduce and signal.action == BUY and "weak_fusion" in signal.warnings:
        blocked.append(
            f"Multi-factor fusion bull weight {indicators.get('fusion_bull_pct', '?')} "
            f"below strategy minimum; BUY blocked."
        )
    if benchmark_gate and not is_reduce:
        from benchmark_gate import blocks_new_open

        is_open = signal.action in {BUY, SELL}
        if is_open:
            blocked_by_bench, bench_reason = blocks_new_open(benchmark_gate, is_reduce_only=False)
            if blocked_by_bench and bench_reason:
                blocked.append(bench_reason)
    required_confidence = strategy.min_sell_confidence if signal.action == SELL else risk.min_confidence
    if trade_learning and not is_reduce:
        required_confidence = required_confidence_with_learning(required_confidence, trade_learning)
    min_reasons = 1 if is_reduce else risk.require_reason_count
    if not is_reduce and signal.confidence < required_confidence:
        blocked.append(f"Confidence {signal.confidence} is below required {required_confidence}.")
    if len(reasons) < min_reasons:
        blocked.append(f"Only {len(reasons)} reasons were produced; require at least {min_reasons}.")
    effective_daily_cap, cap_bonus = profit_adaptive_effective_cap(
        risk.max_daily_trades,
        memory.daily_realized_net_today(),
        risk.profit_adaptive_daily_cap,
    )
    if risk.profit_adaptive_daily_cap.enabled and risk.max_daily_trades > 0:
        # Auditability: record the computed bonus + effective cap in the ledger.
        try:
            indicators["profitAdaptiveCap"] = {
                "baseCap": risk.max_daily_trades,
                "todayNetRealized": str(memory.daily_realized_net_today()),
                "bonus": cap_bonus,
                "effectiveCap": effective_daily_cap,
                "usedToday": memory.count_for_today(),
            }
        except Exception:
            pass
    if not is_reduce and effective_daily_cap > 0 and memory.count_for_today() >= effective_daily_cap:
        blocked.append(
            f"Daily trade cap {effective_daily_cap} has already been reached."
            + (f" (base {risk.max_daily_trades} +{cap_bonus} profit-adaptive)" if cap_bonus else "")
        )
    last_trade_at = memory.last_trade_at.get(symbol)
    if not is_reduce and last_trade_at and snapshot.observed_at - last_trade_at < risk.cooldown_seconds:
        blocked.append(f"Cooldown active for {symbol}; last trade was {snapshot.observed_at - last_trade_at}s ago.")

    current_volatility = decimal_from(indicators.get("volatility", "0") or "0")
    current_drawdown = decimal_from(indicators.get("drawdown", "0") or "0")
    if not is_reduce and current_volatility > risk.max_volatility:
        blocked.append(f"Volatility {current_volatility:.4%} exceeds max {risk.max_volatility:.4%}.")
    if not is_reduce and current_drawdown > risk.max_drawdown:
        blocked.append(f"Drawdown {current_drawdown:.4%} exceeds max {risk.max_drawdown:.4%}.")

    if order and not order.reduce_only and trade_learning:
        scaled_quote = apply_sizing_factor(order.quote_amount, trade_learning)
        if scaled_quote != order.quote_amount:
            order = replace(order, quote_amount=scaled_quote)
            reasons.append(f"Trade learning sizing factor applied: quote {order.quote_amount} USDT.")
        bucket_scaled_quote, bucket_factor = apply_bucket_sizing_factor(
            order.quote_amount,
            trade_learning,
            bucket=str(indicators.get("discovery_bucket") or ""),
            source=str(indicators.get("discovery_source") or ""),
        )
        if bucket_scaled_quote != order.quote_amount:
            order = replace(order, quote_amount=bucket_scaled_quote)
            reasons.append(
                f"Trade learning bucket sizing factor applied: quote {order.quote_amount} USDT "
                f"(x{bucket_factor})."
            )
        canary_scaled_quote, canary_factor = apply_shadow_canary_factor(
            order.quote_amount,
            trade_learning,
            bucket=str(indicators.get("discovery_bucket") or ""),
            source=str(indicators.get("discovery_source") or ""),
        )
        if canary_scaled_quote != order.quote_amount:
            order = replace(order, quote_amount=canary_scaled_quote)
            indicators["trade_learning_shadow_canary"] = "true"
            indicators["trade_learning_shadow_canary_factor"] = str(canary_factor)
            reasons.append(
                f"Trade learning shadow canary sizing applied: quote {order.quote_amount} USDT "
                f"(x{canary_factor})."
            )

    if order and not order.reduce_only:
        qmult = quadrant_quote_fraction_mult(indicators, quadrant_cfg)
        if qmult != Decimal("1") and order.quote_amount > 0:
            scaled = (order.quote_amount * qmult).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            if scaled != order.quote_amount:
                order = replace(order, quote_amount=scaled)
                reasons.append(
                    f"Quadrant {entry_quadrant(indicators)} sizing: quote {order.quote_amount} USDT "
                    f"(x{qmult})."
                )

    if market_context and market_context_cfg and order:
        ctx_reason = market_context_block_reason(
            market_context,
            is_reduce_only=order.reduce_only,
            block_pre_funding=market_context_cfg.block_new_opens_pre_funding,
            block_sessions=market_context_cfg.block_new_opens_sessions,
        )
        if ctx_reason and not order.reduce_only:
            blocked.append(ctx_reason)
        # Stage 4: never open a fresh position on a symbol being delisted.
        if not order.reduce_only:
            try:
                from sentiment_engine import sentiment_symbol_block_reason

                delist_reason = sentiment_symbol_block_reason(
                    market_context.get("sentiment"), symbol
                )
                if delist_reason:
                    blocked.append(delist_reason)
            except Exception:
                pass
        macro_gainer = macro_gainer_long_block_reason(
            market_context,
            order_action=order.action,
            bucket=str(indicators.get("discovery_bucket", "") or ""),
            source=str(indicators.get("discovery_source", "") or ""),
            cfg=quadrant_cfg,
        )
        if macro_gainer and not order.reduce_only:
            blocked.append(macro_gainer)
    if trade_learning and trade_learning_cfg and order:
        discovery_source = str(indicators.get("discovery_source", "") or "")
        is_discovery_open = (
            not order.reduce_only
            and portfolio.position(symbol).quantity == 0
            and discovery_source.startswith("discovery:")
        )
        learn_reason = trade_learning_block_reason(
            trade_learning,
            symbol=symbol,
            is_reduce_only=order.reduce_only,
            cfg=trade_learning_cfg,
            is_discovery_open=is_discovery_open,
        )
        if learn_reason:
            blocked.append(learn_reason)
        shadow_first, shadow_reason = trade_learning_discovery_shadow_first(
            trade_learning,
            cfg=trade_learning_cfg,
            bucket=str(indicators.get("discovery_bucket") or ""),
            is_discovery_open=is_discovery_open,
        )
        if shadow_first and shadow_reason:
            indicators["trade_learning_shadow_first"] = "true"
            indicators["trade_learning_shadow_reason"] = shadow_reason
            reasons.append(f"{shadow_reason} Live deferred to shadow paper.")
        elif shadow_reason:
            indicators["trade_learning_shadow_canary"] = "true"
            indicators["trade_learning_shadow_canary_reason"] = shadow_reason
            reasons.append(f"{shadow_reason}; live canary allowed.")

    from bucket_strategy import bucket_open_block_reasons, bucket_strategy_from_config

    bucket_cfg = bucket_strategy_cfg if bucket_strategy_cfg is not None else bucket_strategy_from_config({})
    if order and not order.reduce_only and bucket_cfg.enabled:
        bucket = str(indicators.get("discovery_bucket") or "")
        source = str(indicators.get("discovery_source") or "")
        base_min = strategy.min_sell_confidence if order.action == SELL else risk.min_confidence
        blocked.extend(
            bucket_open_block_reasons(
                cfg=bucket_cfg,
                bucket=bucket,
                source=source,
                order_action=order.action,
                reduce_only=order.reduce_only,
                indicators=indicators,
                confidence=decimal_from(signal.confidence),
                base_min_confidence=base_min,
            )
        )
        from bucket_strategy import bucket_quote_fraction_mult, normalize_bucket

        bmult = bucket_quote_fraction_mult(bucket_cfg, bucket, source)
        if bmult != Decimal("1") and order.quote_amount > 0:
            scaled = (order.quote_amount * bmult).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            if scaled != order.quote_amount:
                order = replace(order, quote_amount=scaled)
                reasons.append(
                    f"Bucket {normalize_bucket(bucket, source)} sizing: quote {order.quote_amount} USDT "
                    f"(x{bmult})."
                )

    if (
        order
        and not order.reduce_only
        and risk.max_concurrent_positions > 0
        and portfolio.position(symbol).quantity == 0
    ):
        open_count = count_open_positions(portfolio)
        if open_count >= risk.max_concurrent_positions:
            blocked.append(
                f"Max concurrent positions {risk.max_concurrent_positions} reached "
                f"({open_count} open); concentrate margin on existing book."
            )

    if order and not order.reduce_only:
        from entry_economics import discovery_short_block_reason, entry_economics_block_reason

        discovery_source = str(indicators.get("discovery_source", "") or "")
        is_discovery_open = (
            portfolio.position(symbol).quantity == 0 and discovery_source.startswith("discovery:")
        )
        is_new_open = not order.reduce_only and portfolio.position(symbol).quantity == 0
        if not bypass_discovery_short_block(indicators):
            short_block = discovery_short_block_reason(
                discovery_short_mode=strategy.discovery_short_mode,
                allow_discovery_shorts=strategy.allow_discovery_shorts,
                order_action=order.action,
                is_discovery_open=is_discovery_open,
                bucket=str(indicators.get("discovery_bucket", "") or ""),
                regime=str(indicators.get("regime", "") or ""),
                entry_quadrant=str(indicators.get("entry_quadrant", "") or ""),
                profitability_raw=profitability_raw,
            )
            if short_block:
                blocked.append(short_block)
        atr_pct = decimal_from(indicators.get("atr_pct", "0") or "0")
        tp_pct, _ = effective_stop_take_profit_pcts(strategy, auto_exec) if auto_exec else (Decimal("0.03"), Decimal("0.02"))
        econ_block = entry_economics_block_reason(
            enabled=strategy.entry_min_edge_fee_multiple > 0,
            is_new_open=is_new_open,
            quote_amount=order.quote_amount,
            atr_pct=atr_pct,
            min_edge_fee_multiple=strategy.entry_min_edge_fee_multiple,
            take_profit_pct=tp_pct,
            atr_tp_multiplier=auto_exec.atr_tp_multiplier if auto_exec else Decimal("3.5"),
            use_atr_stops=bool(auto_exec and auto_exec.use_atr_stops),
        )
        if econ_block:
            blocked.append(econ_block)

    drag_reason = daily_drag_blocked_reason(risk, memory, equity, is_reduce_only=order.reduce_only)
    if drag_reason and not order.reduce_only:
        blocked.append(drag_reason)

    if not order.reduce_only and order.quote_amount > risk.max_trade_quote:
        order = resize_order_quote(order, risk.max_trade_quote)
        reasons.append(f"Risk max trade quote applied: quote {order.quote_amount} USDT.")

    if (
        not order.reduce_only
        and risk.max_portfolio_heat_pct < Decimal("1")
        and equity > 0
        and order.quote_amount > 0
    ):
        if heat >= risk.max_portfolio_heat_pct:
            blocked.append(
                f"Portfolio heat {heat:.2%} exceeds max {risk.max_portfolio_heat_pct:.2%}; "
                "new exposure blocked (reduce-only still allowed)."
            )
        else:
            leverage_for_heat = Decimal(max(int(order.leverage or 1), 1)) if is_futures_market(order.market) else Decimal("1")
            heat_room_margin = ((risk.max_portfolio_heat_pct - heat) * equity).quantize(
                Decimal("0.00000001"), rounding=ROUND_DOWN
            )
            heat_room_notional = (heat_room_margin * leverage_for_heat).quantize(
                Decimal("0.00000001"), rounding=ROUND_DOWN
            )
            if heat_room_notional < order.quote_amount:
                if heat_room_notional >= risk.min_trade_quote:
                    order = resize_order_quote(order, heat_room_notional)
                    reasons.append(
                        f"Portfolio heat room applied: quote {order.quote_amount} USDT "
                        f"(heat {heat:.2%} -> max {risk.max_portfolio_heat_pct:.2%})."
                    )
                else:
                    blocked.append(
                        f"Portfolio heat room {heat_room_notional} USDT is below min trade quote "
                        f"{risk.min_trade_quote}; new exposure blocked."
                    )

    if order.quote_amount <= 0:
        blocked.append("Order quote amount is zero; no available cash or position.")
    if execution is not None and order is not None:
        notional_reason = open_notional_blocked_reason(order, execution)
        if notional_reason:
            blocked.append(notional_reason)
    if (
        is_spot_market(snapshot.asset.market)
        and order.action == BUY
        and not order.reduce_only
        and execution is None
        and order.quote_amount < Decimal("5")
    ):
        blocked.append(
            f"Spot buy quote {order.quote_amount} USDT is below Binance min notional (~5 USDT); order skipped."
        )
    if is_futures_market(snapshot.asset.market) and not order.reduce_only:
        available_margin = portfolio.available_cash(snapshot.asset.quote_asset, snapshot.asset.market)
        if available_margin < risk.reserve_futures_available_usdt:
            blocked.append(
                f"Futures available margin {available_margin} is below reserve {risk.reserve_futures_available_usdt}; "
                "new opens blocked (reduce-only closes still allowed)."
            )
        if not risk.allow_futures_open:
            blocked.append("Futures open orders are disabled in risk config (allow_futures_open=false).")
    projected_value = portfolio.position_value(symbol, snapshot.price)
    if order.action == BUY and not order.reduce_only:
        projected_value += order.quote_amount
    if not order.reduce_only and projected_value > risk.max_position_quote:
        blocked.append(f"Projected position value {projected_value} exceeds max position quote {risk.max_position_quote}.")

    position_qty = portfolio.position(symbol).quantity
    change_24h = effective_price_change_pct_24h(indicators)
    if order and not order.reduce_only and is_futures_market(snapshot.asset.market):
        if order.action == BUY and position_qty > 0:
            if not strategy.allow_pyramid_adds:
                blocked.append(
                    f"Pyramid add blocked: already long {symbol} qty={position_qty}; "
                    "allow_pyramid_adds=false."
                )
            elif strategy.max_adds_per_symbol_per_day > 0:
                add_key = f"{symbol}:{utc_today_key()}"
                if memory.daily_symbol_add_counts.get(add_key, 0) >= strategy.max_adds_per_symbol_per_day:
                    blocked.append(
                        f"Daily pyramid cap {strategy.max_adds_per_symbol_per_day} reached for {symbol}."
                    )
            if (
                strategy.pump_guard_min_change_24h_pct > 0
                and change_24h >= strategy.pump_guard_min_change_24h_pct
            ):
                blocked.append(
                    f"Pump guard: {symbol} {format_pct_ratio(change_24h)} in 24h with existing long; "
                    "no add on extended move."
                )
        if order.action == SELL and position_qty < 0:
            if not strategy.allow_pyramid_adds:
                blocked.append(
                    f"Pyramid add blocked: already short {symbol} qty={position_qty}; "
                    "allow_pyramid_adds=false."
                )
            elif strategy.max_adds_per_symbol_per_day > 0:
                add_key = f"{symbol}:{utc_today_key()}"
                if memory.daily_symbol_add_counts.get(add_key, 0) >= strategy.max_adds_per_symbol_per_day:
                    blocked.append(
                        f"Daily pyramid cap {strategy.max_adds_per_symbol_per_day} reached for {symbol}."
                    )
            if (
                strategy.pump_guard_min_change_24h_pct > 0
                and change_24h <= -strategy.pump_guard_min_change_24h_pct
            ):
                blocked.append(
                    f"Pump guard: {symbol} {format_pct_ratio(change_24h)} in 24h with existing short; "
                    "no add on extended move."
                )
        if order.action == BUY and position_qty == 0 and strategy.pump_guard_block_new_open_pct > 0:
            if change_24h >= strategy.pump_guard_block_new_open_pct:
                blocked.append(
                    f"Pump guard: {symbol} {format_pct_ratio(change_24h)} 24h exceeds "
                    f"{format_pct_ratio(strategy.pump_guard_block_new_open_pct)}; new long blocked."
                )
        if order.action == SELL and position_qty == 0 and strategy.pump_guard_block_new_open_pct > 0:
            if change_24h <= -strategy.pump_guard_block_new_open_pct:
                blocked.append(
                    f"Pump guard: {symbol} {format_pct_ratio(change_24h)} 24h exceeds "
                    f"{format_pct_ratio(-strategy.pump_guard_block_new_open_pct)}; new short blocked."
                )
            if change_24h >= strategy.pump_guard_block_new_open_pct:
                if entry_quadrant(indicators) != QUADRANT_REVERSAL_SHORT:
                    blocked.append(
                        f"Pump guard: {symbol} {format_pct_ratio(change_24h)} 24h pump — "
                        f"new short blocked (symmetric pump guard)."
                    )

    lessons_cfg = trade_lessons_cfg if trade_lessons_cfg is not None else trade_lessons_from_config({})
    if order and not order.reduce_only and lessons_cfg.enabled:
        momentum = decimal_from(indicators.get("momentum", "0"))
        rsi = decimal_from(indicators.get("rsi", "0"))
        conf = decimal_from(signal.confidence)
        bucket = str(indicators.get("discovery_bucket") or indicators.get("discovery_source") or "")
        lesson_blocks = trade_lesson_block_reasons(
            cfg=lessons_cfg,
            order_action=order.action,
            reduce_only=order.reduce_only,
            position_qty=position_qty,
            regime=str(indicators.get("regime", "")),
            change_24h=change_24h,
            momentum=momentum,
            rsi=rsi,
            confidence=conf,
            bucket=bucket,
            fusion_bull_pct=decimal_from(indicators.get("fusion_bull_pct", "0") or "0"),
            entry_quadrant=str(indicators.get("entry_quadrant", "") or ""),
            mtf_1m=str(indicators.get("mtf_1m", "") or ""),
            mtf_5m=str(indicators.get("mtf_5m", "") or ""),
            mtf_15m=str(indicators.get("mtf_15m", "") or ""),
            lesson_stats=(trade_lessons or {}).get("ruleStats") if trade_lessons else None,
        )
        if skip_gainers_short_lessons(indicators):
            lesson_blocks = [
                reason
                for reason in lesson_blocks
                if "gainers bucket" not in reason.lower()
                and "block short on pump" not in reason.lower()
            ]
        blocked.extend(lesson_blocks)

    # Multi-persona hedging review (meta-layer). Runs only for an otherwise-approved
    # OPEN: it can veto (append a blocked reason) or downgrade (shrink the order).
    # It never un-blocks a trade. The verdict is written into indicators so it is
    # captured in the decision ledger for full auditability.
    if (
        persona_council_cfg is not None
        and getattr(persona_council_cfg, "enabled", False)
        and not blocked
        and order is not None
        and not order.reduce_only
        and signal.action in {BUY, SELL}
    ):
        from persona_council import evaluate_council, VERDICT_VETO, VERDICT_DOWNGRADE

        verdict = evaluate_council(
            signal.action,
            dict(indicators),
            persona_council_cfg,
            context=market_context,
        )
        try:
            indicators["personaCouncil"] = verdict.to_dict()
        except Exception:
            pass
        if verdict.verdict == VERDICT_VETO:
            blocked.append("Persona council veto: " + "; ".join(verdict.reasons))
        elif verdict.verdict == VERDICT_DOWNGRADE and verdict.size_multiplier < 1:
            new_quote = (order.quote_amount * verdict.size_multiplier).quantize(
                Decimal("0.00000001"), rounding=ROUND_DOWN
            )
            order = replace(order, quote_amount=new_quote)
            reasons.append(
                f"Persona council downgrade: size x{verdict.size_multiplier} ({'; '.join(verdict.reasons)})"
            )

    if live_blocked:
        for reason in live_blocked:
            if reason not in blocked:
                blocked.append(reason)

    return RiskDecision(not blocked, signal.action if not blocked else BLOCKED, reasons, blocked, order, risk)


def _entry_timing_rationale_block(order: OrderIntent | None, indicators: dict[str, str] | None) -> dict[str, Any] | None:
    if order is None or order.reduce_only:
        return None
    market_price = order.estimated_price
    if market_price <= 0:
        return None
    block: dict[str, Any] = {
        "orderType": order.order_type,
        "quadrant": (indicators or {}).get("entry_quadrant"),
        "mtf5m": (indicators or {}).get("mtf_5m"),
    }
    if order.limit_price is not None and order.order_type == ORDER_LIMIT:
        lp = order.limit_price
        block["limitPrice"] = str(lp)
        block["marketPrice"] = str(market_price)
        if order.action == BUY:
            block["offsetPct"] = f"{((market_price - lp) / market_price):.4f}"
        else:
            block["offsetPct"] = f"{((lp - market_price) / market_price):.4f}"
    return block


def build_trade_rationale(
    snapshot: MarketSnapshot,
    signal: Signal,
    decision: RiskDecision,
    risk: RiskConfig,
    portfolio: PaperPortfolio,
    memory: TradingMemory,
    market_context: dict[str, Any] | None = None,
    trade_learning: dict[str, Any] | None = None,
    trade_lessons: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from growth_sizing import growth_rationale_block
    from market_context import market_context_rationale_block
    from trade_outcomes import trade_learning_rationale_block
    from trade_lessons import trade_lessons_rationale_block

    symbol = normalize_symbol(snapshot.asset.symbol)
    # Mirror the effective cap used by the enforcement gate so the ledger line is
    # not misleading when the profit-adaptive bonus is active.
    eff_cap, eff_bonus = profit_adaptive_effective_cap(
        risk.max_daily_trades, memory.daily_realized_net_today(), risk.profit_adaptive_daily_cap
    )
    emotion_guardrails = [
        (
            (
                f"Daily trade cap {eff_cap}"
                + (f" (base {risk.max_daily_trades} +{eff_bonus} profit-adaptive)" if eff_bonus else "")
                + f"; used {memory.count_for_today()} today."
            )
            if risk.max_daily_trades > 0
            else f"Daily trades today: {memory.count_for_today()} (no cap)."
        ),
        f"Cooldown {risk.cooldown_seconds}s between trades on the same symbol reduces impulsive re-entry.",
        "Hard risk gates execute before any order; emotional override is not possible in autonomous mode.",
    ]
    last_trade_at = memory.last_trade_at.get(symbol)
    if last_trade_at:
        elapsed = snapshot.observed_at - last_trade_at
        emotion_guardrails.append(f"Last trade on {symbol} was {elapsed}s ago; cooldown may still apply.")

    age = position_age_seconds(memory, symbol, snapshot.observed_at)
    pos = portfolio.positions.get(symbol)
    if pos and pos.quantity != 0:
        peak_key = position_peak_key(symbol, pos.quantity)
        peak_raw = memory.position_peak_price.get(peak_key)
        peak_price = decimal_from(peak_raw) if peak_raw else None
        entry = pos.average_price
        mark = snapshot.price
        unrealized_pct = None
        peak_pnl_pct = None
        giveback_from_peak_pct = None
        if entry > 0 and mark > 0:
            if pos.quantity > 0:
                unrealized_pct = (mark - entry) / entry
            else:
                unrealized_pct = (entry - mark) / entry
        if peak_price and peak_price > 0 and entry > 0:
            if pos.quantity > 0:
                peak_pnl_pct = (peak_price - entry) / entry
                giveback_from_peak_pct = (peak_price - mark) / peak_price if mark > 0 else None
            else:
                peak_pnl_pct = (entry - peak_price) / entry
                giveback_from_peak_pct = (mark - peak_price) / peak_price if mark > 0 else None
        rationale_extra: dict[str, Any] = {
            "positionHolding": {
                "ageSeconds": age,
                "ageHours": f"{(age or 0) / 3600:.2f}" if age is not None else None,
                "quantity": str(pos.quantity),
                "leverage": pos.leverage or None,
                "entryPrice": str(entry),
                "markPrice": str(mark),
                "unrealizedPct": f"{unrealized_pct:.4f}" if unrealized_pct is not None else None,
                "peakPrice": str(peak_price) if peak_price else None,
                "peakPnlPct": f"{peak_pnl_pct:.4f}" if peak_pnl_pct is not None else None,
                "givebackFromPeakPct": (
                    f"{giveback_from_peak_pct:.4f}" if giveback_from_peak_pct is not None else None
                ),
            }
        }
    else:
        rationale_extra = {}

    rationale = {
        "summary": (
            f"{decision.action} {symbol} @ {snapshot.price} "
            f"(confidence={signal.confidence}, status={'approved' if decision.approved else 'blocked'})"
        ),
        "growthMetrics": growth_rationale_block(portfolio, risk, symbol, snapshot.price, memory),
        "entryQuadrant": signal.indicators.get("entry_quadrant"),
        "entryQuadrantMode": signal.indicators.get("entry_quadrant_mode"),
        "entryTiming": _entry_timing_rationale_block(decision.order, signal.indicators),
        "marketRegime": {
            "kind": signal.indicators.get("regime", "unknown"),
            "adx": signal.indicators.get("adx"),
            "bbWidth": signal.indicators.get("bb_width"),
            "atrPct": signal.indicators.get("atr_pct"),
        },
        "multiTimeframe": {
            "1m": signal.indicators.get("mtf_1m"),
            "5m": signal.indicators.get("mtf_5m"),
            "15m": signal.indicators.get("mtf_15m"),
        },
        "signalFactors": list(signal.reasons),
        "blockedFactors": list(decision.blocked_reasons),
        "warnings": list(signal.warnings),
        "confidence": str(signal.confidence),
        "emotionGuardrails": emotion_guardrails,
        "portfolioSnapshot": portfolio.snapshot(),
        "marketContext": market_context_rationale_block(market_context or {}),
        "tradeLearning": trade_learning_rationale_block(trade_learning or {}),
        "tradeLessons": trade_lessons_rationale_block(trade_lessons or {}),
        "macroView": (market_context or {}).get("macro"),
        **rationale_extra,
    }
    if decision.order and decision.order.leverage:
        rationale["leveragePlan"] = {
            "applied": decision.order.leverage,
            "default": None,
            "note": "6-8x only when confidence>=0.75 + trend + MTF alignment",
        }
    return rationale


def execute_paper(
    snapshot: MarketSnapshot,
    decision: RiskDecision,
    signal: Signal,
    strategy: StrategyConfig,
    risk: RiskConfig,
    portfolio: PaperPortfolio,
    memory: TradingMemory,
    trade_rationale: dict[str, Any] | None = None,
) -> ExecutionRecord:
    asset = snapshot.asset
    price = snapshot.price
    symbol = normalize_symbol(asset.symbol)
    quantity = Decimal("0")
    quote_amount = Decimal("0")
    status = "blocked"

    if decision.approved and decision.order:
        if decision.order.action == BUY:
            quantity = portfolio.buy(asset, price, decision.order.quote_amount)
            quote_amount = (quantity * price).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        elif decision.order.action == SELL:
            quantity = portfolio.sell(asset, price, strategy.sell_position_fraction)
            quote_amount = (quantity * price).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        if quantity > 0:
            status = "executed"
            memory.record_trade(symbol, snapshot.observed_at)
        else:
            status = "skipped"

    return ExecutionRecord(
        status=status,
        mode=risk.mode,
        symbol=symbol,
        action=decision.action,
        quantity=quantity,
        quote_amount=quote_amount,
        price=price,
        reasons=decision.reasons,
        blocked_reasons=decision.blocked_reasons,
        indicators=signal.indicators,
        portfolio=portfolio.snapshot(),
        timestamp=snapshot.observed_at,
        trade_rationale=trade_rationale or {},
    )


def request_json(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    timeout_seconds: int = 10,
) -> Any:
    from proxy_http import request_json as proxy_request_json

    return proxy_request_json(
        url,
        method=method,
        headers=headers,
        timeout_seconds=timeout_seconds,
    )


def sign_query(params: dict[str, Any], api_secret: str) -> str:
    query = urllib.parse.urlencode(params)
    signature = hmac.new(api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{query}&signature={signature}"


def get_server_time(base_url: str, time_path: str = "/api/v3/time", timeout_seconds: int = 10) -> int:
    global _SERVER_TIME_OFFSET_MS
    if _SERVER_TIME_OFFSET_MS is not None:
        return int(time.time() * 1000) + _SERVER_TIME_OFFSET_MS
    local_before = int(time.time() * 1000)
    payload = request_json(f"{base_url.rstrip('/')}{time_path}", timeout_seconds=timeout_seconds)
    server_time = int(payload["serverTime"])
    local_after = int(time.time() * 1000)
    _SERVER_TIME_OFFSET_MS = server_time - (local_before + local_after) // 2
    return server_time


def reset_server_time_offset() -> None:
    global _SERVER_TIME_OFFSET_MS, _HEDGE_MODE_CACHE
    _SERVER_TIME_OFFSET_MS = None
    _HEDGE_MODE_CACHE = None


def execution_base_url(execution: ExecutionConfig, market: str) -> str:
    if is_futures_market(market):
        return execution.binance_futures_base_url.rstrip("/")
    return execution.binance_base_url.rstrip("/")


def execution_time_path(market: str) -> str:
    return "/fapi/v1/time" if is_futures_market(market) else "/api/v3/time"


def decimal_to_api(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def client_order_id_for(order: OrderIntent, *, now: int | None = None, bucket_seconds: int = 45) -> str:
    """Deterministic newClientOrderId for an order (BIN-001 idempotency).

    Binance dedupes orders that carry the same clientOrderId, so a resend after a
    timestamp/network error (signed_binance_request retry) cannot create a second
    fill. The id is stable within a short time bucket — the same logical intent
    retried within bucket_seconds reuses the id; a genuinely new intent next cycle
    gets a fresh one. Binance futures clientOrderId allows [A-Za-z0-9_-] up to 36.
    """
    now = int(time.time()) if now is None else now
    bucket = now // max(bucket_seconds, 1)
    raw = "|".join(
        [
            normalize_symbol(order.symbol),
            str(order.action),
            str(order.intent_kind),
            "ro" if order.reduce_only else "open",
            decimal_to_api(order.quantity) if order.quantity is not None else decimal_to_api(order.quote_amount),
            str(bucket),
        ]
    )
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
    return f"mat-{digest}"


def kill_switch_active(path: str) -> bool:
    return Path(path).exists()


def _parse_arm_expiry(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        normalized = text.replace("Z", "+00:00") if text.endswith("Z") else text
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def live_trading_armed(path: str) -> bool:
    arm_path = Path(path)
    if not arm_path.exists():
        return False
    try:
        text = arm_path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not text:
        return True
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return True
    if not isinstance(payload, dict):
        return True
    expiry = _parse_arm_expiry(payload.get("expires_at") or payload.get("expiresAt"))
    if expiry is not None and time.time() >= expiry:
        return False
    return True


def live_trading_arm_status(path: str) -> dict[str, Any]:
    arm_path = Path(path)
    exists = arm_path.exists()
    status: dict[str, Any] = {"path": path, "exists": exists, "armed": False}
    if not exists:
        status["reason"] = "missing"
        return status
    try:
        text = arm_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        status["reason"] = f"unreadable:{exc}"
        return status
    if not text:
        status["armed"] = True
        status["format"] = "empty"
        return status
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        status["armed"] = True
        status["format"] = "legacy_text"
        return status
    if not isinstance(payload, dict):
        status["armed"] = True
        status["format"] = "legacy_json"
        return status
    expiry_raw = payload.get("expires_at") or payload.get("expiresAt")
    expiry = _parse_arm_expiry(expiry_raw)
    if expiry is not None:
        status["expiresAt"] = expiry_raw
        status["expiresAtEpoch"] = int(expiry)
        if time.time() >= expiry:
            status["reason"] = "expired"
            return status
    status["armed"] = True
    status["format"] = "json"
    if payload.get("armed_by") or payload.get("armedBy"):
        status["armedBy"] = payload.get("armed_by") or payload.get("armedBy")
    if payload.get("armed_at") or payload.get("armedAt"):
        status["armedAt"] = payload.get("armed_at") or payload.get("armedAt")
    return status


def resolve_api_credentials(execution: ExecutionConfig) -> tuple[str, str]:
    api_key = execution.api_key or env_value("BINANCE_API_KEY")
    api_secret = execution.api_secret or env_value("BINANCE_API_SECRET")
    return api_key, api_secret


def signed_binance_request(
    execution: ExecutionConfig,
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    market: str = "binance_spot",
) -> Any:
    api_key, api_secret = resolve_api_credentials(execution)
    if not api_key or not api_secret:
        raise RuntimeError("BINANCE_API_KEY and BINANCE_API_SECRET must be set for signed Binance requests.")
    base_url = execution_base_url(execution, market)
    signed_params = dict(params or {})
    signed_params["recvWindow"] = execution.recv_window
    signed_params["timestamp"] = get_server_time(
        base_url,
        execution_time_path(market),
        timeout_seconds=execution.timeout_seconds,
    )
    query = sign_query(signed_params, api_secret)
    url = f"{base_url}{path}?{query}"
    try:
        return request_json(
            url,
            method=method,
            headers={"X-MBX-APIKEY": api_key},
            timeout_seconds=execution.timeout_seconds,
        )
    except RuntimeError as exc:
        if "recvWindow" not in str(exc) and "Timestamp" not in str(exc):
            raise
        # A recvWindow/Timestamp rejection is checked by Binance BEFORE matching,
        # so the order did not reach the engine — safe to re-sign and resend. If
        # params carry a newClientOrderId (BIN-001), the resend is idempotent even
        # if that assumption were ever wrong: Binance dedupes the duplicate id.
        reset_server_time_offset()
        signed_params["timestamp"] = get_server_time(
            base_url,
            execution_time_path(market),
            timeout_seconds=execution.timeout_seconds,
        )
        query = sign_query(signed_params, api_secret)
        url = f"{base_url}{path}?{query}"
        try:
            return request_json(
                url,
                method=method,
                headers={"X-MBX-APIKEY": api_key},
                timeout_seconds=execution.timeout_seconds,
            )
        except RuntimeError as retry_exc:
            # Duplicate clientOrderId on resend means the ORIGINAL actually landed.
            # Surface that instead of masking a live position as a failure.
            msg = str(retry_exc)
            if "-4015" in msg or "Duplicate" in msg or "duplicate" in msg:
                client_id = signed_params.get("newClientOrderId")
                if client_id:
                    return {
                        "status": "duplicate_client_order_id",
                        "clientOrderId": client_id,
                        "note": "original order already accepted; not resent",
                    }
            raise


def fetch_spot_account(execution: ExecutionConfig) -> dict[str, Any]:
    payload = signed_binance_request(execution, "GET", "/api/v3/account", {"omitZeroBalances": "true"}, market="binance_spot")
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected account payload: {payload!r}")
    return payload


def fetch_futures_account(execution: ExecutionConfig) -> dict[str, Any]:
    payload = signed_binance_request(execution, "GET", "/fapi/v3/account", {}, market="binance_futures")
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected futures account payload: {payload!r}")
    return payload


def fetch_futures_positions(execution: ExecutionConfig) -> list[dict[str, Any]]:
    payload = signed_binance_request(execution, "GET", "/fapi/v3/positionRisk", {}, market="binance_futures")
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected futures positions payload: {payload!r}")
    return payload


def portfolio_from_binance_account(assets: list[AssetConfig], account: dict[str, Any]) -> PaperPortfolio:
    spot_assets = [asset for asset in assets if is_spot_market(asset.market)]
    balances = {item.get("asset", "").upper(): decimal_from(item.get("free", "0")) for item in account.get("balances", [])}
    cash: dict[str, Decimal] = {}
    positions: dict[str, Position] = {}
    for asset in spot_assets:
        quote = asset.quote_asset.upper()
        base = asset.base_asset.upper()
        cash[quote] = max(cash.get(quote, Decimal("0")), balances.get(quote, Decimal("0")))
        quantity = balances.get(base, Decimal("0"))
        if quantity > 0:
            positions[normalize_symbol(asset.symbol)] = Position(quantity=quantity, average_price=Decimal("0"))
    return PaperPortfolio(cash=cash, positions=positions)


def _position_from_futures_rows(rows: list[dict[str, Any]]) -> Position | None:
    quantity = Decimal("0")
    entry = Decimal("0")
    notional = Decimal("0")
    initial_margin = Decimal("0")
    leverage = 0
    for row in rows:
        qty = decimal_from(row.get("positionAmt", "0"))
        if qty == 0:
            continue
        side = str(row.get("positionSide", "BOTH")).upper()
        if side == "SHORT" and qty > 0:
            qty = -qty
        quantity += qty
        entry = decimal_from(row.get("entryPrice", "0"))
        row_notional = abs(decimal_from(row.get("notional", "0")))
        notional += row_notional
        row_margin = decimal_from(row.get("initialMargin", row.get("isolatedMargin", "0")))
        initial_margin += row_margin
        lev_raw = row.get("leverage")
        if lev_raw not in (None, "", 0, "0"):
            leverage = int(decimal_from(lev_raw))
    if quantity == 0:
        return None
    if initial_margin <= 0 and notional > 0 and leverage > 0:
        initial_margin = (notional / Decimal(leverage)).quantize(Decimal("0.00000001"))
    return Position(
        quantity=quantity,
        average_price=entry,
        initial_margin=initial_margin,
        notional=notional,
        leverage=leverage,
    )


def portfolio_from_futures_account(
    assets: list[AssetConfig],
    account: dict[str, Any],
    position_rows: list[dict[str, Any]],
) -> PaperPortfolio:
    available = decimal_from(account.get("availableBalance", "0"))
    wallet_balance = decimal_from(account.get("totalWalletBalance", account.get("availableBalance", "0")))
    cash = {CASH_USDT_FUTURES: available}
    positions: dict[str, Position] = {}
    rows_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in position_rows:
        sym = normalize_symbol(str(row.get("symbol", "")))
        if sym:
            rows_by_symbol.setdefault(sym, []).append(row)
    for symbol, rows in rows_by_symbol.items():
        pos = _position_from_futures_rows(rows)
        if pos is not None:
            positions[symbol] = pos
    return PaperPortfolio(cash=cash, positions=positions, wallet_balance=wallet_balance)


def fetch_live_portfolio(execution: ExecutionConfig, assets: list[AssetConfig]) -> PaperPortfolio:
    spot_assets = [asset for asset in assets if is_spot_market(asset.market)]
    futures_assets = [asset for asset in assets if is_futures_market(asset.market)]
    cash: dict[str, Decimal] = {}
    positions: dict[str, Position] = {}
    wallet_balance: Decimal | None = None
    if spot_assets:
        spot_portfolio = portfolio_from_binance_account(spot_assets, fetch_spot_account(execution))
        cash[CASH_USDT_SPOT] = spot_portfolio.available_cash("USDT")
        positions.update(spot_portfolio.positions)
    if futures_assets:
        futures_portfolio = portfolio_from_futures_account(
            futures_assets,
            fetch_futures_account(execution),
            fetch_futures_positions(execution),
        )
        cash[CASH_USDT_FUTURES] = futures_portfolio.available_cash("USDT", futures_assets[0].market)
        positions.update(futures_portfolio.positions)
        wallet_balance = futures_portfolio.wallet_balance
    return PaperPortfolio(cash=cash, positions=positions, wallet_balance=wallet_balance)


_SYMBOL_FILTER_CACHE: dict[str, dict[str, Decimal]] = {}
_SERVER_TIME_OFFSET_MS: int | None = None
_HEDGE_MODE_CACHE: bool | None = None
_RUNTIME_FINGERPRINT_CACHE: dict[str, Any] | None = None


def runtime_fingerprint() -> dict[str, Any]:
    """Small runtime fingerprint for verifying which source a live process loaded."""
    global _RUNTIME_FINGERPRINT_CACHE
    if _RUNTIME_FINGERPRINT_CACHE is not None:
        return dict(_RUNTIME_FINGERPRINT_CACHE)
    source_path = Path(__file__).resolve()
    try:
        source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()[:16]
    except OSError:
        source_sha = "unavailable"
    commit = os.environ.get("MARKET_AUTOTRADER_COMMIT", "").strip()
    if not commit:
        for git_dir in (source_path.parent / ".git", Path("/tmp/biancharge-upload/.git")):
            head = git_dir / "HEAD"
            try:
                raw_head = head.read_text(encoding="utf-8").strip()
                if raw_head.startswith("ref:"):
                    ref_path = git_dir / raw_head.split(" ", 1)[1]
                    commit = ref_path.read_text(encoding="utf-8").strip()[:12]
                else:
                    commit = raw_head[:12]
                if commit:
                    break
            except OSError:
                continue
    _RUNTIME_FINGERPRINT_CACHE = {
        "sourceFile": source_path.name,
        "sourceSha": source_sha,
        "commit": commit or "unknown",
    }
    return dict(_RUNTIME_FINGERPRINT_CACHE)


def fetch_futures_hedge_mode(execution: ExecutionConfig) -> bool:
    """True when account uses dual-side (hedge) position mode."""
    global _HEDGE_MODE_CACHE
    if _HEDGE_MODE_CACHE is not None:
        return _HEDGE_MODE_CACHE
    api_key, api_secret = resolve_api_credentials(execution)
    if not api_key or not api_secret:
        _HEDGE_MODE_CACHE = False
        return False
    payload = signed_binance_request(
        execution,
        "GET",
        "/fapi/v1/positionSide/dual",
        {},
        market="binance_futures",
    )
    dual = payload.get("dualSidePosition") if isinstance(payload, dict) else False
    _HEDGE_MODE_CACHE = bool(dual)
    return _HEDGE_MODE_CACHE


def futures_position_side(order: OrderIntent) -> str:
    if order.intent_kind in {INTENT_OPEN_LONG, INTENT_REDUCE_LONG, INTENT_CLOSE_LONG}:
        return "LONG"
    if order.intent_kind in {INTENT_OPEN_SHORT, INTENT_REDUCE_SHORT, INTENT_CLOSE_SHORT}:
        return "SHORT"
    if order.reduce_only:
        return "LONG" if order.action == SELL else "SHORT"
    return "LONG" if order.action == BUY else "SHORT"


def fetch_symbol_filters(symbol: str, execution: ExecutionConfig, market: str = "binance_spot") -> dict[str, Decimal]:
    normalized = normalize_symbol(symbol)
    cache_key = f"{execution.mode}:{execution_base_url(execution, market)}:{market}:{normalized}"
    if cache_key in _SYMBOL_FILTER_CACHE:
        return _SYMBOL_FILTER_CACHE[cache_key]
    if execution.mode in {PAPER, APPROVAL_REQUIRED}:
        filters = {
            "step_size": Decimal("0.00000001"),
            "min_qty": Decimal("0"),
            "min_notional": Decimal("0"),
            "tick_size": Decimal("0.01"),
        }
        _SYMBOL_FILTER_CACHE[cache_key] = filters
        return filters
    base_url = execution_base_url(execution, market)
    info_path = "/fapi/v1/exchangeInfo" if is_futures_market(market) else "/api/v3/exchangeInfo"
    encoded_symbol = urllib.parse.quote(normalized, safe="")
    payload = request_json(
        f"{base_url}{info_path}?symbol={encoded_symbol}",
        timeout_seconds=execution.timeout_seconds,
    )
    symbols = payload.get("symbols", []) if isinstance(payload, dict) else []
    if not symbols:
        raise RuntimeError(f"Could not load exchange filters for {normalized}.")
    symbol_row = next((row for row in symbols if row.get("symbol") == normalized), None)
    if symbol_row is None:
        if len(symbols) == 1:
            symbol_row = symbols[0]
        else:
            raise RuntimeError(
                f"Symbol {normalized} not found in exchangeInfo ({len(symbols)} symbols returned)."
            )
    filters: dict[str, Decimal] = {}
    for item in symbol_row.get("filters", []):
        filter_type = item.get("filterType")
        if filter_type == "LOT_SIZE":
            filters["step_size"] = decimal_from(item.get("stepSize", "0.00000001"))
            filters["min_qty"] = decimal_from(item.get("minQty", "0"))
        elif filter_type in {"MIN_NOTIONAL", "NOTIONAL"}:
            filters["min_notional"] = decimal_from(item.get("minNotional", item.get("notional", "0")))
        elif filter_type == "PRICE_FILTER":
            filters["tick_size"] = decimal_from(item.get("tickSize", "0.01"))
    _SYMBOL_FILTER_CACHE[cache_key] = filters
    return filters


def quantize_price(value: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return value.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    steps = (value / tick).to_integral_value(rounding=ROUND_DOWN)
    return (steps * tick).quantize(tick, rounding=ROUND_DOWN)


def resolved_order_type(order: OrderIntent, execution: ExecutionConfig) -> str:
    if order.order_type and order.order_type != ORDER_MARKET:
        return order.order_type.upper()
    return execution.order_type.upper()


def set_futures_leverage(symbol: str, leverage: int, execution: ExecutionConfig) -> dict[str, Any]:
    payload = signed_binance_request(
        execution,
        "POST",
        "/fapi/v1/leverage",
        {"symbol": normalize_symbol(symbol), "leverage": int(leverage)},
        market="binance_futures",
    )
    return {"endpoint": "/fapi/v1/leverage", "response": payload, "leverage": leverage}


def build_binance_spot_order_params(order: OrderIntent, execution: ExecutionConfig) -> dict[str, Any]:
    order_type = resolved_order_type(order, execution)
    params: dict[str, Any] = {
        "symbol": normalize_symbol(order.symbol),
        "side": order.action,
        "type": order_type,
    }
    filters = fetch_symbol_filters(order.symbol, execution, order.market)
    if order_type == ORDER_MARKET:
        if order.action == BUY:
            quote_amount = order.quote_amount
            min_notional = filters.get("min_notional", Decimal("0"))
            if min_notional and quote_amount < min_notional:
                raise ValueError(f"Quote amount {quote_amount} below min notional {min_notional}.")
            params["quoteOrderQty"] = decimal_to_api(quote_amount)
        elif order.action == SELL:
            params["quantity"] = decimal_to_api(order_quantity_from_intent(order, execution))
        return params
    if order_type == ORDER_LIMIT and order.action == BUY:
        quote_amount = order.quote_amount
        min_notional = filters.get("min_notional", Decimal("0"))
        if min_notional and quote_amount < min_notional:
            raise ValueError(f"Quote amount {quote_amount} below min notional {min_notional}.")
        quantity = order_quantity_from_intent(order, execution)
        limit_price = order.limit_price or order.estimated_price
        tick = filters.get("tick_size", Decimal("0.01"))
        params["timeInForce"] = order.time_in_force
        params["quantity"] = decimal_to_api(quantity)
        params["price"] = decimal_to_api(quantize_price(limit_price, tick))
        return params
    if order_type == ORDER_LIMIT:
        quantity = order_quantity_from_intent(order, execution)
        limit_price = order.limit_price or order.estimated_price
        tick = filters.get("tick_size", Decimal("0.01"))
        params["timeInForce"] = order.time_in_force
        params["quantity"] = decimal_to_api(quantity)
        params["price"] = decimal_to_api(quantize_price(limit_price, tick))
        return params
    raise ValueError(f"Unsupported spot order type: {order_type}")


def build_binance_futures_order_params(
    order: OrderIntent,
    execution: ExecutionConfig,
    *,
    order_type: str | None = None,
    stop_price: Decimal | None = None,
    reduce_only: bool | None = None,
    working_type: str = "MARK_PRICE",
) -> dict[str, Any]:
    resolved_type = (order_type or resolved_order_type(order, execution)).upper()
    params: dict[str, Any] = {
        "symbol": normalize_symbol(order.symbol),
        "side": order.action,
        "type": resolved_type,
    }
    filters = fetch_symbol_filters(order.symbol, execution, order.market)
    tick = filters.get("tick_size", Decimal("0.01"))
    use_reduce_only = order.reduce_only if reduce_only is None else reduce_only
    if resolved_type == ORDER_MARKET:
        params["quantity"] = decimal_to_api(order_quantity_from_intent(order, execution))
    elif resolved_type == ORDER_LIMIT:
        params["timeInForce"] = order.time_in_force
        params["quantity"] = decimal_to_api(order_quantity_from_intent(order, execution))
        params["price"] = decimal_to_api(quantize_price(order.limit_price or order.estimated_price, tick))
    elif resolved_type in {ORDER_STOP_MARKET, ORDER_TAKE_PROFIT_MARKET}:
        trigger = stop_price or order.stop_price or order.take_profit_price
        if trigger is None:
            raise ValueError(f"{resolved_type} requires stop/trigger price.")
        params["quantity"] = decimal_to_api(order_quantity_from_intent(order, execution))
        params["stopPrice"] = decimal_to_api(quantize_price(trigger, tick))
        params["workingType"] = working_type
    else:
        raise ValueError(f"Unsupported futures order type: {resolved_type}")
    hedge_mode = fetch_futures_hedge_mode(execution)
    if hedge_mode:
        params["positionSide"] = futures_position_side(order)
    elif use_reduce_only:
        params["reduceOnly"] = "true"
    return params


def fetch_futures_mark_price(symbol: str, execution: ExecutionConfig) -> Decimal | None:
    """Current mark price for a USDM futures symbol, or None on any failure."""
    try:
        base = execution.binance_futures_base_url.rstrip("/")
        payload = request_json(
            f"{base}/fapi/v1/premiumIndex?symbol={normalize_symbol(symbol)}",
            timeout_seconds=execution.timeout_seconds,
        )
        if isinstance(payload, dict) and payload.get("markPrice") is not None:
            mark = decimal_from(payload.get("markPrice"))
            return mark if mark > 0 else None
    except Exception:
        return None
    return None


def slippage_guard_reason(
    order: OrderIntent,
    execution: ExecutionConfig,
    auto_exec: AutoExecutionConfig,
) -> str | None:
    """BIN-008: block a MARKET entry whose live mark price has drifted beyond
    max_slippage_bps from the decision price. Returns a reason string to block, or
    None to allow. Exits (reduce_only) and non-MARKET orders are never blocked."""
    if auto_exec.max_slippage_bps <= 0:
        return None
    if order.reduce_only or order.order_type != ORDER_MARKET:
        return None
    if not is_futures_market(order.market):
        return None
    reference = order.estimated_price or order.limit_price
    if not reference or reference <= 0:
        return None
    mark = fetch_futures_mark_price(order.symbol, execution)
    if mark is None or mark <= 0:
        # Cannot verify price -> do not block on missing data here; the order still
        # passes normal gates. (Returning a reason would block on any API hiccup.)
        return None
    drift_bps = (abs(mark - reference) / reference) * Decimal("10000")
    if drift_bps > auto_exec.max_slippage_bps:
        return (
            f"Slippage guard: {order.symbol} mark {mark} drifted {drift_bps:.1f}bps from "
            f"decision price {reference} (max {auto_exec.max_slippage_bps}bps); MARKET entry blocked."
        )
    return None


def submit_binance_spot_order(order: OrderIntent, execution: ExecutionConfig) -> dict[str, Any]:
    params = build_binance_spot_order_params(order, execution)
    # BIN-001: idempotency key (spot uses newClientOrderId; default respType FULL
    # already returns fills). BIN-002: ask for FULL explicitly to be safe.
    client_id = client_order_id_for(order)
    params["newClientOrderId"] = client_id
    params["newOrderRespType"] = "FULL"
    payload = signed_binance_request(execution, "POST", "/api/v3/order", params, market=order.market)
    return {
        "endpoint": "/api/v3/order",
        "response": payload,
        "orderType": params.get("type"),
        "intentKind": order.intent_kind,
        "clientOrderId": client_id,
    }


def submit_binance_futures_order(order: OrderIntent, execution: ExecutionConfig) -> dict[str, Any]:
    params = build_binance_futures_order_params(order, execution)
    # BIN-001: idempotency key so a retry after a timestamp/network error cannot
    # double-fill. BIN-002: RESULT guarantees executedQty/avgPrice/cumQuote are in
    # the synchronous response (futures default can be ACK for some types).
    client_id = client_order_id_for(order)
    params["newClientOrderId"] = client_id
    params["newOrderRespType"] = "RESULT"
    payload = signed_binance_request(execution, "POST", "/fapi/v1/order", params, market=order.market)
    return {
        "endpoint": "/fapi/v1/order",
        "response": payload,
        "reduceOnly": order.reduce_only,
        "orderType": params.get("type"),
        "intentKind": order.intent_kind,
        "clientOrderId": client_id,
    }


def submit_binance_live_order(order: OrderIntent, execution: ExecutionConfig) -> dict[str, Any]:
    if is_futures_market(order.market):
        return submit_binance_futures_order(order, execution)
    return submit_binance_spot_order(order, execution)


def fetch_futures_order(execution: ExecutionConfig, symbol: str, order_id: int) -> dict[str, Any]:
    payload = signed_binance_request(
        execution,
        "GET",
        "/fapi/v1/order",
        {"symbol": normalize_symbol(symbol), "orderId": int(order_id)},
        market="binance_futures",
    )
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected futures order payload: {payload!r}")
    return payload


def fetch_futures_open_orders(execution: ExecutionConfig, symbol: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {}
    if symbol:
        params["symbol"] = normalize_symbol(symbol)
    payload = signed_binance_request(execution, "GET", "/fapi/v1/openOrders", params, market="binance_futures")
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected open orders payload: {payload!r}")
    return payload


def fetch_futures_open_algo_orders(execution: ExecutionConfig, symbol: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {}
    if symbol:
        params["symbol"] = normalize_symbol(symbol)
    payload = signed_binance_request(execution, "GET", "/fapi/v1/openAlgoOrders", params, market="binance_futures")
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected algo open orders payload: {payload!r}")
    return payload


PROTECTION_SL_TYPES = frozenset({"STOP", "STOP_MARKET"})
PROTECTION_TP_TYPES = frozenset({"TAKE_PROFIT", "TAKE_PROFIT_MARKET"})


def futures_close_side(*, side_long: bool) -> str:
    return SELL if side_long else BUY


def emergency_close_futures_position(
    symbol: str,
    execution: ExecutionConfig,
    *,
    side_long: bool,
    quantity: Decimal,
) -> dict[str, Any]:
    """Last-resort reduce-only MARKET close used when a stop cannot be attached.

    A leveraged futures position with no stop is the single most dangerous state
    this bot can be in. If protection attachment fails after a fill (F5/F6), we
    would rather flatten the position immediately than leave it naked.
    """
    symbol = normalize_symbol(symbol)
    if quantity <= 0:
        return {"status": "skipped", "reason": "no quantity to close"}
    close = OrderIntent(
        action=futures_close_side(side_long=side_long),
        symbol=symbol,
        market="binance_futures",
        quote_asset="USDT",
        quote_amount=Decimal("0"),
        estimated_price=Decimal("0"),
        reduce_only=True,
        quantity=quantity,
        order_type=ORDER_MARKET,
        intent_kind=INTENT_CLOSE_LONG if side_long else INTENT_CLOSE_SHORT,
    )
    try:
        resp = submit_binance_futures_order(close, execution)
        return {"status": "emergency_closed", "response": resp}
    except Exception as exc:  # pragma: no cover - network failure path
        return {"status": "emergency_close_failed", "error": str(exc)}


def protection_algo_kind(row: dict[str, Any]) -> str | None:
    order_type = str(row.get("orderType", row.get("type", ""))).upper()
    if order_type in PROTECTION_SL_TYPES:
        return "stop_loss"
    if order_type in PROTECTION_TP_TYPES:
        return "take_profit"
    return None


def filter_position_protection_orders(
    open_orders: list[dict[str, Any]], *, side_long: bool
) -> list[dict[str, Any]]:
    close_side = futures_close_side(side_long=side_long)
    rows: list[dict[str, Any]] = []
    for row in open_orders:
        if str(row.get("side", "")).upper() != close_side:
            continue
        if protection_algo_kind(row) is None:
            continue
        rows.append(row)
    return rows


def futures_protection_status(open_orders: list[dict[str, Any]], *, side_long: bool) -> dict[str, bool]:
    protection_rows = filter_position_protection_orders(open_orders, side_long=side_long)
    has_sl = any(protection_algo_kind(row) == "stop_loss" for row in protection_rows)
    has_tp = any(protection_algo_kind(row) == "take_profit" for row in protection_rows)
    return {"stop_loss": has_sl, "take_profit": has_tp}


def cancel_futures_algo_order(execution: ExecutionConfig, algo_id: int) -> dict[str, Any]:
    payload = signed_binance_request(
        execution,
        "DELETE",
        "/fapi/v1/algoOrder",
        {"algoId": int(algo_id)},
        market="binance_futures",
    )
    return {"algoId": int(algo_id), "response": payload}


def cancel_futures_position_protection_orders(
    execution: ExecutionConfig,
    symbol: str,
    *,
    side_long: bool,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    symbol = normalize_symbol(symbol)
    open_orders = fetch_futures_open_algo_orders(execution, symbol)
    targets = filter_position_protection_orders(open_orders, side_long=side_long)
    results: list[dict[str, Any]] = []
    for row in targets:
        algo_id = row.get("algoId")
        if algo_id is None:
            continue
        entry: dict[str, Any] = {
            "symbol": symbol,
            "algoId": algo_id,
            "kind": protection_algo_kind(row),
            "triggerPrice": row.get("triggerPrice"),
            "quantity": row.get("quantity"),
            "createTime": row.get("createTime"),
        }
        if dry_run:
            entry["status"] = "would_cancel"
        else:
            entry["status"] = "cancelled"
            entry["cancelResponse"] = cancel_futures_algo_order(execution, int(algo_id))
        results.append(entry)
    return results


def audit_futures_algo_protection_orders(execution: ExecutionConfig) -> dict[str, Any]:
    """Read-only inventory of open algo TP/SL orders vs futures positions."""
    open_orders = fetch_futures_open_algo_orders(execution)
    positions = fetch_futures_positions(execution)
    position_by_symbol: dict[str, dict[str, Any]] = {}
    for row in positions:
        amt = decimal_from(row.get("positionAmt", "0"))
        if amt == 0:
            continue
        symbol = normalize_symbol(str(row.get("symbol", "")))
        position_by_symbol[symbol] = {
            "quantity": abs(amt),
            "side_long": amt > 0,
            "entryPrice": decimal_from(row.get("entryPrice", "0")),
        }

    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in open_orders:
        symbol = normalize_symbol(str(row.get("symbol", "")))
        kind = protection_algo_kind(row)
        if kind is None:
            continue
        by_symbol.setdefault(symbol, []).append(
            {
                "algoId": row.get("algoId"),
                "kind": kind,
                "side": row.get("side"),
                "triggerPrice": row.get("triggerPrice"),
                "quantity": row.get("quantity"),
                "positionSide": row.get("positionSide"),
                "createTime": row.get("createTime"),
            }
        )

    symbols = sorted(set(by_symbol) | set(position_by_symbol))
    rows_out: list[dict[str, Any]] = []
    duplicate_count = 0
    orphan_count = 0
    for symbol in symbols:
        prot_rows = by_symbol.get(symbol, [])
        pos = position_by_symbol.get(symbol)
        side_long = bool(pos["side_long"]) if pos else None
        sl_rows = [item for item in prot_rows if item["kind"] == "stop_loss"]
        tp_rows = [item for item in prot_rows if item["kind"] == "take_profit"]
        extra = max(0, len(sl_rows) - 1) + max(0, len(tp_rows) - 1)
        duplicate_count += extra
        orphan = pos is None and bool(prot_rows)
        if orphan:
            orphan_count += len(prot_rows)
        position_qty = pos["quantity"] if pos else None
        protected_qty = max(
            (decimal_from(item.get("quantity", "0")) for item in prot_rows),
            default=Decimal("0"),
        )
        qty_mismatch = (
            position_qty is not None
            and protected_qty > 0
            and protected_qty != position_qty
        )
        rows_out.append(
            {
                "symbol": symbol,
                "positionSide": "LONG" if side_long else ("SHORT" if side_long is False else None),
                "positionQuantity": str(position_qty) if position_qty is not None else None,
                "protectionOrderCount": len(prot_rows),
                "stopLossCount": len(sl_rows),
                "takeProfitCount": len(tp_rows),
                "duplicateExtraOrders": extra,
                "orphanOrders": orphan,
                "quantityMismatch": qty_mismatch,
                "protectedQuantityMax": str(protected_qty) if protected_qty > 0 else None,
                "orders": prot_rows,
            }
        )
    return {
        "auditedAt": int(time.time()),
        "totalProtectionOrders": sum(len(item["orders"]) for item in rows_out),
        "duplicateExtraOrders": duplicate_count,
        "orphanOrderCount": orphan_count,
        "symbols": rows_out,
    }


def cleanup_futures_protection_orders(
    execution: ExecutionConfig,
    auto_exec: AutoExecutionConfig,
    *,
    dry_run: bool = True,
    reattach: bool = True,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Cancel duplicate/stale TP/SL algo orders; optionally reattach one set per open position."""
    audit = audit_futures_algo_protection_orders(execution)
    actions: list[dict[str, Any]] = []
    for row in audit.get("symbols", []):
        sym = row["symbol"]
        if symbol and normalize_symbol(symbol) != sym:
            continue
        prot_rows = row.get("orders", [])
        pos_qty = row.get("positionQuantity")
        needs_cleanup = (
            row.get("duplicateExtraOrders", 0) > 0
            or row.get("orphanOrders")
            or row.get("quantityMismatch")
        )
        if not needs_cleanup:
            continue
        side_long = row.get("positionSide") == "LONG"
        cancel_results: list[dict[str, Any]] = []
        if prot_rows:
            if pos_qty is None:
                for item in prot_rows:
                    algo_id = item.get("algoId")
                    if algo_id is None:
                        continue
                    entry = {"symbol": sym, "algoId": algo_id, "kind": item.get("kind"), "status": "would_cancel" if dry_run else "cancelled"}
                    if not dry_run:
                        entry["cancelResponse"] = cancel_futures_algo_order(execution, int(algo_id))
                    cancel_results.append(entry)
            else:
                cancel_results = cancel_futures_position_protection_orders(
                    execution,
                    sym,
                    side_long=side_long,
                    dry_run=dry_run,
                )
        attach_result: dict[str, Any] | None = None
        if reattach and pos_qty is not None and not dry_run:
            attach_result = ensure_futures_position_protection(sym, execution, auto_exec)
        elif reattach and pos_qty is not None and dry_run:
            attach_result = {"symbol": sym, "status": "would_reattach", "positionQuantity": pos_qty}
        actions.append(
            {
                "symbol": sym,
                "reason": {
                    "duplicateExtraOrders": row.get("duplicateExtraOrders", 0),
                    "orphanOrders": row.get("orphanOrders", False),
                    "quantityMismatch": row.get("quantityMismatch", False),
                },
                "cancelled": cancel_results,
                "reattach": attach_result,
            }
        )
    return {"dryRun": dry_run, "reattach": reattach, "audit": audit, "actions": actions}


def replace_futures_position_protection(
    symbol: str,
    execution: ExecutionConfig,
    auto_exec: AutoExecutionConfig,
    *,
    atr_pct: Decimal | None = None,
    stop_price_override: Decimal | None = None,
    take_profit_price_override: Decimal | None = None,
    trade_learning: dict[str, Any] | None = None,
    open_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Cancel existing TP/SL algo orders for a symbol and attach one fresh set for the full position."""
    symbol = normalize_symbol(symbol)
    rows = fetch_futures_positions(execution)
    long_qty = Decimal("0")
    short_qty = Decimal("0")
    for row in rows:
        if row.get("symbol") != symbol:
            continue
        amt = decimal_from(row.get("positionAmt", "0"))
        side = str(row.get("positionSide", "BOTH")).upper()
        if side == "LONG" and amt > 0:
            long_qty = amt
        elif side == "SHORT" and amt < 0:
            short_qty = abs(amt)
        elif side == "BOTH" and amt > 0:
            long_qty = amt
        elif side == "BOTH" and amt < 0:
            short_qty = abs(amt)
    if long_qty > 0:
        side_long = True
    elif short_qty > 0:
        side_long = False
    else:
        return {"symbol": symbol, "status": "skipped", "reason": "no open position"}
    # F5 fix: attach-then-cancel. Previously this cancelled all existing TP/SL
    # FIRST and then re-attached; if the re-attach failed (network blip, filter
    # error) the position was left NAKED — no stop on a leveraged futures position.
    # Now we attach a fresh full-position protection set first (force=True makes
    # ensure_* replace its own entries atomically per-side), verify the stop is
    # actually live on the book, and only then clean up any leftover duplicates.
    attached = ensure_futures_position_protection(
        symbol,
        execution,
        auto_exec,
        atr_pct=atr_pct,
        stop_price_override=stop_price_override,
        take_profit_price_override=take_profit_price_override,
        trade_learning=trade_learning,
        open_context=open_context,
        force=True,
    )
    # Verify a stop-loss is genuinely on the book before we trust the refresh.
    stop_live = False
    try:
        post_orders = fetch_futures_open_algo_orders(execution, symbol)
        stop_live = futures_protection_status(post_orders, side_long=side_long).get("stop_loss", False)
    except Exception as exc:  # pragma: no cover - network failure path
        attached = {**attached, "verifyError": str(exc)}
    if not stop_live and attached.get("status") not in {"protection_attached", "already_protected"}:
        # Do NOT cancel anything — leaving the prior stop (if any) in place is
        # safer than a naked position. Signal the caller to remediate.
        return {
            "symbol": symbol,
            "status": "protection_unconfirmed",
            "stopLive": stop_live,
            "nakedRisk": True,
            **attached,
        }
    # ensure_futures_position_protection(force=True) already de-duped and replaced
    # the per-side protection internally, so no extra cancel pass is needed here.
    return {"symbol": symbol, "stopLive": stop_live, **attached}


def build_futures_algo_protection_params(
    order: OrderIntent,
    quantity: Decimal,
    execution: ExecutionConfig,
    auto_exec: AutoExecutionConfig,
    *,
    order_type: str,
    trigger_price: Decimal,
) -> dict[str, Any]:
    protection_side = SELL if order.intent_kind in {INTENT_OPEN_LONG} else BUY
    filters = fetch_symbol_filters(order.symbol, execution, order.market)
    tick = filters.get("tick_size", Decimal("0.01"))
    step = filters.get("step_size", Decimal("0.001"))
    qty = quantize_to_step(quantity, step)
    params: dict[str, Any] = {
        "algoType": "CONDITIONAL",
        "symbol": normalize_symbol(order.symbol),
        "side": protection_side,
        "type": order_type,
        "triggerPrice": decimal_to_api(quantize_price(trigger_price, tick)),
        "quantity": decimal_to_api(qty),
        "workingType": auto_exec.working_type,
        "priceProtect": "false",
    }
    if fetch_futures_hedge_mode(execution):
        params["positionSide"] = futures_position_side(order)
    else:
        params["reduceOnly"] = "true"
    return params


def submit_futures_algo_protection_order(
    order: OrderIntent,
    quantity: Decimal,
    execution: ExecutionConfig,
    auto_exec: AutoExecutionConfig,
    *,
    order_type: str,
    trigger_price: Decimal,
) -> dict[str, Any]:
    params = build_futures_algo_protection_params(
        order,
        quantity,
        execution,
        auto_exec,
        order_type=order_type,
        trigger_price=trigger_price,
    )
    payload = signed_binance_request(execution, "POST", "/fapi/v1/algoOrder", params, market=order.market)
    return {"endpoint": "/fapi/v1/algoOrder", "params": params, "response": payload}


def poll_futures_market_fill(
    execution: ExecutionConfig,
    symbol: str,
    order_id: int,
    *,
    timeout_seconds: float = 12.0,
    poll_interval_seconds: float = 0.4,
) -> dict[str, Any] | None:
    deadline = time.time() + timeout_seconds
    terminal = {"CANCELED", "REJECTED", "EXPIRED"}
    last_payload: dict[str, Any] | None = None
    while time.time() < deadline:
        payload = fetch_futures_order(execution, symbol, order_id)
        last_payload = payload
        executed = decimal_from(payload.get("executedQty", "0"))
        status = str(payload.get("status", "")).upper()
        if executed > 0:
            return payload
        if status in terminal:
            return None
        time.sleep(poll_interval_seconds)
    if last_payload is None:
        return None
    executed = decimal_from(last_payload.get("executedQty", "0"))
    return last_payload if executed > 0 else None


def resolve_live_fill_from_response(
    order: OrderIntent,
    payload: dict[str, Any],
    execution: ExecutionConfig,
) -> tuple[Decimal, Decimal, dict[str, Any]]:
    """Return (executedQty, cumQuote, details) after optional futures MARKET fill poll."""
    meta: dict[str, Any] = {}
    quantity = decimal_from(payload.get("executedQty", "0"))
    quote_amount = decimal_from(payload.get("cumQuote", payload.get("cummulativeQuoteQty", "0")))
    # BIN-004: poll for fills on reduce_only futures MARKET closes too. Previously
    # `not order.reduce_only` excluded closes, so a close whose initial response
    # reported zero fill was booked as quantity=0 — the bot believed the position
    # was still open, lost PnL data, and could re-issue another close.
    if (
        quantity <= 0
        and is_futures_market(order.market)
        and order.order_type == ORDER_MARKET
        and payload.get("orderId") is not None
    ):
        polled = poll_futures_market_fill(execution, order.symbol, int(payload["orderId"]))
        if polled:
            meta["fillPoll"] = {
                "orderId": polled.get("orderId"),
                "status": polled.get("status"),
                "executedQty": polled.get("executedQty"),
                "avgPrice": polled.get("avgPrice"),
            }
            quantity = decimal_from(polled.get("executedQty", "0"))
            quote_amount = decimal_from(polled.get("cumQuote", polled.get("cummulativeQuoteQty", "0")))
    return quantity, quote_amount, meta


def build_protection_order_for_position(
    symbol: str,
    market: str,
    *,
    entry_price: Decimal,
    quantity: Decimal,
    intent_kind: str,
    auto_exec: AutoExecutionConfig,
    atr_pct: Decimal | None = None,
    stop_price_override: Decimal | None = None,
    take_profit_price_override: Decimal | None = None,
    exit_quality_mult: Decimal | None = None,
) -> OrderIntent:
    if exit_quality_mult is not None and exit_quality_mult > 0:
        auto_exec = replace(
            auto_exec,
            take_profit_pct=auto_exec.take_profit_pct
            * min(max(exit_quality_mult, Decimal("0.50")), Decimal("1.50")),
        )
    action = BUY if intent_kind == INTENT_OPEN_LONG else SELL
    asset = AssetConfig(
        symbol=normalize_symbol(symbol),
        market=market,
        base_asset=symbol.replace("USDT", ""),
        quote_asset="USDT",
        provider={"type": "static"},
    )
    base = OrderIntent(
        action,
        normalize_symbol(symbol),
        market,
        "USDT",
        (entry_price * quantity).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN),
        entry_price,
        quantity=quantity,
        intent_kind=intent_kind,
    )
    protection_auto_exec = replace(auto_exec, entry_order_type=ORDER_MARKET)
    enriched = enrich_order_intent(base, protection_auto_exec, StrategyConfig(), asset, atr_pct=atr_pct)
    if stop_price_override is not None or take_profit_price_override is not None:
        return replace(
            enriched,
            stop_price=stop_price_override if stop_price_override is not None else enriched.stop_price,
            take_profit_price=(
                take_profit_price_override
                if take_profit_price_override is not None
                else enriched.take_profit_price
            ),
        )
    return enriched


def audit_futures_positions_protection(
    execution: ExecutionConfig,
    auto_exec: AutoExecutionConfig | None = None,
) -> dict[str, Any]:
    """Read-only audit of open futures positions vs algo TP/SL coverage."""
    auto_exec = auto_exec or AutoExecutionConfig()
    rows = fetch_futures_positions(execution)
    positions: list[dict[str, Any]] = []
    for row in rows:
        amt = decimal_from(row.get("positionAmt", "0"))
        if amt == 0:
            continue
        symbol = normalize_symbol(str(row.get("symbol", "")))
        side_long = amt > 0
        open_orders = fetch_futures_open_algo_orders(execution, symbol)
        prot = futures_protection_status(open_orders, side_long=side_long)
        positions.append(
            {
                "symbol": symbol,
                "side": "LONG" if side_long else "SHORT",
                "quantity": str(abs(amt)),
                "entryPrice": row.get("entryPrice"),
                "markPrice": row.get("markPrice"),
                "unrealizedPnl": row.get("unRealizedProfit"),
                "leverage": row.get("leverage"),
                "stopLoss": prot["stop_loss"],
                "takeProfit": prot["take_profit"],
                "protected": prot["stop_loss"] and prot["take_profit"],
            }
        )
    unprotected = [item for item in positions if not item["protected"]]
    return {
        "auditedAt": int(time.time()),
        "autoTakeProfitStopLoss": auto_exec.auto_take_profit_stop_loss,
        "positionCount": len(positions),
        "protectedCount": len(positions) - len(unprotected),
        "unprotectedCount": len(unprotected),
        "positions": positions,
        "unprotectedSymbols": [item["symbol"] for item in unprotected],
    }


def attach_protection_all_open_positions(
    execution: ExecutionConfig,
    auto_exec: AutoExecutionConfig,
) -> dict[str, Any]:
    audit = audit_futures_positions_protection(execution, auto_exec)
    results: list[dict[str, Any]] = []
    for symbol in audit.get("unprotectedSymbols", []):
        try:
            results.append(ensure_futures_position_protection(symbol, execution, auto_exec))
        except Exception as exc:
            results.append({"symbol": symbol, "status": "failed", "error": str(exc)})
    return {"audit": audit, "attachResults": results}


def ensure_futures_position_protection(
    symbol: str,
    execution: ExecutionConfig,
    auto_exec: AutoExecutionConfig,
    *,
    atr_pct: Decimal | None = None,
    stop_price_override: Decimal | None = None,
    take_profit_price_override: Decimal | None = None,
    trade_learning: dict[str, Any] | None = None,
    open_context: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Attach TP/SL reduce-only orders for an existing futures position when missing.

    F6: a stop-loss is mandatory for any live futures position. When
    auto_take_profit_stop_loss is false we no longer skip protection entirely —
    we attach the stop-loss only (take-profit is suppressed below). The stop is a
    safety device; only the take-profit is optional.
    """
    symbol = normalize_symbol(symbol)
    stop_only = not auto_exec.auto_take_profit_stop_loss

    rows = fetch_futures_positions(execution)
    long_qty = Decimal("0")
    short_qty = Decimal("0")
    entry_long = Decimal("0")
    entry_short = Decimal("0")
    for row in rows:
        if row.get("symbol") != symbol:
            continue
        amt = decimal_from(row.get("positionAmt", "0"))
        side = str(row.get("positionSide", "BOTH")).upper()
        if side == "LONG" and amt > 0:
            long_qty = amt
            entry_long = decimal_from(row.get("entryPrice", "0"))
        elif side == "SHORT" and amt < 0:
            short_qty = abs(amt)
            entry_short = decimal_from(row.get("entryPrice", "0"))
        elif side == "BOTH" and amt > 0:
            long_qty = amt
            entry_long = decimal_from(row.get("entryPrice", "0"))
        elif side == "BOTH" and amt < 0:
            short_qty = abs(amt)
            entry_short = decimal_from(row.get("entryPrice", "0"))

    if long_qty > 0:
        intent_kind = INTENT_OPEN_LONG
        quantity = long_qty
        entry = entry_long
        side_long = True
    elif short_qty > 0:
        intent_kind = INTENT_OPEN_SHORT
        quantity = short_qty
        entry = entry_short
        side_long = False
    else:
        return {"symbol": symbol, "status": "skipped", "reason": "no open position"}

    filters = fetch_symbol_filters(symbol, execution, "binance_futures")
    quantity = quantize_to_step(quantity, filters.get("step_size", Decimal("0.001")))
    if quantity <= 0:
        return {"symbol": symbol, "status": "failed", "reason": "quantity below step after quantize"}

    open_orders = fetch_futures_open_algo_orders(execution, symbol)
    existing = futures_protection_status(open_orders, side_long=side_long)
    prot_rows = filter_position_protection_orders(open_orders, side_long=side_long)
    sl_count = sum(1 for row in prot_rows if protection_algo_kind(row) == "stop_loss")
    tp_count = sum(1 for row in prot_rows if protection_algo_kind(row) == "take_profit")
    if existing["stop_loss"] and existing["take_profit"] and not force and sl_count <= 1 and tp_count <= 1:
        return {"symbol": symbol, "status": "already_protected", "existing": existing, "quantity": str(quantity)}

    if prot_rows and (force or sl_count > 1 or tp_count > 1 or not existing["stop_loss"] or not existing["take_profit"]):
        cancel_futures_position_protection_orders(execution, symbol, side_long=side_long, dry_run=False)

    exit_quality_mult = (
        Decimal("1")
        if take_profit_price_override is not None
        else exit_quality_threshold_mult(trade_learning, open_context)
    )
    parent = build_protection_order_for_position(
        symbol,
        "binance_futures",
        entry_price=entry,
        quantity=quantity,
        intent_kind=intent_kind,
        auto_exec=auto_exec,
        atr_pct=atr_pct,
        stop_price_override=stop_price_override,
        take_profit_price_override=take_profit_price_override,
        exit_quality_mult=exit_quality_mult,
    )
    protection = submit_futures_protection_orders(
        parent,
        quantity,
        execution,
        auto_exec,
        skip_stop=existing["stop_loss"] and not force and stop_price_override is None,
        # F6 stop_only: when auto_take_profit_stop_loss is off, attach the stop but
        # never the take-profit. Otherwise keep the normal "skip if already present".
        skip_take_profit=stop_only
        or (existing["take_profit"] and not force and take_profit_price_override is None),
    )
    return {
        "symbol": symbol,
        "status": "protection_attached" if protection else "failed",
        "quantity": str(quantity),
        "entryPrice": str(entry),
        "stopPrice": str(parent.stop_price) if parent.stop_price else None,
        "takeProfitPrice": str(parent.take_profit_price) if parent.take_profit_price else None,
        "existingBeforeAttach": existing,
        "breakevenOverride": stop_price_override is not None,
        "exitQualityMultiplier": str(exit_quality_mult) if exit_quality_mult != Decimal("1") else None,
        "protectionOrders": protection,
    }


def submit_spot_oco_take_profit_stop_loss(
    order: OrderIntent,
    quantity: Decimal,
    execution: ExecutionConfig,
    auto_exec: AutoExecutionConfig,
) -> dict[str, Any]:
    filters = fetch_symbol_filters(order.symbol, execution, order.market)
    tick = filters.get("tick_size", Decimal("0.01"))
    reference = order.limit_price or order.estimated_price
    tp_price = order.take_profit_price or reference * (Decimal("1") + auto_exec.take_profit_pct)
    sl_price = order.stop_price or reference * (Decimal("1") - auto_exec.stop_loss_pct)
    qty_order = OrderIntent(
        action=SELL,
        symbol=order.symbol,
        market=order.market,
        quote_asset=order.quote_asset,
        quote_amount=order.quote_amount,
        estimated_price=order.estimated_price,
        quantity=quantity,
        intent_kind=INTENT_SELL_SPOT,
        time_in_force=order.time_in_force,
    )
    params = {
        "symbol": normalize_symbol(order.symbol),
        "side": SELL,
        "quantity": decimal_to_api(order_quantity_from_intent(qty_order, execution)),
        "price": decimal_to_api(quantize_price(tp_price, tick)),
        "stopPrice": decimal_to_api(quantize_price(sl_price, tick)),
        "stopLimitPrice": decimal_to_api(quantize_price(sl_price, tick)),
        "stopLimitTimeInForce": order.time_in_force,
    }
    payload = signed_binance_request(execution, "POST", "/api/v3/order/oco", params, market=order.market)
    return {"endpoint": "/api/v3/order/oco", "response": payload}


def submit_futures_protection_orders(
    order: OrderIntent,
    quantity: Decimal,
    execution: ExecutionConfig,
    auto_exec: AutoExecutionConfig,
    *,
    skip_stop: bool = False,
    skip_take_profit: bool = False,
) -> list[dict[str, Any]]:
    protection_side = SELL if order.intent_kind in {INTENT_OPEN_LONG} else BUY
    qty_order = OrderIntent(
        action=protection_side,
        symbol=order.symbol,
        market=order.market,
        quote_asset=order.quote_asset,
        quote_amount=order.quote_amount,
        estimated_price=order.estimated_price,
        reduce_only=True,
        quantity=quantity,
        intent_kind=order.intent_kind,
        time_in_force=order.time_in_force,
    )
    results: list[dict[str, Any]] = []
    if order.stop_price and not skip_stop:
        sl_payload = submit_futures_algo_protection_order(
            qty_order,
            quantity,
            execution,
            auto_exec,
            order_type=ORDER_STOP_MARKET,
            trigger_price=order.stop_price,
        )
        results.append({"kind": "stop_loss", **sl_payload})
    if order.take_profit_price and not skip_take_profit:
        tp_payload = submit_futures_algo_protection_order(
            qty_order,
            quantity,
            execution,
            auto_exec,
            order_type=ORDER_TAKE_PROFIT_MARKET,
            trigger_price=order.take_profit_price,
        )
        results.append({"kind": "take_profit", **tp_payload})
    return results


def attach_protection_orders(
    order: OrderIntent,
    fill_quantity: Decimal,
    execution: ExecutionConfig,
    auto_exec: AutoExecutionConfig,
) -> list[dict[str, Any]]:
    if not auto_exec.auto_take_profit_stop_loss or order.reduce_only or fill_quantity <= 0:
        return []
    if order.intent_kind not in {INTENT_OPEN_LONG, INTENT_OPEN_SHORT, INTENT_BUY_SPOT}:
        return []
    if is_futures_market(order.market):
        return submit_futures_protection_orders(order, fill_quantity, execution, auto_exec)
    if is_spot_market(order.market) and order.intent_kind == INTENT_BUY_SPOT:
        return [submit_spot_oco_take_profit_stop_loss(order, fill_quantity, execution, auto_exec)]
    return []


def quantize_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    steps = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return (steps * step).quantize(step, rounding=ROUND_DOWN)


def order_quantity_from_intent(order: OrderIntent, execution: ExecutionConfig) -> Decimal:
    if order.quantity is not None and order.quantity > 0:
        quantity = order.quantity
    elif order.estimated_price > 0:
        quantity = order.quote_amount / order.estimated_price
    else:
        raise ValueError("Cannot derive order quantity without price or explicit quantity.")
    filters = fetch_symbol_filters(order.symbol, execution, order.market)
    step = filters.get("step_size", Decimal("0.00000001"))
    min_qty = filters.get("min_qty", Decimal("0"))
    quantity = quantize_to_step(quantity, step)
    if quantity <= 0 or (min_qty and quantity < min_qty):
        raise ValueError(f"Order quantity {quantity} below exchange minimum {min_qty}.")
    if not order.reduce_only:
        min_notional = filters.get("min_notional", Decimal("0"))
        notional = quantity * order.estimated_price
        if min_notional and notional < min_notional:
            raise ValueError(f"Order notional {notional} below min notional {min_notional}.")
    return quantity


def open_quote_meets_min_notional(
    symbol: str,
    market: str,
    max_trade_quote: Decimal,
    execution: ExecutionConfig,
) -> bool:
    if max_trade_quote <= 0:
        return False
    filters = fetch_symbol_filters(symbol, execution, market)
    min_notional = filters.get("min_notional", Decimal("0"))
    if not min_notional:
        return True
    return max_trade_quote >= min_notional


def execute_live_order(
    snapshot: MarketSnapshot,
    decision: RiskDecision,
    signal: Signal,
    strategy: StrategyConfig,
    risk: RiskConfig,
    portfolio: PaperPortfolio,
    memory: TradingMemory,
    execution: ExecutionConfig,
    trade_rationale: dict[str, Any] | None = None,
    auto_exec: AutoExecutionConfig | None = None,
    trade_learning_cfg: Any = None,
    trade_lessons_cfg: Any = None,
    trade_learning: dict[str, Any] | None = None,
) -> ExecutionRecord:
    # These live in growth_sizing; without this import they resolve to module
    # globals that only exist on the monolithic path, so the pipeline path raised
    # NameError: name 'portfolio_equity' is not defined the moment a live order
    # reached the preflight (i.e. right after arming with funds).
    from growth_sizing import mark_prices_from_portfolio, portfolio_equity

    asset = snapshot.asset
    price = snapshot.price
    symbol = normalize_symbol(asset.symbol)
    quantity = Decimal("0")
    quote_amount = Decimal("0")
    status = "blocked"
    details: dict[str, Any] = {}
    auto_exec = auto_exec or AutoExecutionConfig()

    if decision.approved and decision.order:
        try:
            validate_risk_execution_mode_alignment(risk, execution)
        except ValueError as exc:
            return ExecutionRecord(
                status="live_order_failed",
                mode=LIVE,
                symbol=symbol,
                action=decision.action,
                quantity=Decimal("0"),
                quote_amount=Decimal("0"),
                price=price,
                reasons=decision.reasons,
                blocked_reasons=decision.blocked_reasons + [str(exc)],
                indicators=signal.indicators,
                portfolio=portfolio.snapshot(),
                timestamp=snapshot.observed_at,
                execution_details={"stage": "preflight", "error": str(exc)},
                trade_rationale=trade_rationale or {},
            )
        preflight = live_safety_blocked_reasons(
            risk,
            snapshot=snapshot,
            memory=memory,
            equity=portfolio_equity(
                portfolio,
                mark_prices_from_portfolio(portfolio, symbol, price),
            ),
        )
        if decision.order and decision.order.reduce_only and risk.daily_drag_blocks == "new_opens_only":
            preflight = [item for item in preflight if not item.startswith("Daily drag")]
        if preflight:
            return ExecutionRecord(
                status="blocked",
                mode=LIVE,
                symbol=symbol,
                action=BLOCKED,
                quantity=Decimal("0"),
                quote_amount=Decimal("0"),
                price=price,
                reasons=decision.reasons,
                blocked_reasons=decision.blocked_reasons + preflight,
                indicators=signal.indicators,
                portfolio=portfolio.snapshot(),
                timestamp=snapshot.observed_at,
                execution_details={"stage": "preflight", "errors": preflight},
                trade_rationale=trade_rationale or {},
            )

    if decision.approved and decision.order:
        failed_endpoint: str | None = None
        position_qty_before = portfolio.position(symbol).quantity
        try:
            order = decision.order
            order_endpoint = "/fapi/v1/order" if is_futures_market(asset.market) else "/api/v3/order"
            # BIN-008: slippage guard for MARKET entries, BEFORE setting leverage or
            # submitting. If the live mark price has drifted past max_slippage_bps
            # from the decision price, skip the entry rather than chase a moved market.
            slip_reason = slippage_guard_reason(order, execution, auto_exec)
            if slip_reason:
                return blocked_execution_record(
                    snapshot,
                    decision,
                    signal,
                    risk,
                    portfolio,
                    "blocked",
                    {"stage": "slippage_guard", "reason": slip_reason},
                    trade_rationale=trade_rationale,
                )
            if (
                is_futures_market(asset.market)
                and auto_exec.auto_leverage
                and order.leverage
                and not order.reduce_only
            ):
                failed_endpoint = "/fapi/v1/leverage"
                details["leverage"] = set_futures_leverage(symbol, order.leverage, execution)
            failed_endpoint = order_endpoint
            response = submit_binance_live_order(order, execution)
            payload = response.get("response", {})
            quantity, quote_amount, fill_meta = resolve_live_fill_from_response(order, payload, execution)
            if fill_meta:
                details["fillPoll"] = fill_meta.get("fillPoll")
            if quantity <= 0 and order.order_type == ORDER_LIMIT:
                status = "live_order_placed"
            else:
                status = "executed_live" if quantity > 0 or quote_amount > 0 else "live_acknowledged"
            details = {**details, **response}
            is_open = not order.reduce_only and order.intent_kind in {INTENT_OPEN_LONG, INTENT_OPEN_SHORT}
            is_add = is_open and (
                (order.action == BUY and position_qty_before > 0)
                or (order.action == SELL and position_qty_before < 0)
            )
            open_ctx_for_protection: dict[str, Any] | None = None
            if quantity > 0 and is_open and not is_add:
                from trade_lessons import open_context_from_signal

                md = (trade_rationale or {}).get("marketDiscovery") or {}
                fill_entry_price = (quote_amount / quantity) if quantity > 0 and quote_amount > 0 else None
                open_ctx_for_protection = open_context_from_signal(
                    signal_indicators=signal.indicators,
                    confidence=(trade_rationale or {}).get("confidence"),
                    discovery_meta=md,
                    entry_price=fill_entry_price,
                )
                memory.position_open_context[normalize_symbol(symbol)] = open_ctx_for_protection
                memory.position_excursion[normalize_symbol(symbol)] = {"mfePct": "0", "maePct": "0"}
            # F6: a stop-loss is a safety device, not an optional feature. For any
            # futures OPEN we always attach protection, even if auto_take_profit_stop_loss
            # is false (that flag may now only suppress the take-profit, never the stop).
            is_futures_open = (
                is_futures_market(asset.market)
                and order.intent_kind in {INTENT_OPEN_LONG, INTENT_OPEN_SHORT}
            )
            want_protection = quantity > 0 and not order.reduce_only and (
                auto_exec.auto_take_profit_stop_loss or is_futures_open
            )
            if want_protection:
                if is_futures_open:
                    protection_result = replace_futures_position_protection(
                        symbol,
                        execution,
                        auto_exec,
                        trade_learning=trade_learning,
                        open_context=open_ctx_for_protection
                        or memory.position_open_context.get(normalize_symbol(symbol)),
                    )
                    details["protectionOrders"] = protection_result.get("protectionOrders") or protection_result
                    if protection_result.get("exitQualityMultiplier"):
                        details["exitQualityMultiplier"] = protection_result.get("exitQualityMultiplier")
                    good = protection_result.get("status") in {"protection_attached", "already_protected"}
                    if not good:
                        # F5: retry once, then flatten rather than hold a naked position.
                        retry_result = replace_futures_position_protection(
                            symbol,
                            execution,
                            auto_exec,
                            trade_learning=trade_learning,
                            open_context=open_ctx_for_protection
                            or memory.position_open_context.get(normalize_symbol(symbol)),
                        )
                        details["protectionRetry"] = retry_result.get("status")
                        good = retry_result.get("status") in {"protection_attached", "already_protected"} or bool(
                            retry_result.get("stopLive")
                        )
                        if not good:
                            side_long = order.intent_kind == INTENT_OPEN_LONG
                            closed = emergency_close_futures_position(
                                symbol, execution, side_long=side_long, quantity=quantity
                            )
                            details["nakedPositionRemediation"] = closed
                            details["protectionWarning"] = (
                                "stop attach failed after retry; emergency-closed naked position "
                                f"status={closed.get('status')}"
                            )
                elif auto_exec.auto_take_profit_stop_loss:
                    protection = attach_protection_orders(order, quantity, execution, auto_exec)
                    if protection:
                        details["protectionOrders"] = protection
                    elif is_futures_market(asset.market) and order.intent_kind in {INTENT_OPEN_LONG, INTENT_OPEN_SHORT}:
                        details["protectionWarning"] = "fill confirmed but TP/SL not attached (missing stop/take-profit prices)"
            if quantity > 0 or quote_amount > 0:
                memory.record_trade(symbol, snapshot.observed_at, is_open=is_open, is_add=is_add)
                if is_open and not is_add:
                    clear_position_exit_tiers(memory, symbol)
                exit_tier = signal.indicators.get("exit_tier", "")
                if quantity > 0 and order.reduce_only and exit_tier:
                    mark_position_exit_tier(memory, symbol, exit_tier)
            commission = decimal_from(payload.get("commission", "0"))
            if commission > 0:
                memory.record_commission(commission)
            # Realized losses for the daily-loss cap are recorded from the
            # exchange-authoritative /fapi/v1/income REALIZED_PNL sync
            # (trading_costs.apply_new_income_rows -> memory.record_realized_pnl),
            # which runs every cycle, covers BOTH long and short closes, and is
            # deduped by tranId. We deliberately do NOT estimate loss here from a
            # mark price (it only saw SELL-closing-long and would double-count
            # against the income sync).
            if quantity > 0 and order.reduce_only and trade_learning_cfg is not None:
                from trade_outcomes import compute_trade_learning_snapshot, record_trade_outcome

                position = portfolio.position(symbol)
                # RC2: prefer the ACTUAL fill price captured at open over the
                # portfolio's running average_price, which collapses to a stale /
                # rounded value after a series of partial reduces and distorts the
                # recorded entry (and any entry-based analytics). realizedPnl itself
                # is unaffected — it comes from the exchange income sync, not here.
                open_ctx = memory.position_open_context.get(normalize_symbol(symbol)) or {}
                entry = decimal_from(open_ctx.get("entryPrice", "0"))
                if entry <= 0:
                    entry = position.average_price
                if entry > 0:
                    ctx = (trade_rationale or {}).get("marketContext") or {}
                    session_label = (ctx.get("session") or {}).get("label")
                    outcome = record_trade_outcome(
                        trade_learning_cfg,
                        symbol=symbol,
                        side=order.action,
                        quantity=quantity,
                        exit_price=price,
                        entry_price=entry,
                        position_side=futures_position_side(order) if is_futures_market(asset.market) else "LONG",
                        regime=signal.indicators.get("regime"),
                        session=session_label,
                        rationale_summary=(trade_rationale or {}).get("summary"),
                        order_id=payload.get("orderId"),
                        close_source="agent_reduce",
                        open_context=open_context_with_excursion(memory, symbol),
                        fees=commission,
                    )
                    if outcome:
                        details["tradeOutcome"] = outcome
                        compute_trade_learning_snapshot(trade_learning_cfg)
                        if trade_lessons_cfg is not None and getattr(trade_lessons_cfg, "enabled", False):
                            from trade_lessons import refresh_trade_lessons

                            refresh_trade_lessons(trade_lessons_cfg, trade_learning_cfg.outcomes_path)
                    if order.intent_kind in {INTENT_CLOSE_LONG, INTENT_CLOSE_SHORT}:
                        _clear_position_tracking(memory, symbol)
            memory.save(execution.state_path)
        except Exception as exc:
            status = "live_order_failed"
            details = {
                **details,
                "endpoint": failed_endpoint
                or ("/fapi/v1/order" if is_futures_market(asset.market) else "/api/v3/order"),
                "error": str(exc),
                "reduceOnly": decision.order.reduce_only,
                "intentKind": decision.order.intent_kind,
                "orderType": decision.order.order_type,
            }
            if decision.order.quantity is not None:
                details["intendedQuantity"] = str(decision.order.quantity)
            if decision.order.quote_amount:
                details["intendedQuoteAmount"] = str(decision.order.quote_amount)

    return ExecutionRecord(
        status=status,
        mode=LIVE,
        symbol=symbol,
        action=decision.action,
        quantity=quantity,
        quote_amount=quote_amount,
        price=price,
        reasons=decision.reasons,
        blocked_reasons=decision.blocked_reasons,
        indicators=signal.indicators,
        portfolio=portfolio.snapshot(),
        timestamp=snapshot.observed_at,
        execution_details=details,
        trade_rationale=trade_rationale or {},
    )


def build_binance_order_test_params(order: OrderIntent, execution: ExecutionConfig) -> dict[str, Any]:
    params: dict[str, Any] = {
        "symbol": normalize_symbol(order.symbol),
        "side": order.action,
        "type": execution.order_type.upper(),
        "recvWindow": execution.recv_window,
    }
    if params["type"] != "MARKET":
        raise ValueError("Only MARKET order test plans are supported by this safety gateway.")
    if order.action == BUY:
        params["quoteOrderQty"] = decimal_to_api(order.quote_amount)
    elif order.action == SELL:
        quantity = (order.quote_amount / order.estimated_price).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        params["quantity"] = decimal_to_api(quantity)
    else:
        raise ValueError(f"Unsupported order side for Binance order test: {order.action}")
    return params


def submit_binance_order_test(order: OrderIntent, execution: ExecutionConfig) -> dict[str, Any]:
    if not execution.api_key or not execution.api_secret:
        raise RuntimeError("BINANCE_API_KEY and BINANCE_API_SECRET must be set in the local environment for order/test.")
    params = build_binance_order_test_params(order, execution)
    params["timestamp"] = get_server_time(
        execution.binance_base_url,
        "/api/v3/time",
        timeout_seconds=execution.timeout_seconds,
    )
    query = sign_query(params, execution.api_secret)
    url = f"{execution.binance_base_url.rstrip('/')}/api/v3/order/test?{query}"
    payload = request_json(
        url,
        method="POST",
        headers={"X-MBX-APIKEY": execution.api_key},
        timeout_seconds=execution.timeout_seconds,
    )
    return {"endpoint": "/api/v3/order/test", "response": payload}


def write_approval_ticket(
    approval_dir: str,
    snapshot: MarketSnapshot,
    decision: RiskDecision,
    signal: Signal,
    portfolio: PaperPortfolio,
) -> str:
    if not decision.order:
        raise ValueError("Cannot write approval ticket without an order intent.")
    path = Path(approval_dir)
    path.mkdir(parents=True, exist_ok=True)
    timestamp = snapshot.observed_at
    ticket_path = path / f"{timestamp}_{decision.order.symbol}_{decision.order.action}.json"
    ticket = {
        "status": "awaiting_manual_approval",
        "manual_checks": [
            "Confirm symbol, market, side, and order size in the broker UI.",
            "Confirm API key has no withdrawal permission and is not stored in the repo.",
            "Confirm daily loss, daily trade cap, and kill switch are acceptable.",
            "Confirm this ticket is not an authorization for unattended live trading.",
        ],
        "order": {
            "action": decision.order.action,
            "symbol": decision.order.symbol,
            "market": decision.order.market,
            "quote_asset": decision.order.quote_asset,
            "quote_amount": str(decision.order.quote_amount),
            "estimated_price": str(decision.order.estimated_price),
        },
        "reasons": decision.reasons,
        "blocked_reasons": decision.blocked_reasons,
        "indicators": signal.indicators,
        "portfolio": portfolio.snapshot(),
        "timestamp": timestamp,
    }
    ticket_path.write_text(json.dumps(ticket, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(ticket_path)


def blocked_execution_record(
    snapshot: MarketSnapshot,
    decision: RiskDecision,
    signal: Signal,
    risk: RiskConfig,
    portfolio: PaperPortfolio,
    status: str,
    execution_details: dict[str, Any] | None = None,
    trade_rationale: dict[str, Any] | None = None,
) -> ExecutionRecord:
    return ExecutionRecord(
        status=status,
        mode=risk.mode,
        symbol=normalize_symbol(snapshot.asset.symbol),
        action=decision.action,
        quantity=Decimal("0"),
        quote_amount=Decimal("0"),
        price=snapshot.price,
        reasons=decision.reasons,
        blocked_reasons=decision.blocked_reasons,
        indicators=signal.indicators,
        portfolio=portfolio.snapshot(),
        timestamp=snapshot.observed_at,
        execution_details=execution_details or {},
        trade_rationale=trade_rationale or {},
    )


def execute_decision(
    snapshot: MarketSnapshot,
    decision: RiskDecision,
    signal: Signal,
    strategy: StrategyConfig,
    risk: RiskConfig,
    portfolio: PaperPortfolio,
    memory: TradingMemory,
    execution: ExecutionConfig,
    trade_rationale: dict[str, Any] | None = None,
    auto_exec: AutoExecutionConfig | None = None,
    trade_learning_cfg: Any = None,
    trade_learning: dict[str, Any] | None = None,
    trade_lessons_cfg: Any = None,
    shadow_paper_cfg: Any = None,
    watch_entry: dict[str, Any] | None = None,
    quadrant_strategy_cfg: Any = None,
) -> ExecutionRecord:
    risk = decision.effective_risk or risk
    if execution.mode == PAPER:
        return execute_paper(snapshot, decision, signal, strategy, risk, portfolio, memory, trade_rationale)
    if not decision.approved or not decision.order:
        return blocked_execution_record(snapshot, decision, signal, risk, portfolio, "blocked", trade_rationale=trade_rationale)
    if shadow_paper_cfg is not None and execution.mode == LIVE:
        from shadow_paper import record_shadow_decision, should_shadow_instead_of_live
        from trade_outcomes import trade_learning_discovery_shadow_first

        from quadrant_strategy import quadrant_should_shadow, quadrant_strategy_from_config

        qcfg = quadrant_strategy_cfg if quadrant_strategy_cfg is not None else quadrant_strategy_from_config({})
        pos_qty = portfolio.position(normalize_symbol(snapshot.asset.symbol)).quantity
        trade_learning_shadow = False
        trade_learning_shadow_reason: str | None = None
        quadrant_shadow, quadrant_shadow_reason = quadrant_should_shadow(signal.indicators, qcfg)
        if trade_learning and trade_learning_cfg:
            discovery_source = str(signal.indicators.get("discovery_source", "") or "")
            is_discovery_open = (
                not decision.order.reduce_only
                and pos_qty == 0
                and discovery_source.startswith("discovery:")
            )
            trade_learning_shadow, trade_learning_shadow_reason = trade_learning_discovery_shadow_first(
                trade_learning,
                cfg=trade_learning_cfg,
                bucket=str(signal.indicators.get("discovery_bucket") or ""),
                is_discovery_open=is_discovery_open,
            )
        trade_learning_shadow = trade_learning_shadow or quadrant_shadow
        if quadrant_shadow_reason and not trade_learning_shadow_reason:
            trade_learning_shadow_reason = quadrant_shadow_reason
        if should_shadow_instead_of_live(
            shadow_paper_cfg,
            reduce_only=decision.order.reduce_only,
            position_qty=pos_qty,
            indicators=signal.indicators,
            confidence=decimal_from(signal.confidence),
            approved=True,
            trade_learning_shadow=trade_learning_shadow,
        ):
            shadow_watch = dict(watch_entry or {})
            if trade_learning_shadow_reason:
                shadow_watch["shadow_trigger"] = (
                    "quadrant" if quadrant_shadow else "trade_learning"
                )
                shadow_watch["shadow_reason"] = trade_learning_shadow_reason
            record_shadow_decision(
                shadow_paper_cfg,
                snapshot=snapshot,
                signal=signal,
                decision=decision,
                watch_entry=shadow_watch or None,
            )
            details: dict[str, Any] = {"shadowLedger": shadow_paper_cfg.ledger_path}
            if trade_learning_shadow_reason:
                details["tradeLearningShadow"] = True
                details["tradeLearningShadowReason"] = trade_learning_shadow_reason
            return blocked_execution_record(
                snapshot,
                decision,
                signal,
                risk,
                portfolio,
                "shadow_paper",
                details,
                trade_rationale=trade_rationale,
            )
    if execution.mode == APPROVAL_REQUIRED:
        ticket_path = write_approval_ticket(execution.approval_dir, snapshot, decision, signal, portfolio)
        return blocked_execution_record(
            snapshot,
            decision,
            signal,
            risk,
            portfolio,
            "approval_required",
            {"approval_ticket": ticket_path},
            trade_rationale=trade_rationale,
        )
    if execution.mode == BINANCE_ORDER_TEST:
        try:
            response = submit_binance_order_test(decision.order, execution)
            status = "validated_order_test"
            details = response
        except Exception as exc:
            status = "order_test_failed"
            details = {"endpoint": "/api/v3/order/test", "error": str(exc)}
        return blocked_execution_record(snapshot, decision, signal, risk, portfolio, status, details, trade_rationale=trade_rationale)
    if execution.mode == LIVE:
        try:
            validate_risk_execution_mode_alignment(risk, execution)
        except ValueError as exc:
            return blocked_execution_record(
                snapshot,
                decision,
                signal,
                risk,
                portfolio,
                "live_order_failed",
                {"stage": "preflight", "error": str(exc)},
                trade_rationale=trade_rationale,
            )
        preflight = live_safety_blocked_reasons(risk, snapshot=snapshot, memory=memory)
        if preflight:
            return blocked_execution_record(
                snapshot,
                decision,
                signal,
                risk,
                portfolio,
                "blocked",
                {"stage": "preflight", "errors": preflight},
                trade_rationale=trade_rationale,
            )
        return execute_live_order(
            snapshot,
            decision,
            signal,
            strategy,
            risk,
            portfolio,
            memory,
            execution,
            trade_rationale,
            auto_exec=auto_exec,
            trade_learning_cfg=trade_learning_cfg,
            trade_learning=trade_learning,
        )
    raise ValueError(f"Unsupported execution mode: {execution.mode}")


def expand_env(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("env:"):
        return env_value(value[4:])
    if isinstance(value, dict):
        return {key: expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    return value


def load_config(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as file:
        return expand_env(json.load(file))


def strategy_from_config(raw: dict[str, Any]) -> StrategyConfig:
    return StrategyConfig(
        fast_window=int(raw.get("fast_window", 8)),
        slow_window=int(raw.get("slow_window", 21)),
        momentum_window=int(raw.get("momentum_window", 6)),
        rsi_window=int(raw.get("rsi_window", 14)),
        atr_window=int(raw.get("atr_window", 14)),
        adx_window=int(raw.get("adx_window", 14)),
        bb_window=int(raw.get("bb_window", 20)),
        bb_std=decimal_from(raw.get("bb_std", "2")),
        bb_squeeze_width=decimal_from(raw.get("bb_squeeze_width", "0.04")),
        adx_range_max=decimal_from(raw.get("adx_range_max", "22")),
        mtf_resample_5m=int(raw.get("mtf_resample_5m", 5)),
        mtf_resample_15m=int(raw.get("mtf_resample_15m", 15)),
        min_buy_confidence=decimal_from(raw.get("min_buy_confidence", "0.68")),
        min_sell_confidence=decimal_from(raw.get("min_sell_confidence", "0.62")),
        holding_reduce_loss_pct=decimal_from(raw.get("holding_reduce_loss_pct", "0.02")),
        holding_take_profit_pct=decimal_from(raw.get("holding_take_profit_pct", "0.04")),
        holding_trailing_activate_pct=decimal_from(raw.get("holding_trailing_activate_pct", "0.025")),
        holding_peak_giveback_pct=decimal_from(raw.get("holding_peak_giveback_pct", "0.025")),
        holding_early_take_pct=decimal_from(raw.get("holding_early_take_pct", "0.03")),
        holding_early_take_fraction=decimal_from(raw.get("holding_early_take_fraction", "0.30")),
        holding_trend_up_giveback_mult=decimal_from(raw.get("holding_trend_up_giveback_mult", "1.8")),
        holding_adverse_momentum_cut_mult=decimal_from(raw.get("holding_adverse_momentum_cut_mult", "0.75")),
        allow_discovery_shorts=bool(raw.get("allow_discovery_shorts", False)),
        discovery_short_mode=_discovery_short_mode_from_config(raw),
        entry_min_edge_fee_multiple=decimal_from(raw.get("entry_min_edge_fee_multiple", "3")),
        allow_pyramid_adds=bool(raw.get("allow_pyramid_adds", False)),
        max_adds_per_symbol_per_day=int(raw.get("max_adds_per_symbol_per_day", 1)),
        pump_guard_min_change_24h_pct=decimal_from(raw.get("pump_guard_min_change_24h_pct", "0.30")),
        pump_guard_block_new_open_pct=decimal_from(raw.get("pump_guard_block_new_open_pct", "0.50")),
        protection_breakeven_activate_pct=decimal_from(raw.get("protection_breakeven_activate_pct", "0.03")),
        protection_breakeven_buffer_pct=decimal_from(raw.get("protection_breakeven_buffer_pct", "0.001")),
        buy_quote_fraction=decimal_from(raw.get("buy_quote_fraction", "0.10")),
        sell_position_fraction=decimal_from(raw.get("sell_position_fraction", "1.0")),
        min_fusion_bull_pct=decimal_from(raw.get("min_fusion_bull_pct", "0.52")),
        primary_signal_bars=int(raw.get("primary_signal_bars", 0)),
        regime_adaptive_signal=bool(raw.get("regime_adaptive_signal", False)),
        short_in_downtrend_boost=bool(raw.get("short_in_downtrend_boost", False)),
        confidence_scale_sizing=bool(raw.get("confidence_scale_sizing", False)),
        rsrs_enabled=bool(raw.get("rsrs_enabled", True)),
        rsrs_window=int(raw.get("rsrs_window", 18)),
        rsrs_min_buy=decimal_from(raw.get("rsrs_min_buy", "0.6")),
        trade_horizon=str(raw.get("trade_horizon", "scalp")),
        swing_take_profit_pct=decimal_from(raw.get("swing_take_profit_pct", "0.08")),
        swing_stop_loss_pct=decimal_from(raw.get("swing_stop_loss_pct", "0.035")),
        swing_sell_position_fraction=decimal_from(raw.get("swing_sell_position_fraction", "0.25")),
        holding_max_hours=int(raw.get("holding_max_hours", 0)),
        holding_require_5m_exit_confirm=bool(raw.get("holding_require_5m_exit_confirm", True)),
    )


def _profit_adaptive_cap_from_config(raw: dict[str, Any]) -> ProfitAdaptiveCapConfig:
    sub = raw.get("profit_adaptive_daily_cap", {}) or {}
    return ProfitAdaptiveCapConfig(
        enabled=bool(sub.get("enabled", False)),
        step_usdt=decimal_from(sub.get("step_usdt", "0.5")),
        extra_trades_per_step=int(sub.get("extra_trades_per_step", 5)),
        hard_ceiling=int(sub.get("hard_ceiling", 60)),
    )


def risk_from_config(raw: dict[str, Any]) -> RiskConfig:
    return RiskConfig(
        mode=str(raw.get("mode", PAPER)),
        allow_live_trading=bool(raw.get("allow_live_trading", False)),
        max_trade_quote=decimal_from(raw.get("max_trade_quote", "250")),
        max_position_quote=decimal_from(raw.get("max_position_quote", "1000")),
        min_confidence=decimal_from(raw.get("min_confidence", "0.60")),
        max_volatility=decimal_from(raw.get("max_volatility", "0.08")),
        max_drawdown=decimal_from(raw.get("max_drawdown", "0.12")),
        max_daily_trades=int(raw.get("max_daily_trades", 0)),
        max_daily_loss_quote=decimal_from(raw.get("max_daily_loss_quote", "50")),
        cooldown_seconds=int(raw.get("cooldown_seconds", 300)),
        require_reason_count=int(raw.get("require_reason_count", 3)),
        kill_switch_path=str(raw.get("kill_switch_path", "logs/live-trading.kill")),
        live_arm_path=str(raw.get("live_arm_path", "logs/live-trading.armed")),
        reserve_futures_available_usdt=decimal_from(raw.get("reserve_futures_available_usdt", "30")),
        allow_futures_open=bool(raw.get("allow_futures_open", True)),
        allowed_live_markets=tuple(raw.get("allowed_live_markets", [SPOT, FUTURES])),
        risk_per_trade_pct=decimal_from(raw.get("risk_per_trade_pct", "0")),
        scale_sizing_with_equity=bool(raw.get("scale_sizing_with_equity", False)),
        min_trade_quote=decimal_from(raw.get("min_trade_quote", "0")),
        max_portfolio_heat_pct=decimal_from(raw.get("max_portfolio_heat_pct", "1")),
        target_equity_quote=decimal_from(raw.get("target_equity_quote", "300000")),
        max_daily_loss_pct=decimal_from(raw.get("max_daily_loss_pct", "0")),
        min_daily_loss_cap_quote=decimal_from(raw.get("min_daily_loss_cap_quote", "0")),
        daily_drag_scope=str(raw.get("daily_drag_scope", "loss_and_costs")),
        daily_drag_blocks=str(raw.get("daily_drag_blocks", "new_opens_only")),
        max_concurrent_positions=int(raw.get("max_concurrent_positions", 0)),
        min_margin_per_trade=decimal_from(raw.get("min_margin_per_trade", "0")),
        max_position_loss_pct=decimal_from(raw.get("max_position_loss_pct", "0")),
        profit_adaptive_daily_cap=_profit_adaptive_cap_from_config(raw),
    )


def _discovery_short_mode_from_config(raw: dict[str, Any]) -> str:
    mode = str(raw.get("discovery_short_mode", "") or "").strip().lower()
    if bool(raw.get("allow_discovery_shorts", False)) and not mode:
        return "all"
    if not mode:
        return "off"
    return mode


def auto_execution_from_config(raw: dict[str, Any]) -> AutoExecutionConfig:
    return AutoExecutionConfig(
        entry_order_type=str(raw.get("entry_order_type", ORDER_LIMIT)),
        limit_offset_pct=decimal_from(raw.get("limit_offset_pct", "0.001")),
        time_in_force=str(raw.get("time_in_force", "GTC")),
        auto_leverage=bool(raw.get("auto_leverage", True)),
        default_leverage=int(raw.get("default_leverage", 5)),
        max_leverage=int(raw.get("max_leverage", 10)),
        leverage_mid=int(raw.get("leverage_mid", 6)),
        leverage_high=int(raw.get("leverage_high", 8)),
        leverage_extreme=int(raw.get("leverage_extreme", 50)),
        leverage_mid_confidence=decimal_from(raw.get("leverage_mid_confidence", "0.75")),
        leverage_high_confidence=decimal_from(raw.get("leverage_high_confidence", "0.85")),
        leverage_extreme_confidence=decimal_from(raw.get("leverage_extreme_confidence", "0.92")),
        leverage_extreme_mtf_min=int(raw.get("leverage_extreme_mtf_min", 3)),
        max_leverage_crypto=int(raw.get("max_leverage_crypto", raw.get("max_leverage", 50))),
        max_leverage_tradfi_stock=int(raw.get("max_leverage_tradfi_stock", 20)),
        max_leverage_tradfi_commodity=int(raw.get("max_leverage_tradfi_commodity", 25)),
        max_leverage_tradfi_index=int(raw.get("max_leverage_tradfi_index", 15)),
        leverage_atr_penalty_threshold=decimal_from(raw.get("leverage_atr_penalty_threshold", "0.06")),
        leverage_atr_penalty_max_steps=int(raw.get("leverage_atr_penalty_max_steps", 3)),
        leverage_volatility_step_down=int(raw.get("leverage_volatility_step_down", 5)),
        auto_take_profit_stop_loss=bool(raw.get("auto_take_profit_stop_loss", True)),
        take_profit_pct=decimal_from(raw.get("take_profit_pct", "0.02")),
        stop_loss_pct=decimal_from(raw.get("stop_loss_pct", "0.015")),
        use_atr_stops=bool(raw.get("use_atr_stops", False)),
        atr_stop_multiplier=decimal_from(raw.get("atr_stop_multiplier", "2")),
        atr_tp_multiplier=decimal_from(raw.get("atr_tp_multiplier", "3")),
        working_type=str(raw.get("working_type", "MARK_PRICE")),
        full_close_threshold=decimal_from(raw.get("full_close_threshold", "0.99")),
        account_max_leverage=int(raw.get("account_max_leverage", 0)),
        max_slippage_bps=decimal_from(raw.get("max_slippage_bps", "0")),
    )


def execution_from_config(raw: dict[str, Any], fallback_mode: str = PAPER) -> ExecutionConfig:
    return ExecutionConfig(
        mode=str(raw.get("mode", fallback_mode)),
        approval_dir=str(raw.get("approval_dir", "approvals")),
        binance_base_url=str(raw.get("binance_base_url", "https://api.binance.com")),
        api_key=str(raw.get("api_key", "")),
        api_secret=str(raw.get("api_secret", "")),
        recv_window=int(raw.get("recv_window", 60000)),
        order_type=str(raw.get("order_type", "MARKET")),
        timeout_seconds=int(raw.get("timeout_seconds", 10)),
        binance_futures_base_url=str(raw.get("binance_futures_base_url", BINANCE_FUTURES_BASE)),
        allowed_live_markets=tuple(raw.get("allowed_live_markets", [SPOT, FUTURES])),
        state_path=str(raw.get("state_path", "logs/live-trading-state.json")),
    )


def assets_from_config(raw_assets: list[dict[str, Any]]) -> list[AssetConfig]:
    return [
        AssetConfig(
            symbol=normalize_symbol(raw["symbol"]),
            market=raw["market"],
            base_asset=raw["base_asset"],
            quote_asset=raw["quote_asset"],
            provider=raw.get("provider", {"type": "static"}),
        )
        for raw in raw_assets
    ]


def record_to_json(record: ExecutionRecord) -> str:
    def convert(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        if isinstance(value, list):
            return [convert(item) for item in value]
        return value

    payload = convert(asdict(record))
    if isinstance(payload, dict):
        payload.setdefault("runtime", runtime_fingerprint())
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def record_to_stdout_json(record: ExecutionRecord) -> str:
    return json.dumps(json.loads(record_to_json(record)), ensure_ascii=True, sort_keys=True)


def print_json(payload: Any, **kwargs: Any) -> None:
    kwargs.setdefault("ensure_ascii", True)
    print(json.dumps(payload, **kwargs))


def append_ledger(path: str | None, record: ExecutionRecord) -> None:
    if not path:
        return
    ledger_path = Path(path)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as file:
        file.write(record_to_json(record) + "\n")


from exit_engine import apply_holding_priority_signal


def run_once(config: dict[str, Any], memory: TradingMemory | None = None) -> list[ExecutionRecord]:
    from trading_pipeline import TradingPipeline, pipeline_from_config

    if pipeline_from_config(config.get("pipeline", {})).enabled:
        return TradingPipeline(config, memory=memory).run_cycle()
    return _run_once_monolithic(config, memory)


def _run_once_monolithic(config: dict[str, Any], memory: TradingMemory | None = None) -> list[ExecutionRecord]:
    strategy = strategy_from_config(config.get("strategy", {}))
    risk = risk_from_config(config.get("risk", {}))
    execution = execution_from_config(config.get("execution", {}), fallback_mode=risk.mode)
    auto_exec = auto_execution_from_config(config.get("auto_execution", {}))
    static_assets = assets_from_config(config.get("assets", []))
    is_live = execution.mode == LIVE or risk.mode == LIVE
    memory = memory or TradingMemory.load(execution.state_path if is_live else None)
    if is_live:
        reset_server_time_offset()
    account_error: str | None = None
    portfolio_for_discovery: PaperPortfolio | None = None
    if is_live:
        try:
            portfolio = fetch_live_portfolio(execution, static_assets)
            portfolio_for_discovery = portfolio
        except Exception as exc:
            account_error = str(exc)
            portfolio = PaperPortfolio.from_config(config.get("portfolio", {}))
            portfolio_for_discovery = portfolio
    else:
        portfolio = PaperPortfolio.from_config(config.get("portfolio", {}))
        portfolio_for_discovery = portfolio

    if is_live and memory is not None:
        from trading_costs import maybe_refresh_trading_costs

        maybe_refresh_trading_costs(config, memory, portfolio)
        memory.save(execution.state_path)

    from market_discovery import discovery_from_config, resolve_trading_universe

    assets, discovery_scan = resolve_trading_universe(config, portfolio_for_discovery, static_assets)
    discovery_cfg = discovery_from_config(config.get("market_discovery", {}))
    watchlist_by_symbol = {
        (entry["symbol"], entry["market"]): entry for entry in discovery_scan.get("watchlist", [])
    }
    priority_index = {
        (entry["symbol"], entry["market"]): index for index, entry in enumerate(discovery_scan.get("watchlist", []))
    }
    assets = sorted(assets, key=lambda asset: priority_index.get((normalize_symbol(asset.symbol), asset.market), 999))

    from benchmark_gate import benchmark_gate_from_config, evaluate_benchmark_gate

    benchmark_state = evaluate_benchmark_gate(benchmark_gate_from_config(config.get("risk", {}).get("benchmark_gate")))

    from market_context import evaluate_market_context, market_context_from_config, save_market_context_snapshot
    from trade_outcomes import compute_trade_learning_snapshot, trade_learning_from_config
    from trade_lessons import load_lessons_document, refresh_trade_lessons, trade_lessons_from_config

    ctx_cfg = market_context_from_config(config.get("market_context"))
    learning_cfg = trade_learning_from_config(config.get("trade_learning"))
    lessons_cfg = trade_lessons_from_config(config.get("trade_lessons"))
    market_ctx = evaluate_market_context(ctx_cfg)
    if ctx_cfg.enabled:
        save_market_context_snapshot(ctx_cfg.snapshot_path, market_ctx)
    trade_learning = compute_trade_learning_snapshot(learning_cfg)
    trade_lessons = refresh_trade_lessons(lessons_cfg, learning_cfg.outcomes_path)
    if not trade_lessons.get("losses") and not trade_lessons.get("wins"):
        trade_lessons = load_lessons_document(lessons_cfg.lessons_path)

    from supervisor_hints import supervisor_hints_from_config

    supervisor_hints = supervisor_hints_from_config(config)

    from bucket_strategy import bucket_strategy_from_config
    from shadow_paper import shadow_paper_from_config

    bucket_strategy_cfg = bucket_strategy_from_config(config.get("bucket_strategy"))
    shadow_paper_cfg = shadow_paper_from_config(config.get("shadow_paper"))
    from quadrant_strategy import apply_quadrant_signal, quadrant_strategy_from_config

    quadrant_cfg = quadrant_strategy_from_config(config.get("quadrant_strategy"))
    from entry_timing import entry_timing_from_config

    entry_timing_cfg = entry_timing_from_config(config.get("entry_timing"))
    from persona_council import persona_council_from_config

    persona_council_cfg = persona_council_from_config(config.get("persona_council"))
    from capital_scaling import capital_scaling_from_config

    capital_scaling_cfg = capital_scaling_from_config(config.get("capital_scaling"))

    if is_live and account_error is None:
        external_outcomes = process_live_position_closures_at_cycle_start(
            memory, portfolio, learning_cfg, execution
        )
        if external_outcomes:
            trade_learning = compute_trade_learning_snapshot(learning_cfg)

    records: list[ExecutionRecord] = []
    from shadow_paper import reset_blocked_counterfactual_cycle

    reset_blocked_counterfactual_cycle()
    cycle_prices: dict[str, Decimal] = {}
    for asset in assets:
        provider = build_provider(asset)
        snapshot = MarketSnapshot(asset=asset, bars=provider.get_bars(asset), observed_at=int(time.time()))
        cycle_prices[normalize_symbol(asset.symbol)] = snapshot.price
        watch_entry = watchlist_by_symbol.get((normalize_symbol(asset.symbol), asset.market), {})
        signal = build_signal(snapshot, strategy)
        indicator_patch = dict(signal.indicators)
        indicator_patch["discovery_bucket"] = str(watch_entry.get("bucket") or "")
        indicator_patch["discovery_source"] = str(watch_entry.get("source") or "")
        if watch_entry.get("price_change_pct") is not None:
            indicator_patch["price_change_pct_24h"] = str(
                normalize_price_change_pct_24h(watch_entry.get("price_change_pct"))
            )
        signal = replace(signal, indicators=indicator_patch)
        signal = apply_quadrant_signal(
            signal,
            quadrant_cfg,
            bucket=str(watch_entry.get("bucket") or ""),
            source=str(watch_entry.get("source") or ""),
        )
        from entry_economics import suppress_ineligible_discovery_short

        signal = suppress_ineligible_discovery_short(
            signal,
            discovery_short_mode=str(strategy.discovery_short_mode),
            allow_discovery_shorts=bool(strategy.allow_discovery_shorts),
            profitability_raw=None,
        )
        sym = normalize_symbol(asset.symbol)
        pos = portfolio.positions.get(sym)
        if pos is not None and pos.quantity != 0:
            sync_position_peak(memory, sym, snapshot.price, pos.quantity)
        signal = apply_holding_priority_signal(
            snapshot,
            signal,
            strategy,
            portfolio,
            memory,
            risk,
            supervisor_hints,
            trade_learning=trade_learning,
        )
        decision = apply_risk_controls(
            snapshot, signal, strategy, risk, portfolio, memory, auto_exec=auto_exec, benchmark_gate=benchmark_state,
            execution=execution,
            market_context=market_ctx,
            trade_learning=trade_learning,
            trade_learning_cfg=learning_cfg,
            trade_lessons=trade_lessons,
            trade_lessons_cfg=lessons_cfg,
            market_context_cfg=ctx_cfg,
            bucket_strategy_cfg=bucket_strategy_cfg,
            quadrant_strategy_cfg=quadrant_cfg,
            entry_timing_cfg=entry_timing_cfg,
            profitability_raw=None,
            persona_council_cfg=persona_council_cfg,
            capital_scaling_cfg=capital_scaling_cfg,
        )
        analyze_only = discovery_cfg.enabled and watch_entry and not watch_entry.get("executable", True)
        if analyze_only:
            gate_reason = "regime/score gate"
            if not discovery_cfg.trade_discovered:
                gate_reason = "trade_discovered=false"
            elif watch_entry.get("regime_pass") is False:
                gate_reason = f"regime mismatch ({watch_entry.get('regime_kind', 'unknown')})"
            elif watch_entry.get("block_reason") == "min_notional_exceeds_max_trade_quote":
                gate_reason = "max_trade_quote below exchange min notional"
            from trading_pipeline import should_override_discovery_gate

            discovery_override = should_override_discovery_gate(
                decision=decision,
                signal=signal,
                watch_entry=watch_entry,
                trade_learning=trade_learning,
            )
            if not discovery_override:
                decision = RiskDecision(
                    False,
                    BLOCKED,
                    decision.reasons,
                    decision.blocked_reasons + [f"Discovery non-executable for {asset.symbol}; {gate_reason}."],
                    decision.order,
                    decision.effective_risk,
                )
            else:
                try:
                    signal.indicators["discovery_gate_override"] = gate_reason
                except Exception:
                    pass
        if account_error:
            decision = RiskDecision(
                False,
                BLOCKED,
                decision.reasons,
                decision.blocked_reasons + [f"Live account refresh failed: {account_error}"],
                None,
                decision.effective_risk,
            )
        effective_risk = decision.effective_risk or risk
        rationale = build_trade_rationale(
            snapshot, signal, decision, effective_risk, portfolio, memory,
            market_context=market_ctx, trade_learning=trade_learning, trade_lessons=trade_lessons,
        )
        if discovery_scan.get("enabled"):
            rationale["marketDiscovery"] = {
                "source": watch_entry.get("source", "unknown"),
                "executable": watch_entry.get("executable", True),
                "regimeKind": watch_entry.get("regime_kind"),
                "regimePass": watch_entry.get("regime_pass"),
                "discoveryScore": watch_entry.get("discovery_score"),
                "bucket": watch_entry.get("bucket"),
                "priceChangePct24h": watch_entry.get("price_change_pct"),
                "rangePosition24h": watch_entry.get("range_position_24h"),
                "quoteVolume24h": watch_entry.get("quote_volume"),
                "universeSize": discovery_scan.get("filters", {}),
            }
        if benchmark_state.get("enabled"):
            rationale["benchmarkGate"] = benchmark_state
        record = execute_decision(
            snapshot, decision, signal, strategy, effective_risk, portfolio, memory, execution, rationale,
            auto_exec=auto_exec, trade_learning_cfg=learning_cfg, trade_learning=trade_learning,
            shadow_paper_cfg=shadow_paper_cfg,
            watch_entry=watch_entry or None,
            quadrant_strategy_cfg=quadrant_cfg,
        )
        append_ledger(config.get("ledger_path"), record)
        records.append(record)
        if is_live and record.status == "executed_live":
            try:
                portfolio = fetch_live_portfolio(execution, assets)
            except Exception:
                pass
    if is_live and account_error is None:
        try:
            portfolio = fetch_live_portfolio(execution, assets)
        except Exception:
            pass
        process_live_position_tracking_at_cycle_end(
            memory,
            portfolio,
            execution,
            auto_exec,
            strategy,
            trade_learning=trade_learning,
        )
    if shadow_paper_cfg.enabled and cycle_prices:
        from shadow_paper import run_shadow_paper_cycle

        run_shadow_paper_cycle(
            shadow_paper_cfg,
            prices=cycle_prices,
            observed_at=int(time.time()),
            take_profit_pct=shadow_paper_cfg.take_profit_pct,
            stop_loss_pct=shadow_paper_cfg.stop_loss_pct,
        )
        trade_learning = compute_trade_learning_snapshot(learning_cfg)
    if is_live:
        memory.save(execution.state_path)
    return records


def print_records(records: list[ExecutionRecord]) -> None:
    for record in records:
        print(record_to_stdout_json(record))


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe market-aware autonomous paper trading agent.")
    parser.add_argument("--config", default="market_autotrader.example.json", help="Path to JSON config.")
    parser.add_argument("--once", action="store_true", help="Run one decision cycle and exit.")
    parser.add_argument(
        "--audit-protection",
        action="store_true",
        help="Audit TP/SL coverage for all open futures positions (read-only).",
    )
    parser.add_argument(
        "--attach-protection-all",
        action="store_true",
        help="Attach missing TP/SL for every unprotected open futures position.",
    )
    parser.add_argument(
        "--attach-protection",
        metavar="SYMBOL",
        help="Attach missing TP/SL to an open futures position (e.g. BASEDUSDT). Requires live armed.",
    )
    parser.add_argument(
        "--list-algo-orders",
        action="store_true",
        help="List open futures conditional (algo) TP/SL orders (read-only).",
    )
    parser.add_argument(
        "--cleanup-protection",
        action="store_true",
        help="Cancel duplicate/stale futures TP/SL algo orders; optionally reattach one set per position.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="With --cleanup-protection: actually cancel/replace orders (default is dry-run preview).",
    )
    parser.add_argument(
        "--worker",
        choices=["all", "poller", "decision", "execution"],
        default="all",
        help="Pipeline worker stage (requires pipeline.enabled in config).",
    )
    parser.add_argument("--cycle-id", default="", help="Optional pipeline cycle id for standalone workers.")
    args = parser.parse_args()

    config = load_config(args.config)
    from proxy_http import configure_socks5_from_dict, proxy_status

    socks5_info = configure_socks5_from_dict(config.get("execution", {}).get("socks5_proxy"))
    poll_seconds = int(config.get("poll_seconds", 60))
    risk = risk_from_config(config.get("risk", {}))
    execution = execution_from_config(config.get("execution", {}), fallback_mode=risk.mode)
    try:
        validate_risk_execution_mode_alignment(risk, execution)
    except ValueError as exc:
        print_json({"configError": str(exc)})
        return 1
    is_live = execution.mode == LIVE or risk.mode == LIVE
    memory = TradingMemory.load(execution.state_path if is_live else None)
    print_json({"socks5Proxy": {**proxy_status(), **socks5_info}})
    if is_live:
        arm_status = live_trading_arm_status(risk.live_arm_path)
        print_json(
            {
                "liveTrading": True,
                "armed": bool(arm_status.get("armed")),
                "armStatus": arm_status,
                "killSwitch": kill_switch_active(risk.kill_switch_path),
                "allowLiveTrading": risk.allow_live_trading,
                "maxTradeQuote": str(risk.max_trade_quote),
                "maxDailyLossQuote": str(risk.max_daily_loss_quote),
            }
        )

    if args.audit_protection or args.attach_protection_all:
        if not is_live:
            print_json({"error": "protection audit requires live mode"})
            return 1
        auto_exec = auto_execution_from_config(config.get("auto_execution", {}))
        reset_server_time_offset()
        if args.audit_protection and not args.attach_protection_all:
            print_json(audit_futures_positions_protection(execution, auto_exec), indent=2)
            return 0
        if args.attach_protection_all:
            if kill_switch_active(risk.kill_switch_path):
                print_json({"error": "kill switch active"})
                return 1
            if not live_trading_armed(risk.live_arm_path):
                print_json({"error": "live not armed"})
                return 1
            outcome = attach_protection_all_open_positions(execution, auto_exec)
            audit_path = Path("logs/position-protection-audit-latest.json")
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            audit_path.write_text(json.dumps(outcome, ensure_ascii=False, indent=2), encoding="utf-8")
            print_json(outcome, indent=2)
            return 0

    if args.list_algo_orders:
        if not is_live:
            print_json({"error": "list-algo-orders requires live mode"})
            return 1
        reset_server_time_offset()
        print_json(audit_futures_algo_protection_orders(execution), indent=2)
        return 0

    if args.cleanup_protection:
        if not is_live:
            print_json({"error": "cleanup-protection requires live mode"})
            return 1
        if args.execute:
            if kill_switch_active(risk.kill_switch_path):
                print_json({"error": "kill switch active"})
                return 1
            if not live_trading_armed(risk.live_arm_path):
                print_json({"error": "live not armed"})
                return 1
        auto_exec = auto_execution_from_config(config.get("auto_execution", {}))
        reset_server_time_offset()
        outcome = cleanup_futures_protection_orders(
            execution,
            auto_exec,
            dry_run=not args.execute,
            reattach=True,
        )
        audit_path = Path("logs/protection-cleanup-latest.json")
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(json.dumps(outcome, ensure_ascii=False, indent=2), encoding="utf-8")
        print_json(outcome, indent=2, default=str)
        return 0

    if args.attach_protection:
        if not is_live:
            print_json({"error": "attach-protection requires live mode"})
            return 1
        if kill_switch_active(risk.kill_switch_path):
            print_json({"error": "kill switch active"})
            return 1
        if not live_trading_armed(risk.live_arm_path):
            print_json({"error": "live not armed"})
            return 1
        auto_exec = auto_execution_from_config(config.get("auto_execution", {}))
        reset_server_time_offset()
        result: dict[str, Any]
        try:
            result = ensure_futures_position_protection(
                args.attach_protection,
                execution,
                auto_exec,
            )
            append_ledger(
                config.get("ledger_path"),
                ExecutionRecord(
                    status=str(result.get("status", "protection_attach")),
                    mode=LIVE,
                    symbol=normalize_symbol(args.attach_protection),
                    action="PROTECTION",
                    quantity=decimal_from(result.get("quantity", "0") or "0"),
                    quote_amount=Decimal("0"),
                    price=decimal_from(result.get("entryPrice", "0") or "0"),
                    reasons=[f"CLI attach-protection for {args.attach_protection}"],
                    blocked_reasons=[],
                    indicators={},
                    portfolio={},
                    timestamp=int(time.time()),
                    execution_details=result,
                    trade_rationale={"summary": f"attach-protection {result.get('status')}"},
                ),
            )
            print_json(result, default=str)
        except Exception as exc:
            print_json({"error": str(exc), "symbol": args.attach_protection})
            return 1
        return 0 if result.get("status") in {"protection_attached", "already_protected"} else 1

    from trading_pipeline import TradingPipeline, pipeline_from_config

    pipeline_enabled = pipeline_from_config(config.get("pipeline", {})).enabled

    def run_cycle() -> list[ExecutionRecord]:
        if pipeline_enabled and args.worker != "all":
            pipeline = TradingPipeline(config, memory=memory)
            cycle_id = args.cycle_id or None
            if args.worker == "poller":
                pipeline.run_poller_only(cycle_id)
                return []
            if args.worker == "decision":
                pipeline.run_decision_only(cycle_id)
                return []
            if args.worker == "execution":
                return pipeline.run_execution_only(cycle_id)
        return run_once(config, memory=memory)

    while True:
        try:
            records = run_cycle()
            print_records(records)
        except Exception as exc:
            error_path = Path("logs/pipeline-error-latest.txt")
            error_path.parent.mkdir(parents=True, exist_ok=True)
            error_path.write_text(traceback.format_exc(), encoding="utf-8")
            print_json({"pipelineError": str(exc), "timestamp": int(time.time())})
        if args.once:
            return 0
        time.sleep(poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())

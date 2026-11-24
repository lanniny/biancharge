"""Paper/backtest research for BTCUSDT perpetuals and US cash stocks/ETFs.

The CLI and configs in this module are research-only. They refuse live modes,
do not read credential files, and do not submit exchange or broker orders.
Existing live gates in ``market_autotrader.py`` are left unchanged.

Accounting is product-specific:

* ``binance_usdt_perp`` simulates an isolated USDT-M perpetual (margin,
  leverage, fees, slippage, mark/index). Funding is charged only when a
  bar supplies a rate; otherwise the report marks funding unavailable.
* ``us_cash_equity`` and ``us_cash_etf`` are cash long-only books. They are
  not stock options, CME futures, or Binance TradFi synthetic perps.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from research_calendar import is_nyse_session


MODE_BACKTEST = "backtest"
MODE_PAPER = "paper"
ALLOWED_MODES = frozenset({MODE_BACKTEST, MODE_PAPER})

PRODUCT_PERP = "binance_usdt_perp"
PRODUCT_STOCK = "us_cash_equity"
PRODUCT_ETF = "us_cash_etf"
CASH_PRODUCTS = frozenset({PRODUCT_STOCK, PRODUCT_ETF})
SUPPORTED_PRODUCTS = frozenset({PRODUCT_PERP, *CASH_PRODUCTS})

# Binance USDT-M tickers such as AAPLUSDT are exchange synthetics. This
# research path does not treat them as Nasdaq shares, listed ETFs, or a
# substitute for a derivatives adapter.
TRADFI_SYNTHETIC_BASES = frozenset(
    {
        "AAPL",
        "AMZN",
        "COIN",
        "GOOG",
        "GOOGL",
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
        "SPY",
        "QQQ",
        "IWM",
        "DIA",
        "GLD",
        "TLT",
        "HYG",
        "XLF",
        "ARKK",
        "XAU",
        "XAG",
        "PAXG",
        "SPX",
        "US500",
        "NAS100",
        "US30",
        "HK50",
    }
)

UNSUPPORTED_PRODUCT_MESSAGES = {
    "option": "美股/ETF 期权没有适配器，研究路径拒绝模拟。",
    "us_listed_option": "美股上市期权没有适配器，研究路径拒绝模拟。",
    "index_option": "指数期权没有适配器，研究路径拒绝模拟。",
    "cme_future": "CME 期货没有适配器，研究路径拒绝模拟。",
    "cme_futures": "CME 期货没有适配器，研究路径拒绝模拟。",
    "listed_future": "上市期货没有适配器，研究路径拒绝模拟。",
    "listed_futures": "上市期货没有适配器，研究路径拒绝模拟。",
    "generic_contract": "不提供泛化合约模拟，避免把现金 ETF 或合成行情说成衍生品。",
}

BANNED_CONFIG_KEYS = frozenset(
    {
        "api_key",
        "api_secret",
        "apisecret",
        "secret",
        "password",
        "token",
        "private_key",
        "socks_proxy",
        "proxy",
        "proxy_url",
    }
)

BPS = Decimal("10000")


class ResearchSafetyError(Exception):
    """Fail-closed refusal for live trading, unsupported products, or bad research config."""


@dataclass(frozen=True)
class ResearchBar:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    mark_open: Decimal | None = None
    mark_high: Decimal | None = None
    mark_low: Decimal | None = None
    mark_close: Decimal | None = None
    index_close: Decimal | None = None
    funding_rate: Decimal | None = None
    session_daily: bool = False


@dataclass(frozen=True)
class FundingSettlement:
    timestamp: datetime
    rate: Decimal
    mark: Decimal | None


@dataclass(frozen=True)
class Dividend:
    timestamp: datetime
    amount: Decimal


@dataclass(frozen=True)
class Split:
    timestamp: datetime
    ratio: Decimal
    already_in_prices: bool = False


@dataclass
class MarketAux:
    funding: dict[str, list[FundingSettlement]] = field(default_factory=dict)
    dividends: dict[str, list[Dividend]] = field(default_factory=dict)
    splits: dict[str, list[Split]] = field(default_factory=dict)
    funding_complete: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class StrategySpec:
    name: str
    fast_window: int
    slow_window: int
    momentum_window: int
    momentum_threshold: Decimal
    stop_pct: Decimal


@dataclass(frozen=True)
class RiskSpec:
    risk_per_trade_pct: Decimal
    max_position_pct: Decimal
    max_drawdown_pct: Decimal
    max_concurrent_positions: int


@dataclass(frozen=True)
class SessionSpec:
    timezone: str
    open_hhmm: str
    close_hhmm: str


@dataclass(frozen=True)
class AssetSpec:
    symbol: str
    product: str
    quote_asset: str
    csv_path: str | None
    fee_bps: Decimal
    slippage_bps: Decimal
    leverage: int
    allow_short: bool
    maintenance_margin_rate: Decimal
    weight: Decimal | None = None
    source_type: str = ""
    source_interval: str = "1d"
    source_start: str = ""
    source_end: str = ""


@dataclass(frozen=True)
class ResearchConfig:
    mode: str
    data_label: str
    starting_equity: Decimal
    sample_split: datetime | None
    strategy: StrategySpec
    risk: RiskSpec
    session: SessionSpec
    assets: tuple[AssetSpec, ...]
    report_path: str | None
    fills_path: str | None
    notes: tuple[str, ...]
    config_dir: Path
    stress_multiplier: Decimal = Decimal("2")
    parameters_locked: bool = False
    study: bool = False


@dataclass
class Position:
    symbol: str
    product: str
    side: str
    qty: Decimal
    entry_price: Decimal
    entry_time: datetime
    stop_price: Decimal
    margin: Decimal
    leverage: int


@dataclass
class Book:
    kind: str
    currency: str
    cash: Decimal
    starting: Decimal
    positions: dict[str, Position] = field(default_factory=dict)
    last_marks: dict[str, Decimal] = field(default_factory=dict)
    fees: Decimal = Decimal("0")
    slippage: Decimal = Decimal("0")
    realized: Decimal = Decimal("0")
    funding_pnl: Decimal = Decimal("0")
    funding_events: int = 0
    peak: Decimal = Decimal("0")
    max_drawdown: Decimal = Decimal("0")
    max_drawdown_limit: Decimal = Decimal("1")
    halted: bool = False
    sleeve_cash: dict[str, Decimal] = field(default_factory=dict)
    funding_mark_missing: int = 0
    curve: list[tuple[datetime, Decimal]] = field(default_factory=list)
    ledger: list[dict[str, Any]] = field(default_factory=list)


def _dec(value: Any, field_name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - surface the field name
        raise ResearchSafetyError(f"invalid decimal for {field_name}") from exc


def parse_timestamp(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ResearchSafetyError(f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ResearchSafetyError(f"timestamp must include a timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def _iter_keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _iter_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_keys(child)


def _reject_credential_fields(payload: Any) -> None:
    found = sorted({key.lower() for key in _iter_keys(payload)} & BANNED_CONFIG_KEYS)
    if found:
        names = ", ".join(found)
        raise ResearchSafetyError(f"research config refuses credential or proxy fields: {names}")


def _unsupported_product_message(product: str) -> str | None:
    key = product.strip().lower()
    if key in SUPPORTED_PRODUCTS:
        return None
    if key in UNSUPPORTED_PRODUCT_MESSAGES:
        return UNSUPPORTED_PRODUCT_MESSAGES[key]
    if "option" in key:
        return UNSUPPORTED_PRODUCT_MESSAGES["option"]
    if key.startswith("cme") or "cme_future" in key:
        return UNSUPPORTED_PRODUCT_MESSAGES["cme_future"]
    if "future" in key or "contract" in key:
        return (
            f"产品 {product!r} 不受支持。研究路径只做 Binance USDT-M 永续模拟"
            "和美股/ETF 现金买卖，不模拟期权、CME 期货或其他泛化合约。"
        )
    return f"产品 {product!r} 不受支持。允许的产品：{', '.join(sorted(SUPPORTED_PRODUCTS))}。"


def _symbol_base(symbol: str) -> str:
    upper = symbol.upper()
    if upper.endswith("USDT"):
        return upper[:-4]
    return upper


def _validate_symbol(symbol: str, product: str) -> str:
    upper = symbol.strip().upper().replace("/", "").replace("-", "")
    if not upper or not upper.replace(".", "").isalnum():
        raise ResearchSafetyError(f"invalid symbol: {symbol!r}")
    if product == PRODUCT_PERP:
        if not upper.endswith("USDT"):
            raise ResearchSafetyError(f"{upper} is not a USDT perpetual symbol")
        base = _symbol_base(upper)
        if base in TRADFI_SYNTHETIC_BASES:
            raise ResearchSafetyError(
                f"{upper} is a Binance TradFi-style ticker, not a Nasdaq share or listed ETF. "
                "This research path does not simulate it. Use the cash ticker "
                f"({base}) with us_cash_equity or us_cash_etf, and only with cash-equity data."
            )
        return upper
    if upper.endswith("USDT") or upper.endswith("USD"):
        raise ResearchSafetyError(
            f"{upper} is not a cash equity/ETF ticker. Cash products use symbols such as "
            "AAPL, SPY, and QQQ. Binance *USDT synthetics are not listed shares."
        )
    if not upper.isalpha() or not 1 <= len(upper) <= 6:
        raise ResearchSafetyError(f"invalid cash ticker: {symbol!r}")
    return upper


def _hhmm(value: str, field_name: str) -> str:
    text = str(value).strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ResearchSafetyError(f"{field_name} must be HH:MM")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ResearchSafetyError(f"{field_name} must be HH:MM")
    return f"{hour:02d}:{minute:02d}"


def config_from_mapping(payload: dict[str, Any], *, config_dir: Path | None = None) -> ResearchConfig:
    if not isinstance(payload, dict):
        raise ResearchSafetyError("config must be a JSON object")
    _reject_credential_fields(payload)
    mode = str(payload.get("mode", "")).strip().lower()
    if mode not in ALLOWED_MODES:
        raise ResearchSafetyError(
            "research path is paper/backtest only; refusing mode "
            f"{payload.get('mode')!r}. Live orders, test orders, and leverage changes are not available here."
        )
    if payload.get("allow_live_trading") or payload.get("live") or payload.get("send_orders"):
        raise ResearchSafetyError("research path refuses live trading flags")

    label = str(payload.get("data_label", "")).strip()
    if not label:
        raise ResearchSafetyError("data_label is required so reports cannot omit whether data is synthetic")

    strategy_raw = payload.get("strategy") or {}
    if str(strategy_raw.get("name", "baseline_sma_momentum")) != "baseline_sma_momentum":
        raise ResearchSafetyError("only the baseline_sma_momentum strategy is implemented on this path")
    fast = int(strategy_raw.get("fast_window", 8))
    slow = int(strategy_raw.get("slow_window", 21))
    momentum_window = int(strategy_raw.get("momentum_window", 6))
    if fast < 1 or slow <= fast or momentum_window < 1:
        raise ResearchSafetyError("strategy windows must satisfy 1 <= fast < slow and momentum_window >= 1")
    stop_pct = _dec(strategy_raw.get("stop_pct", "0.02"), "strategy.stop_pct")
    if stop_pct <= 0 or stop_pct >= 1:
        raise ResearchSafetyError("strategy.stop_pct must be between 0 and 1")
    strategy = StrategySpec(
        name="baseline_sma_momentum",
        fast_window=fast,
        slow_window=slow,
        momentum_window=momentum_window,
        momentum_threshold=_dec(strategy_raw.get("momentum_threshold", "0"), "strategy.momentum_threshold"),
        stop_pct=stop_pct,
    )

    risk_raw = payload.get("risk") or {}
    risk = RiskSpec(
        risk_per_trade_pct=_dec(risk_raw.get("risk_per_trade_pct", "0.01"), "risk.risk_per_trade_pct"),
        max_position_pct=_dec(risk_raw.get("max_position_pct", "0.25"), "risk.max_position_pct"),
        max_drawdown_pct=_dec(risk_raw.get("max_drawdown_pct", "0.2"), "risk.max_drawdown_pct"),
        max_concurrent_positions=int(risk_raw.get("max_concurrent_positions", 3)),
    )
    if risk.risk_per_trade_pct < 0 or risk.max_position_pct < 0 or risk.max_drawdown_pct < 0:
        raise ResearchSafetyError("risk percentages must be >= 0")
    if risk.max_concurrent_positions < 1:
        raise ResearchSafetyError("max_concurrent_positions must be >= 1")

    session_raw = payload.get("equity_session") or {}
    timezone_name = str(session_raw.get("timezone", "America/New_York"))
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:  # noqa: BLE001
        raise ResearchSafetyError(f"unknown equity session timezone: {timezone_name}") from exc
    session = SessionSpec(
        timezone=timezone_name,
        open_hhmm=_hhmm(session_raw.get("open", "09:30"), "equity_session.open"),
        close_hhmm=_hhmm(session_raw.get("close", "16:00"), "equity_session.close"),
    )

    costs = payload.get("costs") or {}
    default_fee = _dec(costs.get("fee_bps", "5"), "costs.fee_bps")
    default_slip = _dec(costs.get("slippage_bps", "5"), "costs.slippage_bps")
    perp_raw = payload.get("perp") or {}
    default_leverage = int(perp_raw.get("leverage", 1))
    maintenance = _dec(perp_raw.get("maintenance_margin_rate", "0.004"), "perp.maintenance_margin_rate")
    if maintenance < 0 or maintenance >= 1:
        raise ResearchSafetyError("perp.maintenance_margin_rate must be in [0, 1)")
    if default_fee < 0 or default_slip < 0:
        raise ResearchSafetyError("fee_bps and slippage_bps must be >= 0")

    assets_raw = payload.get("assets")
    if not isinstance(assets_raw, list) or not assets_raw:
        raise ResearchSafetyError("config assets must be a non-empty list")
    assets: list[AssetSpec] = []
    seen: set[str] = set()
    for item in assets_raw:
        if not isinstance(item, dict):
            raise ResearchSafetyError("each asset must be an object")
        product = str(item.get("product", "")).strip().lower()
        unsupported = _unsupported_product_message(product)
        if unsupported:
            raise ResearchSafetyError(unsupported)
        symbol = _validate_symbol(str(item.get("symbol", "")), product)
        if symbol in seen:
            raise ResearchSafetyError(f"duplicate symbol: {symbol}")
        seen.add(symbol)
        if product in CASH_PRODUCTS and item.get("allow_short") is True:
            raise ResearchSafetyError(
                f"{symbol} is cash equity/ETF. Short stock, options, and CME futures are not simulated."
            )
        if product in CASH_PRODUCTS:
            leverage = int(item.get("leverage", 1))
            if leverage != 1:
                raise ResearchSafetyError(
                    f"{symbol} cash equity/ETF refuses leverage {leverage}. "
                    "This book buys shares with cash; it is not a margin or contract account."
                )
            allow_short = False
            quote = str(item.get("quote_asset", "USD")).upper()
            if quote != "USD":
                raise ResearchSafetyError("cash equity/ETF quote_asset must be USD")
        else:
            leverage = int(item.get("leverage", default_leverage))
            if leverage < 1 or leverage > 125:
                raise ResearchSafetyError(f"{symbol} simulated leverage must be from 1 to 125")
            allow_short = bool(item.get("allow_short", False))
            quote = str(item.get("quote_asset", "USDT")).upper()
            if quote != "USDT":
                raise ResearchSafetyError("binance_usdt_perp quote_asset must be USDT")
        fee_bps = _dec(item.get("fee_bps", default_fee), f"{symbol}.fee_bps")
        slip_bps = _dec(item.get("slippage_bps", default_slip), f"{symbol}.slippage_bps")
        if fee_bps < 0 or slip_bps < 0:
            raise ResearchSafetyError("fee_bps and slippage_bps must be >= 0")
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        source_type = str(source.get("type", "")).strip()
        if source_type in {"binance_fapi", "alpaca"}:
            raise ResearchSafetyError(f"{symbol} source {source_type} is not available on the credential-free research path")
        csv_path = item.get("csv")
        if not csv_path and not source_type:
            raise ResearchSafetyError(f"{symbol} needs a csv path or a public source")
        weight = _dec(item["weight"], f"{symbol}.weight") if item.get("weight") not in (None, "") else None
        if weight is not None and weight <= 0:
            raise ResearchSafetyError(f"{symbol} weight must be positive")
        assets.append(
            AssetSpec(
                symbol=symbol,
                product=product,
                quote_asset=quote,
                csv_path=str(csv_path) if csv_path else None,
                fee_bps=fee_bps,
                slippage_bps=slip_bps,
                leverage=leverage,
                allow_short=allow_short,
                maintenance_margin_rate=maintenance,
                weight=weight,
                source_type=source_type,
                source_interval=str(source.get("interval", "1d")),
                source_start=str(source.get("start", "")),
                source_end=str(source.get("end", "")),
            )
        )

    split_raw = payload.get("sample_split_timestamp")
    starting = _dec(payload.get("starting_equity", "10000"), "starting_equity")
    if starting <= 0:
        raise ResearchSafetyError("starting_equity must be positive")
    notes = tuple(str(item) for item in payload.get("notes", []))
    return ResearchConfig(
        mode=mode,
        data_label=label,
        starting_equity=starting,
        sample_split=parse_timestamp(split_raw) if split_raw else None,
        strategy=strategy,
        risk=risk,
        session=session,
        assets=tuple(assets),
        report_path=payload.get("report_path"),
        fills_path=payload.get("fills_path"),
        notes=notes,
        config_dir=config_dir or Path("."),
        stress_multiplier=_dec(payload.get("stress_multiplier", "2"), "stress_multiplier"),
        parameters_locked=bool(payload.get("parameters_locked", False)),
        study=bool(payload.get("study", False)),
    )


def load_config(path: str | Path) -> ResearchConfig:
    config_path = Path(path)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    return config_from_mapping(payload, config_dir=config_path.parent)


def _optional_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    return Decimal(text)


def _require_ohlc(symbol: str, open_: Decimal, high: Decimal, low: Decimal, close: Decimal) -> None:
    if min(open_, high, low, close) <= 0:
        raise ResearchSafetyError(f"{symbol} prices must be positive")
    if high < max(open_, close) or low > min(open_, close) or low > high:
        raise ResearchSafetyError(f"{symbol} OHLC is inconsistent at open={open_} high={high} low={low} close={close}")


def parse_bars_csv(path: str | Path, symbol: str) -> list[ResearchBar]:
    csv_path = Path(path)
    if not csv_path.is_file():
        raise ResearchSafetyError(f"bar csv not found: {csv_path}")
    bars: list[ResearchBar] = []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        filtered = (line for line in handle if line.strip() and not line.lstrip().startswith("#"))
        reader = csv.DictReader(filtered)
        if reader.fieldnames is None:
            raise ResearchSafetyError(f"{csv_path} has no header")
        fields = {name.strip() for name in reader.fieldnames}
        required = {"timestamp", "open", "high", "low", "close", "volume"}
        missing = required - fields
        if missing:
            raise ResearchSafetyError(f"{csv_path} missing columns: {', '.join(sorted(missing))}")
        for row in reader:
            open_ = Decimal(row["open"])
            high = Decimal(row["high"])
            low = Decimal(row["low"])
            close = Decimal(row["close"])
            _require_ohlc(symbol, open_, high, low, close)
            bars.append(
                ResearchBar(
                    timestamp=parse_timestamp(row["timestamp"]),
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=Decimal(row.get("volume") or "0"),
                    mark_close=_optional_decimal(row.get("mark_close")),
                    index_close=_optional_decimal(row.get("index_close")),
                    funding_rate=_optional_decimal(row.get("funding_rate")),
                )
            )
    bars.sort(key=lambda bar: bar.timestamp)
    for prev, nxt in zip(bars, bars[1:]):
        if prev.timestamp == nxt.timestamp:
            raise ResearchSafetyError(f"{symbol} has duplicate timestamps {prev.timestamp.isoformat()}")
    if len(bars) < 2:
        raise ResearchSafetyError(f"{symbol} needs at least two bars")
    return bars


def sma(values: list[Decimal], window: int) -> Decimal:
    sample = values[-window:]
    return sum(sample, Decimal("0")) / Decimal(window)


def desired_side(closes: list[Decimal], strategy: StrategySpec, *, allow_short: bool) -> str | None:
    """Side implied by completed closes only. Returns None when history is short."""
    if len(closes) < strategy.slow_window or len(closes) <= strategy.momentum_window:
        return None
    fast = sma(closes, strategy.fast_window)
    slow = sma(closes, strategy.slow_window)
    anchor = closes[-1 - strategy.momentum_window]
    if anchor <= 0:
        return None
    momentum = (closes[-1] - anchor) / anchor
    if fast > slow and momentum >= strategy.momentum_threshold:
        return "long"
    if fast < slow and momentum <= -strategy.momentum_threshold and allow_short:
        return "short"
    return "flat"


def apply_slippage(price: Decimal, order_side: str, bps: Decimal) -> Decimal:
    adjustment = bps / BPS
    if order_side == "buy":
        filled = price * (Decimal("1") + adjustment)
    elif order_side == "sell":
        filled = price * (Decimal("1") - adjustment)
    else:
        raise ResearchSafetyError(f"unknown order side: {order_side}")
    if filled <= 0:
        raise ResearchSafetyError("slippage consumed the fill price")
    return filled


def liquidation_price(position: Position, maintenance_margin_rate: Decimal) -> Decimal | None:
    """Isolated liquidation price. None when the formula does not yield a positive price."""
    qty = position.qty
    if qty <= 0 or maintenance_margin_rate < 0 or maintenance_margin_rate >= 1:
        return None
    if position.side == "long":
        price = (position.entry_price * qty - position.margin) / (qty * (Decimal("1") - maintenance_margin_rate))
    else:
        price = (position.margin + position.entry_price * qty) / (qty * (Decimal("1") + maintenance_margin_rate))
    if price <= 0:
        return None
    return price


def mark_price(bar: ResearchBar) -> Decimal:
    return bar.mark_close if bar.mark_close is not None else bar.close


def mark_shift(bar: ResearchBar) -> Decimal:
    if bar.mark_close is None:
        return Decimal("0")
    return bar.mark_close - bar.close


def in_regular_session(timestamp: datetime, session: SessionSpec) -> bool:
    local = timestamp.astimezone(ZoneInfo(session.timezone))
    if local.weekday() >= 5:
        return False
    open_h, open_m = (int(part) for part in session.open_hhmm.split(":"))
    close_h, close_m = (int(part) for part in session.close_hhmm.split(":"))
    minutes = local.hour * 60 + local.minute
    return (open_h * 60 + open_m) <= minutes < (close_h * 60 + close_m)


def _order_side_for_open(side: str) -> str:
    return "buy" if side == "long" else "sell"


def _order_side_for_close(side: str) -> str:
    return "sell" if side == "long" else "buy"


def unrealized(position: Position, price: Decimal) -> Decimal:
    if position.side == "long":
        return (price - position.entry_price) * position.qty
    return (position.entry_price - price) * position.qty


def _account_cash(book: Book, symbol: str) -> Decimal:
    if book.sleeve_cash:
        return book.sleeve_cash[symbol]
    return book.cash


def _set_account_cash(book: Book, symbol: str, value: Decimal) -> None:
    if book.sleeve_cash:
        book.sleeve_cash[symbol] = value
    else:
        book.cash = value


def book_equity(book: Book) -> Decimal:
    equity = sum(book.sleeve_cash.values(), Decimal("0")) if book.sleeve_cash else book.cash
    for position in book.positions.values():
        mark = book.last_marks.get(position.symbol, position.entry_price)
        if book.kind == "cash":
            equity += position.qty * mark
        else:
            equity += position.margin + unrealized(position, mark)
    return equity


def _iso(timestamp: datetime) -> str:
    return timestamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dec_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value.normalize(), "f")
    if text == "-0":
        return "0"
    return text


def _booked_net(book: Book) -> Decimal:
    """Account change from the ledger. This is the audited net, not a second mark-to-market rounding."""
    return sum((event["amount"] for event in book.ledger), Decimal("0"))


def _first_trade_hit(
    side: str,
    open_: Decimal,
    high: Decimal,
    low: Decimal,
    triggers: list[tuple[str, Decimal]],
) -> tuple[str, Decimal] | None:
    """First trigger touched while price moves away from the open. Prices are trade prices."""
    if side == "long":
        gapped = [(name, price) for name, price in triggers if open_ <= price]
        if gapped:
            name, _price = max(gapped, key=lambda item: item[1])
            return f"{name}_gap", open_
        ahead = [(name, price) for name, price in triggers if price < open_]
        if not ahead:
            return None
        name, price = max(ahead, key=lambda item: item[1])
        if low <= price:
            return name, price
        return None
    gapped = [(name, price) for name, price in triggers if open_ >= price]
    if gapped:
        name, _price = min(gapped, key=lambda item: item[1])
        return f"{name}_gap", open_
    ahead = [(name, price) for name, price in triggers if price > open_]
    if not ahead:
        return None
    name, price = min(ahead, key=lambda item: item[1])
    if high >= price:
        return name, price
    return None


def protective_exit(
    position: Position,
    bar: ResearchBar,
    maintenance_margin_rate: Decimal,
) -> tuple[str, Decimal] | None:
    """Return (reason, raw_fill_price) for a stop or isolated liquidation.

    When mark OHLC exists, the trigger is the mark path. A gap through the
    mark open fills at the trade open. An intrabar touch fills at the trigger
    itself: the bar does not say whether the high or the low printed first, so
    the adverse level is used and the favorable extreme is not a fill.
    Without mark OHLC, trade prices are used and a close-only mark is shifted
    onto that path. Index prices are ignored. Liquidation is a single
    maintenance-rate approximation, not Binance's tier schedule.
    """
    use_mark = None not in (bar.mark_open, bar.mark_high, bar.mark_low)
    if use_mark:
        open_, high, low = bar.mark_open, bar.mark_high, bar.mark_low
        gap_price = bar.open
    else:
        shift = mark_shift(bar)
        open_, high, low = bar.open + shift, bar.high + shift, bar.low + shift
        gap_price = bar.open
    assert open_ is not None and high is not None and low is not None
    triggers: list[tuple[str, Decimal]] = [("stop", position.stop_price)]
    if position.product == PRODUCT_PERP:
        liq = liquidation_price(position, maintenance_margin_rate)
        if liq is not None:
            triggers.append(("liquidation", liq if use_mark else liq - mark_shift(bar)))
    hit = _first_trade_hit(position.side, open_, high, low, triggers)
    if hit is None:
        return None
    reason, raw = hit
    if reason.endswith("_gap"):
        return reason, gap_price
    if not use_mark:
        return reason, raw - mark_shift(bar)
    return reason, raw


def _append_ledger(book: Book, timestamp: datetime, kind: str, amount: Decimal, symbol: str) -> None:
    book.ledger.append(
        {"time": timestamp, "kind": kind, "amount": amount, "symbol": symbol}
    )


def _close_position(
    book: Book,
    asset: AssetSpec,
    position: Position,
    *,
    timestamp: datetime,
    raw_price: Decimal,
    reason: str,
    signal_timestamp: datetime | None,
    fills: list[dict[str, Any]],
    bar: ResearchBar,
) -> None:
    order_side = _order_side_for_close(position.side)
    fill_price = apply_slippage(raw_price, order_side, asset.slippage_bps)
    price_pnl = unrealized(position, fill_price)
    fee = fill_price * position.qty * asset.fee_bps / BPS
    slippage_cost = abs(fill_price - raw_price) * position.qty
    cash = _account_cash(book, asset.symbol)
    if book.kind == "cash":
        cash += fill_price * position.qty - fee
    else:
        cash += position.margin + price_pnl - fee
    _set_account_cash(book, asset.symbol, cash)
    book.fees += fee
    book.slippage += slippage_cost
    book.realized += price_pnl
    _append_ledger(book, timestamp, "realized", price_pnl, asset.symbol)
    _append_ledger(book, timestamp, "fee", -fee, asset.symbol)
    del book.positions[asset.symbol]
    fills.append(
        _fill_record(
            asset=asset,
            timestamp=timestamp,
            action=f"close_{position.side}",
            reason=reason,
            qty=position.qty,
            raw_price=raw_price,
            fill_price=fill_price,
            fee=fee,
            slippage_cost=slippage_cost,
            signal_timestamp=signal_timestamp,
            mark=mark_price(bar),
            index_price=bar.index_close,
            stop_price=position.stop_price,
            realized=price_pnl,
        )
    )


def _fill_record(
    *,
    asset: AssetSpec,
    timestamp: datetime,
    action: str,
    reason: str,
    qty: Decimal,
    raw_price: Decimal,
    fill_price: Decimal,
    fee: Decimal,
    slippage_cost: Decimal,
    signal_timestamp: datetime | None,
    mark: Decimal,
    index_price: Decimal | None,
    stop_price: Decimal | None,
    realized: Decimal,
) -> dict[str, Any]:
    return {
        "timestamp": _iso(timestamp),
        "symbol": asset.symbol,
        "product": asset.product,
        "action": action,
        "reason": reason,
        "qty": _dec_str(qty),
        "raw_price": _dec_str(raw_price),
        "fill_price": _dec_str(fill_price),
        "fee": _dec_str(fee),
        "slippage_cost": _dec_str(slippage_cost),
        "signal_timestamp": _iso(signal_timestamp) if signal_timestamp else None,
        "mark_price": _dec_str(mark),
        "index_price": _dec_str(index_price),
        "stop_price": _dec_str(stop_price),
        "realized_price_pnl": _dec_str(realized),
        "index_used_for_pnl": False,
    }


def _sizing_equity(book: Book, symbol: str, mark: Decimal) -> Decimal:
    if book.sleeve_cash:
        cash = book.sleeve_cash[symbol]
        position = book.positions.get(symbol)
        if position is None:
            return cash
        if book.kind == "cash":
            return cash + position.qty * mark
        return cash + position.margin + unrealized(position, mark)
    saved = book.last_marks.get(symbol)
    book.last_marks[symbol] = mark
    try:
        return book_equity(book)
    finally:
        if saved is None:
            book.last_marks.pop(symbol, None)
        else:
            book.last_marks[symbol] = saved


def _try_open(
    book: Book,
    asset: AssetSpec,
    bar: ResearchBar,
    side: str,
    strategy: StrategySpec,
    risk: RiskSpec,
    session: SessionSpec,
    signal_timestamp: datetime,
    fills: list[dict[str, Any]],
    rejections: list[dict[str, Any]],
) -> None:
    reason = None
    if book.halted:
        reason = "max_drawdown_halt"
    elif asset.product in CASH_PRODUCTS and (reason := cash_entry_block_reason(bar, session)):
        pass
    elif len(book.positions) >= risk.max_concurrent_positions:
        reason = "max_concurrent_positions"
    elif risk.risk_per_trade_pct <= 0:
        reason = "risk_per_trade_pct"
    elif risk.max_position_pct <= 0:
        reason = "max_position_pct"
    if reason:
        rejections.append(
            {
                "timestamp": _iso(bar.timestamp),
                "symbol": asset.symbol,
                "product": asset.product,
                "desired": side,
                "reason": reason,
            }
        )
        return

    raw_price = bar.open
    fill_price = apply_slippage(raw_price, _order_side_for_open(side), asset.slippage_bps)
    equity = _sizing_equity(book, asset.symbol, raw_price)
    if equity <= 0:
        rejections.append(
            {
                "timestamp": _iso(bar.timestamp),
                "symbol": asset.symbol,
                "product": asset.product,
                "desired": side,
                "reason": "non_positive_equity",
            }
        )
        return
    per_unit_risk = fill_price * strategy.stop_pct
    qty = (equity * risk.risk_per_trade_pct) / per_unit_risk
    qty = min(qty, (equity * risk.max_position_pct) / fill_price)
    fee_rate = asset.fee_bps / BPS
    if book.kind == "cash":
        unit_cash = fill_price * (Decimal("1") + fee_rate)
    else:
        unit_cash = fill_price / Decimal(asset.leverage) + fill_price * fee_rate
    if unit_cash > 0:
        qty = min(qty, _account_cash(book, asset.symbol) / unit_cash)
    if qty <= 0:
        rejections.append(
            {
                "timestamp": _iso(bar.timestamp),
                "symbol": asset.symbol,
                "product": asset.product,
                "desired": side,
                "reason": "insufficient_cash",
            }
        )
        return

    notional = fill_price * qty
    fee = notional * fee_rate
    margin = Decimal("0") if book.kind == "cash" else notional / Decimal(asset.leverage)
    cash = _account_cash(book, asset.symbol)
    if book.kind == "cash":
        cash -= notional + fee
    else:
        cash -= margin + fee
    _set_account_cash(book, asset.symbol, cash)
    stop_price = fill_price * (Decimal("1") - strategy.stop_pct) if side == "long" else fill_price * (
        Decimal("1") + strategy.stop_pct
    )
    position = Position(
        symbol=asset.symbol,
        product=asset.product,
        side=side,
        qty=qty,
        entry_price=fill_price,
        entry_time=bar.timestamp,
        stop_price=stop_price,
        margin=margin,
        leverage=asset.leverage,
    )
    book.positions[asset.symbol] = position
    book.fees += fee
    slippage_cost = abs(fill_price - raw_price) * qty
    book.slippage += slippage_cost
    _append_ledger(book, bar.timestamp, "fee", -fee, asset.symbol)
    fills.append(
        _fill_record(
            asset=asset,
            timestamp=bar.timestamp,
            action=f"open_{side}",
            reason="signal_next_bar_open",
            qty=qty,
            raw_price=raw_price,
            fill_price=fill_price,
            fee=fee,
            slippage_cost=slippage_cost,
            signal_timestamp=signal_timestamp,
            mark=mark_price(bar),
            index_price=bar.index_close,
            stop_price=stop_price,
            realized=Decimal("0"),
        )
    )


def cash_entry_block_reason(bar: ResearchBar, session: SessionSpec) -> str | None:
    local = bar.timestamp.astimezone(ZoneInfo(session.timezone))
    if local.weekday() >= 5:
        return "outside_session"
    if not is_nyse_session(local.date()):
        return "exchange_holiday"
    if bar.session_daily:
        return None
    if not in_regular_session(bar.timestamp, session):
        return "outside_session"
    return None


def _apply_corporate_actions(
    book: Book,
    asset: AssetSpec,
    bar: ResearchBar,
    dividends: tuple[Dividend, ...] | list[Dividend],
    splits: tuple[Split, ...] | list[Split],
) -> None:
    position = book.positions.get(asset.symbol)
    for split in splits:
        if split.timestamp != bar.timestamp or split.already_in_prices or position is None:
            continue
        if split.ratio <= 0:
            raise ResearchSafetyError("split ratio must be positive")
        position.qty *= split.ratio
        position.entry_price /= split.ratio
        position.stop_price /= split.ratio
    if position is None or position.side != "long":
        return
    for dividend in dividends:
        if dividend.timestamp != bar.timestamp:
            continue
        cash_amount = dividend.amount * position.qty
        _set_account_cash(book, asset.symbol, _account_cash(book, asset.symbol) + cash_amount)
        _append_ledger(book, bar.timestamp, "dividend", cash_amount, asset.symbol)


def _apply_funding_settlement(
    book: Book,
    asset: AssetSpec,
    position: Position,
    settlement: FundingSettlement,
) -> None:
    if settlement.mark is None or settlement.mark <= 0:
        book.funding_mark_missing += 1
        return
    notional = position.qty * settlement.mark
    payment = notional * settlement.rate
    if position.side == "short":
        payment = -payment
    cash = _account_cash(book, asset.symbol) - payment
    if cash < 0:
        pull = min(position.margin, -cash)
        position.margin -= pull
        cash += pull
    _set_account_cash(book, asset.symbol, cash)
    book.funding_pnl -= payment
    book.funding_events += 1
    _append_ledger(book, settlement.timestamp, "funding", -payment, asset.symbol)


def _apply_funding(book: Book, asset: AssetSpec, position: Position, bar: ResearchBar) -> None:
    if asset.product != PRODUCT_PERP or bar.funding_rate is None:
        return
    notional = position.qty * mark_price(bar)
    payment = notional * bar.funding_rate
    if position.side == "short":
        payment = -payment
    cash = _account_cash(book, asset.symbol) - payment
    if cash < 0:
        pull = min(position.margin, -cash)
        position.margin -= pull
        cash += pull
    _set_account_cash(book, asset.symbol, cash)
    book.funding_pnl -= payment
    book.funding_events += 1
    _append_ledger(book, bar.timestamp, "funding", -payment, asset.symbol)


def _update_drawdown(book: Book, timestamp: datetime) -> None:
    equity = book_equity(book)
    book.curve.append((timestamp, equity))
    if equity > book.peak:
        book.peak = equity
    if book.peak > 0:
        drawdown = (book.peak - equity) / book.peak
        if drawdown > book.max_drawdown:
            book.max_drawdown = drawdown
        if drawdown > 0 and drawdown >= book.max_drawdown_limit:
            book.halted = True


def _process_bar(
    book: Book,
    asset: AssetSpec,
    bars: list[ResearchBar],
    index: int,
    strategy: StrategySpec,
    risk: RiskSpec,
    session: SessionSpec,
    fills: list[dict[str, Any]],
    rejections: list[dict[str, Any]],
    *,
    signal_mode: str = "baseline",
    open_funding: list[FundingSettlement] | None = None,
    later_funding: list[FundingSettlement] | None = None,
    dividends: list[Dividend] | None = None,
    splits: list[Split] | None = None,
    use_bar_funding: bool = True,
) -> None:
    bar = bars[index]
    stops_enabled = signal_mode != "buy_and_hold"
    _apply_corporate_actions(book, asset, bar, dividends or [], splits or [])
    position = book.positions.get(asset.symbol)
    for settlement in open_funding or []:
        if position is not None:
            _apply_funding_settlement(book, asset, position, settlement)
    stopped = False
    if position is not None and stops_enabled:
        hit = protective_exit(position, bar, asset.maintenance_margin_rate)
        if hit is not None and hit[0].endswith("_gap"):
            reason, raw = hit
            _close_position(
                book,
                asset,
                position,
                timestamp=bar.timestamp,
                raw_price=raw,
                reason=reason,
                signal_timestamp=bars[index - 1].timestamp if index else None,
                fills=fills,
                bar=bar,
            )
            stopped = True
            position = None

    if signal_mode == "buy_and_hold":
        desired: str | None = "long" if index >= strategy.slow_window else None
    elif index == 0:
        desired = None
    else:
        desired = desired_side(
            [item.close for item in bars[:index]],
            strategy,
            allow_short=asset.allow_short,
        )
    if desired is not None and not stopped:
        signal_timestamp = bars[index - 1].timestamp
        current = book.positions.get(asset.symbol)
        if current is not None and current.side != desired:
            _close_position(
                book,
                asset,
                current,
                timestamp=bar.timestamp,
                raw_price=bar.open,
                reason="signal_flip" if desired in {"long", "short"} else "signal_flat",
                signal_timestamp=signal_timestamp,
                fills=fills,
                bar=bar,
            )
            current = None
        if current is None and desired in {"long", "short"}:
            _try_open(
                book,
                asset,
                bar,
                desired,
                strategy,
                risk,
                session,
                signal_timestamp,
                fills,
                rejections,
            )

    position = book.positions.get(asset.symbol)
    if position is not None and not stopped and stops_enabled:
        hit = protective_exit(position, bar, asset.maintenance_margin_rate)
        if hit is not None and not hit[0].endswith("_gap"):
            reason, raw = hit
            _close_position(
                book,
                asset,
                position,
                timestamp=bar.timestamp,
                raw_price=raw,
                reason=reason,
                signal_timestamp=bars[index - 1].timestamp if index else None,
                fills=fills,
                bar=bar,
            )
            position = None
    if position is not None and use_bar_funding:
        _apply_funding(book, asset, position, bar)
    if position is not None:
        for settlement in later_funding or []:
            _apply_funding_settlement(book, asset, position, settlement)
    book.last_marks[asset.symbol] = mark_price(bar)
    _update_drawdown(book, bar.timestamp)


def _coverage(bars: list[ResearchBar]) -> dict[str, str]:
    full_marks = [
        bar.mark_open is not None and bar.mark_high is not None and bar.mark_low is not None and bar.mark_close is not None
        for bar in bars
    ]
    marks = [bar.mark_close is not None for bar in bars]
    indexes = [bar.index_close is not None for bar in bars]
    funding = [bar.funding_rate is not None for bar in bars]
    if full_marks and all(full_marks):
        mark_status = "provided_ohlc"
    elif all(marks):
        mark_status = "provided"
    elif any(marks):
        mark_status = "partial_fallback_trade_prices"
    else:
        mark_status = "unavailable_fallback_trade_prices"
    if any(indexes):
        index_status = "provided_not_used_for_pnl"
    else:
        index_status = "unavailable"
    if any(funding):
        funding_status = "provided_series"
    else:
        funding_status = "unavailable"
    return {
        "mark_status": mark_status,
        "index_status": index_status,
        "funding_status": funding_status,
    }


def _segment_drawdown(curve: list[tuple[datetime, Decimal]], start: datetime | None, end: datetime | None) -> Decimal:
    peak: Decimal | None = None
    worst = Decimal("0")
    for timestamp, equity in curve:
        if start is not None and timestamp < start:
            continue
        if end is not None and timestamp >= end:
            continue
        if peak is None or equity > peak:
            peak = equity
        if peak and peak > 0:
            drawdown = (peak - equity) / peak
            if drawdown > worst:
                worst = drawdown
    return worst


def _segment_amounts(
    ledger: list[dict[str, Any]],
    *,
    start: datetime | None,
    end: datetime | None,
) -> dict[str, Decimal]:
    totals = {
        "net_pnl": Decimal("0"),
        "fees": Decimal("0"),
        "funding": Decimal("0"),
        "realized": Decimal("0"),
        "dividends": Decimal("0"),
    }
    for event in ledger:
        timestamp = event["time"]
        if start is not None and timestamp < start:
            continue
        if end is not None and timestamp >= end:
            continue
        amount = event["amount"]
        totals["net_pnl"] += amount
        if event["kind"] == "fee":
            totals["fees"] += -amount
        elif event["kind"] == "funding":
            totals["funding"] += amount
        elif event["kind"] == "realized":
            totals["realized"] += amount
        elif event["kind"] == "unrealized":
            totals["realized"] += amount
        elif event["kind"] == "dividend":
            totals["dividends"] += amount
    return totals


def _fill_count(fills: list[dict[str, Any]], symbol: str | None, start: datetime | None, end: datetime | None) -> int:
    count = 0
    for fill in fills:
        if symbol is not None and fill["symbol"] != symbol:
            continue
        timestamp = parse_timestamp(fill["timestamp"])
        if start is not None and timestamp < start:
            continue
        if end is not None and timestamp >= end:
            continue
        count += 1
    return count


def _funding_field(status: str, amount: Decimal) -> str | None:
    if status in {"provided_series", "incomplete"}:
        return _dec_str(amount)
    return None


def _sample_block(
    book: Book,
    fills: list[dict[str, Any]],
    symbols: set[str],
    *,
    start: datetime | None,
    end: datetime | None,
    funding_status: str,
    period_start: datetime | None,
    period_end: datetime | None,
    include_drawdown: bool,
) -> dict[str, Any]:
    events = [event for event in book.ledger if event["symbol"] in symbols]
    selected = [fill for fill in fills if fill["symbol"] in symbols]
    totals = _segment_amounts(events, start=start, end=end)
    drawdown = _segment_drawdown(book.curve, start, end) if include_drawdown else None
    return {
        "net_pnl": _dec_str(totals["net_pnl"]),
        "net_pnl_includes_funding": funding_status in {"provided_series", "not_applicable"},
        "fees": _dec_str(totals["fees"]),
        "funding": _funding_field(funding_status, totals["funding"]),
        "dividends": _dec_str(totals["dividends"]),
        "realized_price_pnl": _dec_str(totals["realized"]),
        "fills": _fill_count(selected, None, start, end),
        "max_drawdown": _dec_str(drawdown) if drawdown is not None else None,
        "max_drawdown_scope": "book" if include_drawdown else "book_only",
        "drawdown_resets_at_boundaries": True,
        "period_start": _iso(period_start) if period_start else None,
        "period_end": _iso(period_end) if period_end else None,
    }


def _limitations(config: ResearchConfig, asset_coverage: dict[str, dict[str, str]]) -> list[str]:
    notes = [
        "本路径只做回测或纸面账本，不会发送实盘、测试网或券商订单，也不会修改交易所杠杆。",
        "成交规则：信号只用已收盘 K 线，下一根 K 线开盘价成交；止损跳空按开盘价成交，不按更优的止损价回填。",
        "基线策略是固定参数的均线动量，没有为示例数据调参，也不代表未来收益。",
        "data_label 由配置声明，程序不证明行情来源。带 synthetic/fixture 的结果不能当作真实盈利。",
        "美股与 ETF 是现金买卖，按 America/New_York 常规时段限制开仓；它们不是期权、CME 期货，也不能代替合约适配器。",
        "Binance 的 AAPLUSDT 一类代码是交易所合成行情，本路径拒绝把它们当成纳斯达克股票或上市 ETF。",
        "期权与 CME 期货未实现；请求这些产品会直接失败。",
        "永续模拟使用逐仓保证金。标记价缺失时回退到成交价，并在报告中标明；指数价即使提供也不进入盈亏。",
        "资金费来自结算序列或 K 线列。没有序列时 funding 为 null，净盈亏不含资金费，不会把缺失记成 0。",
        "有完整标记 OHLC 时，止损和强平看标记价路径；同一根 K 线的高低点先后未知，按不利方向成交。只有收盘标记价时才用收盘差平移。",
        "样本外区间只做报告切分，不会反过来选择参数。未提供 sample_split_timestamp 时不输出样本外结论。",
        "模拟杠杆只存在于研究账本，不会给任何账户开启杠杆。",
    ]
    if any(item["funding_status"] == "unavailable" for item in asset_coverage.values()):
        notes.append("至少有一个永续品种没有资金费序列，对应净盈亏不能解释为已扣除真实资金费。")
    if "synthetic" in config.data_label.lower() or "fixture" in config.data_label.lower():
        notes.append("当前 data_label 表明这是合成或示例数据，输出指标只用于核对账本。")
    notes.extend(config.notes)
    return notes


def _make_book(kind: str, currency: str, assets: list[AssetSpec], config: ResearchConfig) -> Book:
    weights = [asset.weight for asset in assets]
    sleeve: dict[str, Decimal] = {}
    if any(weight is not None for weight in weights):
        if any(weight is None for weight in weights):
            raise ResearchSafetyError(f"{kind} ledger weights must be set on every symbol")
        total = sum((weight for weight in weights if weight is not None), Decimal("0"))
        if abs(total - Decimal("1")) > Decimal("0.0000001"):
            raise ResearchSafetyError(f"{kind} allocation weights must sum to 1")
        for asset, weight in zip(assets, weights):
            assert weight is not None
            sleeve[asset.symbol] = config.starting_equity * weight
        cash = Decimal("0")
    else:
        cash = config.starting_equity
    return Book(
        kind=kind,
        currency=currency,
        cash=cash,
        starting=config.starting_equity,
        peak=config.starting_equity,
        max_drawdown_limit=config.risk.max_drawdown_pct,
        sleeve_cash=sleeve,
    )


def _bucket_by_bar(bars: list[ResearchBar], events: list[Any]) -> list[list[Any]]:
    buckets: list[list[Any]] = [[] for _ in bars]
    ordered = sorted(events, key=lambda item: item.timestamp)
    cursor = 0
    for index, bar in enumerate(bars):
        end = bars[index + 1].timestamp if index + 1 < len(bars) else bar.timestamp + timedelta(days=2)
        while cursor < len(ordered) and ordered[cursor].timestamp < end:
            if ordered[cursor].timestamp >= bar.timestamp:
                buckets[index].append(ordered[cursor])
            cursor += 1
    return buckets


def run_research(
    config: ResearchConfig,
    series: dict[str, list[ResearchBar]],
    *,
    aux: MarketAux | None = None,
    signal_mode: str = "baseline",
) -> dict[str, Any]:
    if config.mode not in ALLOWED_MODES:
        raise ResearchSafetyError("research path is paper/backtest only")
    if signal_mode not in {"baseline", "buy_and_hold"}:
        raise ResearchSafetyError(f"unknown signal mode {signal_mode}")
    missing = [asset.symbol for asset in config.assets if asset.symbol not in series]
    if missing:
        raise ResearchSafetyError(f"missing bars for: {', '.join(missing)}")
    aux = aux or MarketAux()

    perp_assets = [asset for asset in config.assets if asset.product == PRODUCT_PERP]
    cash_assets = [asset for asset in config.assets if asset.product in CASH_PRODUCTS]
    books: dict[str, Book] = {}
    asset_book: dict[str, str] = {}
    if perp_assets:
        books["perp"] = _make_book("perp", "USDT", perp_assets, config)
        for asset in perp_assets:
            asset_book[asset.symbol] = "perp"
    if cash_assets:
        books["cash"] = _make_book("cash", "USD", cash_assets, config)
        for asset in cash_assets:
            asset_book[asset.symbol] = "cash"
    coverage = {asset.symbol: _coverage(series[asset.symbol]) for asset in config.assets}
    fills: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    events: list[tuple[datetime, int, int]] = []
    assets = list(config.assets)
    funding_buckets = {
        asset.symbol: _bucket_by_bar(series[asset.symbol], list(aux.funding.get(asset.symbol, [])))
        for asset in assets
    }
    for asset_index, asset in enumerate(assets):
        for bar_index, bar in enumerate(series[asset.symbol]):
            events.append((bar.timestamp, asset_index, bar_index))
    events.sort(key=lambda item: (item[0], item[1], item[2]))
    for _timestamp, asset_index, bar_index in events:
        asset = assets[asset_index]
        bucket = funding_buckets[asset.symbol][bar_index]
        bar = series[asset.symbol][bar_index]
        _process_bar(
            books[asset_book[asset.symbol]],
            asset,
            series[asset.symbol],
            bar_index,
            config.strategy,
            config.risk,
            config.session,
            fills,
            rejections,
            signal_mode=signal_mode,
            open_funding=[item for item in bucket if item.timestamp == bar.timestamp],
            later_funding=[item for item in bucket if item.timestamp > bar.timestamp],
            dividends=aux.dividends.get(asset.symbol, []),
            splits=aux.splits.get(asset.symbol, []),
            use_bar_funding=not bool(aux.funding.get(asset.symbol)),
        )

    pending: list[dict[str, Any]] = []
    for asset in assets:
        bars = series[asset.symbol]
        closes = [bar.close for bar in bars]
        if signal_mode == "buy_and_hold":
            side = "long"
        else:
            side = desired_side(closes, config.strategy, allow_short=asset.allow_short)
        position = books[asset_book[asset.symbol]].positions.get(asset.symbol)
        current = position.side if position else "flat"
        if side is not None and side != current:
            pending.append(
                {
                    "symbol": asset.symbol,
                    "product": asset.product,
                    "desired": side,
                    "current": current,
                    "signal_timestamp": _iso(bars[-1].timestamp),
                    "status": "unfilled_no_next_bar",
                    "note": "信号形成于最后一根已收盘 K 线，没有下一根开盘价可供成交，因此不成交。",
                }
            )
        book = books[asset_book[asset.symbol]]
        if position is not None:
            _append_ledger(
                book,
                bars[-1].timestamp,
                "unrealized",
                unrealized(position, book.last_marks[asset.symbol]),
                asset.symbol,
            )

    for asset in assets:
        if asset.product in CASH_PRODUCTS:
            continue
        has_aux = bool(aux.funding.get(asset.symbol))
        if not has_aux:
            continue
        book = books[asset_book[asset.symbol]]
        if book.funding_mark_missing or not aux.funding_complete.get(asset.symbol, False):
            coverage[asset.symbol]["funding_status"] = "incomplete"
        else:
            coverage[asset.symbol]["funding_status"] = "provided_series"

    report = _build_report(config, books, assets, asset_book, series, coverage, fills, rejections, pending)
    report["signal_mode"] = signal_mode
    report["leverage_changes_sent"] = 0
    return report


def _book_funding_status(book: Book, assets: list[AssetSpec], coverage: dict[str, dict[str, str]]) -> str:
    if book.kind == "cash":
        return "not_applicable"
    statuses = {coverage[asset.symbol]["funding_status"] for asset in assets if asset.product == PRODUCT_PERP}
    if statuses == {"provided_series"}:
        return "provided_series"
    if "incomplete" in statuses or ("provided_series" in statuses and "unavailable" in statuses):
        return "incomplete"
    return "unavailable"


def _build_report(
    config: ResearchConfig,
    books: dict[str, Book],
    assets: list[AssetSpec],
    asset_book: dict[str, str],
    series: dict[str, list[ResearchBar]],
    coverage: dict[str, dict[str, str]],
    fills: list[dict[str, Any]],
    rejections: list[dict[str, Any]],
    pending: list[dict[str, Any]],
) -> dict[str, Any]:
    split = config.sample_split
    book_reports = []
    for name, book in books.items():
        group = [asset for asset in assets if asset_book[asset.symbol] == name]
        symbols = {asset.symbol for asset in group}
        funding_status = _book_funding_status(book, group, coverage)
        booked = _booked_net(book)
        ending = book.starting + booked
        all_times = [bar.timestamp for asset in group for bar in series[asset.symbol]]
        period_start = min(all_times)
        period_end = max(all_times)
        in_sample = None
        out_sample = None
        if split is not None:
            in_sample = _sample_block(
                book,
                fills,
                symbols,
                start=None,
                end=split,
                funding_status=funding_status,
                period_start=period_start if period_start < split else None,
                period_end=split,
                include_drawdown=True,
            )
            out_sample = _sample_block(
                book,
                fills,
                symbols,
                start=split,
                end=None,
                funding_status=funding_status,
                period_start=split,
                period_end=period_end if period_end >= split else None,
                include_drawdown=True,
            )
        book_reports.append(
            {
                "book": name,
                "accounting": "isolated_usdt_m_perpetual" if book.kind == "perp" else "cash_long_only",
                "currency": book.currency,
                "starting_equity": _dec_str(book.starting),
                "ending_equity": _dec_str(ending),
                "net_pnl": _dec_str(booked),
                "net_pnl_includes_funding": funding_status in {"provided_series", "not_applicable"},
                "fees": _dec_str(book.fees),
                "funding": (
                    _dec_str(book.funding_pnl)
                    if funding_status == "provided_series" or (funding_status == "incomplete" and book.funding_events)
                    else None
                ),
                "funding_status": funding_status,
                "slippage_cost": _dec_str(book.slippage),
                "slippage_already_in_net_pnl": True,
                "max_drawdown": _dec_str(book.max_drawdown),
                "drawdown_halted": book.halted,
                "open_positions": [
                    {
                        "symbol": position.symbol,
                        "side": position.side,
                        "qty": _dec_str(position.qty),
                        "entry_price": _dec_str(position.entry_price),
                        "margin": _dec_str(position.margin),
                        "leverage": position.leverage,
                        "mark_price": _dec_str(book.last_marks.get(position.symbol)),
                    }
                    for position in book.positions.values()
                ],
                "full_sample": _sample_block(
                    book,
                    fills,
                    symbols,
                    start=None,
                    end=None,
                    funding_status=funding_status,
                    period_start=period_start,
                    period_end=period_end,
                    include_drawdown=True,
                ),
                "in_sample": in_sample,
                "out_of_sample": out_sample,
            }
        )

    asset_reports = []
    for asset in assets:
        bars = series[asset.symbol]
        book = books[asset_book[asset.symbol]]
        symbols = {asset.symbol}
        funding_status = "not_applicable" if asset.product in CASH_PRODUCTS else coverage[asset.symbol]["funding_status"]
        if asset.product in CASH_PRODUCTS:
            mark_status = "not_applicable_cash_uses_trade_close"
            index_status = "not_applicable"
        else:
            mark_status = coverage[asset.symbol]["mark_status"]
            index_status = coverage[asset.symbol]["index_status"]
        allocated = config.starting_equity * asset.weight if asset.weight is not None else None
        in_sample = None
        out_sample = None
        if config.sample_split is not None:
            in_sample = _sample_block(
                book,
                fills,
                symbols,
                start=None,
                end=config.sample_split,
                funding_status=funding_status,
                period_start=bars[0].timestamp if bars[0].timestamp < config.sample_split else None,
                period_end=config.sample_split,
                include_drawdown=False,
            )
            out_sample = _sample_block(
                book,
                fills,
                symbols,
                start=config.sample_split,
                end=None,
                funding_status=funding_status,
                period_start=config.sample_split,
                period_end=bars[-1].timestamp if bars[-1].timestamp >= config.sample_split else None,
                include_drawdown=False,
            )
        asset_reports.append(
            {
                "symbol": asset.symbol,
                "product": asset.product,
                "quote_asset": asset.quote_asset,
                "leverage": asset.leverage,
                "allow_short": asset.allow_short,
                "weight": _dec_str(asset.weight) if asset.weight is not None else None,
                "allocated_starting_equity": _dec_str(allocated) if allocated is not None else None,
                "fee_bps": _dec_str(asset.fee_bps),
                "slippage_bps": _dec_str(asset.slippage_bps),
                "sample_period": {"start": _iso(bars[0].timestamp), "end": _iso(bars[-1].timestamp)},
                "mark_status": mark_status,
                "index_status": index_status,
                "funding_status": funding_status,
                "session": "24x7_utc" if asset.product == PRODUCT_PERP else config.session.timezone,
                "fills": _fill_count(fills, asset.symbol, None, None),
                "full_sample": _sample_block(
                    book,
                    fills,
                    symbols,
                    start=None,
                    end=None,
                    funding_status=funding_status,
                    period_start=bars[0].timestamp,
                    period_end=bars[-1].timestamp,
                    include_drawdown=False,
                ),
                "in_sample": in_sample,
                "out_of_sample": out_sample,
            }
        )

    return {
        "mode": config.mode,
        "execution": "historical_backtest" if config.mode == MODE_BACKTEST else "simulated_paper_ledger",
        "paper_only": True,
        "live_trading": False,
        "orders_sent": 0,
        "leverage_changes_sent": 0,
        "strategy": config.strategy.name,
        "data_label": config.data_label,
        "profitability_claim": "none",
        "sample_split_timestamp": _iso(config.sample_split) if config.sample_split else None,
        "books": book_reports,
        "assets": asset_reports,
        "fills": fills,
        "rejections": rejections,
        "pending_signals": pending,
        "limitations": _limitations(config, coverage),
    }


def load_series(config: ResearchConfig) -> dict[str, list[ResearchBar]]:
    series: dict[str, list[ResearchBar]] = {}
    for asset in config.assets:
        if not asset.csv_path:
            raise ResearchSafetyError(f"{asset.symbol} is missing csv")
        path = Path(asset.csv_path)
        if not path.is_absolute():
            candidate = Path.cwd() / path
            path = candidate if candidate.is_file() else config.config_dir / asset.csv_path
        series[asset.symbol] = parse_bars_csv(path, asset.symbol)
    return series


def write_outputs(report: dict[str, Any], report_path: str | None, fills_path: str | None) -> None:
    if report_path:
        target = Path(report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if fills_path:
        target = Path(fills_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(fill, ensure_ascii=False) for fill in report["fills"]]
        target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _display_decimal(value: Any) -> str:
    if value is None:
        return "null"
    return format(Decimal(str(value)).quantize(Decimal("0.00000001")), "f")


def format_summary(report: dict[str, Any]) -> str:
    lines = [
        f"模式: {report['mode']} / {report['execution']}",
        f"数据标签: {report['data_label']}",
        "实盘订单: 0",
        "收益承诺: 无",
    ]
    for book in report["books"]:
        lines.append(
            "账本 {book} {accounting} {currency} 净盈亏={net} 手续费={fees} 资金费={funding} "
            "资金费状态={funding_status} 回撤={dd} 成交见资产".format(
                book=book["book"],
                accounting=book["accounting"],
                currency=book["currency"],
                net=_display_decimal(book["net_pnl"]),
                fees=_display_decimal(book["fees"]),
                funding=_display_decimal(book["funding"]),
                funding_status=book["funding_status"],
                dd=_display_decimal(book["max_drawdown"]),
            )
        )
    for asset in report["assets"]:
        sample = asset["full_sample"]
        lines.append(
            "{symbol} {product} 区间={start}..{end} 成交={fills} 净盈亏={net} 手续费={fees} "
            "资金费={funding} ({funding_status}) 标记={mark} 指数={index}".format(
                symbol=asset["symbol"],
                product=asset["product"],
                start=asset["sample_period"]["start"],
                end=asset["sample_period"]["end"],
                fills=asset["fills"],
                net=_display_decimal(sample["net_pnl"]),
                fees=_display_decimal(sample["fees"]),
                funding=_display_decimal(sample["funding"]),
                funding_status=asset["funding_status"],
                mark=asset["mark_status"],
                index=asset["index_status"],
            )
        )
    if report["sample_split_timestamp"]:
        lines.append(f"样本切分: {report['sample_split_timestamp']}（切分不用于调参）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Paper/backtest-only BTCUSDT perpetual and US cash equity/ETF research."
    )
    parser.add_argument("--config", required=True, help="Path to a research JSON config")
    parser.add_argument("--mode", choices=sorted(ALLOWED_MODES), help="Override config mode; live is impossible")
    parser.add_argument("--report", help="Override report JSON path")
    parser.add_argument("--fills", help="Override fills JSONL path")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.mode:
            config = replace(config, mode=args.mode)
        if config.study or any(asset.source_type for asset in config.assets):
            from research_data import format_study, run_historical_study, write_study_files

            study = run_historical_study(config)
            write_study_files(study, args.report or config.report_path, args.fills or config.fills_path)
            print(format_study(study))
            return 0
        report = run_research(config, load_series(config))
        write_outputs(report, args.report or config.report_path, args.fills or config.fills_path)
    except ResearchSafetyError as exc:
        print(f"研究路径拒绝执行: {exc}", file=sys.stderr)
        return 2
    print(format_summary(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

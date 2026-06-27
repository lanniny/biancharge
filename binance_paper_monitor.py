import argparse
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any


DEFAULT_PRICE_ENDPOINTS = [
    "https://api.binance.com/api/v3/ticker/price",
    "https://api.binance.us/api/v3/ticker/price",
]
DEFAULT_BASE_URLS = [
    "https://api.binance.com",
    "https://api.binance.us",
]


class BinanceApiError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"Binance API error HTTP {status_code}: {message}")
        self.status_code = status_code
        self.message = message


def normalize_symbol(symbol: str) -> str:
    value = symbol.strip().upper().replace("/", "")
    if not value or not value.isalnum():
        raise ValueError(f"Invalid symbol: {symbol!r}")
    return value


def decimal_from(value: Any) -> Decimal:
    return Decimal(str(value))


def fetch_price(symbol: str, endpoints: list[str] | None = None, timeout_seconds: int = 10) -> Decimal:
    price_endpoints = endpoints or DEFAULT_PRICE_ENDPOINTS
    query = urllib.parse.urlencode({"symbol": normalize_symbol(symbol)})
    errors: list[str] = []
    for endpoint in price_endpoints:
        url = f"{endpoint}?{query}"
        try:
            with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return decimal_from(payload["price"])
        except urllib.error.HTTPError as exc:
            errors.append(f"{endpoint} -> HTTP {exc.code}")
        except urllib.error.URLError as exc:
            errors.append(f"{endpoint} -> {exc.reason}")

    joined_errors = "; ".join(errors) if errors else "no endpoints configured"
    raise RuntimeError(f"Could not fetch public price for {normalize_symbol(symbol)}: {joined_errors}")


def request_json(url: str, headers: dict[str, str] | None = None, timeout_seconds: int = 10) -> Any:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw_body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw_body)
            message = payload.get("msg") or raw_body
        except json.JSONDecodeError:
            message = raw_body or exc.reason
        raise BinanceApiError(exc.code, message) from exc


def try_public_json(path: str, params: dict[str, Any] | None = None, base_urls: list[str] | None = None) -> Any:
    bases = base_urls or DEFAULT_BASE_URLS
    query = urllib.parse.urlencode(params or {})
    suffix = f"{path}?{query}" if query else path
    errors: list[str] = []
    for base_url in bases:
        url = f"{base_url.rstrip('/')}{suffix}"
        try:
            return request_json(url)
        except urllib.error.HTTPError as exc:
            errors.append(f"{base_url} -> HTTP {exc.code}")
        except urllib.error.URLError as exc:
            errors.append(f"{base_url} -> {exc.reason}")

    raise RuntimeError(f"Could not fetch public API path {path}: {'; '.join(errors)}")


def sign_query(params: dict[str, Any], api_secret: str) -> str:
    query = urllib.parse.urlencode(params)
    signature = hmac.new(api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{query}&signature={signature}"


def get_server_time(base_url: str) -> int:
    payload = request_json(f"{base_url.rstrip('/')}/api/v3/time")
    return int(payload["serverTime"])


def signed_get(base_url: str, path: str, api_key: str, api_secret: str, params: dict[str, Any] | None = None, timestamp_ms: int | None = None) -> Any:
    signed_params = dict(params or {})
    signed_params["timestamp"] = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    signed_params.setdefault("recvWindow", 5000)
    query = sign_query(signed_params, api_secret)
    url = f"{base_url.rstrip('/')}{path}?{query}"
    return request_json(url, headers={"X-MBX-APIKEY": api_key})


def get_account_info(base_url: str, api_key: str, api_secret: str, omit_zero_balances: bool = True) -> dict[str, Any]:
    server_time = get_server_time(base_url)
    return signed_get(
        base_url=base_url,
        path="/api/v3/account",
        api_key=api_key,
        api_secret=api_secret,
        timestamp_ms=server_time,
        params={"omitZeroBalances": "true" if omit_zero_balances else "false"},
    )


def summarize_account(account: dict[str, Any]) -> dict[str, Any]:
    balances = []
    for item in account.get("balances", []):
        free = decimal_from(item.get("free", "0"))
        locked = decimal_from(item.get("locked", "0"))
        if free == 0 and locked == 0:
            continue
        balances.append({"asset": item.get("asset"), "free": str(free), "locked": str(locked)})

    return {
        "accountType": account.get("accountType"),
        "canTrade": account.get("canTrade"),
        "canWithdraw": account.get("canWithdraw"),
        "canDeposit": account.get("canDeposit"),
        "permissions": account.get("permissions", []),
        "nonZeroBalances": balances,
    }


def get_public_info(symbols: list[str], base_urls: list[str] | None = None) -> dict[str, Any]:
    normalized_symbols = [normalize_symbol(symbol) for symbol in symbols]
    exchange_info = try_public_json("/api/v3/exchangeInfo", base_urls=base_urls)
    ticker_24h = {
        symbol: try_public_json("/api/v3/ticker/24hr", params={"symbol": symbol}, base_urls=base_urls)
        for symbol in normalized_symbols
    }
    available_symbols = {
        item["symbol"]: {
            "status": item.get("status"),
            "baseAsset": item.get("baseAsset"),
            "quoteAsset": item.get("quoteAsset"),
            "orderTypes": item.get("orderTypes", []),
            "permissions": item.get("permissions", []),
        }
        for item in exchange_info.get("symbols", [])
        if item.get("symbol") in normalized_symbols
    }
    return {"symbols": available_symbols, "ticker24h": ticker_24h}


def evaluate_alerts(alerts: list[dict[str, Any]], prices: dict[str, Decimal]) -> list[str]:
    messages: list[str] = []
    for alert in alerts:
        symbol = normalize_symbol(alert["symbol"])
        price = prices.get(symbol)
        if price is None:
            continue

        above = alert.get("above")
        below = alert.get("below")
        if above is not None and price >= decimal_from(above):
            messages.append(f"{symbol} price {price} is at or above {above}")
        if below is not None and price <= decimal_from(below):
            messages.append(f"{symbol} price {price} is at or below {below}")
    return messages


@dataclass
class PaperTrade:
    side: str
    symbol: str
    price: Decimal
    base_amount: Decimal
    quote_amount: Decimal


class PaperPortfolio:
    def __init__(self, balances: dict[str, Any]) -> None:
        self.balances = {asset.upper(): decimal_from(amount) for asset, amount in balances.items()}

    def balance(self, asset: str) -> Decimal:
        return self.balances.get(asset.upper(), Decimal("0"))

    def buy(self, symbol: str, base_asset: str, quote_asset: str, price: Decimal, quote_amount: Decimal) -> PaperTrade | None:
        available = self.balance(quote_asset)
        spend = min(available, quote_amount)
        if spend <= 0:
            return None

        base_amount = (spend / price).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        if base_amount <= 0:
            return None

        actual_spend = (base_amount * price).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        self.balances[quote_asset.upper()] = available - actual_spend
        self.balances[base_asset.upper()] = self.balance(base_asset) + base_amount
        return PaperTrade("BUY", normalize_symbol(symbol), price, base_amount, actual_spend)

    def sell_all(self, symbol: str, base_asset: str, quote_asset: str, price: Decimal) -> PaperTrade | None:
        base_amount = self.balance(base_asset).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        if base_amount <= 0:
            return None

        quote_amount = (base_amount * price).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        self.balances[base_asset.upper()] = self.balance(base_asset) - base_amount
        self.balances[quote_asset.upper()] = self.balance(quote_asset) + quote_amount
        return PaperTrade("SELL", normalize_symbol(symbol), price, base_amount, quote_amount)

    def snapshot(self) -> dict[str, str]:
        return {asset: str(amount.normalize()) for asset, amount in sorted(self.balances.items())}


def run_paper_strategy(strategy: list[dict[str, Any]], prices: dict[str, Decimal], portfolio: PaperPortfolio) -> list[PaperTrade]:
    trades: list[PaperTrade] = []
    for rule in strategy:
        symbol = normalize_symbol(rule["symbol"])
        price = prices.get(symbol)
        if price is None:
            continue

        base_asset = rule["base_asset"]
        quote_asset = rule["quote_asset"]
        buy_below = rule.get("buy_below")
        sell_above = rule.get("sell_above")

        if buy_below is not None and price <= decimal_from(buy_below):
            trade = portfolio.buy(
                symbol=symbol,
                base_asset=base_asset,
                quote_asset=quote_asset,
                price=price,
                quote_amount=decimal_from(rule.get("trade_quote_amount", 0)),
            )
            if trade:
                trades.append(trade)
            continue

        if sell_above is not None and price >= decimal_from(sell_above):
            trade = portfolio.sell_all(symbol=symbol, base_asset=base_asset, quote_asset=quote_asset, price=price)
            if trade:
                trades.append(trade)
    return trades


def load_config(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def collect_prices(symbols: list[str], endpoints: list[str] | None = None) -> dict[str, Decimal]:
    prices: dict[str, Decimal] = {}
    for symbol in symbols:
        normalized = normalize_symbol(symbol)
        prices[normalized] = fetch_price(normalized, endpoints=endpoints)
    return prices


def print_tick(prices: dict[str, Decimal], alerts: list[str], trades: list[PaperTrade], portfolio: PaperPortfolio) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"))
    for symbol, price in sorted(prices.items()):
        print(f"PRICE {symbol}: {price}")
    for alert in alerts:
        print(f"ALERT {alert}")
    for trade in trades:
        print(f"PAPER {trade.side} {trade.symbol}: base={trade.base_amount} quote={trade.quote_amount} price={trade.price}")
    print(f"PORTFOLIO {json.dumps(portfolio.snapshot(), ensure_ascii=False)}")
    print()


def print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe Binance public price monitor with paper trading.")
    parser.add_argument("--config", default="config.example.json", help="Path to JSON config.")
    parser.add_argument("--once", action="store_true", help="Run one polling tick and exit.")
    parser.add_argument("--public-info", action="store_true", help="Print public exchange and 24h ticker information, then exit.")
    parser.add_argument("--account", action="store_true", help="Print read-only account summary from BINANCE_API_KEY and BINANCE_API_SECRET, then exit.")
    parser.add_argument("--base-url", default=os.environ.get("BINANCE_BASE_URL", "https://api.binance.com"), help="Base URL for signed account requests.")
    parser.add_argument("--show-zero-balances", action="store_true", help="Include zero balances in the signed account request.")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.public_info:
        print_json(get_public_info(config.get("symbols", []), base_urls=config.get("base_urls")))
        return 0

    if args.account:
        api_key = os.environ.get("BINANCE_API_KEY")
        api_secret = os.environ.get("BINANCE_API_SECRET")
        if not api_key or not api_secret:
            raise RuntimeError("Set BINANCE_API_KEY and BINANCE_API_SECRET in your local environment first.")
        try:
            account = get_account_info(args.base_url, api_key, api_secret, omit_zero_balances=not args.show_zero_balances)
        except BinanceApiError as exc:
            print_json({"ok": False, "status": exc.status_code, "message": exc.message})
            return 1
        print_json(summarize_account(account))
        return 0

    portfolio = PaperPortfolio(config.get("paper_wallet", {}))
    poll_seconds = int(config.get("poll_seconds", 10))

    while True:
        prices = collect_prices(config.get("symbols", []), endpoints=config.get("price_endpoints"))
        alerts = evaluate_alerts(config.get("alerts", []), prices)
        trades = run_paper_strategy(config.get("paper_strategy", []), prices, portfolio)
        print_tick(prices, alerts, trades, portfolio)

        if args.once:
            return 0
        time.sleep(poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
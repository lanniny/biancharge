import unittest
from decimal import Decimal

from unittest.mock import patch

from binance_paper_monitor import (
    BinanceApiError,
    PaperPortfolio,
    evaluate_alerts,
    fetch_price,
    get_public_info,
    get_account_info,
    normalize_symbol,
    sign_query,
    summarize_account,
    request_json,
    run_paper_strategy,
)


class BinancePaperMonitorTests(unittest.TestCase):
    def test_normalize_symbol_accepts_common_forms(self) -> None:
        self.assertEqual(normalize_symbol(" btc/usdt "), "BTCUSDT")

    def test_normalize_symbol_rejects_empty_symbol(self) -> None:
        with self.assertRaises(ValueError):
            normalize_symbol("   ")

    def test_evaluate_alerts_reports_above_and_below(self) -> None:
        alerts = [{"symbol": "BTCUSDT", "above": 100, "below": 90}]

        self.assertEqual(evaluate_alerts(alerts, {"BTCUSDT": Decimal("101")}), ["BTCUSDT price 101 is at or above 100"])
        self.assertEqual(evaluate_alerts(alerts, {"BTCUSDT": Decimal("89")}), ["BTCUSDT price 89 is at or below 90"])

    def test_paper_strategy_buys_with_virtual_balance(self) -> None:
        portfolio = PaperPortfolio({"USDT": 1000, "BTC": 0})
        strategy = [
            {
                "symbol": "BTCUSDT",
                "base_asset": "BTC",
                "quote_asset": "USDT",
                "buy_below": 100,
                "trade_quote_amount": 250,
            }
        ]

        trades = run_paper_strategy(strategy, {"BTCUSDT": Decimal("50")}, portfolio)

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].side, "BUY")
        self.assertEqual(portfolio.balance("BTC"), Decimal("5.00000000"))
        self.assertEqual(portfolio.balance("USDT"), Decimal("750.00000000"))

    def test_paper_strategy_sells_all_virtual_base_asset(self) -> None:
        portfolio = PaperPortfolio({"USDT": 0, "BTC": "0.5"})
        strategy = [
            {
                "symbol": "BTCUSDT",
                "base_asset": "BTC",
                "quote_asset": "USDT",
                "sell_above": 100,
            }
        ]

        trades = run_paper_strategy(strategy, {"BTCUSDT": Decimal("120")}, portfolio)

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].side, "SELL")
        self.assertEqual(portfolio.balance("BTC"), Decimal("0E-8"))
        self.assertEqual(portfolio.balance("USDT"), Decimal("60.00000000"))

    def test_fetch_price_tries_next_public_endpoint_after_http_error(self) -> None:
        class FakeHttpError(Exception):
            code = 451

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self) -> bytes:
                return b'{"price":"123.45"}'

        import urllib.error

        calls = []

        def fake_urlopen(url, timeout):
            calls.append(url)
            if len(calls) == 1:
                raise urllib.error.HTTPError(url, 451, "Unavailable", hdrs=None, fp=None)
            return FakeResponse()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            price = fetch_price("BTCUSDT", endpoints=["https://first.example/price", "https://second.example/price"])

        self.assertEqual(price, Decimal("123.45"))
        self.assertEqual(len(calls), 2)

    def test_sign_query_uses_hmac_sha256(self) -> None:
        signature = sign_query({"symbol": "BTCUSDT", "timestamp": 1}, "secret")

        self.assertEqual(signature, "symbol=BTCUSDT&timestamp=1&signature=ef9d3d77a34d9a13a21a4c2d7f3e8cb091888a74ca62b5b62f430e78eded95ba")

    def test_summarize_account_keeps_only_non_zero_balances(self) -> None:
        summary = summarize_account(
            {
                "accountType": "SPOT",
                "canTrade": True,
                "canWithdraw": False,
                "canDeposit": True,
                "permissions": ["SPOT"],
                "balances": [
                    {"asset": "BTC", "free": "0.1", "locked": "0"},
                    {"asset": "ETH", "free": "0", "locked": "0"},
                ],
            }
        )

        self.assertEqual(summary["accountType"], "SPOT")
        self.assertEqual(summary["nonZeroBalances"], [{"asset": "BTC", "free": "0.1", "locked": "0"}])

    def test_get_public_info_summarizes_requested_symbols(self) -> None:
        def fake_try_public_json(path, params=None, base_urls=None):
            if path == "/api/v3/exchangeInfo":
                return {
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "status": "TRADING",
                            "baseAsset": "BTC",
                            "quoteAsset": "USDT",
                            "orderTypes": ["LIMIT"],
                            "permissions": ["SPOT"],
                        }
                    ]
                }
            return {"symbol": params["symbol"], "lastPrice": "100"}

        with patch("binance_paper_monitor.try_public_json", side_effect=fake_try_public_json):
            info = get_public_info(["BTCUSDT"])

        self.assertEqual(info["symbols"]["BTCUSDT"]["status"], "TRADING")
        self.assertEqual(info["ticker24h"]["BTCUSDT"]["lastPrice"], "100")

    def test_request_json_reports_binance_error_message_without_url(self) -> None:
        import urllib.error

        class FakeBody:
            def read(self):
                return b'{"code":-1021,"msg":"Timestamp outside recvWindow"}'

            def close(self):
                pass

        def fake_urlopen(request, timeout):
            raise urllib.error.HTTPError(
                "https://example.test/private?signature=secret",
                400,
                "Bad Request",
                hdrs=None,
                fp=FakeBody(),
            )

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(BinanceApiError) as context:
                request_json("https://example.test/private?signature=secret")

        self.assertEqual(context.exception.status_code, 400)
        self.assertEqual(context.exception.message, "Timestamp outside recvWindow")

    def test_get_account_info_uses_server_time_for_signed_request(self) -> None:
        with patch("binance_paper_monitor.get_server_time", return_value=123456), patch("binance_paper_monitor.signed_get") as signed_get:
            get_account_info("https://api.example", "key", "secret")

        signed_get.assert_called_once()
        self.assertEqual(signed_get.call_args.kwargs["timestamp_ms"], 123456)


if __name__ == "__main__":
    unittest.main()
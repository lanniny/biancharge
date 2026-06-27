"""Tests for Stage 2 Binance execution-correctness fixes.

Covers:
- BIN-001: deterministic newClientOrderId (idempotency)
- BIN-008: slippage guard blocks MARKET entries past max_slippage_bps
- BIN-003: numeric Binance error code is preserved on the raised exception
"""

import unittest
from decimal import Decimal

import market_autotrader as ma
from market_autotrader import (
    BUY,
    SELL,
    AutoExecutionConfig,
    ExecutionConfig,
    OrderIntent,
    client_order_id_for,
    open_notional_blocked_reason,
    order_quantity_from_intent,
    slippage_guard_reason,
)


def _open_order(symbol="BTCUSDT", price="100", qty="1"):
    return OrderIntent(
        action=BUY,
        symbol=symbol,
        market="binance_futures",
        quote_asset="USDT",
        quote_amount=Decimal("100"),
        estimated_price=Decimal(price),
        quantity=Decimal(qty),
        order_type=ma.ORDER_MARKET,
        intent_kind=ma.INTENT_OPEN_LONG,
    )


class ClientOrderIdTests(unittest.TestCase):
    def test_same_intent_same_bucket_is_stable(self):
        order = _open_order()
        a = client_order_id_for(order, now=1_000_000)
        b = client_order_id_for(order, now=1_000_010)  # within same 45s bucket
        self.assertEqual(a, b)

    def test_different_bucket_changes_id(self):
        order = _open_order()
        a = client_order_id_for(order, now=1_000_000)
        b = client_order_id_for(order, now=1_000_000 + 100)  # next bucket
        self.assertNotEqual(a, b)

    def test_different_side_changes_id(self):
        long_order = _open_order()
        short_order = OrderIntent(
            action=SELL,
            symbol="BTCUSDT",
            market="binance_futures",
            quote_asset="USDT",
            quote_amount=Decimal("100"),
            estimated_price=Decimal("100"),
            quantity=Decimal("1"),
            order_type=ma.ORDER_MARKET,
            intent_kind=ma.INTENT_OPEN_SHORT,
        )
        self.assertNotEqual(
            client_order_id_for(long_order, now=1_000_000),
            client_order_id_for(short_order, now=1_000_000),
        )

    def test_id_within_binance_length_and_charset(self):
        cid = client_order_id_for(_open_order(), now=1_000_000)
        self.assertLessEqual(len(cid), 36)
        self.assertTrue(all(c.isalnum() or c in "-_" for c in cid))


class SlippageGuardTests(unittest.TestCase):
    def setUp(self):
        self._orig = ma.fetch_futures_mark_price

    def tearDown(self):
        ma.fetch_futures_mark_price = self._orig

    def test_disabled_when_bps_zero(self):
        ma.fetch_futures_mark_price = lambda *a, **k: Decimal("999999")
        auto = AutoExecutionConfig(max_slippage_bps=Decimal("0"))
        self.assertIsNone(slippage_guard_reason(_open_order(), ExecutionConfig(), auto))

    def test_blocks_when_mark_drifts_beyond_bps(self):
        # decision price 100, mark 101 -> 100 bps drift; cap 50 bps -> block
        ma.fetch_futures_mark_price = lambda *a, **k: Decimal("101")
        auto = AutoExecutionConfig(max_slippage_bps=Decimal("50"))
        reason = slippage_guard_reason(_open_order(price="100"), ExecutionConfig(), auto)
        self.assertIsNotNone(reason)
        self.assertIn("Slippage guard", reason)

    def test_allows_within_bps(self):
        # decision 100, mark 100.2 -> 20 bps; cap 50 bps -> allow
        ma.fetch_futures_mark_price = lambda *a, **k: Decimal("100.2")
        auto = AutoExecutionConfig(max_slippage_bps=Decimal("50"))
        self.assertIsNone(slippage_guard_reason(_open_order(price="100"), ExecutionConfig(), auto))

    def test_never_blocks_reduce_only_exit(self):
        ma.fetch_futures_mark_price = lambda *a, **k: Decimal("200")  # huge drift
        auto = AutoExecutionConfig(max_slippage_bps=Decimal("10"))
        order = OrderIntent(
            action=SELL,
            symbol="BTCUSDT",
            market="binance_futures",
            quote_asset="USDT",
            quote_amount=Decimal("100"),
            estimated_price=Decimal("100"),
            quantity=Decimal("1"),
            reduce_only=True,
            order_type=ma.ORDER_MARKET,
            intent_kind=ma.INTENT_CLOSE_LONG,
        )
        self.assertIsNone(slippage_guard_reason(order, ExecutionConfig(), auto))

    def test_missing_mark_price_does_not_block(self):
        ma.fetch_futures_mark_price = lambda *a, **k: None
        auto = AutoExecutionConfig(max_slippage_bps=Decimal("10"))
        self.assertIsNone(slippage_guard_reason(_open_order(), ExecutionConfig(), auto))


class BinanceErrorCodeTests(unittest.TestCase):
    def test_request_json_preserves_numeric_code(self):
        import io
        import json
        import urllib.error

        import proxy_http

        body = json.dumps({"code": -2022, "msg": "ReduceOnly Order is rejected."}).encode()

        def fake_urlopen(request, timeout_seconds=10):
            raise urllib.error.HTTPError(
                request.full_url, 400, "Bad Request", {}, io.BytesIO(body)
            )

        orig = proxy_http.urlopen
        proxy_http.urlopen = fake_urlopen
        try:
            with self.assertRaises(RuntimeError) as ctx:
                proxy_http.request_json("https://fapi.binance.com/fapi/v1/order", method="POST")
            exc = ctx.exception
            self.assertEqual(getattr(exc, "binance_code", None), -2022)
            self.assertIn("-2022", str(exc))
        finally:
            proxy_http.urlopen = orig


class OrderQuantitySizingTests(unittest.TestCase):
    def setUp(self):
        self._orig = ma.fetch_symbol_filters

    def tearDown(self):
        ma.fetch_symbol_filters = self._orig

    def test_open_order_below_min_notional_blocks_instead_of_upsizing(self):
        ma.fetch_symbol_filters = lambda *a, **k: {
            "step_size": Decimal("1"),
            "min_qty": Decimal("1"),
            "min_notional": Decimal("5"),
        }
        order = OrderIntent(
            action=BUY,
            symbol="TESTUSDT",
            market="binance_futures",
            quote_asset="USDT",
            quote_amount=Decimal("4.90"),
            estimated_price=Decimal("1"),
            order_type=ma.ORDER_MARKET,
            intent_kind=ma.INTENT_OPEN_LONG,
        )
        reason = open_notional_blocked_reason(order, ExecutionConfig())
        self.assertIsNotNone(reason)
        self.assertIn("below min notional", reason)
        with self.assertRaises(ValueError):
            order_quantity_from_intent(order, ExecutionConfig())

    def test_reduce_only_explicit_quantity_not_blocked_by_open_min_notional(self):
        ma.fetch_symbol_filters = lambda *a, **k: {
            "step_size": Decimal("1"),
            "min_qty": Decimal("1"),
            "min_notional": Decimal("5"),
        }
        order = OrderIntent(
            action=SELL,
            symbol="TESTUSDT",
            market="binance_futures",
            quote_asset="USDT",
            quote_amount=Decimal("4"),
            estimated_price=Decimal("1"),
            quantity=Decimal("4"),
            reduce_only=True,
            order_type=ma.ORDER_MARKET,
            intent_kind=ma.INTENT_CLOSE_LONG,
        )
        self.assertEqual(order_quantity_from_intent(order, ExecutionConfig()), Decimal("4"))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""One-shot tactical live: close SPCX long, then open BASEDUSDT long."""
from __future__ import annotations

import json
import sys
import time
from decimal import Decimal
from pathlib import Path

import market_autotrader as ma

CLOSE_SYMBOL = "SPCXUSDT"
OPEN_SYMBOL = "BASEDUSDT"
MIN_OPEN_NOTIONAL = Decimal("50")
CONFIG_PATH = "market_autotrader.open-now.json"


def _long_qty(rows: list[dict], symbol: str) -> Decimal:
    qty = Decimal("0")
    for row in rows:
        if row.get("symbol") != symbol:
            continue
        side = str(row.get("positionSide", "BOTH")).upper()
        amt = ma.decimal_from(row.get("positionAmt", "0"))
        if side == "LONG" and amt > 0:
            qty = amt
        elif side == "BOTH" and amt > 0:
            qty = amt
    return qty


def _mark_price(symbol: str, execution: ma.ExecutionConfig) -> Decimal:
    payload = ma.request_json(
        f"{execution.binance_futures_base_url}/fapi/v1/ticker/price?symbol={symbol}",
        timeout_seconds=execution.timeout_seconds,
    )
    return ma.decimal_from(payload["price"])


def _submit(
    order: ma.OrderIntent,
    execution: ma.ExecutionConfig,
    auto_exec: ma.AutoExecutionConfig,
) -> dict:
    if order.leverage and auto_exec.auto_leverage and not order.reduce_only:
        ma.set_futures_leverage(order.symbol, order.leverage, execution)
    return ma.submit_binance_futures_order(order, execution)


def main() -> int:
    cfg = ma.load_config(CONFIG_PATH)
    risk = ma.risk_from_config(cfg["risk"])
    execution = ma.execution_from_config(cfg["execution"], fallback_mode=risk.mode)
    auto_exec = ma.auto_execution_from_config(cfg["auto_execution"])
    ledger = cfg.get("ledger_path")

    if not Path(risk.live_arm_path).exists():
        print(json.dumps({"error": "live not armed"}))
        return 1
    if Path(risk.kill_switch_path).exists():
        print(json.dumps({"error": "kill switch active"}))
        return 1

    ma.reset_server_time_offset()
    results: list[dict] = []

    rows = ma.fetch_futures_positions(execution)
    close_qty = _long_qty(rows, CLOSE_SYMBOL)
    if close_qty > 0:
        price = _mark_price(CLOSE_SYMBOL, execution)
        filters = ma.fetch_symbol_filters(CLOSE_SYMBOL, execution, "binance_futures")
        close_qty = ma.quantize_to_step(close_qty, filters.get("step_size", Decimal("0.01")))
        close_order = ma.OrderIntent(
            ma.SELL,
            CLOSE_SYMBOL,
            "binance_futures",
            "USDT",
            (close_qty * price).quantize(Decimal("0.00000001")),
            price,
            reduce_only=True,
            quantity=close_qty,
            intent_kind=ma.INTENT_CLOSE_LONG,
        )
        try:
            payload = _submit(close_order, execution, auto_exec)
            results.append({"step": "close_spcx", "ok": True, "qty": str(close_qty), **payload})
            print(json.dumps({"step": "close_spcx", "status": "ok", "qty": str(close_qty), "orderId": payload.get("response", {}).get("orderId")}))
        except Exception as exc:
            results.append({"step": "close_spcx", "ok": False, "error": str(exc)})
            print(json.dumps({"step": "close_spcx", "status": "failed", "error": str(exc)}))
            return 1
        time.sleep(2)

    account = ma.fetch_futures_account(execution)
    available = ma.decimal_from(account.get("availableBalance", "0"))
    if available < MIN_OPEN_NOTIONAL:
        print(json.dumps({"step": "open_based", "status": "blocked", "available": str(available), "need": str(MIN_OPEN_NOTIONAL)}))
        return 1

    open_notional = min(available, risk.max_trade_quote, available * Decimal("0.95"))
    if open_notional < MIN_OPEN_NOTIONAL:
        open_notional = MIN_OPEN_NOTIONAL
    price = _mark_price(OPEN_SYMBOL, execution)
    leverage = min(auto_exec.default_leverage, auto_exec.max_leverage)
    open_order = ma.OrderIntent(
        ma.BUY,
        OPEN_SYMBOL,
        "binance_futures",
        "USDT",
        open_notional,
        price,
        reduce_only=False,
        intent_kind=ma.INTENT_OPEN_LONG,
        leverage=leverage,
    )
    try:
        payload = _submit(open_order, execution, auto_exec)
        qty = ma.order_quantity_from_intent(open_order, execution)
        results.append({"step": "open_based", "ok": True, "notional": str(open_notional), "qty": str(qty), **payload})
        print(json.dumps({
            "step": "open_based",
            "status": "ok",
            "notional": str(open_notional),
            "orderId": payload.get("response", {}).get("orderId"),
        }))
    except Exception as exc:
        results.append({"step": "open_based", "ok": False, "error": str(exc)})
        print(json.dumps({"step": "open_based", "status": "failed", "error": str(exc)}))
        return 1

    if ledger:
        summary = {"tactical": True, "results": results, "timestamp": int(time.time())}
        Path(ledger).parent.mkdir(parents=True, exist_ok=True)
        with Path(ledger).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(summary, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

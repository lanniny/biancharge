"""Second paper process for the frozen slow-momentum basket.

ETHUSDT, SOLUSDT, BNBUSDT and XRPUSDT each keep their own 10000 cash.
The process does not read or write the running BTCUSDT 1x daily state,
does not send orders, and does not take API keys or a proxy.
"""

from __future__ import annotations

import json
import signal
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from paper_forward import (
    FEED_HOST,
    PaperForwardError,
    _banned_keys,
    _paths,
    append_log,
    load_warmup_bars,
    new_state,
    on_book,
    on_closed_bar,
    on_mark,
    on_next_open,
    save_state,
)
from paper_forward import normalize_stream
from paper_ws import PublicUmSocket
from research_alt_validation import ALT_SYMBOLS, FROZEN_PRESET
from research_swing_study import _public_preset
from strategy_candidates import preset_matches

PORTFOLIO = "alt_slow_momentum"


def load_alt_config(payload: dict[str, Any], path: str | Path) -> dict[str, Any]:
    banned = _banned_keys(payload)
    if banned:
        raise PaperForwardError(f"paper config refuses credential or proxy fields: {', '.join(sorted(set(banned)))}")
    if str(payload.get("mode", "")).lower() != "paper":
        raise PaperForwardError("alt paper accepts only mode paper")
    if payload.get("allow_live_trading") or payload.get("send_orders") or payload.get("live"):
        raise PaperForwardError("alt paper refuses live trading flags")
    if tuple(payload.get("symbols") or ()) != ALT_SYMBOLS:
        raise PaperForwardError("alt paper symbols are frozen as ETH, SOL, BNB and XRP")
    if str(payload.get("product", "")).lower() != "binance_usdt_perp":
        raise PaperForwardError("alt paper is USD-M perpetual only")
    if str(payload.get("leverage_policy")) != "risk_bounded_dynamic":
        raise PaperForwardError("alt paper uses the risk-bounded planner")
    if not 1 <= int(payload.get("max_contract_leverage", 0)) <= 50:
        raise PaperForwardError("alt contract leverage must stay inside 1..50")
    if str(payload.get("bar_interval")) != "4h":
        raise PaperForwardError("alt paper reads 4h bars")
    if str(payload.get("risk_per_trade_pct")) != "0.01":
        raise PaperForwardError("alt risk per trade is frozen at 1%")
    strategy = payload.get("strategy") or {}
    preset = strategy.get("preset")
    if strategy.get("name") != "tsmom_slow_4h" or not isinstance(preset, dict):
        raise PaperForwardError("alt paper must name the frozen slow momentum preset")
    if not preset_matches(FROZEN_PRESET, preset):
        raise PaperForwardError("alt preset does not match the BTC-selected asymmetric rule")
    state_name = Path(str(payload.get("state_path", ""))).name
    if state_name in {"", "btcusdt-paper-state.json"}:
        raise PaperForwardError("alt paper must use its own state file")
    feed = payload.get("feed") or {}
    mark_url = str(feed.get("mark_kline_url", ""))
    book_url = str(feed.get("book_url", ""))
    for url in (mark_url, book_url):
        if not url.startswith(f"wss://{FEED_HOST}/"):
            raise PaperForwardError("feed URLs must stay on the public fstream.binance.com USDT-M host")
        if "fapi" in url or "order" in url or "proxy" in url:
            raise PaperForwardError("feed URL must not be an order, FAPI, or proxy endpoint")
    if "/market/" not in mark_url or "kline_4h" not in mark_url or "markPrice" not in mark_url:
        raise PaperForwardError("alt mark feed must carry 4h klines and markPrice")
    if "bookTicker" not in book_url:
        raise PaperForwardError("alt quote feed must be bookTicker")
    lowered = mark_url.lower() + book_url.lower()
    for symbol in ALT_SYMBOLS:
        if symbol.lower() not in lowered:
            raise PaperForwardError(f"alt feed is missing {symbol}")
    if "btcusdt" in lowered:
        raise PaperForwardError("alt feed must not attach the running BTCUSDT daily stream")
    payload["_path"] = str(path)
    return payload


def _view(config: dict[str, Any]) -> dict[str, Any]:
    view = dict(config)
    view["symbol"] = "PORTFOLIO"
    return view


def new_portfolio_state(config: dict[str, Any]) -> dict[str, Any]:
    sleeves = {}
    for symbol in ALT_SYMBOLS:
        state = new_state(config)
        state["symbol"] = symbol
        sleeves[symbol] = state
    return {
        "version": 1,
        "portfolio": PORTFOLIO,
        "mode": "paper",
        "orders_sent": 0,
        "symbols": sleeves,
        "running": False,
        "heartbeat_ms": None,
    }


def load_portfolio_state(path: Path, config: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return new_portfolio_state(config)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("portfolio") != PORTFOLIO:
        raise PaperForwardError("state file is not the alt slow-momentum ledger")
    if int(payload.get("orders_sent", 0)) != 0:
        raise PaperForwardError("state claims orders were sent; refusing to continue")
    for symbol in ALT_SYMBOLS:
        sleeve = payload["symbols"][symbol]
        if sleeve.get("symbol") != symbol or int(sleeve.get("orders_sent", 0)) != 0:
            raise PaperForwardError(f"{symbol} sleeve is not a paper ledger")
    return payload


def apply_alt_event(portfolio: dict[str, Any], config: dict[str, Any], message: dict[str, Any]) -> list[dict[str, Any]]:
    event = normalize_stream(message, allowed_symbols=frozenset(ALT_SYMBOLS))
    if event is None:
        return []
    symbol = event["symbol"]
    state = portfolio["symbols"][symbol]
    view = _view(config)
    if event["type"] == "book":
        events = on_book(state, view, event)
    elif event["type"] == "mark":
        events = on_mark(state, view, event)
    elif event["type"] == "kline":
        if event.get("interval") != "4h":
            raise PaperForwardError("alt paper accepts only 4h klines")
        if event["closed"]:
            events = on_closed_bar(
                state,
                view,
                {
                    "open_ms": event["open_ms"],
                    "close_ms": event["close_ms"],
                    "close": event["close"],
                    "high": event["high"],
                    "low": event["low"],
                },
                live=True,
            )
        else:
            events = on_next_open(state, view, event)
    else:
        events = []
    for item in events:
        item["symbol"] = symbol
        item["orders_sent"] = 0
    portfolio["orders_sent"] = 0
    return events


def _warmup(portfolio: dict[str, Any], config: dict[str, Any], loader: Callable[[str], list[dict[str, Any]]]) -> None:
    view = _view(config)
    for symbol in ALT_SYMBOLS:
        state = portfolio["symbols"][symbol]
        if state["closed_bars"]:
            continue
        for bar in loader(symbol):
            on_closed_bar(state, view, bar, live=False)
            state["warmup_bar_open_ms"].append(int(bar["open_ms"]))


def cmd_start(
    config: dict[str, Any],
    *,
    feed_factory: Callable[[str], Any] | None = None,
    warmup_loader: Callable[[str], list[dict[str, Any]]] | None = None,
) -> int:
    state_path, log_path, stop_path = _paths(config)
    if stop_path.exists():
        stop_path.unlink()
    portfolio = load_portfolio_state(state_path, config)
    loader = warmup_loader or (lambda symbol: load_warmup_bars(interval="4h", symbol=symbol))
    _warmup(portfolio, config, loader)
    append_log(
        log_path,
        {
            "type": "warmup",
            "portfolio": PORTFOLIO,
            "bars": {symbol: len(portfolio["symbols"][symbol]["closed_bars"]) for symbol in ALT_SYMBOLS},
            "forward_trades": 0,
            "orders_sent": 0,
            "note": "historical 4h bars initialize the frozen slow momentum book only",
        },
    )
    for sleeve in portfolio["symbols"].values():
        sleeve["running"] = True
        sleeve["feed_down"] = False
        sleeve.setdefault("forward_fills", 0)
    portfolio["running"] = True
    portfolio["orders_sent"] = 0
    save_state(state_path, portfolio)
    factory = feed_factory or (lambda url: PublicUmSocket(url))
    from paper_forward import _next_feed_item, _reconnect_if_stale, _supervisor

    supervisor = _supervisor(
        config,
        stop_path,
        factory,
        ("mark_kline", config["feed"]["mark_kline_url"]),
        ("book", config["feed"]["book_url"]),
    )
    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGTERM, lambda _signum, _frame: setattr(supervisor, "stopping", True))
    signal.signal(signal.SIGINT, lambda _signum, _frame: setattr(supervisor, "stopping", True))
    supervisor.start()
    last_heartbeat = 0.0
    from paper_forward import _now_ms

    last_rx = {label: _now_ms() for label, _url in supervisor.streams}
    last_stale_log = {label: 0 for label in last_rx}
    try:
        while not supervisor.stopping:
            if stop_path.exists():
                break
            item = _next_feed_item(supervisor)
            if item is not None:
                _handle_alt_feed_item(item, portfolio, config, log_path, state_path, last_rx)
            _reconnect_if_stale(
                supervisor,
                config,
                last_rx,
                last_stale_log,
                log_path,
                lambda: _mark_sleeves_down(portfolio),
            )
            if time.time() - last_heartbeat >= int(config.get("heartbeat_seconds", 30)):
                now_ms = _now_ms()
                portfolio["heartbeat_ms"] = now_ms
                for sleeve in portfolio["symbols"].values():
                    sleeve["heartbeat_ms"] = now_ms
                append_log(log_path, portfolio_status(portfolio, config, now_ms) | {"type": "heartbeat"})
                save_state(state_path, portfolio)
                last_heartbeat = time.time()
    finally:
        supervisor.stop()
        portfolio["running"] = False
        portfolio["orders_sent"] = 0
        save_state(state_path, portfolio)
        from paper_forward import _iso

        append_log(log_path, {"type": "stopped", "orders_sent": 0, "ts": _iso(_now_ms())})
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
    print(json.dumps({"stopped": True, "orders_sent": 0, "portfolio": PORTFOLIO, "state": str(state_path)}, ensure_ascii=False))
    return 0


def _mark_sleeves_down(portfolio: dict[str, Any]) -> None:
    for sleeve in portfolio["symbols"].values():
        sleeve["feed_down"] = True


def _handle_alt_feed_item(item, portfolio, config, log_path, state_path, last_rx) -> None:
    from paper_forward import _iso, _now_ms

    if item["type"] in {"feed_error", "feed_disconnect", "feed_reconnect"}:
        append_log(log_path, item)
        if item["type"] != "feed_reconnect":
            _mark_sleeves_down(portfolio)
        return
    if item["type"] != "raw":
        return
    last_rx[item["stream"]] = _now_ms()
    try:
        payload = json.loads(item["text"])
    except json.JSONDecodeError as exc:
        append_log(
            log_path,
            {"type": "feed_error", "stream": item["stream"], "error": f"JSONDecodeError: {exc}", "ts": _iso(_now_ms()), "orders_sent": 0},
        )
        return
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    quote_symbol = str((data or {}).get("s") or "").upper()
    if (data or {}).get("e") in {"markPriceUpdate", "bookTicker"} and quote_symbol in portfolio["symbols"]:
        portfolio["symbols"][quote_symbol]["feed_down"] = False
    try:
        events = apply_alt_event(portfolio, config, payload)
    except PaperForwardError as exc:
        append_log(log_path, {"type": "feed_error", "stream": item["stream"], "error": f"PaperForwardError: {exc}", "ts": _iso(_now_ms()), "orders_sent": 0})
        return
    for event in events:
        event["ts"] = _iso(_now_ms())
        append_log(log_path, event)
        sleeve = portfolio["symbols"].get(event.get("symbol"))
        if sleeve is not None and event["type"] == "fill":
            sleeve["forward_fills"] = int(sleeve.get("forward_fills") or 0) + 1
        if event["type"] in {"fill", "funding", "signal", "rejected", "warning"}:
            save_state(state_path, portfolio)


def portfolio_status(portfolio: dict[str, Any], config: dict[str, Any], now_ms: int | None = None) -> dict[str, Any]:
    from paper_forward import _iso, _now_ms, status_report

    now_ms = _now_ms() if now_ms is None else now_ms
    symbols: dict[str, Any] = {}
    processes = []
    for symbol, sleeve in portfolio["symbols"].items():
        report_state = sleeve
        if sleeve.get("heartbeat_ms") is None and portfolio.get("heartbeat_ms") is not None:
            report_state = dict(sleeve)
            report_state["heartbeat_ms"] = portfolio["heartbeat_ms"]
        report = status_report(report_state, config, now_ms)
        processes.append(report["process"])
        symbols[symbol] = {
            "process": report["process"],
            "heartbeat_time": report["heartbeat_time"],
            "last_mark_time": report["last_mark_time"],
            "mark": (sleeve.get("last_mark") or {}).get("price"),
            "mark_equity": report["mark_equity"],
            "cash": sleeve.get("cash"),
            "position": sleeve.get("position"),
            "forward_fills": int(sleeve.get("forward_fills") or 0),
            "valuation": report["valuation"],
            "feed_down": bool(sleeve.get("feed_down")),
        }
    if any(item == "stale" for item in processes):
        overall = "stale"
    elif processes and all(item == "heartbeat_fresh" for item in processes):
        overall = "heartbeat_fresh"
    else:
        overall = "not_running"
    return {
        "process": overall,
        "portfolio": PORTFOLIO,
        "orders_sent": 0,
        "ts": _iso(now_ms),
        "heartbeat_time": _iso(portfolio.get("heartbeat_ms")),
        "symbols": symbols,
    }


def dispatch(command: str, config: dict[str, Any]) -> int:
    state_path, _log, stop_path = _paths(config)
    if command == "stop":
        stop_path.parent.mkdir(parents=True, exist_ok=True)
        stop_path.write_text("stop\n", encoding="utf-8")
        print(json.dumps({"stopped_requested": True, "orders_sent": 0, "portfolio": PORTFOLIO}, ensure_ascii=False))
        return 0
    if command == "status":
        if not state_path.exists():
            print(json.dumps({"process": "not_started", "portfolio": PORTFOLIO, "orders_sent": 0}, ensure_ascii=False))
            return 0
        payload = load_portfolio_state(state_path, config)
        if stop_path.exists():
            payload["running"] = False
            for sleeve in payload["symbols"].values():
                sleeve["running"] = False
        print(json.dumps(portfolio_status(payload, config), ensure_ascii=False, indent=2))
        return 0
    if command == "preflight":
        print(json.dumps({"ok": False, "blocker": "use start on the deployment host; this check does not open orders", "orders_sent": 0}, ensure_ascii=False))
        return 0
    if command == "start":
        return cmd_start(config)
    raise PaperForwardError(f"unknown command {command}")


def example_payload() -> dict[str, Any]:
    preset = _public_preset(FROZEN_PRESET)
    streams = "/".join(f"{symbol.lower()}@markPrice@1s/{symbol.lower()}@kline_4h" for symbol in ALT_SYMBOLS)
    books = "/".join(f"{symbol.lower()}@bookTicker" for symbol in ALT_SYMBOLS)
    return {
        "mode": "paper",
        "portfolio": PORTFOLIO,
        "symbols": list(ALT_SYMBOLS),
        "product": "binance_usdt_perp",
        "starting_equity": "10000",
        "leverage_policy": "risk_bounded_dynamic",
        "max_contract_leverage": 50,
        "bar_interval": "4h",
        "fee_bps": "5",
        "slippage_bps": "2",
        "risk_per_trade_pct": "0.01",
        "maintenance_rate": "0.004",
        "liquidation_fee_rate": "0.005",
        "extra_buffer": "0.001",
        "stale_mark_seconds": 30,
        "stale_book_seconds": 15,
        "heartbeat_seconds": 30,
        "state_path": "paper-state/alt-slow-momentum-state.json",
        "log_path": "paper-state/alt-slow-momentum.jsonl",
        "stop_path": "paper-state/alt-slow-momentum.stop",
        "strategy": {"name": "tsmom_slow_4h", "preset": preset},
        "feed": {
            "type": "binance_usdt_m_public_ws",
            "mark_kline_url": f"wss://fstream.binance.com/market/stream?streams={streams}",
            "book_url": f"wss://fstream.binance.com/stream?streams={books}",
        },
        "notes": [
            "第二套纸面进程。每个币各 10000 USDT，不读取、不覆盖 btcusdt-paper-state.json。",
            "规则是 BTC 走步选中的非对称慢速动量，参数没有在这些币上重选。做多和做空都允许。",
            "四币在 2026-08-01 前通过了预先写死的标准。保留段里 BTC 和 XRP 为负，不能当成稳定优势。",
        ],
    }

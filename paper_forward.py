"""Forward paper runner for BTCUSDT USD-M perpetuals.

This process watches the public Binance USD-M market WebSocket and keeps a
virtual position. It does not import an order client, does not read
credentials, and does not send exchange orders. Historical bars warm the
daily moving averages only; they are not backfilled as forward trades.

A daily bar can create a signal only after it closes. The fill is the first
fresh executable quote after that close, or the next daily bar's open if no
fresh quote arrives. Missing funding is recorded as missing. It is not booked
as zero.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from paper_ws import PaperFeedError, PublicUmSocket
from research_backtest import ResearchSafetyError, StrategySpec, apply_slippage, desired_side
from research_data import fetch_vision_zip, parse_vision_kline_csv, zip_csv_text

EIGHT_HOURS_MS = 8 * 60 * 60 * 1000
BPS = Decimal("10000")
PRODUCT = "binance_usdt_perp"
SYMBOL = "BTCUSDT"
FEED_HOST = "fstream.binance.com"


class PaperForwardError(RuntimeError):
    """Fail closed for live controls, bad config, or a missing public feed."""


def _dec(value: Any, name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise PaperForwardError(f"invalid decimal {name}") from exc


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _d(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value.normalize(), "f")
    return "0" if text == "-0" else text


def _banned_keys(value: Any) -> list[str]:
    banned = {
        "api_key",
        "api_secret",
        "apisecret",
        "secret",
        "password",
        "token",
        "private_key",
        "proxy",
        "proxy_url",
        "socks_proxy",
        "socks5",
    }
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in banned:
                found.append(str(key))
            found.extend(_banned_keys(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_banned_keys(child))
    return found


def load_paper_config(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise PaperForwardError("config must be an object")
    banned = _banned_keys(payload)
    if banned:
        raise PaperForwardError(f"paper config refuses credential or proxy fields: {', '.join(sorted(set(banned)))}")
    mode = str(payload.get("mode", "")).lower()
    if mode != "paper":
        raise PaperForwardError(f"forward runner accepts only mode paper, got {payload.get('mode')!r}")
    if payload.get("allow_live_trading") or payload.get("send_orders") or payload.get("live"):
        raise PaperForwardError("forward runner refuses live trading flags")
    if str(payload.get("symbol", "")).upper() != SYMBOL:
        raise PaperForwardError("forward runner is BTCUSDT only")
    if str(payload.get("product", "")).lower() != PRODUCT:
        raise PaperForwardError("forward runner is the USD-M perpetual only; spot is not a substitute")
    leverage = int(payload.get("leverage", 0))
    if leverage != 1:
        raise PaperForwardError("this forward paper session uses simulated leverage 1")
    strategy = payload.get("strategy") or {}
    if int(strategy.get("fast_window", 0)) != 20 or int(strategy.get("slow_window", 0)) != 50:
        raise PaperForwardError("strategy windows are frozen at MA 20/50")
    if int(strategy.get("momentum_window", 0)) != 20:
        raise PaperForwardError("momentum window is frozen at 20")
    if Decimal(str(strategy.get("stop_pct", "0"))) != Decimal("0.08"):
        raise PaperForwardError("stop is frozen at 8%")
    feed = payload.get("feed") or {}
    mark_url = str(feed.get("mark_kline_url", ""))
    book_url = str(feed.get("book_url", ""))
    for url in (mark_url, book_url):
        if not url.startswith(f"wss://{FEED_HOST}/"):
            raise PaperForwardError("feed URLs must stay on the public fstream.binance.com USDT-M host")
        if "fapi" in url or "order" in url or "proxy" in url:
            raise PaperForwardError("feed URL must not be an order, FAPI, or proxy endpoint")
    if "/market/" not in mark_url or "kline_1d" not in mark_url or "markPrice" not in mark_url:
        raise PaperForwardError("mark/kline feed must be the public USD-M daily kline and markPrice stream")
    if "bookTicker" not in book_url:
        raise PaperForwardError("quote feed must be the public USD-M bookTicker stream")
    payload["_path"] = str(path)
    return payload


def strategy_from(config: dict[str, Any]) -> StrategySpec:
    raw = config["strategy"]
    return StrategySpec(
        name="baseline_sma_momentum",
        fast_window=20,
        slow_window=50,
        momentum_window=20,
        momentum_threshold=_dec(raw.get("momentum_threshold", "0"), "momentum_threshold"),
        stop_pct=Decimal("0.08"),
    )


def new_state(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "mode": "paper",
        "symbol": SYMBOL,
        "product": PRODUCT,
        "orders_sent": 0,
        "leverage_changes_sent": 0,
        "simulated_leverage": 1,
        "starting_equity": str(_dec(config.get("starting_equity", "10000"), "starting_equity")),
        "cash": str(_dec(config.get("starting_equity", "10000"), "starting_equity")),
        "fees": "0",
        "funding_pnl": "0",
        "funding_status": "unobserved",
        "missed_funding_ms": [],
        "position": None,
        "pending": None,
        "closed_bars": [],
        "warmup_bar_open_ms": [],
        "processed_closed_open_ms": [],
        "filled_signal_open_ms": [],
        "processed_funding_ms": [],
        "last_mark": None,
        "last_book": None,
        "last_mark_event_ms": None,
        "heartbeat_ms": None,
        "source": "binance_usdt_m_public_ws",
        "running": False,
    }


def load_state(path: Path, config: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return new_state(config)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("symbol") != SYMBOL or payload.get("product") != PRODUCT:
        raise PaperForwardError("state file is not the BTCUSDT USD-M paper ledger")
    if int(payload.get("orders_sent", 0)) != 0:
        raise PaperForwardError("state claims orders were sent; refusing to continue")
    return payload


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def append_log(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        handle.flush()


def _cash(state: dict[str, Any]) -> Decimal:
    return Decimal(state["cash"])


def _position(state: dict[str, Any]) -> dict[str, Any] | None:
    position = state.get("position")
    return position if isinstance(position, dict) else None


def mark_equity(state: dict[str, Any]) -> Decimal | None:
    mark = state.get("last_mark") or {}
    price = mark.get("price")
    if price is None:
        return None
    mark_price = Decimal(price)
    equity = _cash(state)
    position = _position(state)
    if position is not None:
        equity += Decimal(position["margin"]) + (mark_price - Decimal(position["entry"])) * Decimal(position["qty"])
    return equity


def valuation(state: dict[str, Any], now_ms: int, config: dict[str, Any]) -> str:
    mark = state.get("last_mark") or {}
    if not mark.get("event_ms"):
        return "incomplete_no_mark"
    age = now_ms - int(mark["event_ms"])
    if age > int(config.get("stale_mark_seconds", 30)) * 1000:
        return "incomplete_stale_mark"
    if state.get("missed_funding_ms"):
        return "incomplete_missed_funding"
    if state.get("funding_status") in {"unobserved", "incomplete_missing_rate", "incomplete_gap"}:
        return "incomplete_funding"
    return "fresh"


def entries_blocked(state: dict[str, Any], now_ms: int, config: dict[str, Any]) -> str | None:
    label = valuation(state, now_ms, config)
    if label != "fresh":
        return label
    if len(state.get("closed_bars") or []) < 50:
        return "warmup_incomplete"
    return None


def _fee(notional: Decimal, config: dict[str, Any]) -> Decimal:
    return abs(notional) * _dec(config.get("fee_bps", "5"), "fee_bps") / BPS


def _slip(price: Decimal, side: str, config: dict[str, Any]) -> Decimal:
    return apply_slippage(price, side, _dec(config.get("slippage_bps", "2"), "slippage_bps"))


def _open_long(state: dict[str, Any], config: dict[str, Any], raw: Decimal, event_ms: int, reason: str, signal_open_ms: int) -> dict[str, Any]:
    fill = _slip(raw, "buy", config)
    equity = mark_equity(state) or _cash(state)
    stop_pct = Decimal("0.08")
    risk_pct = _dec(config.get("risk_per_trade_pct", "0.01"), "risk_per_trade_pct")
    max_pct = _dec(config.get("max_position_pct", "1"), "max_position_pct")
    qty = (equity * risk_pct) / (fill * stop_pct)
    qty = min(qty, (equity * max_pct) / fill)
    fee_rate = _dec(config.get("fee_bps", "5"), "fee_bps") / BPS
    affordable = _cash(state) / (fill * (Decimal("1") + fee_rate))
    qty = min(qty, affordable)
    if qty <= 0:
        return {"type": "rejected", "reason": "insufficient_cash", "ts_ms": event_ms}
    notional = fill * qty
    fee = _fee(notional, config)
    margin = notional  # simulated 1x isolated margin
    state["cash"] = _d(_cash(state) - margin - fee)
    state["fees"] = _d(Decimal(state["fees"]) + fee)
    state["position"] = {
        "side": "long",
        "qty": _d(qty),
        "entry": _d(fill),
        "stop": _d(fill * (Decimal("1") - stop_pct)),
        "margin": _d(margin),
        "entry_ms": event_ms,
        "signal_bar_open_ms": signal_open_ms,
    }
    state["filled_signal_open_ms"].append(signal_open_ms)
    state["pending"] = None
    return {
        "type": "fill",
        "action": "open_long",
        "reason": reason,
        "qty": _d(qty),
        "raw_price": _d(raw),
        "fill_price": _d(fill),
        "fee": _d(fee),
        "ts_ms": event_ms,
        "signal_bar_open_ms": signal_open_ms,
        "orders_sent": 0,
    }


def _close_long(state: dict[str, Any], config: dict[str, Any], raw: Decimal, event_ms: int, reason: str, signal_open_ms: int | None) -> dict[str, Any]:
    position = _position(state)
    if position is None:
        return {"type": "noop", "ts_ms": event_ms}
    qty = Decimal(position["qty"])
    fill = _slip(raw, "sell", config)
    fee = _fee(fill * qty, config)
    pnl = (fill - Decimal(position["entry"])) * qty
    state["cash"] = _d(_cash(state) + Decimal(position["margin"]) + pnl - fee)
    state["fees"] = _d(Decimal(state["fees"]) + fee)
    state["position"] = None
    state["pending"] = None
    if signal_open_ms is not None:
        state["filled_signal_open_ms"].append(signal_open_ms)
    return {
        "type": "fill",
        "action": "close_long",
        "reason": reason,
        "qty": _d(qty),
        "raw_price": _d(raw),
        "fill_price": _d(fill),
        "fee": _d(fee),
        "realized_price_pnl": _d(pnl),
        "ts_ms": event_ms,
        "signal_bar_open_ms": signal_open_ms,
        "orders_sent": 0,
    }


def on_closed_bar(state: dict[str, Any], config: dict[str, Any], bar: dict[str, Any], *, live: bool) -> list[dict[str, Any]]:
    """Record a completed daily bar. Live bars may create a pending signal; warmup bars never do."""
    open_ms = int(bar["open_ms"])
    if open_ms in state["processed_closed_open_ms"]:
        return []
    state["processed_closed_open_ms"].append(open_ms)
    state["closed_bars"].append({"open_ms": open_ms, "close_ms": int(bar["close_ms"]), "close": str(bar["close"])})
    if not live or open_ms in state["warmup_bar_open_ms"]:
        return [{"type": "warmup_bar", "open_ms": open_ms, "counted_as_forward_trade": False}]
    closes = [Decimal(item["close"]) for item in state["closed_bars"]]
    side = desired_side(closes, strategy_from(config), allow_short=False)
    current = "long" if _position(state) else "flat"
    events: list[dict[str, Any]] = [{"type": "closed_bar", "open_ms": open_ms, "close_ms": int(bar["close_ms"]), "desired": side}]
    if side is None or side == current:
        return events
    if side == "long":
        blocked = entries_blocked(state, int(bar["close_ms"]), config)
        if blocked:
            events.append({"type": "rejected", "reason": blocked, "open_ms": open_ms, "desired": "long"})
            return events
    state["pending"] = {
        "signal_bar_open_ms": open_ms,
        "signal_bar_close_ms": int(bar["close_ms"]),
        "side": side,
    }
    events.append({"type": "signal", "side": side, "signal_bar_open_ms": open_ms, "fill": "waiting_for_quote_or_next_open"})
    return events


def on_book(state: dict[str, Any], config: dict[str, Any], quote: dict[str, Any]) -> list[dict[str, Any]]:
    event_ms = int(quote["event_ms"])
    state["last_book"] = {
        "bid": str(quote["bid"]),
        "ask": str(quote["ask"]),
        "event_ms": event_ms,
    }
    pending = state.get("pending")
    if not pending:
        return []
    if event_ms <= int(pending["signal_bar_close_ms"]):
        return []
    if int(pending["signal_bar_open_ms"]) in state["filled_signal_open_ms"]:
        state["pending"] = None
        return []
    age = _now_ms() - event_ms
    if age > int(config.get("stale_book_seconds", 15)) * 1000:
        return [{"type": "warning", "reason": "stale_book_not_used_for_fill", "event_ms": event_ms}]
    if pending["side"] == "long" and _position(state) is None:
        return [_open_long(state, config, Decimal(quote["ask"]), event_ms, "first_quote_after_signal", int(pending["signal_bar_open_ms"]))]
    if pending["side"] == "flat" and _position(state) is not None:
        return [_close_long(state, config, Decimal(quote["bid"]), event_ms, "first_quote_after_signal", int(pending["signal_bar_open_ms"]))]
    return []


def on_next_open(state: dict[str, Any], config: dict[str, Any], bar_open: dict[str, Any]) -> list[dict[str, Any]]:
    """Fill a still-pending signal at the next daily bar's open if no quote filled it."""
    pending = state.get("pending")
    if not pending:
        return []
    if int(bar_open["open_ms"]) <= int(pending["signal_bar_open_ms"]):
        return []
    if int(bar_open["event_ms"]) <= int(pending["signal_bar_close_ms"]):
        return []
    if int(pending["signal_bar_open_ms"]) in state["filled_signal_open_ms"]:
        state["pending"] = None
        return []
    event_ms = int(bar_open["event_ms"])
    raw = Decimal(bar_open["open"])
    if pending["side"] == "long" and _position(state) is None:
        return [_open_long(state, config, raw, event_ms, "next_bar_open", int(pending["signal_bar_open_ms"]))]
    if pending["side"] == "flat" and _position(state) is not None:
        return [_close_long(state, config, raw, event_ms, "next_bar_open", int(pending["signal_bar_open_ms"]))]
    return []


def _boundaries_between(start_ms: int, end_ms: int) -> list[int]:
    if end_ms <= start_ms:
        return []
    first = ((start_ms // EIGHT_HOURS_MS) + 1) * EIGHT_HOURS_MS
    values = []
    cursor = first
    while cursor <= end_ms:
        values.append(cursor)
        cursor += EIGHT_HOURS_MS
    return values


def _charge_funding(state: dict[str, Any], boundary_ms: int, mark: Decimal, rate: Decimal) -> dict[str, Any]:
    position = _position(state)
    if position is None:
        state["processed_funding_ms"].append(boundary_ms)
        state["funding_status"] = "observed_flat"
        return {"type": "funding", "boundary_ms": boundary_ms, "charged": "0", "reason": "flat"}
    payment = Decimal(position["qty"]) * mark * rate
    state["cash"] = _d(_cash(state) - payment)
    state["funding_pnl"] = _d(Decimal(state["funding_pnl"]) - payment)
    state["processed_funding_ms"].append(boundary_ms)
    state["funding_status"] = "observed"
    return {
        "type": "funding",
        "boundary_ms": boundary_ms,
        "rate": _d(rate),
        "mark": _d(mark),
        "cashflow": _d(-payment),
        "charged_as_zero": False,
    }


def on_mark(state: dict[str, Any], config: dict[str, Any], mark: dict[str, Any]) -> list[dict[str, Any]]:
    event_ms = int(mark["event_ms"])
    price = Decimal(mark["mark"])
    rate = mark.get("funding_rate")
    previous = state.get("last_mark_event_ms")
    previous_price = Decimal(state["last_mark"]["price"]) if (state.get("last_mark") or {}).get("price") else None
    state["last_mark"] = {
        "price": str(price),
        "index": None if mark.get("index") is None else str(mark["index"]),
        "funding_rate": None if rate is None else str(rate),
        "event_ms": event_ms,
    }
    events: list[dict[str, Any]] = []
    if previous is None and rate is not None and state.get("funding_status") == "unobserved":
        state["funding_status"] = "rate_seen"
    if previous is not None:
        boundaries = [item for item in _boundaries_between(int(previous), event_ms) if item not in state["processed_funding_ms"]]
        if boundaries:
            if rate is None:
                for boundary in boundaries:
                    if boundary not in state["missed_funding_ms"]:
                        state["missed_funding_ms"].append(boundary)
                state["funding_status"] = "incomplete_missing_rate"
                events.append({"type": "warning", "reason": "funding_missing_not_zeroed", "boundaries": boundaries})
            else:
                for boundary in boundaries[:-1]:
                    if boundary not in state["missed_funding_ms"]:
                        state["missed_funding_ms"].append(boundary)
                if len(boundaries) > 1:
                    state["funding_status"] = "incomplete_gap"
                    events.append({"type": "warning", "reason": "funding_gap_not_backfilled", "missed": boundaries[:-1]})
                events.append(_charge_funding(state, boundaries[-1], price, Decimal(rate)))
    state["last_mark_event_ms"] = event_ms
    position = _position(state)
    if position is not None and price <= Decimal(position["stop"]):
        reason = "stop_gap" if previous_price is not None and previous_price > Decimal(position["stop"]) else "stop"
        events.append(_close_long(state, config, price, event_ms, reason, None))
    return events


def normalize_stream(message: dict[str, Any]) -> dict[str, Any] | None:
    data = message.get("data", message)
    if not isinstance(data, dict):
        return None
    kind = data.get("e")
    symbol = str(data.get("s") or (data.get("k") or {}).get("s") or "")
    if symbol and symbol.upper() != SYMBOL:
        raise PaperForwardError(f"feed symbol {symbol} is not BTCUSDT; not switching market")
    if kind == "bookTicker":
        return {
            "type": "book",
            "bid": Decimal(str(data["b"])),
            "ask": Decimal(str(data["a"])),
            "event_ms": int(data.get("E") or data.get("T")),
        }
    if kind == "markPriceUpdate":
        rate = data.get("r")
        index = data.get("i")
        return {
            "type": "mark",
            "mark": Decimal(str(data["p"])),
            "index": None if index in (None, "") else Decimal(str(index)),
            "funding_rate": None if rate in (None, "") else Decimal(str(rate)),
            "event_ms": int(data["E"]),
        }
    if kind == "kline":
        candle = data.get("k") or {}
        if candle.get("i") != "1d":
            raise PaperForwardError(f"kline interval {candle.get('i')!r} is not 1d; daily strategy was not changed")
        return {
            "type": "kline",
            "open_ms": int(candle["t"]),
            "close_ms": int(candle["T"]),
            "open": Decimal(str(candle["o"])),
            "high": Decimal(str(candle["h"])),
            "low": Decimal(str(candle["l"])),
            "close": Decimal(str(candle["c"])),
            "closed": bool(candle.get("x")),
            "event_ms": int(data["E"]),
        }
    return None


def apply_feed_event(state: dict[str, Any], config: dict[str, Any], message: dict[str, Any]) -> list[dict[str, Any]]:
    event = normalize_stream(message)
    if event is None:
        return []
    if event["type"] == "book":
        return on_book(state, config, event)
    if event["type"] == "mark":
        return on_mark(state, config, event)
    if event["closed"]:
        return on_closed_bar(
            state,
            config,
            {"open_ms": event["open_ms"], "close_ms": event["close_ms"], "close": event["close"]},
            live=True,
        )
    return on_next_open(state, config, event)


def status_report(state: dict[str, Any], config: dict[str, Any], now_ms: int | None = None) -> dict[str, Any]:
    now_ms = _now_ms() if now_ms is None else now_ms
    heartbeat = state.get("heartbeat_ms")
    fresh_for = int(config.get("heartbeat_seconds", 30)) * 2 * 1000
    running = bool(state.get("running")) and heartbeat is not None and now_ms - int(heartbeat) <= fresh_for
    equity = mark_equity(state)
    return {
        "process": "heartbeat_fresh" if running else "not_running",
        "paper_only": True,
        "orders_sent": 0,
        "symbol": SYMBOL,
        "product": PRODUCT,
        "source": state.get("source"),
        "valuation": valuation(state, now_ms, config),
        "funding_status": state.get("funding_status"),
        "cash": state.get("cash"),
        "mark_equity": _d(equity),
        "realized_funding_pnl": state.get("funding_pnl"),
        "fees": state.get("fees"),
        "position": state.get("position"),
        "pending": state.get("pending"),
        "last_mark_time": _iso((state.get("last_mark") or {}).get("event_ms")),
        "last_closed_bar_open": _iso((state.get("closed_bars") or [{}])[-1].get("open_ms") if state.get("closed_bars") else None),
        "heartbeat_time": _iso(heartbeat),
        "forward_fills_are_not_historical_backfill": True,
    }


def _paths(config: dict[str, Any]) -> tuple[Path, Path, Path]:
    return Path(config["state_path"]), Path(config["log_path"]), Path(config["stop_path"])


def cmd_stop(config: dict[str, Any]) -> int:
    _state, _log, stop = _paths(config)
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text("stop\n", encoding="utf-8")
    print(json.dumps({"stopped_requested": True, "stop_path": str(stop), "orders_sent": 0}, ensure_ascii=False))
    return 0


def cmd_status(config: dict[str, Any]) -> int:
    state_path, _log, stop = _paths(config)
    if not state_path.exists():
        print(json.dumps({"process": "not_started", "paper_only": True, "orders_sent": 0, "symbol": SYMBOL}, ensure_ascii=False))
        return 0
    state = load_state(state_path, config)
    if stop.exists():
        state["running"] = False
    print(json.dumps(status_report(state, config), ensure_ascii=False, indent=2))
    return 0


def _collect_streams(url: str, timeout: float) -> tuple[str, dict[str, Any], str | None]:
    sock = PublicUmSocket(url, timeout=timeout)
    found: dict[str, Any] = {}
    blocker = None
    handshake = ""
    try:
        handshake = sock.connect()
        deadline = time.time() + timeout
        while time.time() < deadline:
            text = sock.read_text(min(2.0, max(deadline - time.time(), 0.1)))
            if text is None:
                continue
            event = normalize_stream(json.loads(text))
            if event is None:
                continue
            kind = "kline_1d" if event["type"] == "kline" else event["type"]
            if kind not in found:
                found[kind] = {"event_ms": event.get("event_ms"), "time": _iso(event.get("event_ms"))}
                if event["type"] == "mark":
                    found[kind]["mark"] = str(event["mark"])
                    found[kind]["funding_rate"] = None if event["funding_rate"] is None else str(event["funding_rate"])
            if kind in {"mark", "kline_1d", "book"} and (
                ("mark" in found and "kline_1d" in found) or "book" in found and "kline_1d" not in url and "markPrice" not in url
            ):
                if "bookTicker" in url and "book" in found:
                    break
                if "mark" in found and "kline_1d" in found:
                    break
    except (PaperFeedError, OSError, TimeoutError, json.JSONDecodeError) as exc:
        blocker = str(exc)
    finally:
        sock.close()
    return handshake, found, blocker


def preflight(config: dict[str, Any], timeout: float = 12.0) -> dict[str, Any]:
    """Read-only connectivity check. It does not create a paper position."""
    result: dict[str, Any] = {
        "ok": False,
        "host": FEED_HOST,
        "product": PRODUCT,
        "symbol": SYMBOL,
        "proxy": "disabled",
        "rest_fapi": "not_called",
        "orders_sent": 0,
        "this_is_not_a_running_paper_session": True,
        "streams": {},
        "handshakes": {},
        "blocker": None,
        "note": "On this host, daily kline and markPrice arrive on /market/stream; bookTicker arrives on /ws. Both are public fstream.binance.com USD-M streams.",
    }
    blockers = []
    for label, url in (
        ("mark_kline", config["feed"]["mark_kline_url"]),
        ("book", config["feed"]["book_url"]),
    ):
        handshake, found, blocker = _collect_streams(url, timeout)
        result["handshakes"][label] = handshake
        result["streams"].update(found)
        if blocker:
            blockers.append(f"{label}: {blocker}")
    missing = [name for name in ("book", "mark", "kline_1d") if name not in result["streams"]]
    if missing:
        blockers.append("public USDT-M feed did not deliver: " + ", ".join(missing))
    if blockers:
        result["blocker"] = "; ".join(blockers)
    else:
        result["ok"] = True
    return result


def cmd_preflight(config: dict[str, Any]) -> int:
    report = preflight(config)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


def _bars_from_kline_zip(payload: bytes, today: date) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for row in parse_vision_kline_csv(zip_csv_text(payload)):
        open_time = row["timestamp"]
        if not isinstance(open_time, datetime) or open_time.date() >= today:
            continue
        open_ms = int(open_time.timestamp() * 1000)
        parsed.append(
            {
                "open_ms": open_ms,
                "close_ms": open_ms + 86_400_000 - 1,
                "close": row["close"],
            }
        )
    return parsed


def load_warmup_bars(
    now: datetime | None = None,
    cache_root: Path | None = None,
    fetcher: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    """Completed daily trade klines from the public archive. No FAPI and no fills.

    The current UTC month often has no monthly file yet, so that 404 is skipped
    and each completed day is requested from the daily archive. A missing or
    unreadable day does not abort startup. Bars that open today are incomplete
    and are left out.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise PaperForwardError("warmup clock must be timezone-aware")
    today = now.astimezone(timezone.utc).date()
    months: list[date] = []
    cursor = date(today.year, today.month, 1)
    for _ in range(4):
        months.append(cursor)
        if cursor.month == 1:
            cursor = date(cursor.year - 1, 12, 1)
        else:
            cursor = date(cursor.year, cursor.month - 1, 1)
    bars: list[dict[str, Any]] = []
    cache = cache_root or Path("data/research-cache")
    read = fetcher or fetch_vision_zip
    for month in reversed(months):
        stamp = f"{month.year:04d}-{month.month:02d}"
        url = f"https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/1d/BTCUSDT-1d-{stamp}.zip"
        try:
            payload, _meta = read(url, cache)
            bars.extend(_bars_from_kline_zip(payload, today))
        except (ResearchSafetyError, OSError, ValueError):
            continue
    day = date(today.year, today.month, 1)
    while day < today:
        url = (
            "https://data.binance.vision/data/futures/um/daily/klines/BTCUSDT/1d/"
            f"BTCUSDT-1d-{day.isoformat()}.zip"
        )
        try:
            payload, _meta = read(url, cache)
            bars.extend(_bars_from_kline_zip(payload, today))
        except (ResearchSafetyError, OSError, ValueError):
            pass
        day += timedelta(days=1)
    deduped: dict[int, dict[str, Any]] = {}
    for bar in bars:
        deduped[int(bar["open_ms"])] = bar
    ordered = [deduped[key] for key in sorted(deduped)]
    return ordered[-120:]


def cmd_start(
    config: dict[str, Any],
    *,
    feed_factory: Callable[[str], Any] | None = None,
    warmup_loader: Callable[[], list[dict[str, Any]]] | None = None,
) -> int:
    check = preflight(config, timeout=20.0) if feed_factory is None else {"ok": True, "streams": {"injected": True}}
    if not check.get("ok"):
        print(json.dumps({"started": False, "blocker": check.get("blocker"), "orders_sent": 0}, ensure_ascii=False), file=sys.stderr)
        return 2
    state_path, log_path, stop_path = _paths(config)
    if stop_path.exists():
        stop_path.unlink()
    state = load_state(state_path, config)
    if not state["closed_bars"]:
        for bar in (warmup_loader or load_warmup_bars)():
            on_closed_bar(state, config, bar, live=False)
            state["warmup_bar_open_ms"].append(int(bar["open_ms"]))
        append_log(
            log_path,
            {
                "type": "warmup",
                "bars": len(state["closed_bars"]),
                "forward_trades": 0,
                "note": "historical closes initialize MA20/50 only",
            },
        )
    state["running"] = True
    save_state(state_path, state)
    mark_url = config["feed"]["mark_kline_url"]
    book_url = config["feed"]["book_url"]
    mark_sock = feed_factory(mark_url) if feed_factory else PublicUmSocket(mark_url)
    book_sock = feed_factory(book_url) if feed_factory else PublicUmSocket(book_url)
    stopping = {"flag": False}

    def _handle(_signum: int, _frame: Any) -> None:
        stopping["flag"] = True

    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    import queue
    import threading

    messages: queue.Queue[tuple[str, str]] = queue.Queue()

    def _pump(label: str, sock: PublicUmSocket) -> None:
        try:
            sock.connect()
            while not stopping["flag"] and not stop_path.exists():
                text = sock.read_text(1.0)
                if text:
                    messages.put((label, text))
        except Exception as exc:  # noqa: BLE001 - surface the feed failure and stop
            messages.put((label, json.dumps({"type": "feed_error", "error": str(exc)})))
            stopping["flag"] = True

    threads = [
        threading.Thread(target=_pump, args=("mark_kline", mark_sock), daemon=True),
        threading.Thread(target=_pump, args=("book", book_sock), daemon=True),
    ]
    for thread in threads:
        thread.start()
    last_heartbeat = 0.0
    try:
        while not stopping["flag"]:
            if stop_path.exists():
                break
            try:
                _label, text = messages.get(timeout=1.0)
            except queue.Empty:
                text = ""
            if text:
                payload = json.loads(text)
                if payload.get("type") == "feed_error":
                    append_log(log_path, {"type": "feed_error", "error": payload["error"], "orders_sent": 0})
                    break
                for event in apply_feed_event(state, config, payload):
                    event["source"] = state["source"]
                    event["orders_sent"] = 0
                    append_log(log_path, event)
                    if event["type"] in {"fill", "funding", "signal", "rejected", "warning"}:
                        save_state(state_path, state)
            now = time.time()
            if now - last_heartbeat >= int(config.get("heartbeat_seconds", 30)):
                state["heartbeat_ms"] = _now_ms()
                beat = status_report(state, config)
                beat["type"] = "heartbeat"
                append_log(log_path, beat)
                save_state(state_path, state)
                last_heartbeat = now
    finally:
        stopping["flag"] = True
        state["running"] = False
        state["heartbeat_ms"] = _now_ms()
        save_state(state_path, state)
        append_log(log_path, {"type": "stopped", "orders_sent": 0, "ts": _iso(state["heartbeat_ms"])})
        mark_sock.close()
        book_sock.close()
        for thread in threads:
            thread.join(timeout=2)
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
    print(json.dumps({"stopped": True, "orders_sent": 0, "state": str(state_path)}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BTCUSDT USD-M public-feed paper runner. No orders.")
    parser.add_argument("command", choices=["preflight", "start", "status", "stop"])
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        config = load_paper_config(args.config)
        if args.command == "preflight":
            return cmd_preflight(config)
        if args.command == "status":
            return cmd_status(config)
        if args.command == "stop":
            return cmd_stop(config)
        return cmd_start(config)
    except (PaperForwardError, PaperFeedError) as exc:
        print(f"纸面前向拒绝执行: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

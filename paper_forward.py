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
    if str(payload.get("portfolio", "")) == "alt_slow_momentum":
        from paper_alt_forward import load_alt_config

        return load_alt_config(payload, path)
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
    policy = str(payload.get("leverage_policy", "fixed_1x"))
    if policy not in {"fixed_1x", "risk_bounded_dynamic"}:
        raise PaperForwardError("leverage_policy must be fixed_1x or risk_bounded_dynamic")
    strategy = payload.get("strategy") or {}
    from strategy_candidates import SWING_STRATEGY_NAMES

    swing = str(strategy.get("name", "")) in SWING_STRATEGY_NAMES
    if policy == "fixed_1x":
        if swing:
            raise PaperForwardError("4h long/short paper uses risk_bounded_dynamic; the fixed 1x daily config stays long-only")
        if int(payload.get("leverage", 0)) != 1:
            raise PaperForwardError("the running fixed-1x paper session cannot change leverage")
        if int(strategy.get("fast_window", 0)) != 20 or int(strategy.get("slow_window", 0)) != 50:
            raise PaperForwardError("strategy windows are frozen at MA 20/50")
        if int(strategy.get("momentum_window", 0)) != 20:
            raise PaperForwardError("momentum window is frozen at 20")
        if Decimal(str(strategy.get("stop_pct", "0"))) != Decimal("0.08"):
            raise PaperForwardError("stop is frozen at 8%")
    elif swing:
        _validate_swing_config(payload)
    else:
        ceiling = int(payload.get("max_contract_leverage", 0))
        if not 1 <= ceiling <= 50:
            raise PaperForwardError("dynamic contract leverage must stay inside 1..50")
        # The existing dynamic daily feed remains the MA20/50 8% stop. It is not the 4h book.
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
    kline_token = "kline_4h" if swing else "kline_1d"
    if "/market/" not in mark_url or kline_token not in mark_url or "markPrice" not in mark_url:
        raise PaperForwardError("mark/kline feed must be the public USD-M kline and markPrice stream for this config")
    if "bookTicker" not in book_url:
        raise PaperForwardError("quote feed must be the public USD-M bookTicker stream")
    payload["_path"] = str(path)
    return payload


def _validate_swing_config(payload: dict[str, Any]) -> None:
    """A second paper process: long and short, own state files, frozen 4h preset."""
    from strategy_candidates import SWING_GRIDS, preset_matches

    strategy = payload["strategy"]
    name = str(strategy["name"])
    preset = strategy.get("preset")
    if not isinstance(preset, dict) or preset.get("allow_short") is not True or preset.get("name") != name:
        raise PaperForwardError("swing preset must set allow_short=True and the same strategy name")
    if str(payload.get("bar_interval", "")) != "4h":
        raise PaperForwardError("swing paper reads 4h bars")
    ceiling = int(payload.get("max_contract_leverage", 0))
    if not 1 <= ceiling <= 50 or str(payload.get("leverage_policy")) != "risk_bounded_dynamic":
        raise PaperForwardError("swing paper uses the 1..50 risk-bounded planner")
    state_name = Path(str(payload.get("state_path", ""))).name
    if state_name in {"", "btcusdt-paper-state.json"}:
        raise PaperForwardError("swing paper must use its own state file, not the running 1x daily state")
    if not any(preset_matches(item, preset) for item in SWING_GRIDS[name]):
        raise PaperForwardError("swing preset is not one of the frozen long/short grids")


def _is_swing(config: dict[str, Any]) -> bool:
    from strategy_candidates import SWING_STRATEGY_NAMES

    return str((config.get("strategy") or {}).get("name", "")) in SWING_STRATEGY_NAMES


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


def migrate_state(payload: dict[str, Any]) -> dict[str, Any]:
    """Read an existing 1x paper ledger without rewriting its cash or open margin."""
    payload["version"] = int(payload.get("version", 1))
    payload.setdefault("leverage_changes_sent", 0)
    payload.setdefault("simulated_leverage", 1)
    payload.setdefault("orders_sent", 0)
    position = payload.get("position")
    if isinstance(position, dict) and "contract_leverage" not in position:
        assumed = int(position.get("leverage") or payload.get("simulated_leverage") or 1)
        position["contract_leverage"] = assumed
        position["leverage_locked_from_prior_state"] = True
        payload["migration"] = {
            "open_position_margin_unchanged": True,
            "assumed_contract_leverage": assumed,
        }
    return payload


def load_state(path: Path, config: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return new_state(config)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("symbol") != SYMBOL or payload.get("product") != PRODUCT:
        raise PaperForwardError("state file is not the BTCUSDT USD-M paper ledger")
    if int(payload.get("orders_sent", 0)) != 0:
        raise PaperForwardError("state claims orders were sent; refusing to continue")
    return migrate_state(payload)


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
        qty = Decimal(position["qty"])
        entry = Decimal(position["entry"])
        pnl = (entry - mark_price) * qty if position.get("side") == "short" else (mark_price - entry) * qty
        equity += Decimal(position["margin"]) + pnl
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
    if state.get("feed_down"):
        return "feed_down"
    label = valuation(state, now_ms, config)
    if label != "fresh":
        return label
    if len(state.get("closed_bars") or []) < 50:
        return "warmup_incomplete"
    return None


def _pending_stop(state: dict[str, Any], config: dict[str, Any]) -> Decimal:
    pending = state.get("pending") or {}
    if pending.get("stop_distance") not in (None, ""):
        return _dec(pending["stop_distance"], "stop_distance")
    return _dec(config["strategy"].get("stop_pct", "0.08"), "stop_pct")


def _fee(notional: Decimal, config: dict[str, Any]) -> Decimal:
    return abs(notional) * _dec(config.get("fee_bps", "5"), "fee_bps") / BPS


def _slip(price: Decimal, side: str, config: dict[str, Any]) -> Decimal:
    return apply_slippage(price, side, _dec(config.get("slippage_bps", "2"), "slippage_bps"))


def _open_long(state: dict[str, Any], config: dict[str, Any], raw: Decimal, event_ms: int, reason: str, signal_open_ms: int) -> dict[str, Any]:
    if str(config.get("leverage_policy", "fixed_1x")) == "risk_bounded_dynamic":
        return _open_long_dynamic(state, config, raw, event_ms, reason, signal_open_ms)
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


def _open_long_dynamic(state: dict[str, Any], config: dict[str, Any], raw: Decimal, event_ms: int, reason: str, signal_open_ms: int) -> dict[str, Any]:
    """Opt-in planner. The default fixed-1x path does not call this."""
    from leverage_policy import MaintenanceModel, plan_leverage

    fill = _slip(raw, "buy", config)
    equity = mark_equity(state) or _cash(state)
    model = MaintenanceModel(
        rate=_dec(config.get("maintenance_rate", "0.004"), "maintenance_rate"),
        liquidation_fee_rate=_dec(config.get("liquidation_fee_rate", "0.005"), "liquidation_fee_rate"),
    )
    plan = plan_leverage(
        equity=equity,
        cash=_cash(state),
        price=fill,
        side="long",
        stop_distance=_pending_stop(state, config),
        risk_fraction=_dec(config.get("risk_per_trade_pct", "0.01"), "risk_per_trade_pct"),
        model=model,
        fee_rate=_dec(config.get("fee_bps", "5"), "fee_bps") / BPS,
        slippage_rate=_dec(config.get("slippage_bps", "2"), "slippage_bps") / BPS,
        extra_buffer=_dec(config.get("extra_buffer", "0.001"), "extra_buffer"),
        max_leverage=int(config.get("max_contract_leverage", 50)),
    )
    if not plan.accepted or plan.qty <= 0:
        return {
            "type": "rejected",
            "reason": plan.reason,
            "contract_leverage": plan.contract_leverage,
            "liquidation_distance": _d(plan.liquidation_distance),
            "required_buffer": _d(plan.required_buffer),
            "ts_ms": event_ms,
            "orders_sent": 0,
        }
    fee = plan.notional * _dec(config.get("fee_bps", "5"), "fee_bps") / BPS
    state["cash"] = _d(_cash(state) - plan.initial_margin - fee)
    state["fees"] = _d(Decimal(state["fees"]) + fee)
    stop = fill * (Decimal("1") - plan.stop_distance)
    state["position"] = {
        "side": "long",
        "qty": _d(plan.qty),
        "entry": _d(fill),
        "stop": _d(stop),
        "margin": _d(plan.initial_margin),
        "entry_ms": event_ms,
        "signal_bar_open_ms": signal_open_ms,
        "contract_leverage": plan.contract_leverage,
        "effective_leverage": _d(plan.effective_leverage),
        "liquidation_distance": _d(plan.liquidation_distance),
        "liquidation_price": _d(fill * (Decimal("1") - plan.liquidation_distance)),
        "leverage_reason": plan.reason,
        "leverage_locked_from_prior_state": False,
    }
    state["filled_signal_open_ms"].append(signal_open_ms)
    state["pending"] = None
    return {
        "type": "fill",
        "action": "open_long",
        "reason": reason,
        "qty": _d(plan.qty),
        "raw_price": _d(raw),
        "fill_price": _d(fill),
        "fee": _d(fee),
        "contract_leverage": plan.contract_leverage,
        "effective_leverage": _d(plan.effective_leverage),
        "initial_margin": _d(plan.initial_margin),
        "notional": _d(plan.notional),
        "stop_distance": _d(plan.stop_distance),
        "liquidation_distance": _d(plan.liquidation_distance),
        "leverage_reason": plan.reason,
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


def _open_short(state: dict[str, Any], config: dict[str, Any], raw: Decimal, event_ms: int, reason: str, signal_open_ms: int) -> dict[str, Any]:
    """Open a short only when a long/short strategy asked for one. The daily config never does."""
    from leverage_policy import MaintenanceModel, plan_leverage

    fill = _slip(raw, "sell", config)
    equity = mark_equity(state) or _cash(state)
    ceiling = 1 if str(config.get("leverage_policy", "fixed_1x")) == "fixed_1x" else int(config.get("max_contract_leverage", 50))
    plan = plan_leverage(
        equity=equity,
        cash=_cash(state),
        price=fill,
        side="short",
        stop_distance=_pending_stop(state, config),
        risk_fraction=_dec(config.get("risk_per_trade_pct", "0.01"), "risk_per_trade_pct"),
        model=MaintenanceModel(
            rate=_dec(config.get("maintenance_rate", "0.004"), "maintenance_rate"),
            liquidation_fee_rate=_dec(config.get("liquidation_fee_rate", "0.005"), "liquidation_fee_rate"),
        ),
        fee_rate=_dec(config.get("fee_bps", "5"), "fee_bps") / BPS,
        slippage_rate=_dec(config.get("slippage_bps", "2"), "slippage_bps") / BPS,
        extra_buffer=_dec(config.get("extra_buffer", "0.001"), "extra_buffer"),
        max_leverage=ceiling,
    )
    if not plan.accepted or plan.qty <= 0:
        return {"type": "rejected", "reason": plan.reason, "ts_ms": event_ms, "orders_sent": 0}
    fee = plan.notional * _dec(config.get("fee_bps", "5"), "fee_bps") / BPS
    state["cash"] = _d(_cash(state) - plan.initial_margin - fee)
    state["fees"] = _d(Decimal(state["fees"]) + fee)
    state["position"] = {
        "side": "short",
        "qty": _d(plan.qty),
        "entry": _d(fill),
        "stop": _d(fill * (Decimal("1") + plan.stop_distance)),
        "margin": _d(plan.initial_margin),
        "entry_ms": event_ms,
        "signal_bar_open_ms": signal_open_ms,
        "contract_leverage": plan.contract_leverage,
        "effective_leverage": _d(plan.effective_leverage),
        "liquidation_distance": _d(plan.liquidation_distance),
        "liquidation_price": _d(fill * (Decimal("1") + plan.liquidation_distance)),
        "leverage_reason": plan.reason,
        "allow_short": True,
    }
    state["filled_signal_open_ms"].append(signal_open_ms)
    state["pending"] = None
    return {
        "type": "fill",
        "action": "open_short",
        "reason": reason,
        "qty": _d(plan.qty),
        "raw_price": _d(raw),
        "fill_price": _d(fill),
        "fee": _d(fee),
        "contract_leverage": plan.contract_leverage,
        "effective_leverage": _d(plan.effective_leverage),
        "stop_distance": _d(plan.stop_distance),
        "liquidation_distance": _d(plan.liquidation_distance),
        "ts_ms": event_ms,
        "signal_bar_open_ms": signal_open_ms,
        "orders_sent": 0,
    }


def _close_short(state: dict[str, Any], config: dict[str, Any], raw: Decimal, event_ms: int, reason: str, signal_open_ms: int | None) -> dict[str, Any]:
    position = _position(state)
    if position is None or position.get("side") != "short":
        return {"type": "noop", "ts_ms": event_ms}
    qty = Decimal(position["qty"])
    fill = _slip(raw, "buy", config)
    fee = _fee(fill * qty, config)
    pnl = (Decimal(position["entry"]) - fill) * qty
    state["cash"] = _d(_cash(state) + Decimal(position["margin"]) + pnl - fee)
    state["fees"] = _d(Decimal(state["fees"]) + fee)
    state["position"] = None
    state["pending"] = None
    if signal_open_ms is not None:
        state["filled_signal_open_ms"].append(signal_open_ms)
    return {
        "type": "fill",
        "action": "close_short",
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
    stored = {"open_ms": open_ms, "close_ms": int(bar["close_ms"]), "close": str(bar["close"])}
    if bar.get("high") is not None and bar.get("low") is not None:
        stored["high"] = str(bar["high"])
        stored["low"] = str(bar["low"])
    state["closed_bars"].append(stored)
    if not live or open_ms in state["warmup_bar_open_ms"]:
        return [{"type": "warmup_bar", "open_ms": open_ms, "counted_as_forward_trade": False}]
    if _is_swing(config):
        side, stop_distance = _swing_signal(state, config)
    else:
        closes = [Decimal(item["close"]) for item in state["closed_bars"]]
        side = desired_side(closes, strategy_from(config), allow_short=False)
        stop_distance = None
    position = _position(state)
    current = position.get("side") if position else "flat"
    events: list[dict[str, Any]] = [{"type": "closed_bar", "open_ms": open_ms, "close_ms": int(bar["close_ms"]), "desired": side}]
    if side is None or side == "hold" or side == current:
        return events
    if side in {"long", "short"} and current == "flat":
        blocked = entries_blocked(state, int(bar["close_ms"]), config)
        if blocked:
            events.append({"type": "rejected", "reason": blocked, "open_ms": open_ms, "desired": side})
            return events
    pending = {
        "signal_bar_open_ms": open_ms,
        "signal_bar_close_ms": int(bar["close_ms"]),
        "side": side,
    }
    if stop_distance is not None:
        pending["stop_distance"] = str(stop_distance)
    state["pending"] = pending
    events.append({"type": "signal", "side": side, "signal_bar_open_ms": open_ms, "fill": "waiting_for_quote_or_next_open"})
    return events


def _swing_signal(state: dict[str, Any], config: dict[str, Any]) -> tuple[str | None, Decimal | None]:
    from strategy_candidates import SwingBook, materialize_preset

    book = SwingBook(materialize_preset(config["strategy"]["preset"]))
    decision: str | None = None
    fraction: Decimal | None = None
    for bar in state["closed_bars"]:
        if "high" not in bar or "low" not in bar:
            raise PaperForwardError("4h long/short bars require high and low")
        decision, fraction = book.on_bar(
            datetime.fromtimestamp(int(bar["open_ms"]) / 1000, tz=timezone.utc),
            Decimal(bar["high"]),
            Decimal(bar["low"]),
            Decimal(bar["close"]),
        )
    return decision, fraction


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
    signal_open_ms = int(pending["signal_bar_open_ms"])
    return _fill_pending(state, config, pending, event_ms, signal_open_ms, Decimal(quote["bid"]), Decimal(quote["ask"]), "first_quote_after_signal")


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
    signal_open_ms = int(pending["signal_bar_open_ms"])
    return _fill_pending(state, config, pending, event_ms, signal_open_ms, raw, raw, "next_bar_open")


def _fill_pending(
    state: dict[str, Any],
    config: dict[str, Any],
    pending: dict[str, Any],
    event_ms: int,
    signal_open_ms: int,
    bid: Decimal,
    ask: Decimal,
    reason: str,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    position = _position(state)
    if position is not None and pending["side"] != position.get("side"):
        if position.get("side") == "short":
            events.append(_close_short(state, config, ask, event_ms, reason, signal_open_ms if pending["side"] == "flat" else None))
        else:
            events.append(_close_long(state, config, bid, event_ms, reason, signal_open_ms if pending["side"] == "flat" else None))
        if pending["side"] in {"long", "short"}:
            state["pending"] = pending
        position = _position(state)
    if position is None and pending["side"] == "long":
        events.append(_open_long(state, config, ask, event_ms, reason, signal_open_ms))
    elif position is None and pending["side"] == "short":
        events.append(_open_short(state, config, bid, event_ms, reason, signal_open_ms))
    return events


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
    if position.get("side") == "short":
        payment = -payment
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
    if position is not None:
        stop = Decimal(position["stop"])
        if position.get("side") == "short" and price >= stop:
            reason = "stop_gap" if previous_price is not None and previous_price < stop else "stop"
            events.append(_close_short(state, config, price, event_ms, reason, None))
        elif position.get("side", "long") != "short" and price <= stop:
            reason = "stop_gap" if previous_price is not None and previous_price > stop else "stop"
            events.append(_close_long(state, config, price, event_ms, reason, None))
    return events


def normalize_stream(message: dict[str, Any], allowed_symbols: frozenset[str] | None = None) -> dict[str, Any] | None:
    data = message.get("data", message)
    if not isinstance(data, dict):
        return None
    kind = data.get("e")
    symbol = str(data.get("s") or (data.get("k") or {}).get("s") or "")
    allowed = allowed_symbols or frozenset({SYMBOL})
    if symbol and symbol.upper() not in allowed:
        raise PaperForwardError(f"feed symbol {symbol} is not in the paper universe; not switching market")
    if kind == "bookTicker":
        return {
            "type": "book",
            "symbol": symbol.upper() or SYMBOL,
            "bid": Decimal(str(data["b"])),
            "ask": Decimal(str(data["a"])),
            "event_ms": int(data.get("E") or data.get("T")),
        }
    if kind == "markPriceUpdate":
        rate = data.get("r")
        index = data.get("i")
        return {
            "type": "mark",
            "symbol": symbol.upper() or SYMBOL,
            "mark": Decimal(str(data["p"])),
            "index": None if index in (None, "") else Decimal(str(index)),
            "funding_rate": None if rate in (None, "") else Decimal(str(rate)),
            "event_ms": int(data["E"]),
        }
    if kind == "kline":
        candle = data.get("k") or {}
        interval = candle.get("i")
        if interval not in {"1d", "4h"}:
            raise PaperForwardError(f"kline interval {interval!r} is not a supported paper interval")
        return {
            "type": "kline",
            "symbol": symbol.upper() or SYMBOL,
            "interval": interval,
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
    expected = str(config.get("bar_interval", "1d"))
    if event.get("interval") != expected:
        raise PaperForwardError(f"kline interval {event.get('interval')!r} does not match this config ({expected})")
    if event["closed"]:
        return on_closed_bar(
            state,
            config,
            {
                "open_ms": event["open_ms"],
                "close_ms": event["close_ms"],
                "close": event["close"],
                "high": event["high"],
                "low": event["low"],
            },
            live=True,
        )
    return on_next_open(state, config, event)


def status_report(state: dict[str, Any], config: dict[str, Any], now_ms: int | None = None) -> dict[str, Any]:
    now_ms = _now_ms() if now_ms is None else now_ms
    heartbeat = state.get("heartbeat_ms")
    fresh_for = int(config.get("heartbeat_seconds", 30)) * 2 * 1000
    running = bool(state.get("running")) and heartbeat is not None and now_ms - int(heartbeat) <= fresh_for
    equity = mark_equity(state)
    market = _market_process(state, config, now_ms, running)
    return {
        "process": market,
        "ts": _iso(now_ms),
        "paper_only": True,
        "orders_sent": 0,
        "symbol": state.get("symbol") or SYMBOL,
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
        "forward_fills": int(state.get("forward_fills") or 0),
        "feed_down": bool(state.get("feed_down")),
        "forward_fills_are_not_historical_backfill": True,
    }


def _market_process(state: dict[str, Any], config: dict[str, Any], now_ms: int, running: bool) -> str:
    """A live heartbeat with an old quote is stale, not fresh."""
    if not running:
        return "not_running"
    if state.get("feed_down"):
        return "stale"
    mark_ms = (state.get("last_mark") or {}).get("event_ms")
    if mark_ms is None or now_ms - int(mark_ms) > int(config.get("stale_mark_seconds", 30)) * 1000:
        return "stale"
    book_ms = (state.get("last_book") or {}).get("event_ms")
    if book_ms is not None and now_ms - int(book_ms) > int(config.get("stale_book_seconds", 15)) * 1000:
        return "stale"
    return "heartbeat_fresh"


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
            kind = f"kline_{event.get('interval', '1d')}" if event["type"] == "kline" else event["type"]
            if kind not in found:
                found[kind] = {"event_ms": event.get("event_ms"), "time": _iso(event.get("event_ms"))}
                if event["type"] == "mark":
                    found[kind]["mark"] = str(event["mark"])
                    found[kind]["funding_rate"] = None if event["funding_rate"] is None else str(event["funding_rate"])
            kline_names = [name for name in found if name.startswith("kline_")]
            if kind in {"mark", "book"} or kind.startswith("kline_"):
                if "bookTicker" in url and "book" in found and "kline_" not in url and "markPrice" not in url:
                    break
                if "mark" in found and kline_names:
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
    expected_kline = "kline_4h" if str(config.get("bar_interval", "1d")) == "4h" else "kline_1d"
    missing = [name for name in ("book", "mark", expected_kline) if name not in result["streams"]]
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


def _bars_from_kline_zip(payload: bytes, today: date, interval: str = "1d", now: datetime | None = None) -> list[dict[str, Any]]:
    width = 86_400_000 if interval == "1d" else 4 * 60 * 60 * 1000
    now_ms = None if now is None else int(now.timestamp() * 1000)
    parsed: list[dict[str, Any]] = []
    for row in parse_vision_kline_csv(zip_csv_text(payload)):
        open_time = row["timestamp"]
        if not isinstance(open_time, datetime):
            continue
        open_ms = int(open_time.timestamp() * 1000)
        close_ms = open_ms + width - 1
        if interval == "4h":
            # Keep today's bars once they have closed. The open bar stays out.
            if now_ms is None or close_ms >= now_ms:
                continue
        elif open_time.date() >= today:
            continue
        item = {
            "open_ms": open_ms,
            "close_ms": close_ms,
            "close": row["close"],
        }
        if interval == "4h":
            item["high"] = row["high"]
            item["low"] = row["low"]
        parsed.append(item)
    return parsed


def load_warmup_bars(
    now: datetime | None = None,
    cache_root: Path | None = None,
    fetcher: Callable[..., Any] | None = None,
    interval: str = "1d",
    symbol: str = "BTCUSDT",
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
    if interval not in {"1d", "4h"}:
        raise PaperForwardError(f"warmup interval {interval!r} is not supported")
    today = now.astimezone(timezone.utc).date()
    months: list[date] = []
    cursor = date(today.year, today.month, 1)
    for _ in range(8 if interval == "4h" else 4):
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
        url = f"https://data.binance.vision/data/futures/um/monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{stamp}.zip"
        try:
            payload, _meta = read(url, cache)
            bars.extend(_bars_from_kline_zip(payload, today, interval, now))
        except (ResearchSafetyError, OSError, ValueError):
            continue
    day = date(today.year, today.month, 1)
    last_day = today if interval == "4h" else today - timedelta(days=1)
    while day <= last_day:
        url = (
            f"https://data.binance.vision/data/futures/um/daily/klines/{symbol}/{interval}/"
            f"{symbol}-{interval}-{day.isoformat()}.zip"
        )
        try:
            payload, _meta = read(url, cache)
            bars.extend(_bars_from_kline_zip(payload, today, interval, now))
        except (ResearchSafetyError, OSError, ValueError):
            pass
        day += timedelta(days=1)
    deduped: dict[int, dict[str, Any]] = {}
    for bar in bars:
        deduped[int(bar["open_ms"])] = bar
    ordered = [deduped[key] for key in sorted(deduped)]
    keep = 1000 if interval == "4h" else 120
    return ordered[-keep:]


def _supervisor(config: dict[str, Any], stop_path: Path, factory: Callable[[str], Any], *streams: tuple[str, str]):
    from paper_feed import FeedSupervisor

    return FeedSupervisor(
        list(streams),
        factory,
        stop_path,
        initial_backoff=float(config.get("reconnect_initial_seconds", 1)),
        max_backoff=float(config.get("reconnect_max_seconds", 60)),
    )


def _next_feed_item(supervisor: Any) -> dict[str, Any] | None:
    try:
        return supervisor.messages.get(timeout=0.5)
    except Exception:
        return None


def _mark_feed_down(state: dict[str, Any]) -> None:
    state["feed_down"] = True


def _handle_btc_feed_item(
    item: dict[str, Any],
    state: dict[str, Any],
    config: dict[str, Any],
    log_path: Path,
    state_path: Path,
    last_rx: dict[str, int],
) -> None:
    if item["type"] in {"feed_error", "feed_disconnect", "feed_reconnect"}:
        append_log(log_path, item)
        if item["type"] != "feed_reconnect":
            state["feed_down"] = True
        return
    if item["type"] != "raw":
        return
    last_rx[item["stream"]] = _now_ms()
    try:
        payload = json.loads(item["text"])
    except json.JSONDecodeError as exc:
        append_log(log_path, {"type": "feed_error", "stream": item["stream"], "error": f"JSONDecodeError: {exc}", "ts": _iso(_now_ms()), "orders_sent": 0})
        return
    try:
        events = apply_feed_event(state, config, payload)
    except PaperForwardError as exc:
        append_log(log_path, {"type": "feed_error", "stream": item["stream"], "error": f"PaperForwardError: {exc}", "ts": _iso(_now_ms()), "orders_sent": 0})
        return
    for event in events:
        event["source"] = state["source"]
        event["orders_sent"] = 0
        event["ts"] = _iso(_now_ms())
        append_log(log_path, event)
        if event["type"] == "fill":
            state["forward_fills"] = int(state.get("forward_fills") or 0) + 1
        if event["type"] in {"fill", "funding", "signal", "rejected", "warning"}:
            save_state(state_path, state)
    if payload.get("e") in {"markPriceUpdate", "bookTicker"} or payload.get("data", {}).get("e") in {"markPriceUpdate", "bookTicker"}:
        state["feed_down"] = False


def _reconnect_if_stale(
    supervisor: Any,
    config: dict[str, Any],
    last_rx: dict[str, int],
    last_stale_log: dict[str, int],
    log_path: Path,
    mark_down: Callable[[], None],
) -> None:
    now = _now_ms()
    limits = {
        "mark_kline": int(config.get("stale_mark_seconds", 30)),
        "book": int(config.get("stale_book_seconds", 15)),
    }
    for label in last_rx:
        limit_ms = limits.get(label, int(config.get("stale_mark_seconds", 30))) * 1000
        if now - last_rx[label] <= limit_ms:
            continue
        if now - last_stale_log[label] <= limit_ms:
            continue
        last_stale_log[label] = now
        mark_down()
        append_log(
            log_path,
            {
                "type": "feed_disconnect",
                "stream": label,
                "reason": "stale_quote",
                "last_rx": _iso(last_rx[label]),
                "ts": _iso(now),
                "orders_sent": 0,
            },
        )
        supervisor.request_reconnect(label)


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
        loader = warmup_loader or (lambda: load_warmup_bars(interval=str(config.get("bar_interval", "1d"))))
        for bar in loader():
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
    state["feed_down"] = False
    state.setdefault("forward_fills", 0)
    save_state(state_path, state)
    factory = feed_factory or (lambda url: PublicUmSocket(url))
    supervisor = _supervisor(config, stop_path, factory, ("mark_kline", config["feed"]["mark_kline_url"]), ("book", config["feed"]["book_url"]))
    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGTERM, lambda _signum, _frame: setattr(supervisor, "stopping", True))
    signal.signal(signal.SIGINT, lambda _signum, _frame: setattr(supervisor, "stopping", True))
    supervisor.start()
    last_heartbeat = 0.0
    last_rx = {label: _now_ms() for label, _url in supervisor.streams}
    last_stale_log = {label: 0 for label in last_rx}
    try:
        while not supervisor.stopping:
            if stop_path.exists():
                break
            item = _next_feed_item(supervisor)
            if item is not None:
                _handle_btc_feed_item(item, state, config, log_path, state_path, last_rx)
            _reconnect_if_stale(supervisor, config, last_rx, last_stale_log, log_path, lambda: _mark_feed_down(state))
            if time.time() - last_heartbeat >= int(config.get("heartbeat_seconds", 30)):
                state["heartbeat_ms"] = _now_ms()
                beat = status_report(state, config)
                beat["type"] = "heartbeat"
                append_log(log_path, beat)
                save_state(state_path, state)
                last_heartbeat = time.time()
    finally:
        supervisor.stop()
        state["running"] = False
        state["heartbeat_ms"] = _now_ms()
        save_state(state_path, state)
        append_log(log_path, {"type": "stopped", "orders_sent": 0, "ts": _iso(state["heartbeat_ms"])})
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
        if config.get("portfolio") == "alt_slow_momentum":
            from paper_alt_forward import dispatch

            return dispatch(args.command, config)
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

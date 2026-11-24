"""Walk-forward study for predeclared 4h BTCUSDT momentum/trend rules.

Selection uses only bars before 2026-08-01. The reserved segment from that
date forward is evaluated once, after the walk-forward choice is fixed.
Parameters are the grids in strategy_candidates.py. Nothing is added after
the test windows are scored.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from leverage_policy import MaintenanceModel, plan_leverage
from research_data import fetch_vision_zip, parse_vision_funding_csv, parse_vision_kline_csv, zip_csv_text
from strategy_candidates import DONCHIAN_GRID, ATR_BREAKOUT_GRID, ROUND2_GRIDS, TSMOM_GRID, SwingBook

HOLDOUT_START = date(2026, 8, 1)
ROOT = "https://data.binance.vision/data/futures/um"
MIN_TRAIN_TRADES = 8
MIN_STITCH_TRADES = 20
MAX_DRAWDOWN = Decimal("0.35")
MIN_SHARPE = Decimal("0.5")
BARS_PER_YEAR = Decimal("2190")  # 4h bars, 365.25 * 6


@dataclass
class Candle:
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    mark_open: Decimal | None
    mark_high: Decimal | None
    mark_low: Decimal | None
    mark_close: Decimal | None

    def mark_path(self) -> tuple[Decimal, Decimal, Decimal, Decimal, bool]:
        if None not in (self.mark_open, self.mark_high, self.mark_low, self.mark_close):
            return self.mark_open, self.mark_high, self.mark_low, self.mark_close, True  # type: ignore[return-value]
        return self.open, self.high, self.low, self.close, False


def _months(start: date, end: date) -> list[date]:
    cursor = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    out = []
    while cursor <= last:
        out.append(cursor)
        cursor = date(cursor.year + 1, 1, 1) if cursor.month == 12 else date(cursor.year, cursor.month + 1, 1)
    return out


def _load(kind: str, interval: str, start: date, end: date, symbol: str = "BTCUSDT") -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    missing = []
    folder = "klines" if kind == "trade" else "markPriceKlines"
    for month in _months(start, end):
        stamp = f"{month:%Y-%m}"
        url = f"{ROOT}/monthly/{folder}/{symbol}/{interval}/{symbol}-{interval}-{stamp}.zip"
        try:
            payload, _meta = fetch_vision_zip(url, Path("data/research-cache"))
            rows.extend(parse_vision_kline_csv(zip_csv_text(payload)))
        except Exception:
            missing.append(stamp)
    kept = [row for row in rows if isinstance(row["timestamp"], datetime) and start <= row["timestamp"].date() <= end]
    kept.sort(key=lambda row: row["timestamp"])
    return kept, missing


def _funding(start: date, end: date, symbol: str = "BTCUSDT") -> tuple[list[tuple[datetime, Decimal]], int]:
    events: list[tuple[datetime, Decimal]] = []
    missing = 0
    for month in _months(start, end):
        stamp = f"{month:%Y-%m}"
        url = f"{ROOT}/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{stamp}.zip"
        try:
            payload, _meta = fetch_vision_zip(url, Path("data/research-cache"))
            events.extend(parse_vision_funding_csv(zip_csv_text(payload)))
        except Exception:
            missing += 1
    return [(ts, rate) for ts, rate in events if start <= ts.date() <= end], missing


def _join(trade: list[dict], mark: list[dict]) -> list[Candle]:
    marks = {row["timestamp"]: row for row in mark}
    out = []
    for row in trade:
        marked = marks.get(row["timestamp"])
        out.append(
            Candle(
                ts=row["timestamp"],
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                mark_open=None if marked is None else marked["open"],
                mark_high=None if marked is None else marked["high"],
                mark_low=None if marked is None else marked["low"],
                mark_close=None if marked is None else marked["close"],
            )
        )
    return out


def walk_forward_folds(first: date, holdout: date) -> list[tuple[date, date]]:
    """Six-month test windows that stop at the reserved segment."""
    folds = []
    cursor = first
    while cursor < holdout:
        nxt = date(cursor.year + (cursor.month + 5) // 12, (cursor.month + 5) % 12 + 1, 1)
        # Adding 6 months: cursor month + 6.
        month_index = cursor.month - 1 + 6
        nxt = date(cursor.year + month_index // 12, month_index % 12 + 1, 1)
        end = min(nxt, holdout)
        if end > cursor:
            folds.append((cursor, end))
        cursor = end
    return folds


def _adverse(side: str, open_: Decimal, low: Decimal, high: Decimal, stop: Decimal, liq: Decimal):
    triggers = [("stop", stop), ("liquidation", liq)]
    if side == "long":
        gapped = [item for item in triggers if open_ <= item[1]]
        if gapped:
            return f"{max(gapped, key=lambda item: item[1])[0]}_gap", open_
        ahead = [item for item in triggers if item[1] < open_]
        if not ahead:
            return None
        name, level = max(ahead, key=lambda item: item[1])
        if low <= level:
            return name, level
        return None
    gapped = [item for item in triggers if open_ >= item[1]]
    if gapped:
        return f"{min(gapped, key=lambda item: item[1])[0]}_gap", open_
    ahead = [item for item in triggers if item[1] > open_]
    if not ahead:
        return None
    name, level = min(ahead, key=lambda item: item[1])
    if high >= level:
        return name, level
    return None


def _d(value: Decimal) -> str:
    return format(value, "f")


def simulate_preset(
    candles: list[Candle],
    funding: list[tuple[datetime, Decimal]],
    preset: dict,
    *,
    trade_start: datetime,
    trade_end: datetime,
    equity0: Decimal,
    model: MaintenanceModel,
    fee_rate: Decimal,
    slippage: Decimal,
    extra_buffer: Decimal,
    risk_fraction: Decimal,
    signals: list[tuple[str | None, Decimal | None]] | None = None,
) -> dict[str, Any]:
    if trade_start >= HOLDOUT_START_DT and trade_end > HOLDOUT_START_DT:
        pass  # caller is responsible for the one-shot holdout
    if preset.get("allow_short") is not True:
        raise RuntimeError("every 4h candidate must set allow_short=True")
    book = None if signals is not None else SwingBook(preset)
    cash = equity0
    position = None
    pending = None
    fees = Decimal("0")
    funding_pnl = Decimal("0")
    missed = 0
    liquidations = 0
    rejections: Counter[str] = Counter()
    leverages: Counter[int] = Counter()
    effective: list[Decimal] = []
    trades: list[dict[str, Any]] = []
    curve: list[tuple[datetime, Decimal]] = []
    fund_i = 0
    entry_snapshot = None

    def charge(start: datetime, end: datetime, mark: Decimal | None) -> None:
        nonlocal cash, funding_pnl, missed, fund_i, position
        while fund_i < len(funding) and funding[fund_i][0] < start:
            fund_i += 1
        while fund_i < len(funding) and funding[fund_i][0] < end:
            stamp, rate = funding[fund_i]
            fund_i += 1
            if position is None or stamp < trade_start or stamp >= trade_end:
                continue
            if mark is None:
                missed += 1
                continue
            payment = Decimal(position["qty"]) * mark * rate
            if position["side"] == "short":
                payment = -payment
            cash -= payment
            funding_pnl -= payment

    def close_position(raw: Decimal, reason: str, when: datetime) -> None:
        nonlocal cash, fees, position, liquidations, entry_snapshot
        assert position is not None
        if position["side"] == "long":
            slipped = raw * (Decimal(1) - slippage)
            pnl = (slipped - Decimal(position["entry"])) * Decimal(position["qty"])
        else:
            slipped = raw * (Decimal(1) + slippage)
            pnl = (Decimal(position["entry"]) - slipped) * Decimal(position["qty"])
        fee = abs(slipped * Decimal(position["qty"])) * fee_rate
        if reason.startswith("liquidation"):
            fee += abs(slipped * Decimal(position["qty"])) * model.liquidation_fee_rate
            liquidations += 1
        cash += Decimal(position["margin"]) + pnl - fee
        fees += fee
        if trade_start <= when < trade_end:
            trades.append(
                {
                    "side": position["side"],
                    "pnl": pnl - fee,
                    "leverage": position["leverage"],
                    "reason": reason,
                    "entry": entry_snapshot,
                    "exit": when,
                }
            )
        position = None
        entry_snapshot = None

    for index, candle in enumerate(candles):
        if candle.ts >= trade_end:
            break
        nxt = candles[index + 1].ts if index + 1 < len(candles) else candle.ts + timedelta(hours=4)
        open_, high, low, close, used_mark = candle.mark_path()
        active = trade_start <= candle.ts < trade_end
        if position is not None and active:
            charge(candle.ts, candle.ts + timedelta(seconds=1), candle.mark_close)
            hit = _adverse(position["side"], open_, low, high, Decimal(position["stop"]), Decimal(position["liq"]))
            if hit is not None:
                close_position(hit[1], hit[0], candle.ts)
        if pending is not None and position is None and active:
            side, stop_distance = pending
            raw = candle.open
            fill = raw * (Decimal(1) + slippage) if side == "long" else raw * (Decimal(1) - slippage)
            plan = plan_leverage(
                equity=cash,
                cash=cash,
                price=fill,
                side=side,
                stop_distance=stop_distance,
                risk_fraction=risk_fraction,
                model=model,
                fee_rate=fee_rate,
                slippage_rate=slippage,
                extra_buffer=extra_buffer,
            )
            if (not used_mark and plan.contract_leverage > 1) or not plan.accepted:
                rejections[plan.reason if not plan.accepted else "high_leverage_needs_mark_ohlc"] += 1
            else:
                fee = plan.notional * fee_rate
                cash -= plan.initial_margin + fee
                fees += fee
                if side == "long":
                    stop = fill * (Decimal(1) - stop_distance)
                    liq = fill * (Decimal(1) - plan.liquidation_distance)
                else:
                    stop = fill * (Decimal(1) + stop_distance)
                    liq = fill * (Decimal(1) + plan.liquidation_distance)
                position = {
                    "side": side,
                    "qty": plan.qty,
                    "entry": fill,
                    "stop": stop,
                    "liq": liq,
                    "margin": plan.initial_margin,
                    "leverage": plan.contract_leverage,
                }
                entry_snapshot = candle.ts
                leverages[plan.contract_leverage] += 1
                effective.append(plan.effective_leverage)
                hit = _adverse(side, open_, low, high, stop, liq)
                if hit is not None:
                    close_position(hit[1], hit[0], candle.ts)
        pending = None
        if position is not None and active:
            charge(candle.ts + timedelta(seconds=1), nxt, candle.mark_close)
        if signals is None:
            decision, fraction = book.on_bar(candle.ts, candle.high, candle.low, candle.close)
        else:
            decision, fraction = signals[index]
        if fraction is not None and fraction > 0:
            if decision == "long":
                if position is not None and position["side"] != "long" and active:
                    close_position(close, "flip", candle.ts)
                if position is None or position["side"] != "long":
                    pending = ("long", fraction)
            elif decision == "short":
                if position is not None and position["side"] != "short" and active:
                    close_position(close, "flip", candle.ts)
                if position is None or position["side"] != "short":
                    pending = ("short", fraction)
            elif decision == "flat" and position is not None and active:
                close_position(close, "signal_flat", candle.ts)
        if active:
            mark = candle.mark_close or candle.close
            equity = cash
            if position is not None:
                qty = Decimal(position["qty"])
                upnl = (mark - Decimal(position["entry"])) * qty if position["side"] == "long" else (Decimal(position["entry"]) - mark) * qty
                equity += Decimal(position["margin"]) + upnl
            curve.append((candle.ts, equity))
    if position is not None and trade_start <= candles[-1].ts < trade_end:
        mark = candles[-1].mark_close or candles[-1].close
        qty = Decimal(position["qty"])
        upnl = (mark - Decimal(position["entry"])) * qty if position["side"] == "long" else (Decimal(position["entry"]) - mark) * qty
        trades.append({"side": position["side"], "pnl": upnl, "leverage": position["leverage"], "reason": "open_at_end", "entry": entry_snapshot, "exit": candles[-1].ts})
    return {
        "ending": cash if position is None else curve[-1][1] if curve else equity0,
        "fees": fees,
        "funding_pnl": funding_pnl,
        "missed_funding": missed,
        "liquidations": liquidations,
        "rejections": dict(rejections),
        "leverages": dict(leverages),
        "effective": effective,
        "trades": trades,
        "curve": curve,
    }


HOLDOUT_START_DT = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _sharpe(curve: list[tuple[datetime, Decimal]]) -> Decimal:
    if len(curve) < 3:
        return Decimal("0")
    returns = []
    for prev, nxt in zip(curve, curve[1:]):
        if prev[1] > 0:
            returns.append((nxt[1] - prev[1]) / prev[1])
    if len(returns) < 2:
        return Decimal("0")
    mean = sum(returns, Decimal("0")) / Decimal(len(returns))
    var = sum((item - mean) ** 2 for item in returns) / Decimal(len(returns) - 1)
    if var <= 0:
        return Decimal("0")
    std = var.sqrt()
    return mean / std * BARS_PER_YEAR.sqrt()


def _max_dd(curve: list[tuple[datetime, Decimal]]) -> Decimal:
    peak = None
    worst = Decimal("0")
    for _ts, equity in curve:
        if peak is None or equity > peak:
            peak = equity
        if peak and peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst


def _trade_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [trade for trade in trades if trade["reason"] != "open_at_end"]
    wins = [trade["pnl"] for trade in closed if trade["pnl"] > 0]
    losses = [trade["pnl"] for trade in closed if trade["pnl"] <= 0]
    by_side = {}
    for side in ("long", "short"):
        subset = [trade for trade in closed if trade["side"] == side]
        by_side[side] = {"trades": len(subset), "net_pnl": _d(sum((trade["pnl"] for trade in subset), Decimal("0")))}
    avg_win = sum(wins, Decimal("0")) / Decimal(len(wins)) if wins else Decimal("0")
    avg_loss = sum(losses, Decimal("0")) / Decimal(len(losses)) if losses else Decimal("0")
    payoff = (avg_win / abs(avg_loss)) if avg_loss != 0 else Decimal("0")
    return {
        "trades": len(closed),
        "win_rate": _d((Decimal(len(wins)) / Decimal(len(closed))) if closed else Decimal("0")),
        "payoff_ratio": _d(payoff),
        "by_side": by_side,
    }


def _metrics(result: dict[str, Any], equity0: Decimal, start: datetime, end: datetime) -> dict[str, Any]:
    net = result["ending"] - equity0
    years = Decimal(str(max((end - start).days, 1))) / Decimal("365.25")
    if equity0 > 0 and result["ending"] > 0 and years > 0:
        annual = (result["ending"] / equity0) ** (Decimal(1) / years) - Decimal(1)
    else:
        annual = Decimal("-1")
    stats = _trade_stats(result["trades"])
    return {
        "net_pnl": _d(net),
        "annualized_return": _d(annual),
        "max_drawdown": _d(_max_dd(result["curve"])),
        "sharpe": _d(_sharpe(result["curve"])),
        "fees": _d(result["fees"]),
        "funding_pnl": _d(result["funding_pnl"]),
        "funding_missing_events": result["missed_funding"],
        "funding_status": "incomplete" if result["missed_funding"] else "provided_series",
        "liquidations": result["liquidations"],
        "rejections": result["rejections"],
        "leverage_counts": {str(key): value for key, value in sorted(result["leverages"].items())},
        "mean_effective_leverage": _d(sum(result["effective"], Decimal("0")) / Decimal(len(result["effective"]))) if result["effective"] else "0",
        **stats,
    }


def _slice_year(curve, trades, year: int) -> dict[str, str]:
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    points = [(ts, equity) for ts, equity in curve if start <= ts < end]
    subset = [trade for trade in trades if trade.get("exit") and start <= trade["exit"] < end and trade["reason"] != "open_at_end"]
    net = (points[-1][1] - points[0][1]) if len(points) >= 2 else Decimal("0")
    by_side = {}
    for side in ("long", "short"):
        chosen = [trade for trade in subset if trade["side"] == side]
        by_side[side] = {"trades": len(chosen), "net_pnl": _d(sum((trade["pnl"] for trade in chosen), Decimal("0")))}
    return {"net_pnl": _d(net), "max_drawdown": _d(_max_dd(points)), "trades": str(len(subset)), "by_side": by_side}


def _passes(metrics: dict[str, Any], *, min_trades: int) -> bool:
    return (
        Decimal(metrics["net_pnl"]) > 0
        and Decimal(metrics["sharpe"]) >= MIN_SHARPE
        and Decimal(metrics["max_drawdown"]) <= MAX_DRAWDOWN
        and int(metrics["trades"]) >= min_trades
        and int(metrics["liquidations"]) == 0
    )


def _signal_series(candles: list[Candle], preset: dict) -> list[tuple[str | None, Decimal | None]]:
    book = SwingBook(preset)
    return [book.on_bar(candle.ts, candle.high, candle.low, candle.close) for candle in candles]


def _score_train(candles, funding, preset, model, fee, slip, extra, risk, equity0, train_end: datetime, signals) -> dict[str, Any]:
    result = simulate_preset(
        candles,
        funding,
        preset,
        trade_start=candles[0].ts,
        trade_end=train_end,
        equity0=equity0,
        model=model,
        fee_rate=fee,
        slippage=slip,
        extra_buffer=extra,
        risk_fraction=risk,
        signals=signals,
    )
    return _metrics(result, equity0, candles[0].ts, train_end)


def _public_preset(preset: dict) -> dict[str, Any]:
    def convert(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, tuple):
            return [convert(item) for item in value]
        return value

    return {key: convert(value) for key, value in preset.items()}


def run_swing_study(config: dict[str, Any], grids: dict | None = None, iteration: int = 1) -> dict[str, Any]:
    start = date.fromisoformat(config["sample_start"])
    end = date.fromisoformat(config["sample_end"])
    if end < HOLDOUT_START:
        raise RuntimeError("sample must include the reserved segment so it can be scored once")
    trade, missing_trade = _load("trade", "4h", start, end)
    mark, missing_mark = _load("mark", "4h", start, end)
    funding, missing_funding = _funding(start, end)
    candles = _join(trade, mark)
    selection_candles = [bar for bar in candles if bar.ts.date() < HOLDOUT_START]
    holdout_candles = candles
    if any(bar.ts.date() >= HOLDOUT_START for bar in selection_candles):
        raise RuntimeError("selection window leaked into the reserved segment")
    model = MaintenanceModel(
        rate=Decimal(config["maintenance_rate"]),
        liquidation_fee_rate=Decimal(config["liquidation_fee_rate"]),
        source=config["maintenance_source"],
        exchange_schedule_verified=False,
    )
    fee = Decimal(config["fee_bps"]) / Decimal("10000")
    slip = Decimal(config["slippage_bps"]) / Decimal("10000")
    equity0 = Decimal(config["starting_equity"])
    risk = Decimal(config["risk_per_trade_pct"])
    extra = Decimal(config["extra_buffer"])
    folds = walk_forward_folds(date(2021, 1, 1), HOLDOUT_START)
    if grids is None:
        grids = {"atr_breakout_4h": ATR_BREAKOUT_GRID, "tsmom_4h": TSMOM_GRID, "donchian_trail_4h": DONCHIAN_GRID}
    if sum(len(grid) for grid in grids.values()) > 6 and iteration > 1:
        raise RuntimeError("round 2 is limited to 6 frozen presets")
    for grid in grids.values():
        for preset in grid:
            if preset.get("allow_short") is not True:
                raise RuntimeError("candidate grid contains a long-only preset")
    family_reports = {}
    for family, grid in grids.items():
        selections = []
        stitched_trades = []
        stitched_curve = []
        equity = equity0
        total_fees = Decimal("0")
        total_funding = Decimal("0")
        missed = 0
        liquidations = 0
        rejections: Counter[str] = Counter()
        leverages: Counter[int] = Counter()
        effective: list[Decimal] = []
        signal_cache = {id(preset): _signal_series(selection_candles, preset) for preset in grid}
        for fold_start, fold_end in folds:
            train_end = datetime(fold_start.year, fold_start.month, fold_start.day, tzinfo=timezone.utc)
            test_end = datetime(fold_end.year, fold_end.month, fold_end.day, tzinfo=timezone.utc)
            scored = []
            for preset in grid:
                metrics = _score_train(
                    selection_candles, funding, preset, model, fee, slip, extra, risk, equity0, train_end, signal_cache[id(preset)]
                )
                scored.append((preset, metrics))
            eligible = [item for item in scored if _passes(item[1], min_trades=MIN_TRAIN_TRADES)]
            if not eligible:
                selections.append({"test_start": str(fold_start), "test_end": str(fold_end), "preset": None, "reason": "no_train_candidate_passed"})
                continue
            preset, train_metrics = max(eligible, key=lambda item: Decimal(item[1]["sharpe"]))
            selections.append(
                {
                    "test_start": str(fold_start),
                    "test_end": str(fold_end),
                    "index": grid.index(preset),
                    "preset": _public_preset(preset),
                    "train_sharpe": train_metrics["sharpe"],
                }
            )
            tested = simulate_preset(
                selection_candles,
                funding,
                preset,
                trade_start=train_end,
                trade_end=test_end,
                equity0=equity,
                model=model,
                fee_rate=fee,
                slippage=slip,
                extra_buffer=extra,
                risk_fraction=risk,
                signals=signal_cache[id(preset)],
            )
            stitched_trades.extend(tested["trades"])
            if tested["curve"]:
                stitched_curve.extend(tested["curve"])
            equity = tested["ending"]
            total_fees += tested["fees"]
            total_funding += tested["funding_pnl"]
            missed += tested["missed_funding"]
            liquidations += tested["liquidations"]
            rejections.update(tested["rejections"])
            leverages.update(tested["leverages"])
            effective.extend(tested["effective"])
        stitched = {
            "ending": equity,
            "fees": total_fees,
            "funding_pnl": total_funding,
            "missed_funding": missed,
            "liquidations": liquidations,
            "rejections": dict(rejections),
            "leverages": leverages,
            "effective": effective,
            "trades": stitched_trades,
            "curve": stitched_curve,
        }
        walk = _metrics(stitched, equity0, datetime(2021, 1, 1, tzinfo=timezone.utc), HOLDOUT_START_DT)
        walk["by_year"] = {
            str(year): _slice_year(stitched["curve"], stitched["trades"], year) for year in range(2021, 2027)
        }
        family_reports[family] = {"selections": selections, "walk_forward": walk, "signal_cache": signal_cache}
    # Stress the same selected presets. Do not choose again.
    stress = {}
    for family, grid in grids.items():
        equity = equity0
        curve = []
        trades = []
        fees_paid = Decimal("0")
        funding_pnl = Decimal("0")
        missed = 0
        liquidations = 0
        for item in family_reports[family]["selections"]:
            if item["preset"] is None:
                continue
            preset = grid[int(item["index"])]
            cached = family_reports[family]["signal_cache"][id(preset)]
            fold_start = date.fromisoformat(item["test_start"])
            fold_end = date.fromisoformat(item["test_end"])
            tested = simulate_preset(
                selection_candles,
                funding,
                preset,
                trade_start=datetime(fold_start.year, fold_start.month, fold_start.day, tzinfo=timezone.utc),
                trade_end=datetime(fold_end.year, fold_end.month, fold_end.day, tzinfo=timezone.utc),
                equity0=equity,
                model=model,
                fee_rate=fee * Decimal(config["stress_multiplier"]),
                slippage=slip * Decimal(config["stress_multiplier"]),
                extra_buffer=extra,
                risk_fraction=risk,
                signals=cached,
            )
            equity = tested["ending"]
            curve.extend(tested["curve"])
            trades.extend(tested["trades"])
            fees_paid += tested["fees"]
            funding_pnl += tested["funding_pnl"]
            missed += tested["missed_funding"]
            liquidations += tested["liquidations"]
        stress[family] = _metrics(
            {
                "ending": equity,
                "fees": fees_paid,
                "funding_pnl": funding_pnl,
                "missed_funding": missed,
                "liquidations": liquidations,
                "rejections": {},
                "leverages": Counter(),
                "effective": [],
                "trades": trades,
                "curve": curve,
            },
            equity0,
            datetime(2021, 1, 1, tzinfo=timezone.utc),
            HOLDOUT_START_DT,
        )
    acceptable = []
    for family, report in family_reports.items():
        base = report["walk_forward"]
        stressed = stress[family]
        if _passes(base, min_trades=MIN_STITCH_TRADES) and Decimal(stressed["net_pnl"]) > 0 and int(stressed["liquidations"]) == 0:
            acceptable.append(family)
    if acceptable:
        winner = max(acceptable, key=lambda name: Decimal(family_reports[name]["walk_forward"]["sharpe"]))
        reason = "highest_walk_forward_sharpe_among_candidates_that_also_survived_cost_stress"
    else:
        winner = None
        reason = "no_candidate_survived_walk_forward_and_cost_stress"
    holdout_report = None
    if winner is not None:
        last = next(item for item in reversed(family_reports[winner]["selections"]) if item["preset"] is not None)
        preset = grids[winner][int(last["index"])]
        holdout = simulate_preset(
            holdout_candles,
            funding,
            preset,
            trade_start=HOLDOUT_START_DT,
            trade_end=datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1),
            equity0=equity0,
            model=model,
            fee_rate=fee,
            slippage=slip,
            extra_buffer=extra,
            risk_fraction=risk,
        )
        holdout_metrics = _metrics(holdout, equity0, HOLDOUT_START_DT, datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1))
        holdout_report = {"preset": last["preset"], "metrics": holdout_metrics, "evaluated_once": True}
        if Decimal(holdout_metrics["net_pnl"]) <= 0 or Decimal(holdout_metrics["max_drawdown"]) > MAX_DRAWDOWN:
            winner = None
            reason = "walk_forward_candidate_failed_the_reserved_segment"
    buy_hold = _buy_hold(selection_candles, funding, equity0, fee, model)
    for report in family_reports.values():
        report.pop("signal_cache", None)
    return {
        "paper_only": True,
        "orders_sent": 0,
        "iteration": iteration,
        "selection_bias": (
            "Second pass on the same 2021-01-01 to 2026-08-01 stitched window. "
            "The slower rules and the choice to drop ATR breakout were made after reading round 1, "
            "so this window is no longer an untouched holdout."
            if iteration > 1
            else "First pass. Parameters were frozen before this run. 2024 onward was already inspected by the earlier daily study."
        ),
        "selection_rule": {
            "train_metric": "sharpe",
            "min_train_trades": MIN_TRAIN_TRADES,
            "min_stitched_trades": MIN_STITCH_TRADES,
            "min_sharpe": str(MIN_SHARPE),
            "max_drawdown": str(MAX_DRAWDOWN),
            "liquidations": 0,
            "allow_short": True,
            "stress": "fee_and_slippage_times_2_must_stay_profitable",
            "reserved_segment": "2026-08-01 onward, scored once after selection",
        },
        "provenance": {
            "bars_4h": len(candles),
            "funding_events": len(funding),
            "missing_trade_months": missing_trade,
            "missing_mark_months": missing_mark,
            "missing_funding_months": missing_funding,
            "sample_start": config["sample_start"],
            "sample_end": config["sample_end"],
            "holdout_start": "2026-08-01",
        },
        "families": family_reports,
        "cost_stress": stress,
        "buy_and_hold_before_holdout": buy_hold,
        "recommendation": {"strategy": winner, "reason": reason, "holdout": holdout_report},
    }


def _buy_hold(candles, funding, equity0, fee, model, start: datetime | None = None, end: datetime | None = None) -> dict[str, Any]:
    if not candles:
        return {}
    start = start or datetime(2021, 1, 1, tzinfo=timezone.utc)
    end = end or HOLDOUT_START_DT
    first = next(bar for bar in candles if bar.ts >= start)
    qty = (equity0 * (Decimal(1) - fee)) / first.open
    cash = equity0 - qty * first.open * (Decimal(1) + fee)
    missed = 0
    funding_pnl = Decimal("0")
    cursor = 0
    for stamp, rate in funding:
        if stamp < first.ts or stamp >= end:
            continue
        while cursor + 1 < len(candles) and candles[cursor + 1].ts <= stamp:
            cursor += 1
        mark = None if candles[cursor].ts > stamp else candles[cursor].mark_close
        if mark is None:
            missed += 1
            continue
        payment = qty * mark * rate
        cash -= payment
        funding_pnl -= payment
    last = candles[-1] if candles[-1].ts < end else next(bar for bar in reversed(candles) if bar.ts < end)
    equity = cash + qty * (last.mark_close or last.close)
    return {
        "net_pnl": _d(equity - equity0),
        "funding_pnl": _d(funding_pnl),
        "funding_missing_events": missed,
        "ending_equity": _d(equity),
    }


def main() -> int:
    config = json.loads(Path("leverage_study.example.json").read_text(encoding="utf-8"))
    # Swing study uses the same cost and maintenance assumptions, plus the reserved date.
    config.setdefault("sample_start", "2020-01-01")
    config.setdefault("sample_end", "2026-10-31")
    report = run_swing_study(config)
    target = Path("artifacts/research/swing-walkforward.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["recommendation"], ensure_ascii=False, indent=2))
    for name, family in report["families"].items():
        row = family["walk_forward"]
        print(name, row["net_pnl"], "sharpe", row["sharpe"], "dd", row["max_drawdown"], "trades", row["trades"], "liq", row["liquidations"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

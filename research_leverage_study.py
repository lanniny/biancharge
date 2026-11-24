"""Compare fixed 1x, risk-bounded leverage, and a predeclared 4h breakout.

The 2024-01-01 onward window was already inspected for the daily MA study.
Those rows are labeled reused_holdout. They are not a fresh out-of-sample
selection. Forward paper after this run is the next unused check.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from leverage_policy import MaintenanceModel, plan_leverage
from research_data import fetch_vision_zip, parse_vision_funding_csv, parse_vision_kline_csv, zip_csv_text
from strategy_candidates import atr_breakout_decision, baseline_side

ROOT = "https://data.binance.vision/data/futures/um"


@dataclass
class Candle:
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    mark_open: Decimal | None = None
    mark_high: Decimal | None = None
    mark_low: Decimal | None = None
    mark_close: Decimal | None = None

    def path(self, use_mark: bool) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        if use_mark and None not in (self.mark_open, self.mark_high, self.mark_low, self.mark_close):
            return self.mark_open, self.mark_high, self.mark_low, self.mark_close  # type: ignore[return-value]
        return self.open, self.high, self.low, self.close


def _months(start: date, end: date) -> list[date]:
    cursor = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    months = []
    while cursor <= last:
        months.append(cursor)
        cursor = date(cursor.year + 1, 1, 1) if cursor.month == 12 else date(cursor.year, cursor.month + 1, 1)
    return months


def _load_rows(kind: str, interval: str, start: date, end: date) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    folder = "klines" if kind == "trade" else "markPriceKlines"
    for month in _months(start, end):
        stamp = f"{month:%Y-%m}"
        url = f"{ROOT}/monthly/{folder}/BTCUSDT/{interval}/BTCUSDT-{interval}-{stamp}.zip"
        try:
            payload, _meta = fetch_vision_zip(url, Path("data/research-cache"))
            rows.extend(parse_vision_kline_csv(zip_csv_text(payload)))
        except Exception:
            missing.append(stamp)
    kept = []
    for row in rows:
        stamp = row["timestamp"]
        if isinstance(stamp, datetime) and start <= stamp.date() <= end:
            kept.append(row)
    kept.sort(key=lambda row: row["timestamp"])
    return kept, missing


def _load_funding(start: date, end: date) -> tuple[list[tuple[datetime, Decimal]], int]:
    events: list[tuple[datetime, Decimal]] = []
    missing = 0
    for month in _months(start, end):
        stamp = f"{month:%Y-%m}"
        url = f"{ROOT}/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-{stamp}.zip"
        try:
            payload, _meta = fetch_vision_zip(url, Path("data/research-cache"))
            events.extend(parse_vision_funding_csv(zip_csv_text(payload)))
        except Exception:
            missing += 1
    return [(ts, rate) for ts, rate in events if start <= ts.date() <= end], missing


def _join(trade: list[dict[str, Any]], mark: list[dict[str, Any]]) -> list[Candle]:
    marks = {row["timestamp"]: row for row in mark}
    candles = []
    for row in trade:
        stamp = row["timestamp"]
        marked = marks.get(stamp)
        candles.append(
            Candle(
                ts=stamp,  # type: ignore[arg-type]
                open=row["open"],  # type: ignore[arg-type]
                high=row["high"],  # type: ignore[arg-type]
                low=row["low"],  # type: ignore[arg-type]
                close=row["close"],  # type: ignore[arg-type]
                mark_open=None if marked is None else marked["open"],  # type: ignore[arg-type]
                mark_high=None if marked is None else marked["high"],  # type: ignore[arg-type]
                mark_low=None if marked is None else marked["low"],  # type: ignore[arg-type]
                mark_close=None if marked is None else marked["close"],  # type: ignore[arg-type]
            )
        )
    return candles


def _adverse(side: str, open_: Decimal, low: Decimal, high: Decimal, stop: Decimal, liq: Decimal) -> tuple[str, Decimal] | None:
    if side == "long":
        triggers = [("stop", stop), ("liquidation", liq)]
        gapped = [item for item in triggers if open_ <= item[1]]
        if gapped:
            name, _level = max(gapped, key=lambda item: item[1])
            return f"{name}_gap", open_
        ahead = [item for item in triggers if item[1] < open_]
        if not ahead:
            return None
        name, level = max(ahead, key=lambda item: item[1])
        if low <= level:
            return name, level
        return None
    triggers = [("stop", stop), ("liquidation", liq)]
    gapped = [item for item in triggers if open_ >= item[1]]
    if gapped:
        name, _level = min(gapped, key=lambda item: item[1])
        return f"{name}_gap", open_
    ahead = [item for item in triggers if item[1] > open_]
    if not ahead:
        return None
    name, level = min(ahead, key=lambda item: item[1])
    if high >= level:
        return name, level
    return None


def _d(value: Decimal) -> str:
    return format(value, "f")


def simulate(
    candles: list[Candle],
    funding: list[tuple[datetime, Decimal]],
    signal_at: Callable[[int], tuple[str | None, Decimal | None]],
    *,
    policy: str,
    equity0: Decimal,
    model: MaintenanceModel,
    fee_rate: Decimal,
    slippage: Decimal,
    extra_buffer: Decimal,
    risk_fraction: Decimal,
    require_mark_above: int = 1,
) -> dict[str, Any]:
    cash = equity0
    position: dict[str, Any] | None = None
    pending: tuple[str, Decimal] | None = None
    fees = Decimal("0")
    funding_pnl = Decimal("0")
    missed_funding = 0
    liquidations = 0
    rejections: Counter[str] = Counter()
    leverages: Counter[int] = Counter()
    effective: list[Decimal] = []
    curve: list[tuple[datetime, Decimal]] = []
    ledger: list[dict[str, Any]] = []
    fund_index = 0

    def apply_funding(start: datetime, end: datetime, mark: Decimal | None) -> None:
        nonlocal cash, funding_pnl, missed_funding, fund_index, position
        while fund_index < len(funding) and funding[fund_index][0] < start:
            fund_index += 1
        while fund_index < len(funding) and funding[fund_index][0] < end:
            stamp, rate = funding[fund_index]
            fund_index += 1
            if position is None:
                continue
            if mark is None or mark <= 0:
                missed_funding += 1
                ledger.append({"time": stamp, "kind": "funding_missing", "amount": None})
                continue
            payment = Decimal(position["qty"]) * mark * rate
            if position["side"] == "short":
                payment = -payment
            cash -= payment
            funding_pnl -= payment
            ledger.append({"time": stamp, "kind": "funding", "amount": -payment})

    def close_position(raw: Decimal, reason: str, when: datetime) -> None:
        nonlocal cash, fees, position, liquidations
        assert position is not None
        slipped = raw * (Decimal(1) - slippage) if position["side"] == "long" else raw * (Decimal(1) + slippage)
        qty = Decimal(position["qty"])
        fee = abs(slipped * qty) * fee_rate
        if reason.startswith("liquidation"):
            fee += abs(slipped * qty) * model.liquidation_fee_rate
            liquidations += 1
        pnl = (slipped - Decimal(position["entry"])) * qty if position["side"] == "long" else (Decimal(position["entry"]) - slipped) * qty
        cash += Decimal(position["margin"]) + pnl - fee
        fees += fee
        ledger.append({"time": when, "kind": "realized", "amount": pnl, "reason": reason, "leverage": position["leverage"]})
        ledger.append({"time": when, "kind": "fee", "amount": -fee})
        position = None

    for index, candle in enumerate(candles):
        nxt = candles[index + 1].ts if index + 1 < len(candles) else candle.ts + timedelta(hours=4)
        use_mark = candle.mark_close is not None
        open_, high, low, close_price = candle.path(use_mark)
        mark_for_funding = candle.mark_close if use_mark else None
        if position is not None:
            apply_funding(candle.ts, candle.ts + timedelta(seconds=1), mark_for_funding)
            hit = _adverse(position["side"], open_, low, high, Decimal(position["stop"]), Decimal(position["liq"]))
            if hit is not None:
                close_position(hit[1], hit[0], candle.ts)
        if pending is not None and position is None:
            side, stop_distance = pending
            raw = candle.open
            fill = raw * (Decimal(1) + slippage) if side == "long" else raw * (Decimal(1) - slippage)
            if policy == "fixed_1x":
                risk_budget = (cash) * risk_fraction
                qty = risk_budget / (fill * stop_distance)
                notional = qty * fill
                if notional > cash:
                    qty = cash / (fill * (Decimal(1) + fee_rate))
                    notional = qty * fill
                leverage = 1
                margin = notional
                distance = liquidation_distance_safe(side, 1, model)
                accepted = qty > 0 and distance > stop_distance
                reason = "fixed_1x"
            else:
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
                accepted = plan.accepted
                qty = plan.qty
                notional = plan.notional
                leverage = plan.contract_leverage
                margin = plan.initial_margin
                distance = plan.liquidation_distance
                reason = plan.reason
            if leverage > require_mark_above and not use_mark:
                accepted = False
                reason = "high_leverage_needs_mark_ohlc"
            if not accepted or qty <= 0 or margin + (notional * fee_rate) > cash:
                rejections[reason if not accepted else "insufficient_cash"] += 1
            else:
                fee = notional * fee_rate
                cash -= margin + fee
                fees += fee
                if side == "long":
                    stop = fill * (Decimal(1) - stop_distance)
                    liq = fill * (Decimal(1) - distance)
                else:
                    stop = fill * (Decimal(1) + stop_distance)
                    liq = fill * (Decimal(1) + distance)
                position = {
                    "side": side,
                    "qty": qty,
                    "entry": fill,
                    "stop": stop,
                    "liq": liq,
                    "margin": margin,
                    "leverage": leverage,
                }
                leverages[leverage] += 1
                effective.append(notional / (cash + margin))
                ledger.append({"time": candle.ts, "kind": "fee", "amount": -fee, "leverage": leverage, "reason": reason})
                hit = _adverse(side, open_, low, high, stop, liq)
                if hit is not None:
                    close_position(hit[1], hit[0], candle.ts)
        pending = None
        if position is not None:
            apply_funding(candle.ts + timedelta(seconds=1), nxt, mark_for_funding)
        side, stop_distance = signal_at(index)
        if side == "long" and position is None and stop_distance is not None:
            pending = (side, stop_distance)
        elif side == "flat" and position is not None:
            close_position(close_price, "signal_flat", candle.ts)
        mark = candle.mark_close if use_mark else candle.close
        equity = cash
        if position is not None:
            qty = Decimal(position["qty"])
            upnl = (mark - Decimal(position["entry"])) * qty if position["side"] == "long" else (Decimal(position["entry"]) - mark) * qty
            equity += Decimal(position["margin"]) + upnl
        curve.append((candle.ts, equity))
    if position is not None:
        last = candles[-1]
        mark = last.mark_close or last.close
        qty = Decimal(position["qty"])
        upnl = (mark - Decimal(position["entry"])) * qty if position["side"] == "long" else (Decimal(position["entry"]) - mark) * qty
        ledger.append({"time": last.ts, "kind": "unrealized", "amount": upnl})
    return _summarize(equity0, cash, fees, funding_pnl, missed_funding, liquidations, rejections, leverages, effective, curve, ledger)


def liquidation_distance_safe(side: str, leverage: int, model: MaintenanceModel) -> Decimal:
    from leverage_policy import liquidation_distance

    return liquidation_distance(side=side, leverage=leverage, model=model)


def _summarize(equity0, cash, fees, funding_pnl, missed, liquidations, rejections, leverages, effective, curve, ledger) -> dict[str, Any]:
    ending = curve[-1][1] if curve else equity0
    peak = equity0
    max_dd = Decimal("0")
    for _ts, equity in curve:
        if equity > peak:
            peak = equity
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
    net = ending - equity0
    return {
        "starting_equity": _d(equity0),
        "ending_equity": _d(ending),
        "net_pnl": _d(net),
        "net_return": _d(net / equity0),
        "fees": _d(fees),
        "funding_pnl": _d(funding_pnl),
        "funding_missing_events": missed,
        "funding_status": "incomplete" if missed else "provided_series",
        "net_pnl_includes_complete_funding": missed == 0,
        "liquidations": liquidations,
        "rejections": dict(rejections),
        "leverage_counts": {str(key): value for key, value in sorted(leverages.items())},
        "mean_effective_leverage_on_entry": _d(sum(effective, Decimal("0")) / Decimal(len(effective))) if effective else "0",
        "max_drawdown": _d(max_dd),
        "fills": sum(leverages.values()) + liquidations,
        "curve": curve,
        "ledger": ledger,
    }


def _slice_metrics(result: dict[str, Any], start: datetime, end: datetime, *, label: str) -> dict[str, Any]:
    equity0 = Decimal(result["starting_equity"])
    # Rebuild a segment return from the equity curve inside the window.
    points = [(ts, equity) for ts, equity in result["curve"] if start <= ts < end]
    if len(points) < 2:
        net = Decimal("0")
        dd = Decimal("0")
        base = equity0
    else:
        base = points[0][1]
        peak = base
        dd = Decimal("0")
        for _ts, equity in points:
            if equity > peak:
                peak = equity
            if peak > 0:
                dd = max(dd, (peak - equity) / peak)
        net = points[-1][1] - base
    return {
        "label": label,
        "start": start.strftime("%Y-%m-%d"),
        "end": end.strftime("%Y-%m-%d"),
        "net_pnl": _d(net),
        "return_on_segment_start": _d(net / base) if base else "0",
        "max_drawdown": _d(dd),
        "selection_bias": "reused_holdout" if start >= datetime(2024, 1, 1, tzinfo=timezone.utc) else "not_the_previously_published_holdout",
    }


def run_study(config: dict[str, Any]) -> dict[str, Any]:
    start = date.fromisoformat(config["sample_start"])
    end = date.fromisoformat(config["sample_end"])
    model = MaintenanceModel(
        rate=Decimal(config["maintenance_rate"]),
        liquidation_fee_rate=Decimal(config["liquidation_fee_rate"]),
        source=config["maintenance_source"],
        exchange_schedule_verified=False,
    )
    daily_trade, missing_daily = _load_rows("trade", "1d", start, end)
    daily_mark, missing_daily_mark = _load_rows("mark", "1d", start, end)
    four_trade, missing_4h = _load_rows("trade", "4h", start, end)
    four_mark, missing_4h_mark = _load_rows("mark", "4h", start, end)
    funding, missing_funding_months = _load_funding(start, end)
    daily = _join(daily_trade, daily_mark)
    four = _join(four_trade, four_mark)
    daily_by_day = {}
    for candle in daily:
        daily_by_day[candle.ts.date()] = candle.close
    ordered_days = sorted(daily_by_day)

    def daily_before(stamp: datetime) -> list[Decimal]:
        return [daily_by_day[day] for day in ordered_days if day < stamp.date()]

    fee = Decimal(config["fee_bps"]) / Decimal("10000")
    slip = Decimal(config["slippage_bps"]) / Decimal("10000")
    equity0 = Decimal(config["starting_equity"])
    risk = Decimal(config["risk_per_trade_pct"])
    extra = Decimal(config["extra_buffer"])
    stop = Decimal(config["baseline"]["stop_pct"])
    alt = config["alternative"]

    def baseline_signal(index: int) -> tuple[str | None, Decimal | None]:
        side = baseline_side([bar.close for bar in daily[: index + 1]])
        if side == "long":
            return "long", stop
        if side == "flat":
            return "flat", stop
        return None, None

    def atr_signal(index: int) -> tuple[str | None, Decimal | None]:
        candle = four[index]
        side, fraction = atr_breakout_decision(
            [bar.high for bar in four[: index + 1]],
            [bar.low for bar in four[: index + 1]],
            [bar.close for bar in four[: index + 1]],
            index,
            breakout_window=int(alt["breakout_window"]),
            atr_window=int(alt["atr_window"]),
            atr_multiple=Decimal(alt["atr_stop_multiple"]),
            daily_closes_before_today=daily_before(candle.ts),
        )
        return side, fraction

    def run_one(name: str, candles: list[Candle], signal, policy: str, fee_rate: Decimal, slip_rate: Decimal) -> dict[str, Any]:
        result = simulate(
            candles,
            funding,
            signal,
            policy=policy,
            equity0=equity0,
            model=model,
            fee_rate=fee_rate,
            slippage=slip_rate,
            extra_buffer=extra,
            risk_fraction=risk,
        )
        result.pop("curve")
        result.pop("ledger")
        # Keep curve on a side copy for slices. simulate already returned them; recompute slices before pop.
        return result

    def run_with_curve(name, candles, signal, policy, fee_rate, slip_rate):
        result = simulate(
            candles,
            funding,
            signal,
            policy=policy,
            equity0=equity0,
            model=model,
            fee_rate=fee_rate,
            slippage=slip_rate,
            extra_buffer=extra,
            risk_fraction=risk,
        )
        holdout = datetime(2024, 1, 1, tzinfo=timezone.utc)
        finish = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1)
        slices = {
            "before_reused_holdout": _slice_metrics(result, datetime(start.year, start.month, start.day, tzinfo=timezone.utc), holdout, label="before_reused_holdout"),
            "reused_holdout": _slice_metrics(result, holdout, finish, label="reused_holdout"),
        }
        for year in range(2021, 2027):
            slices[str(year)] = _slice_metrics(
                result,
                datetime(year, 1, 1, tzinfo=timezone.utc),
                datetime(year + 1, 1, 1, tzinfo=timezone.utc),
                label=f"walk_forward_{year}_frozen_params",
            )
        public = {key: value for key, value in result.items() if key not in {"curve", "ledger"}}
        public["segments"] = slices
        public["name"] = name
        return public

    tracks = {
        "baseline_fixed_1x_daily": run_with_curve("baseline_fixed_1x_daily", daily, baseline_signal, "fixed_1x", fee, slip),
        "baseline_dynamic_daily": run_with_curve("baseline_dynamic_daily", daily, baseline_signal, "dynamic", fee, slip),
        "atr_breakout_4h_dynamic": run_with_curve("atr_breakout_4h_dynamic", four, atr_signal, "dynamic", fee, slip),
        "baseline_fixed_1x_daily_cost_stress": run_with_curve("baseline_fixed_1x_daily_cost_stress", daily, baseline_signal, "fixed_1x", fee * 2, slip * 2),
        "atr_breakout_4h_dynamic_cost_stress": run_with_curve("atr_breakout_4h_dynamic_cost_stress", four, atr_signal, "dynamic", fee * 2, slip * 2),
    }
    return {
        "study": "risk_bounded_leverage",
        "paper_only": True,
        "orders_sent": 0,
        "parameters_locked": True,
        "optimized_on_reused_holdout": False,
        "holdout_label": config["holdout_label"],
        "maintenance_model": {
            "rate": config["maintenance_rate"],
            "liquidation_fee_rate": config["liquidation_fee_rate"],
            "source": config["maintenance_source"],
            "exchange_schedule_verified": False,
            "fapi_leverage_bracket": config["fapi_leverage_bracket"],
        },
        "provenance": {
            "daily_trade_bars": len(daily),
            "four_hour_trade_bars": len(four),
            "funding_events": len(funding),
            "missing_daily_months": missing_daily,
            "missing_daily_mark_months": missing_daily_mark,
            "missing_4h_months": missing_4h,
            "missing_4h_mark_months": missing_4h_mark,
            "missing_funding_months": missing_funding_months,
            "sample_start": config["sample_start"],
            "sample_end": config["sample_end"],
        },
        "tracks": tracks,
        "limitations": [
            "2024-01-01 之后的区间已经在日线均线研究里看过，这里标成 reused_holdout，不能再当作从未见过的样本外。",
            "维持保证金和强平费是近似值。杠杆档位接口返回 HTTP 451，没有用代理去取，也不声称与交易所逐档表一致。",
            "8% 止损在 50 倍下会落在估算强平价之外，规划器不会为了用满 50 倍而放大仓位。",
            "4 小时标记价的高低点用来判断止损和强平谁先碰到。这仍是一根 K 线内从开盘向不利极值移动的假设，不是逐笔路径。",
            "更高杠杆不能弥补信号本身弱。前向纸面才是这次比较之后还没走过的验证。",
        ],
    }


def main() -> int:
    config = json.loads(Path("leverage_study.example.json").read_text(encoding="utf-8"))
    if not config.get("parameters_locked"):
        raise SystemExit("parameters must be locked")
    report = run_study(config)
    target = Path("artifacts/research/leverage-study.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name, track in report["tracks"].items():
        hold = track["segments"]["reused_holdout"]
        print(
            f"{name} full_net={track['net_pnl']} holdout_net={hold['net_pnl']} "
            f"dd={track['max_drawdown']} liq={track['liquidations']} "
            f"lev={track['leverage_counts']} rej={track['rejections']} "
            f"funding_missing={track['funding_missing_events']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

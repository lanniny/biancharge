"""Freeze the BTC-selected slow momentum rule and run it on other USD-M perps.

The preset is the asymmetric candidate already chosen on BTC. Nothing in this
file is allowed to pick a new parameter. Scoring uses bars before 2026-08-01.
That reserved segment is simulated only after the four-coin rule passes.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from leverage_policy import MaintenanceModel
from research_swing_study import (
    HOLDOUT_START,
    HOLDOUT_START_DT,
    _buy_hold,
    _d,
    _funding,
    _join,
    _load,
    _metrics,
    _public_preset,
    _max_dd,
    _sharpe,
    _signal_series,
    _slice_year,
    simulate_preset,
)
from strategy_candidates import ROUND2_GRIDS

# The walk-forward on BTC selected this exact preset on every fold.
FROZEN_PRESET = ROUND2_GRIDS["tsmom_slow_4h"][1]
ALT_SYMBOLS = ("ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")
SCORE_START = date(2021, 1, 1)
WARMUP_START = date(2020, 1, 1)
MIN_POSITIVE_COINS = 3
MIN_PORTFOLIO_SHARPE = Decimal("0.5")


def validation_passes(rows: list[dict[str, Any]], portfolio_sharpe: Decimal) -> dict[str, Any]:
    """Predeclared rule. Three of four coins must be profitable at both cost levels."""
    positive = []
    liquidations = 0
    for row in rows:
        liquidations += int(row["liquidations"]) + int(row["stress_liquidations"])
        if (
            Decimal(row["net_pnl"]) > 0
            and Decimal(row["stress_net_pnl"]) > 0
            and int(row["liquidations"]) == 0
            and int(row["stress_liquidations"]) == 0
        ):
            positive.append(row["symbol"])
    passed = (
        len(rows) == len(ALT_SYMBOLS)
        and len(positive) >= MIN_POSITIVE_COINS
        and liquidations == 0
        and portfolio_sharpe >= MIN_PORTFOLIO_SHARPE
    )
    return {
        "passed": passed,
        "positive_symbols": positive,
        "positive_count": len(positive),
        "required_positive": MIN_POSITIVE_COINS,
        "liquidations": liquidations,
        "portfolio_sharpe": _d(portfolio_sharpe),
        "min_portfolio_sharpe": str(MIN_PORTFOLIO_SHARPE),
    }


def equal_weight_curve(
    curves: list[list[tuple[datetime, Decimal]]],
    equity0: Decimal,
) -> list[tuple[datetime, Decimal]]:
    """Sum of the books. A coin that has not started yet stays at its starting cash."""
    stamps = sorted({ts for curve in curves for ts, _equity in curve})
    indexes = [0 for _ in curves]
    last = [equity0 for _ in curves]
    out = []
    for ts in stamps:
        for index, curve in enumerate(curves):
            while indexes[index] < len(curve) and curve[indexes[index]][0] <= ts:
                last[index] = curve[indexes[index]][1]
                indexes[index] += 1
        out.append((ts, sum(last, Decimal("0"))))
    return out


def _gap_months(candles) -> list[str]:
    if not candles:
        return []
    present = {bar.ts.strftime("%Y-%m") for bar in candles}
    cursor = date(candles[0].ts.year, candles[0].ts.month, 1)
    last = date(candles[-1].ts.year, candles[-1].ts.month, 1)
    missing = []
    while cursor <= last:
        stamp = f"{cursor:%Y-%m}"
        if stamp not in present:
            missing.append(stamp)
        cursor = date(cursor.year + 1, 1, 1) if cursor.month == 12 else date(cursor.year, cursor.month + 1, 1)
    return missing


def _score_symbol(
    candles,
    funding,
    preset,
    *,
    trade_start: datetime,
    trade_end: datetime,
    equity0: Decimal,
    model: MaintenanceModel,
    fee: Decimal,
    slip: Decimal,
    extra: Decimal,
    risk: Decimal,
    stress_multiplier: Decimal,
) -> dict[str, Any]:
    window = [bar for bar in candles if bar.ts < trade_end]
    signals = _signal_series(window, preset)
    base = simulate_preset(
        window,
        funding,
        preset,
        trade_start=trade_start,
        trade_end=trade_end,
        equity0=equity0,
        model=model,
        fee_rate=fee,
        slippage=slip,
        extra_buffer=extra,
        risk_fraction=risk,
        signals=signals,
    )
    stressed = simulate_preset(
        window,
        funding,
        preset,
        trade_start=trade_start,
        trade_end=trade_end,
        equity0=equity0,
        model=model,
        fee_rate=fee * stress_multiplier,
        slippage=slip * stress_multiplier,
        extra_buffer=extra,
        risk_fraction=risk,
        signals=signals,
    )
    metrics = _metrics(base, equity0, trade_start, trade_end)
    metrics["by_year"] = {str(year): _slice_year(base["curve"], base["trades"], year) for year in range(2021, 2027)}
    stress_metrics = _metrics(stressed, equity0, trade_start, trade_end)
    buy = _buy_hold(window, funding, equity0, fee, model, start=trade_start, end=trade_end)
    return {"base": base, "metrics": metrics, "stress": stress_metrics, "buy_and_hold": buy}


def run_alt_validation(
    config: dict[str, Any],
    *,
    loader: Callable[[str, date, date], tuple[list, list, list[str]]] | None = None,
) -> dict[str, Any]:
    if FROZEN_PRESET is not ROUND2_GRIDS["tsmom_slow_4h"][1]:
        raise RuntimeError("alt validation must use the BTC-selected preset object")
    if FROZEN_PRESET.get("filter") != "daily_long_weekly_short" or FROZEN_PRESET.get("allow_short") is not True:
        raise RuntimeError("frozen preset is not the asymmetric long/short rule")
    sample_end = date.fromisoformat(config["sample_end"])
    if sample_end < HOLDOUT_START:
        raise RuntimeError("sample must extend through the reserved segment")
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
    stress_multiplier = Decimal(config["stress_multiplier"])
    score_start = datetime(SCORE_START.year, SCORE_START.month, SCORE_START.day, tzinfo=timezone.utc)
    load_end = sample_end

    def default_loader(symbol: str, start: date, end: date):
        trade, missing_trade = _load("trade", "4h", start, end, symbol)
        mark, missing_mark = _load("mark", "4h", start, end, symbol)
        funding, missing_funding = _funding(start, end, symbol)
        candles = _join(trade, mark)
        return candles, funding, missing_trade, missing_mark, missing_funding

    read = loader or default_loader
    rows = []
    curves = []
    books = {}
    for symbol in ALT_SYMBOLS:
        if loader is None:
            print(f"load {symbol}", flush=True)
        loaded = read(symbol, WARMUP_START, load_end)
        candles, funding = loaded[0], loaded[1]
        extra_missing = loaded[2:]
        if not candles:
            raise RuntimeError(f"{symbol} has no public 4h bars to validate")
        first = candles[0].ts
        trade_start = max(score_start, first)
        scored = _score_symbol(
            candles,
            funding,
            FROZEN_PRESET,
            trade_start=trade_start,
            trade_end=HOLDOUT_START_DT,
            equity0=equity0,
            model=model,
            fee=fee,
            slip=slip,
            extra=extra,
            risk=risk,
            stress_multiplier=stress_multiplier,
        )
        selection = [bar for bar in candles if bar.ts < HOLDOUT_START_DT]
        metrics = scored["metrics"]
        row = {
            "symbol": symbol,
            "first_bar": first.isoformat(),
            "score_start": trade_start.isoformat(),
            "score_end": HOLDOUT_START_DT.isoformat(),
            "bars_before_holdout": len(selection),
            "interior_gap_months": _gap_months(selection),
            "net_pnl": metrics["net_pnl"],
            "stress_net_pnl": scored["stress"]["net_pnl"],
            "liquidations": metrics["liquidations"],
            "stress_liquidations": scored["stress"]["liquidations"],
        }
        if extra_missing:
            row["missing_trade_months"] = extra_missing[0]
            row["missing_mark_months"] = extra_missing[1] if len(extra_missing) > 1 else []
            row["missing_funding_months"] = extra_missing[2] if len(extra_missing) > 2 else 0
        rows.append(row)
        curves.append(scored["base"]["curve"])
        books[symbol] = {
            "metrics": metrics,
            "cost_stress": scored["stress"],
            "buy_and_hold": scored["buy_and_hold"],
            "first_bar": first.isoformat(),
            "allow_short": True,
            "interior_gap_months": row["interior_gap_months"],
            "missing_funding_months": row.get("missing_funding_months", 0),
        }
    portfolio_curve = equal_weight_curve(curves, equity0)
    portfolio_sharpe = _sharpe(portfolio_curve)
    portfolio_net = (portfolio_curve[-1][1] - equity0 * len(ALT_SYMBOLS)) if portfolio_curve else Decimal("0")
    decision = validation_passes(rows, portfolio_sharpe)
    long_pnl = Decimal("0")
    short_pnl = Decimal("0")
    for book in books.values():
        long_pnl += Decimal(book["metrics"]["by_side"]["long"]["net_pnl"])
        short_pnl += Decimal(book["metrics"]["by_side"]["short"]["net_pnl"])
    holdout = None
    if decision["passed"]:
        holdout = _score_holdout(
            config,
            read,
            model=model,
            fee=fee,
            slip=slip,
            extra=extra,
            risk=risk,
            equity0=equity0,
            load_end=load_end,
        )
    return {
        "paper_only": True,
        "orders_sent": 0,
        "parameters_reselected": False,
        "frozen_preset": _public_preset(FROZEN_PRESET),
        "symbols": list(ALT_SYMBOLS),
        "limitations": [
            "The rule was chosen after seeing BTC results, then applied here without a new search.",
            "These perps move with BTC, so the sample is not an independent economic regime.",
            "Bars before 2021-01-01 are indicator warmup only and are not scored.",
            "2026-08-01 onward is scored only after this four-coin rule passes.",
            "Maintenance margin is the same approximation used on BTC, not an exchange bracket.",
            "A missing funding rate or mark is left missing and is not booked as zero.",
        ],
        "decision": decision,
        "portfolio": {
            "weighting": "equal starting cash, unmarked coins stay in cash",
            "starting_equity": _d(equity0 * len(ALT_SYMBOLS)),
            "net_pnl": _d(portfolio_net),
            "sharpe": _d(portfolio_sharpe),
            "max_drawdown": _d(_max_dd(portfolio_curve)),
            "by_side": {
                "long": {"net_pnl": _d(long_pnl)},
                "short": {"net_pnl": _d(short_pnl)},
            },
        },
        "coins": books,
        "holdout_evaluated": holdout is not None,
        "holdout": holdout,
    }


def _score_holdout(config, read, *, model, fee, slip, extra, risk, equity0, load_end: date) -> dict[str, Any]:
    """One look at 2026-08-01 onward. Called only after the predeclared rule passes."""
    end = datetime(load_end.year, load_end.month, load_end.day, tzinfo=timezone.utc) + timedelta(days=1)
    symbols = ("BTCUSDT",) + ALT_SYMBOLS
    out = {}
    for symbol in symbols:
        loaded = read(symbol, WARMUP_START, load_end)
        candles, funding = loaded[0], loaded[1]
        result = simulate_preset(
            candles,
            funding,
            FROZEN_PRESET,
            trade_start=HOLDOUT_START_DT,
            trade_end=end,
            equity0=equity0,
            model=model,
            fee_rate=fee,
            slippage=slip,
            extra_buffer=extra,
            risk_fraction=risk,
        )
        out[symbol] = _metrics(result, equity0, HOLDOUT_START_DT, end)
        out[symbol]["evaluated_once"] = True
    return out


def main() -> int:
    config = json.loads(Path("leverage_study.example.json").read_text(encoding="utf-8"))
    report = run_alt_validation(config)
    target = Path("artifacts/research/alt-momentum-validation.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"decision": report["decision"], "portfolio": report["portfolio"]}, ensure_ascii=False, indent=2))
    for symbol, book in report["coins"].items():
        row = book["metrics"]
        print(
            symbol,
            "net", row["net_pnl"],
            "sharpe", row["sharpe"],
            "dd", row["max_drawdown"],
            "trades", row["trades"],
            "stress", book["cost_stress"]["net_pnl"],
            "long", row["by_side"]["long"]["net_pnl"],
            "short", row["by_side"]["short"]["net_pnl"],
            "buy", book["buy_and_hold"].get("net_pnl"),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

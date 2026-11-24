"""Research backtest accounting, timing, and safety tests.

Bars built here are synthetic. Assertions check the ledger, not profitability.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from research_backtest import (
    AssetSpec,
    ResearchBar,
    ResearchConfig,
    ResearchSafetyError,
    RiskSpec,
    SessionSpec,
    StrategySpec,
    apply_slippage,
    config_from_mapping,
    desired_side,
    load_config,
    main,
    parse_bars_csv,
    run_research,
)


def _ts(index: int, *, start: datetime | None = None, step: timedelta | None = None) -> datetime:
    base = start or datetime(2024, 1, 1, tzinfo=timezone.utc)
    return base + (step or timedelta(hours=1)) * index


def _bar(
    index: int,
    close: str,
    *,
    open_: str | None = None,
    high: str | None = None,
    low: str | None = None,
    mark: str | None = None,
    index_price: str | None = None,
    funding: str | None = None,
    start: datetime | None = None,
    step: timedelta | None = None,
) -> ResearchBar:
    close_dec = Decimal(close)
    open_dec = Decimal(open_) if open_ is not None else close_dec
    high_dec = Decimal(high) if high is not None else max(open_dec, close_dec)
    low_dec = Decimal(low) if low is not None else min(open_dec, close_dec)
    return ResearchBar(
        timestamp=_ts(index, start=start, step=step),
        open=open_dec,
        high=high_dec,
        low=low_dec,
        close=close_dec,
        volume=Decimal("1"),
        mark_close=Decimal(mark) if mark is not None else None,
        index_close=Decimal(index_price) if index_price is not None else None,
        funding_rate=Decimal(funding) if funding is not None else None,
    )


def _strategy(**overrides: object) -> StrategySpec:
    raw: dict[str, object] = {
        "name": "baseline_sma_momentum",
        "fast_window": 2,
        "slow_window": 3,
        "momentum_window": 1,
        "momentum_threshold": Decimal("0"),
        "stop_pct": Decimal("0.05"),
    }
    raw.update(overrides)
    return StrategySpec(**raw)  # type: ignore[arg-type]


def _risk(**overrides: object) -> RiskSpec:
    raw: dict[str, object] = {
        "risk_per_trade_pct": Decimal("0.01"),
        "max_position_pct": Decimal("0.50"),
        "max_drawdown_pct": Decimal("0.50"),
        "max_concurrent_positions": 3,
    }
    raw.update(overrides)
    return RiskSpec(**raw)  # type: ignore[arg-type]


def _session() -> SessionSpec:
    return SessionSpec(timezone="America/New_York", open_hhmm="09:30", close_hhmm="16:00")


def _asset(symbol: str, product: str, **overrides: object) -> AssetSpec:
    raw: dict[str, object] = {
        "symbol": symbol,
        "product": product,
        "quote_asset": "USDT" if product == "binance_usdt_perp" else "USD",
        "csv_path": None,
        "fee_bps": Decimal("0"),
        "slippage_bps": Decimal("0"),
        "leverage": 1 if product != "binance_usdt_perp" else 2,
        "allow_short": False,
        "maintenance_margin_rate": Decimal("0.004"),
    }
    raw.update(overrides)
    return AssetSpec(**raw)  # type: ignore[arg-type]


def _config(assets: list[AssetSpec], **overrides: object) -> ResearchConfig:
    raw: dict[str, object] = {
        "mode": "backtest",
        "data_label": "synthetic_unit",
        "starting_equity": Decimal("10000"),
        "sample_split": None,
        "strategy": _strategy(),
        "risk": _risk(),
        "session": _session(),
        "assets": tuple(assets),
        "report_path": None,
        "fills_path": None,
        "notes": (),
        "config_dir": Path("."),
    }
    raw.update(overrides)
    return ResearchConfig(**raw)  # type: ignore[arg-type]


def _uptrend() -> list[ResearchBar]:
    closes = ["100", "101", "102", "104", "106", "108"]
    bars = []
    previous = Decimal("100")
    for index, close in enumerate(closes):
        close_dec = Decimal(close)
        bars.append(
            _bar(
                index,
                close,
                open_=str(previous),
                high=str(max(previous, close_dec) + Decimal("0.5")),
                low=str(min(previous, close_dec) - Decimal("0.2")),
            )
        )
        previous = close_dec
    return bars


class ResearchSafetyTests(unittest.TestCase):
    def test_live_mode_and_credentials_are_refused(self) -> None:
        with self.assertRaises(ResearchSafetyError) as raised:
            config_from_mapping({"mode": "live", "data_label": "synthetic", "assets": []})
        self.assertIn("paper/backtest", str(raised.exception))
        secret = "SUPERSECRET-VALUE"
        with self.assertRaises(ResearchSafetyError) as raised:
            config_from_mapping(
                {
                    "mode": "backtest",
                    "data_label": "synthetic",
                    "api_key": secret,
                    "assets": [],
                }
            )
        self.assertNotIn(secret, str(raised.exception))

    def test_cli_live_override_is_rejected_without_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "live.json"
            path.write_text(
                json.dumps(
                    {
                        "mode": "live",
                        "data_label": "synthetic_fixture",
                        "assets": [
                            {
                                "symbol": "BTCUSDT",
                                "product": "binance_usdt_perp",
                                "csv": "missing.csv",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = main(["--config", str(path)])
        self.assertEqual(code, 2)

    def test_options_and_cme_are_unsupported(self) -> None:
        for product in ("option", "cme_future", "listed_futures", "SPY_CALL"):
            with self.assertRaises(ResearchSafetyError):
                config_from_mapping(
                    {
                        "mode": "paper",
                        "data_label": "synthetic",
                        "assets": [{"symbol": "SPY", "product": product, "csv": "x.csv"}],
                    }
                )

    def test_tradfi_usdt_ticker_is_not_a_cash_share(self) -> None:
        with self.assertRaises(ResearchSafetyError) as raised:
            config_from_mapping(
                {
                    "mode": "backtest",
                    "data_label": "synthetic",
                    "assets": [{"symbol": "AAPLUSDT", "product": "binance_usdt_perp", "csv": "x.csv"}],
                }
            )
        self.assertIn("not a Nasdaq share", str(raised.exception))
        with self.assertRaises(ResearchSafetyError):
            config_from_mapping(
                {
                    "mode": "backtest",
                    "data_label": "synthetic",
                    "assets": [{"symbol": "AAPLUSDT", "product": "us_cash_equity", "csv": "x.csv"}],
                }
            )

    def test_cash_leverage_and_shorts_are_refused(self) -> None:
        base = {
            "mode": "paper",
            "data_label": "synthetic",
            "assets": [
                {
                    "symbol": "AAPL",
                    "product": "us_cash_equity",
                    "csv": "x.csv",
                    "leverage": 2,
                }
            ],
        }
        with self.assertRaises(ResearchSafetyError):
            config_from_mapping(base)
        base["assets"][0]["leverage"] = 1
        base["assets"][0]["allow_short"] = True
        with self.assertRaises(ResearchSafetyError):
            config_from_mapping(base)


class ResearchAccountingTests(unittest.TestCase):
    def test_fees_reduce_pnl_and_match_the_ledger(self) -> None:
        bars = _uptrend()
        # Force a later flat exit by appending a decline that crosses the slow SMA.
        previous = Decimal(bars[-1].close)
        for offset, close in enumerate(["107", "103", "99", "97"], start=len(bars)):
            close_dec = Decimal(close)
            bars.append(
                _bar(
                    offset,
                    close,
                    open_=str(previous),
                    high=str(max(previous, close_dec) + Decimal("0.2")),
                    low=str(min(previous, close_dec) - Decimal("0.2")),
                )
            )
            previous = close_dec
        free = run_research(_config([_asset("BTCUSDT", "binance_usdt_perp")]), {"BTCUSDT": bars})
        paid = run_research(
            _config([_asset("BTCUSDT", "binance_usdt_perp", fee_bps=Decimal("10"))]),
            {"BTCUSDT": bars},
        )
        free_book = free["books"][0]
        paid_book = paid["books"][0]
        self.assertGreater(Decimal(paid_book["fees"]), 0)
        self.assertEqual(free_book["fees"], "0")
        self.assertLess(Decimal(paid_book["net_pnl"]), Decimal(free_book["net_pnl"]))
        self.assertEqual(
            Decimal(paid_book["net_pnl"]),
            Decimal(free_book["net_pnl"]) - Decimal(paid_book["fees"]),
        )
        self.assertEqual(Decimal(paid_book["full_sample"]["net_pnl"]), Decimal(paid_book["net_pnl"]))

    def test_funding_series_is_charged_and_missing_funding_stays_null(self) -> None:
        bars = _uptrend()
        funded = []
        for bar in bars:
            funded.append(
                ResearchBar(
                    timestamp=bar.timestamp,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    funding_rate=Decimal("0.001"),
                )
            )
        with_funding = run_research(
            _config([_asset("BTCUSDT", "binance_usdt_perp", leverage=1)]),
            {"BTCUSDT": funded},
        )
        without = run_research(
            _config([_asset("BTCUSDT", "binance_usdt_perp", leverage=1)]),
            {"BTCUSDT": bars},
        )
        funded_book = with_funding["books"][0]
        missing_book = without["books"][0]
        self.assertEqual(funded_book["funding_status"], "provided_series")
        self.assertIsNotNone(funded_book["funding"])
        self.assertLess(Decimal(funded_book["funding"]), 0)
        self.assertTrue(funded_book["net_pnl_includes_funding"])
        self.assertEqual(missing_book["funding_status"], "unavailable")
        self.assertIsNone(missing_book["funding"])
        self.assertFalse(missing_book["net_pnl_includes_funding"])
        self.assertLess(Decimal(funded_book["net_pnl"]), Decimal(missing_book["net_pnl"]))
        asset = with_funding["assets"][0]
        self.assertEqual(asset["index_status"], "unavailable")

    def test_short_receives_positive_funding(self) -> None:
        bars = []
        previous = Decimal("100")
        closes = ["100", "99", "97", "94", "90"]
        for index, close in enumerate(closes):
            close_dec = Decimal(close)
            bars.append(
                _bar(
                    index,
                    close,
                    open_=str(previous),
                    high=str(max(previous, close_dec) + Decimal("0.2")),
                    low=str(min(previous, close_dec) - Decimal("0.2")),
                    funding="0.001",
                )
            )
            previous = close_dec
        report = run_research(
            _config([_asset("BTCUSDT", "binance_usdt_perp", allow_short=True, leverage=1)]),
            {"BTCUSDT": bars},
        )
        self.assertGreater(Decimal(report["books"][0]["funding"]), 0)
        self.assertTrue(any(fill["action"] == "open_short" for fill in report["fills"]))

    def test_mark_used_for_unrealized_and_index_ignored(self) -> None:
        bars = _uptrend()
        last = bars[-1]
        bars[-1] = ResearchBar(
            timestamp=last.timestamp,
            open=last.open,
            high=last.high,
            low=last.low,
            close=last.close,
            volume=last.volume,
            mark_close=last.close + Decimal("10"),
            index_close=Decimal("1"),
        )
        report = run_research(
            _config([_asset("BTCUSDT", "binance_usdt_perp", fee_bps=Decimal("0"), slippage_bps=Decimal("0"), leverage=1)]),
            {"BTCUSDT": bars},
        )
        position = report["books"][0]["open_positions"][0]
        qty = Decimal(position["qty"])
        entry = Decimal(position["entry_price"])
        mark = Decimal(position["mark_price"])
        self.assertEqual(mark, last.close + Decimal("10"))
        self.assertNotEqual(mark, Decimal("1"))
        expected = qty * (mark - entry)
        self.assertEqual(Decimal(report["books"][0]["net_pnl"]), expected)
        self.assertTrue(all(fill["index_used_for_pnl"] is False for fill in report["fills"]))
        self.assertEqual(report["assets"][0]["index_status"], "provided_not_used_for_pnl")

    def test_cash_book_is_fully_funded_and_perp_book_uses_margin(self) -> None:
        bars = _uptrend()
        session_start = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
        cash_bars = []
        for index, bar in enumerate(bars):
            cash_bars.append(
                ResearchBar(
                    timestamp=session_start + timedelta(days=index),
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                )
            )
        report = run_research(
            _config(
                [
                    _asset("BTCUSDT", "binance_usdt_perp", leverage=5),
                    _asset("AAPL", "us_cash_equity", leverage=1),
                    _asset("SPY", "us_cash_etf", leverage=1),
                ]
            ),
            {"BTCUSDT": bars, "AAPL": cash_bars, "SPY": cash_bars},
        )
        books = {item["book"]: item for item in report["books"]}
        self.assertEqual(books["perp"]["accounting"], "isolated_usdt_m_perpetual")
        self.assertEqual(books["cash"]["accounting"], "cash_long_only")
        self.assertEqual(books["cash"]["funding_status"], "not_applicable")
        self.assertIsNone(books["cash"]["funding"])
        perp_position = books["perp"]["open_positions"][0]
        cash_position = next(item for item in books["cash"]["open_positions"] if item["symbol"] == "AAPL")
        perp_notional = Decimal(perp_position["qty"]) * Decimal(perp_position["entry_price"])
        self.assertEqual(Decimal(perp_position["margin"]), perp_notional / Decimal("5"))
        self.assertEqual(cash_position["leverage"], 1)
        self.assertEqual(Decimal(cash_position["margin"]), 0)
        # Separate starting balances: a perp mark does not move the cash book.
        perp_only = run_research(
            _config([_asset("BTCUSDT", "binance_usdt_perp", leverage=5)]),
            {"BTCUSDT": bars},
        )
        self.assertEqual(books["perp"]["net_pnl"], perp_only["books"][0]["net_pnl"])
        self.assertNotEqual(books["perp"]["currency"], books["cash"]["currency"])


class ResearchTimingAndRiskTests(unittest.TestCase):
    def test_signal_has_no_lookahead_and_fills_next_open(self) -> None:
        bars = _uptrend()
        strategy = _strategy()
        prefix = [bar.close for bar in bars[:4]]
        mutated = list(prefix)
        self.assertEqual(desired_side(prefix, strategy, allow_short=False), "long")
        changed_future = prefix + [Decimal("1")]
        self.assertEqual(
            desired_side(changed_future[:4], strategy, allow_short=False),
            desired_side(prefix, strategy, allow_short=False),
        )
        self.assertEqual(mutated, prefix)

        altered = list(bars)
        last = altered[-1]
        altered[-1] = ResearchBar(
            timestamp=last.timestamp,
            open=Decimal("50"),
            high=Decimal("50"),
            low=Decimal("50"),
            close=Decimal("50"),
            volume=Decimal("1"),
        )
        original = run_research(_config([_asset("BTCUSDT", "binance_usdt_perp")]), {"BTCUSDT": bars})
        other = run_research(_config([_asset("BTCUSDT", "binance_usdt_perp")]), {"BTCUSDT": altered})
        first = original["fills"][0]
        self.assertEqual(first["reason"], "signal_next_bar_open")
        self.assertEqual(Decimal(first["raw_price"]), bars[3].open)
        self.assertNotEqual(Decimal(first["raw_price"]), bars[3].close)
        self.assertLess(first["signal_timestamp"], first["timestamp"])
        self.assertEqual(first["raw_price"], other["fills"][0]["raw_price"])
        self.assertEqual(first["timestamp"], other["fills"][0]["timestamp"])

    def test_terminal_signal_is_not_filled_without_a_next_bar(self) -> None:
        closes = ["5", "5", "5", "5", "5", "6"]
        bars = [_bar(index, close, open_="5", high="6", low="5") for index, close in enumerate(closes)]
        report = run_research(
            _config(
                [_asset("BTCUSDT", "binance_usdt_perp")],
                strategy=_strategy(fast_window=2, slow_window=5, momentum_window=2),
                mode="paper",
            ),
            {"BTCUSDT": bars},
        )
        self.assertEqual(report["orders_sent"], 0)
        self.assertEqual(report["execution"], "simulated_paper_ledger")
        self.assertEqual(report["fills"], [])
        self.assertEqual(report["pending_signals"][0]["status"], "unfilled_no_next_bar")
        self.assertEqual(report["pending_signals"][0]["desired"], "long")

    def test_stop_gap_fills_at_the_open(self) -> None:
        bars = _uptrend()[:5]
        # Entry is on bar 3. Bar 4 opens through the long stop.
        entry_open = bars[3].open
        stop = entry_open * Decimal("0.95")
        bars.append(
            _bar(
                5,
                "89",
                open_="90",
                high="92",
                low="88",
            )
        )
        report = run_research(
            _config([_asset("BTCUSDT", "binance_usdt_perp", slippage_bps=Decimal("10"))]),
            {"BTCUSDT": bars},
        )
        stop_fill = next(fill for fill in report["fills"] if fill["reason"] == "stop_gap")
        self.assertEqual(Decimal(stop_fill["raw_price"]), Decimal("90"))
        self.assertLess(Decimal(stop_fill["raw_price"]), stop)
        self.assertLess(Decimal(stop_fill["fill_price"]), Decimal(stop_fill["raw_price"]))
        slipped = apply_slippage(Decimal("90"), "sell", Decimal("10"))
        self.assertEqual(Decimal(stop_fill["fill_price"]), slipped)

    def test_liquidation_hits_before_a_wide_stop(self) -> None:
        bars = _uptrend()[:4]
        entry_open = bars[3].open
        bars.append(
            _bar(4, "80", open_=str(entry_open), high=str(entry_open), low="70")
        )
        report = run_research(
            _config(
                [
                    _asset(
                        "BTCUSDT",
                        "binance_usdt_perp",
                        leverage=10,
                        maintenance_margin_rate=Decimal("0.05"),
                    )
                ],
                strategy=_strategy(stop_pct=Decimal("0.50")),
            ),
            {"BTCUSDT": bars},
        )
        reasons = [fill["reason"] for fill in report["fills"]]
        self.assertIn("liquidation", reasons)
        self.assertNotIn("stop", reasons)
        self.assertNotIn("stop_gap", reasons)

    def test_risk_and_drawdown_reject_new_entries(self) -> None:
        bars = _uptrend()
        zero_risk = run_research(
            _config(
                [_asset("BTCUSDT", "binance_usdt_perp")],
                risk=_risk(risk_per_trade_pct=Decimal("0")),
            ),
            {"BTCUSDT": bars},
        )
        self.assertEqual(zero_risk["fills"], [])
        self.assertTrue(any(item["reason"] == "risk_per_trade_pct" for item in zero_risk["rejections"]))

        # Large risk and a wide stop, then a gap loss, then a fresh uptrend that
        # would re-enter. The drawdown halt must keep that re-entry unfilled.
        crash = _uptrend()[:5]
        crash.append(_bar(5, "70", open_="70", high="71", low="69"))
        for index, close in enumerate(["71", "80", "90", "100", "110"], start=6):
            crash.append(_bar(index, close, open_=close, high=str(Decimal(close) + 1), low=str(Decimal(close) - 1)))
        halted = run_research(
            _config(
                [_asset("BTCUSDT", "binance_usdt_perp", leverage=1)],
                strategy=_strategy(stop_pct=Decimal("0.40")),
                risk=_risk(risk_per_trade_pct=Decimal("0.20"), max_position_pct=Decimal("1"), max_drawdown_pct=Decimal("0.01")),
            ),
            {"BTCUSDT": crash},
        )
        self.assertTrue(halted["books"][0]["drawdown_halted"])
        self.assertTrue(any(item["reason"] == "max_drawdown_halt" for item in halted["rejections"]))
        opens = [fill for fill in halted["fills"] if fill["action"].startswith("open_")]
        self.assertEqual(len(opens), 1)

    def test_cash_session_blocks_entries_that_perps_allow(self) -> None:
        bars = []
        previous = Decimal("100")
        start = datetime(2024, 1, 2, 21, 30, tzinfo=timezone.utc)  # 16:30 EST, after the cash close
        for index, close in enumerate(["100", "101", "103", "106", "108"]):
            close_dec = Decimal(close)
            bars.append(
                _bar(
                    index,
                    close,
                    open_=str(previous),
                    high=str(max(previous, close_dec)),
                    low=str(min(previous, close_dec)),
                    start=start,
                )
            )
            previous = close_dec
        cash = run_research(_config([_asset("QQQ", "us_cash_etf")]), {"QQQ": bars})
        perp = run_research(_config([_asset("BTCUSDT", "binance_usdt_perp")]), {"BTCUSDT": bars})
        self.assertEqual(cash["fills"], [])
        self.assertTrue(any(item["reason"] == "outside_session" for item in cash["rejections"]))
        self.assertTrue(any(fill["action"] == "open_long" for fill in perp["fills"]))
        self.assertEqual(cash["assets"][0]["session"], "America/New_York")
        self.assertEqual(perp["assets"][0]["session"], "24x7_utc")

    def test_summer_session_uses_new_york_time(self) -> None:
        # 13:30 UTC is 09:30 EDT in July, inside the session. 13:00 UTC is 09:00 EDT, outside it.
        inside = []
        outside = []
        previous = Decimal("100")
        inside_start = datetime(2024, 7, 1, 13, 30, tzinfo=timezone.utc)
        outside_start = datetime(2024, 7, 1, 13, 0, tzinfo=timezone.utc)
        for index, close in enumerate(["100", "102", "104", "107", "110"]):
            close_dec = Decimal(close)
            kwargs = {
                "open_": str(previous),
                "high": str(max(previous, close_dec)),
                "low": str(min(previous, close_dec)),
            }
            inside.append(_bar(index, close, start=inside_start, step=timedelta(days=1), **kwargs))
            outside.append(_bar(index, close, start=outside_start, step=timedelta(days=1), **kwargs))
            previous = close_dec
        opened = run_research(_config([_asset("SPY", "us_cash_etf")]), {"SPY": inside})
        blocked = run_research(_config([_asset("SPY", "us_cash_etf")]), {"SPY": outside})
        self.assertTrue(any(fill["action"] == "open_long" for fill in opened["fills"]))
        self.assertEqual(blocked["fills"], [])
        self.assertTrue(any(item["reason"] == "outside_session" for item in blocked["rejections"]))

    def test_sample_split_is_reported_and_sums_to_full_net(self) -> None:
        bars = _uptrend()
        split = bars[4].timestamp
        report = run_research(
            _config([_asset("BTCUSDT", "binance_usdt_perp")], sample_split=split),
            {"BTCUSDT": bars},
        )
        book = report["books"][0]
        combined = Decimal(book["in_sample"]["net_pnl"]) + Decimal(book["out_of_sample"]["net_pnl"])
        self.assertEqual(combined, Decimal(book["net_pnl"]))
        self.assertEqual(report["sample_split_timestamp"], "2024-01-01T04:00:00Z")


class ResearchFixtureTests(unittest.TestCase):
    def test_labeled_fixture_csv_and_example_config(self) -> None:
        root = Path(__file__).resolve().parents[1]
        config = load_config(root / "research_backtest.example.json")
        self.assertIn("synthetic", config.data_label)
        bars = parse_bars_csv(root / "fixtures" / "research" / "btcusdt_perp_synthetic.csv", "BTCUSDT")
        text = (root / "fixtures" / "research" / "btcusdt_perp_synthetic.csv").read_text(encoding="utf-8")
        self.assertIn("SYNTHETIC", text)
        self.assertGreaterEqual(len(bars), 2)
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "report.json"
            fills_path = Path(tmp) / "fills.jsonl"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = main(
                    [
                        "--config",
                        str(root / "research_backtest.example.json"),
                        "--report",
                        str(report_path),
                        "--fills",
                        str(fills_path),
                    ]
                )
            self.assertEqual(code, 0)
            report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertFalse(report["live_trading"])
        self.assertEqual(report["orders_sent"], 0)
        self.assertEqual(report["profitability_claim"], "none")
        symbols = {item["symbol"]: item for item in report["assets"]}
        self.assertEqual(set(symbols), {"BTCUSDT", "AAPL", "SPY", "QQQ"})
        self.assertEqual(symbols["BTCUSDT"]["product"], "binance_usdt_perp")
        self.assertEqual(symbols["AAPL"]["product"], "us_cash_equity")
        self.assertEqual(symbols["SPY"]["product"], "us_cash_etf")
        self.assertEqual(symbols["BTCUSDT"]["funding_status"], "unavailable")
        self.assertIsNone(symbols["BTCUSDT"]["full_sample"]["funding"])
        self.assertEqual(symbols["AAPL"]["funding_status"], "not_applicable")
        self.assertGreaterEqual(symbols["BTCUSDT"]["fills"], 1)
        self.assertGreaterEqual(symbols["AAPL"]["fills"], 1)
        self.assertTrue(any("期权" in note or "CME" in note for note in report["limitations"]))


if __name__ == "__main__":
    unittest.main()

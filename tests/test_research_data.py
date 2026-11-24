"""Parsers, calendar, allocation, and funding-status tests.

Network archives are not contacted here. Historical evidence comes from the
offline study command, not from these fixtures.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from research_backtest import (
    AssetSpec,
    FundingSettlement,
    MarketAux,
    ResearchBar,
    ResearchConfig,
    ResearchSafetyError,
    RiskSpec,
    SessionSpec,
    StrategySpec,
    run_research,
)
from research_calendar import is_nyse_session, nyse_full_holidays
from research_data import http_get, parse_vision_funding_csv, parse_vision_kline_csv, parse_yahoo_chart


def _strategy() -> StrategySpec:
    return StrategySpec("baseline_sma_momentum", 2, 3, 1, Decimal("0"), Decimal("0.05"))


def _config(assets: list[AssetSpec], **overrides: object) -> ResearchConfig:
    raw: dict[str, object] = {
        "mode": "backtest",
        "data_label": "synthetic_unit",
        "starting_equity": Decimal("10000"),
        "sample_split": None,
        "strategy": _strategy(),
        "risk": RiskSpec(Decimal("0.01"), Decimal("0.5"), Decimal("0.5"), 3),
        "session": SessionSpec("America/New_York", "09:30", "16:00"),
        "assets": tuple(assets),
        "report_path": None,
        "fills_path": None,
        "notes": (),
        "config_dir": __import__("pathlib").Path("."),
    }
    raw.update(overrides)
    return ResearchConfig(**raw)  # type: ignore[arg-type]


def _equity(symbol: str, **overrides: object) -> AssetSpec:
    raw: dict[str, object] = {
        "symbol": symbol,
        "product": "us_cash_equity" if symbol == "AAPL" else "us_cash_etf",
        "quote_asset": "USD",
        "csv_path": None,
        "fee_bps": Decimal("0"),
        "slippage_bps": Decimal("0"),
        "leverage": 1,
        "allow_short": False,
        "maintenance_margin_rate": Decimal("0.004"),
    }
    raw.update(overrides)
    return AssetSpec(**raw)  # type: ignore[arg-type]


class CalendarTests(unittest.TestCase):
    def test_known_nyse_closures(self) -> None:
        self.assertIn(datetime(2024, 1, 1).date(), nyse_full_holidays(2024))
        self.assertIn(datetime(2024, 7, 4).date(), nyse_full_holidays(2024))
        self.assertIn(datetime(2024, 11, 28).date(), nyse_full_holidays(2024))
        self.assertIn(datetime(2022, 6, 20).date(), nyse_full_holidays(2022))  # Juneteenth observed Monday
        self.assertNotIn(datetime(2021, 6, 18).date(), nyse_full_holidays(2021))
        self.assertTrue(is_nyse_session(datetime(2024, 1, 2).date()))
        self.assertFalse(is_nyse_session(datetime(2024, 1, 1).date()))
        self.assertFalse(is_nyse_session(datetime(2025, 1, 9).date()))


class VisionParserTests(unittest.TestCase):
    def test_kline_and_funding_parsers(self) -> None:
        text = "\n".join(
            [
                "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore",
                "1704067200000,42314.00,44266.00,42207.90,44230.20,10,1704153599999,1,1,1,1,0",
                "1704153600000,44230.30,45950.00,44200.90,44979.80,11,1704239999999,1,1,1,1,0",
            ]
        )
        rows = parse_vision_kline_csv(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["close"], Decimal("44230.20"))
        funding = parse_vision_funding_csv(
            "calc_time,funding_interval_hours,last_funding_rate\n1704067200001,8,0.00037409\n"
        )
        self.assertEqual(funding[0][1], Decimal("0.00037409"))
        self.assertEqual(funding[0][0].second, 0)

    def test_bad_ohlc_is_rejected(self) -> None:
        with self.assertRaises(ResearchSafetyError):
            parse_vision_kline_csv("1704067200000,10,9,8,10,1,1,1,1,1,1,0\n")

    def test_fapi_host_is_refused(self) -> None:
        with self.assertRaises(ResearchSafetyError):
            http_get("https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1d&limit=1")


class YahooParserTests(unittest.TestCase):
    def test_split_adjusted_prices_do_not_reapply_the_split(self) -> None:
        start = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
        stamps = [int((start + timedelta(days=index)).timestamp()) for index in range(4)]
        payload = {
            "chart": {
                "result": [
                    {
                        "meta": {
                            "symbol": "AAPL",
                            "instrumentType": "EQUITY",
                            "currency": "USD",
                            "exchangeTimezoneName": "America/New_York",
                            "dataGranularity": "1d",
                        },
                        "timestamp": stamps,
                        "indicators": {
                            "quote": [
                                {
                                    "open": [100, 101, 102, 103],
                                    "high": [101, 102, 103, 104],
                                    "low": [99, 100, 101, 102],
                                    "close": [100.5, 101.5, 102.5, 103.5],
                                    "volume": [1, 1, 1, 1],
                                }
                            ]
                        },
                        "events": {
                            "splits": {
                                "1": {
                                    "date": stamps[2],
                                    "numerator": 4,
                                    "denominator": 1,
                                    "splitRatio": "4:1",
                                }
                            },
                            "dividends": {"1": {"date": stamps[1], "amount": 0.25}},
                        },
                    }
                ]
            }
        }
        bars, dividends, splits, info = parse_yahoo_chart(
            payload,
            symbol="AAPL",
            start=start.date(),
            end=(start + timedelta(days=3)).date(),
        )
        self.assertEqual(len(bars), 4)
        self.assertTrue(bars[0].session_daily)
        self.assertFalse(splits[0].already_in_prices is False and info["splits_reapplied"])
        self.assertFalse(info["splits_reapplied"])
        self.assertEqual(dividends[0].amount, Decimal("0.25"))
        self.assertIn("split_adjusted", info["price_convention"])


class AllocationAndFundingTests(unittest.TestCase):
    def _session_bars(self) -> list[ResearchBar]:
        start = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
        closes = [Decimal(value) for value in ("100", "101", "103", "106", "108")]
        bars = []
        previous = closes[0]
        for index, close in enumerate(closes):
            bars.append(
                ResearchBar(
                    timestamp=start + timedelta(days=index),
                    open=previous,
                    high=max(previous, close),
                    low=min(previous, close),
                    close=close,
                    volume=Decimal("1"),
                    session_daily=True,
                )
            )
            previous = close
        return bars

    def test_sleeves_do_not_spend_each_others_cash(self) -> None:
        bars = self._session_bars()
        assets = [
            _equity("AAPL", weight=Decimal("0.5")),
            _equity("SPY", weight=Decimal("0.5"), product="us_cash_etf"),
        ]
        report = run_research(
            _config(
                assets,
                risk=RiskSpec(Decimal("1"), Decimal("1"), Decimal("0.9"), 3),
                strategy=StrategySpec("baseline_sma_momentum", 2, 3, 1, Decimal("0"), Decimal("0.5")),
            ),
            {"AAPL": bars, "SPY": bars},
        )
        opens = [fill for fill in report["fills"] if fill["action"] == "open_long"]
        self.assertEqual({fill["symbol"] for fill in opens}, {"AAPL", "SPY"})
        for fill in opens:
            notional = Decimal(fill["qty"]) * Decimal(fill["fill_price"])
            self.assertLessEqual(notional, Decimal("5100"))

    def test_holiday_blocks_a_daily_session_bar(self) -> None:
        # MLK Day 2024-01-15 is the first bar that has enough history to enter.
        days = [
            datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc),
            datetime(2024, 1, 3, 14, 30, tzinfo=timezone.utc),
            datetime(2024, 1, 4, 14, 30, tzinfo=timezone.utc),
            datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc),
            datetime(2024, 1, 16, 14, 30, tzinfo=timezone.utc),
        ]
        closes = [Decimal(value) for value in ("100", "102", "104", "106", "108")]
        bars = []
        previous = closes[0]
        for stamp, close in zip(days, closes):
            bars.append(
                ResearchBar(
                    timestamp=stamp,
                    open=previous,
                    high=max(previous, close),
                    low=min(previous, close),
                    close=close,
                    volume=Decimal("1"),
                    session_daily=True,
                )
            )
            previous = close
        report = run_research(_config([_equity("QQQ", product="us_cash_etf")]), {"QQQ": bars})
        self.assertTrue(any(item["reason"] == "exchange_holiday" for item in report["rejections"]))

    def test_missing_funding_mark_is_not_booked_as_zero(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        bars = []
        previous = Decimal("100")
        for index, close in enumerate([Decimal(v) for v in ("100", "101", "103", "106", "108")]):
            bars.append(
                ResearchBar(
                    timestamp=start + timedelta(days=index),
                    open=previous,
                    high=max(previous, close) + Decimal("1"),
                    low=min(previous, close) - Decimal("1"),
                    close=close,
                    volume=Decimal("1"),
                    mark_open=previous,
                    mark_high=max(previous, close) + Decimal("1"),
                    mark_low=min(previous, close) - Decimal("1"),
                    mark_close=close,
                )
            )
            previous = close
        aux = MarketAux(
            funding={
                "BTCUSDT": [
                    FundingSettlement(timestamp=bars[4].timestamp, rate=Decimal("0.001"), mark=None),
                ]
            },
            funding_complete={"BTCUSDT": False},
        )
        asset = AssetSpec(
            "BTCUSDT",
            "binance_usdt_perp",
            "USDT",
            None,
            Decimal("0"),
            Decimal("0"),
            1,
            False,
            Decimal("0.004"),
        )
        report = run_research(_config([asset]), {"BTCUSDT": bars}, aux=aux)
        book = report["books"][0]
        self.assertEqual(book["funding_status"], "incomplete")
        self.assertIsNone(book["funding"])
        self.assertFalse(book["net_pnl_includes_funding"])

    def test_funding_settlement_uses_the_supplied_mark(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        bars = []
        previous = Decimal("100")
        for index, close in enumerate([Decimal(v) for v in ("100", "101", "103", "106", "110")]):
            bars.append(
                ResearchBar(
                    timestamp=start + timedelta(days=index),
                    open=previous,
                    high=max(previous, close),
                    low=min(previous, close),
                    close=close,
                    volume=Decimal("1"),
                    mark_open=previous,
                    mark_high=max(previous, close),
                    mark_low=min(previous, close),
                    mark_close=close,
                )
            )
            previous = close
        # Settlement inside the last bar's window, after the entry.
        aux = MarketAux(
            funding={
                "BTCUSDT": [
                    FundingSettlement(
                        timestamp=bars[-1].timestamp + timedelta(hours=8),
                        rate=Decimal("0.001"),
                        mark=Decimal("110"),
                    )
                ]
            },
            funding_complete={"BTCUSDT": True},
        )
        bare = run_research(
            _config(
                [
                    AssetSpec(
                        "BTCUSDT",
                        "binance_usdt_perp",
                        "USDT",
                        None,
                        Decimal("0"),
                        Decimal("0"),
                        1,
                        False,
                        Decimal("0.004"),
                    )
                ]
            ),
            {"BTCUSDT": bars},
        )
        funded = run_research(
            _config(
                [
                    AssetSpec(
                        "BTCUSDT",
                        "binance_usdt_perp",
                        "USDT",
                        None,
                        Decimal("0"),
                        Decimal("0"),
                        1,
                        False,
                        Decimal("0.004"),
                    )
                ]
            ),
            {"BTCUSDT": bars},
            aux=aux,
        )
        self.assertEqual(funded["books"][0]["funding_status"], "provided_series")
        self.assertLess(Decimal(funded["books"][0]["net_pnl"]), Decimal(bare["books"][0]["net_pnl"]))
        self.assertTrue(funded["books"][0]["net_pnl_includes_funding"])

    def test_intrabar_stop_does_not_fill_at_the_high(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        closes = [Decimal(v) for v in ("100", "101", "103", "106")]
        bars = []
        previous = closes[0]
        for index, close in enumerate(closes):
            bars.append(
                ResearchBar(
                    timestamp=start + timedelta(days=index),
                    open=previous,
                    high=max(previous, close),
                    low=min(previous, close),
                    close=close,
                    volume=Decimal("1"),
                )
            )
            previous = close
        entry_open = bars[3].open
        bars.append(
            ResearchBar(
                timestamp=start + timedelta(days=4),
                open=entry_open,
                high=entry_open + Decimal("50"),
                low=entry_open * Decimal("0.90"),
                close=entry_open,
                volume=Decimal("1"),
            )
        )
        report = run_research(
            _config(
                [
                    AssetSpec(
                        "BTCUSDT",
                        "binance_usdt_perp",
                        "USDT",
                        None,
                        Decimal("0"),
                        Decimal("0"),
                        1,
                        False,
                        Decimal("0.004"),
                    )
                ]
            ),
            {"BTCUSDT": bars},
        )
        stop = next(fill for fill in report["fills"] if fill["reason"] == "stop")
        self.assertLess(Decimal(stop["raw_price"]), entry_open)
        self.assertNotEqual(Decimal(stop["raw_price"]), entry_open + Decimal("50"))


if __name__ == "__main__":
    unittest.main()

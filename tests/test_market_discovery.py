import json
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from market_autotrader import AssetConfig, CASH_USDT_FUTURES, PaperPortfolio, Position
from market_discovery import (
    MarketDiscoveryConfig,
    bucket_regime_match,
    build_watchlist,
    collect_tradfi_rows,
    discovery_entry_score,
    discovery_from_config,
    finalize_watchlist,
    holdings_symbols,
    is_tradable_ticker,
    rank_by_change,
    rank_by_volume,
    resolve_trading_universe,
    run_market_scan,
    TickerRow,
    WatchlistEntry,
)


def sample_ticker(symbol: str, market: str, volume: str, change: str) -> dict:
    return {
        "symbol": symbol,
        "lastPrice": "100",
        "priceChangePercent": change,
        "quoteVolume": volume,
        "highPrice": "110",
        "lowPrice": "90",
        "count": 1000,
        "market": market,
    }


def sample_ticker_at(
    symbol: str,
    market: str,
    volume: str,
    change: str,
    *,
    last: str,
    low: str = "90",
    high: str = "110",
) -> dict:
    row = sample_ticker(symbol, market, volume, change)
    row["lastPrice"] = last
    row["lowPrice"] = low
    row["highPrice"] = high
    return row


class MarketDiscoveryTests(unittest.TestCase):
    def test_is_tradable_ticker_excludes_leveraged_and_stable(self) -> None:
        cfg = MarketDiscoveryConfig()
        self.assertFalse(is_tradable_ticker("USDCUSDT", cfg))
        self.assertFalse(is_tradable_ticker("BTCUPUSDT", cfg))
        self.assertFalse(is_tradable_ticker("ETHUSDT_260925", cfg))
        self.assertFalse(is_tradable_ticker("币安人生USDT", cfg))
        self.assertTrue(is_tradable_ticker("BTCUSDT", cfg))

    def test_discovery_from_config_defaults_disabled(self) -> None:
        cfg = discovery_from_config({})
        self.assertFalse(cfg.enabled)

    def test_rank_helpers(self) -> None:
        rows = [
            TickerRow("A", "spot", Decimal("1"), Decimal("5"), Decimal("100"), Decimal("2"), Decimal("1"), 1),
            TickerRow("B", "spot", Decimal("1"), Decimal("-3"), Decimal("200"), Decimal("2"), Decimal("1"), 1),
        ]
        self.assertEqual(rank_by_volume(rows, 1)[0].symbol, "B")
        self.assertEqual(rank_by_change(rows, 1, gainers=True)[0].symbol, "A")

    @patch("market_discovery.fetch_tickers_24hr")
    def test_run_market_scan_shapes_buckets(self, fetch_mock) -> None:
        fetch_mock.side_effect = [
            [
                TickerRow("BTCUSDT", "spot", Decimal("1"), Decimal("2"), Decimal("9000000"), Decimal("2"), Decimal("1"), 1),
            ],
            [
                TickerRow("ETHUSDT", "futures", Decimal("1"), Decimal("-4"), Decimal("8000000"), Decimal("2"), Decimal("1"), 1),
            ],
        ]
        scan = run_market_scan(MarketDiscoveryConfig(enabled=True))
        self.assertEqual(len(scan["spotTopVolume"]), 1)
        self.assertEqual(len(scan["futuresTopVolume"]), 1)
        self.assertIn("scannedAtIso", scan)

    def test_bucket_regime_match(self) -> None:
        self.assertTrue(bucket_regime_match("futuresGainers", "trend_up"))
        self.assertFalse(bucket_regime_match("futuresGainers", "range"))
        self.assertTrue(bucket_regime_match("futuresLosers", "trend_down"))
        self.assertTrue(bucket_regime_match("futuresTradFi", "range"))

    def test_discovery_score_penalizes_overextended_range_position(self) -> None:
        pullback = WatchlistEntry(
            "PULLUSDT",
            "binance_futures",
            "discovery:futuresGainers",
            "9000000",
            "12",
            True,
            "futuresGainers",
            "trend_up",
            True,
            range_position_24h="0.60",
        )
        high_chase = WatchlistEntry(
            "HIGHUSDT",
            "binance_futures",
            "discovery:futuresGainers",
            "9000000",
            "12",
            True,
            "futuresGainers",
            "trend_up",
            True,
            range_position_24h="0.96",
        )

        self.assertGreater(discovery_entry_score(pullback), discovery_entry_score(high_chase))

    def test_collect_tradfi_rows(self) -> None:
        rows = [
            TickerRow("TSLAUSDT", "futures", Decimal("1"), Decimal("2"), Decimal("5000000"), Decimal("2"), Decimal("1"), 1),
            TickerRow("BTCUSDT", "futures", Decimal("1"), Decimal("2"), Decimal("9000000"), Decimal("2"), Decimal("1"), 1),
        ]
        cfg = MarketDiscoveryConfig(include_tradfi=True, max_tradfi_symbols=4)
        picked = collect_tradfi_rows(rows, cfg)
        self.assertEqual([row.symbol for row in picked], ["TSLAUSDT"])

    @patch("market_discovery.probe_regime_kind", return_value="trend_up")
    def test_finalize_watchlist_limits_discovered_trades(self, _probe) -> None:
        entries = [
            WatchlistEntry("AAAUSDT", "binance_futures", "discovery:futuresGainers", "9000000", "10", True),
            WatchlistEntry("BBBUSDT", "binance_futures", "discovery:futuresGainers", "8000000", "8", True),
            WatchlistEntry("SPCXUSDT", "binance_futures", "pinned", "7000000", "1", True),
        ]
        cfg = MarketDiscoveryConfig(trade_discovered=True, max_discovered_trades_per_cycle=1, regime_filter_enabled=True)
        final = finalize_watchlist(entries, cfg, {})
        tradeable = [e for e in final if e.source.startswith("discovery") and e.executable]
        self.assertEqual(len(tradeable), 1)
        self.assertTrue(any(e.symbol == "SPCXUSDT" and e.executable for e in final))

    @patch("market_discovery.probe_regime_kind", return_value="trend_up")
    @patch("market_autotrader.open_quote_meets_min_notional", return_value=False)
    def test_finalize_watchlist_blocks_discovery_when_min_notional_too_high(self, _notional, _probe) -> None:
        entries = [
            WatchlistEntry("HEIUSDT", "binance_futures", "discovery:futuresGainers", "9000000", "10", True),
        ]
        cfg = MarketDiscoveryConfig(trade_discovered=True, regime_filter_enabled=True)
        final = finalize_watchlist(
            entries,
            cfg,
            {},
            max_trade_quote=Decimal("5"),
            execution_raw={"mode": "live"},
        )
        self.assertEqual(len(final), 1)
        self.assertFalse(final[0].executable)
        self.assertEqual(final[0].block_reason, "min_notional_exceeds_max_trade_quote")

    @patch("market_discovery.probe_regime_kind", return_value="trend_up")
    @patch("market_autotrader.open_quote_meets_min_notional", side_effect=UnicodeEncodeError("ascii", "黄金", 0, 1, "bad"))
    def test_finalize_watchlist_degrades_bad_symbol_notional_check(self, _notional, _probe) -> None:
        entries = [
            WatchlistEntry("黄金USDT", "binance_futures", "discovery:futuresGainers", "9000000", "10", True),
        ]
        cfg = MarketDiscoveryConfig(trade_discovered=True, regime_filter_enabled=True)
        final = finalize_watchlist(
            entries,
            cfg,
            {},
            max_trade_quote=Decimal("5"),
            execution_raw={"mode": "live"},
        )
        self.assertEqual(len(final), 1)
        self.assertFalse(final[0].executable)
        self.assertTrue(final[0].block_reason.startswith("min_notional_check_failed:"))

    def test_holdings_symbols_ignores_futures_ledger_cash(self) -> None:
        portfolio = PaperPortfolio(
            cash={CASH_USDT_FUTURES: Decimal("11.6")},
            positions={"SPCXUSDT": Position(quantity=Decimal("4.22"), average_price=Decimal("188.53"))},
        )
        symbols = holdings_symbols(portfolio)
        self.assertEqual(symbols, [("SPCXUSDT", "binance_futures")])

    @patch("market_discovery.run_market_scan")
    def test_build_watchlist_marks_discovered_as_non_executable(self, scan_mock) -> None:
        scan_mock.return_value = {
            "futuresGainers": [sample_ticker("SOLUSDT", "futures", "6000000", "12")],
            "futuresLosers": [],
            "futuresTopVolume": [],
            "spotGainers": [],
            "spotLosers": [],
            "spotTopVolume": [],
        }
        cfg = MarketDiscoveryConfig(
            enabled=True,
            trade_discovered=False,
            pinned=({"symbol": "SPCXUSDT", "market": "binance_futures"},),
        )
        watchlist = build_watchlist(cfg, scan_mock.return_value)
        by_symbol = {entry.symbol: entry for entry in watchlist}
        self.assertTrue(by_symbol["SPCXUSDT"].executable)
        self.assertFalse(by_symbol["SOLUSDT"].executable)

    @patch("market_discovery.probe_regime_kind")
    def test_discovery_candidate_pool_does_not_let_gainers_starve_losers(self, probe_mock) -> None:
        regimes = {
            "GAIN1USDT": "range",
            "GAIN2USDT": "range",
            "LOSS1USDT": "trend_down",
        }
        probe_mock.side_effect = lambda symbol, market, config, strategy_raw: regimes[symbol]
        scan = {
            "futuresGainers": [
                sample_ticker("GAIN1USDT", "futures", "9000000", "20"),
                sample_ticker("GAIN2USDT", "futures", "8500000", "18"),
            ],
            "futuresLosers": [sample_ticker("LOSS1USDT", "futures", "8000000", "-12")],
            "futuresTopVolume": [],
            "futuresTradFi": [],
            "spotGainers": [],
            "spotLosers": [],
            "spotTopVolume": [],
        }
        cfg = MarketDiscoveryConfig(
            enabled=True,
            trade_discovered=True,
            max_analyze_per_cycle=2,
            max_discovered_trades_per_cycle=1,
            regime_filter_enabled=True,
            regime_filter_mode="strict",
            include_tradfi=False,
        )

        watchlist = finalize_watchlist(build_watchlist(cfg, scan), cfg, {})
        by_symbol = {entry.symbol: entry for entry in watchlist}

        self.assertIn("LOSS1USDT", by_symbol)
        self.assertTrue(by_symbol["LOSS1USDT"].executable)
        self.assertLessEqual(
            sum(1 for entry in watchlist if entry.source.startswith("discovery")),
            cfg.max_analyze_per_cycle,
        )

    @patch("market_discovery.probe_regime_kind", return_value="trend_up")
    def test_finalize_watchlist_prefers_pullback_gainer_over_24h_high_chase(self, _probe) -> None:
        scan = {
            "futuresGainers": [
                sample_ticker_at("HIGHUSDT", "futures", "9000000", "12", last="109.5"),
                sample_ticker_at("PULLUSDT", "futures", "8500000", "12", last="102"),
            ],
            "futuresLosers": [],
            "futuresTopVolume": [],
            "futuresTradFi": [],
            "spotGainers": [],
            "spotLosers": [],
            "spotTopVolume": [],
        }
        cfg = MarketDiscoveryConfig(
            enabled=True,
            trade_discovered=True,
            max_analyze_per_cycle=2,
            max_discovered_trades_per_cycle=1,
            regime_filter_enabled=True,
            include_tradfi=False,
        )

        final = finalize_watchlist(build_watchlist(cfg, scan), cfg, {})
        by_symbol = {entry.symbol: entry for entry in final}

        self.assertEqual(by_symbol["HIGHUSDT"].range_position_24h, "0.9750")
        self.assertEqual(by_symbol["PULLUSDT"].range_position_24h, "0.6000")
        self.assertFalse(by_symbol["HIGHUSDT"].executable)
        self.assertTrue(by_symbol["PULLUSDT"].executable)

    @patch("market_discovery.probe_regime_kind", return_value="trend_up")
    @patch("market_discovery.run_market_scan")
    def test_resolve_trading_universe_writes_snapshot(self, scan_mock, _probe) -> None:
        scan_mock.return_value = {
            "futuresGainers": [sample_ticker("SOLUSDT", "futures", "6000000", "12")],
            "futuresLosers": [],
            "futuresTopVolume": [],
            "spotGainers": [],
            "spotLosers": [],
            "spotTopVolume": [],
            "filters": {"spotUniverse": 1, "futuresUniverse": 1},
        }
        with TemporaryDirectory() as tmp:
            snapshot_path = str(Path(tmp) / "discovery.json")
            static = [
                AssetConfig(
                    symbol="SPCXUSDT",
                    market="binance_futures",
                    base_asset="SPCX",
                    quote_asset="USDT",
                    provider={"type": "static"},
                )
            ]
            config = {
                "market_discovery": {
                    "enabled": True,
                    "snapshot_path": snapshot_path,
                    "trade_discovered": False,
                    "pinned": [{"symbol": "SPCXUSDT", "market": "binance_futures"}],
                }
            }
            assets, scan = resolve_trading_universe(config, PaperPortfolio(cash={"USDT": Decimal("10")}), static)
            self.assertTrue(Path(snapshot_path).exists())
            self.assertTrue(any(asset.symbol == "SPCXUSDT" for asset in assets))
            self.assertTrue(any(asset.symbol == "SOLUSDT" for asset in assets))
            payload = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
            self.assertTrue(payload.get("enabled"))


if __name__ == "__main__":
    unittest.main()

"""Credential-free historical data for the research backtest.

BTCUSDT uses the public Binance Vision archive of USD-M futures
(https://data.binance.vision, documented by binance/binance-public-data).
That is the perpetual kline, mark, index, and funding history. The FAPI
endpoint is not called: this environment receives HTTP 451 from it, and the
research path does not use a proxy to get around that.

US cash names use Yahoo's public chart endpoint. OHLC there is split-adjusted.
Dividends are cash events in split-adjusted units. They are not also taken
from adjusted close, so total return is not double-counted. This is not an
exchange-official blotter.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from research_backtest import (
    PRODUCT_PERP,
    AssetSpec,
    Dividend,
    FundingSettlement,
    MarketAux,
    ResearchBar,
    ResearchConfig,
    ResearchSafetyError,
    Split,
    run_research,
)
from research_calendar import expected_nyse_sessions, is_nyse_session, nyse_early_closes

VISION_HOST = "data.binance.vision"
YAHOO_HOST = "query1.finance.yahoo.com"
VISION_ROOT = f"https://{VISION_HOST}/data/futures/um"
USER_AGENT = "biancharge-research-backtest/1.0 (public-archive read)"
CACHE_ROOT = Path("data/research-cache")
NY = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class ArchivedFile:
    url: str
    sha256: str
    rows: int
    checksum_ok: bool


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _allowed_url(url: str) -> None:
    lowered = url.lower()
    if lowered.startswith("https://fapi.binance.com") or lowered.startswith("https://api.binance.com"):
        raise ResearchSafetyError(
            "Binance FAPI/API is not used. This location receives HTTP 451, and the research path will not proxy around it."
        )
    host = urllib.request.urlparse(url).hostname or ""
    if host not in {VISION_HOST, YAHOO_HOST}:
        raise ResearchSafetyError(f"research download refuses host {host or url!r}")
    if any(token in lowered for token in ("api_key", "signature=", "secret", "socks5", "proxy=")):
        raise ResearchSafetyError("research download refuses credential or proxy URLs")


def http_get(url: str, timeout: int = 60) -> bytes:
    """GET with proxies disabled. No .env and no credential headers."""
    _allowed_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            final = response.geturl()
            if urllib.request.urlparse(final).hostname not in {VISION_HOST, YAHOO_HOST}:
                raise ResearchSafetyError(f"refusing redirect off the public archive host: {final}")
            return response.read()
    except urllib.error.HTTPError as exc:
        raise ResearchSafetyError(f"HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise ResearchSafetyError(f"download failed for {url}: {exc.reason}") from exc


def _parse_checksum(text: str) -> str:
    token = text.strip().split()[0].lower()
    if len(token) != 64 or any(ch not in "0123456789abcdef" for ch in token):
        raise ResearchSafetyError("archive checksum file is not a sha256 digest")
    return token


def fetch_vision_zip(url: str, cache_root: Path = CACHE_ROOT) -> tuple[bytes, ArchivedFile]:
    rel = url.split(f"https://{VISION_HOST}/", 1)[-1]
    path = cache_root / "binance-vision" / rel
    checksum_path = path.parent / f"{path.name}.CHECKSUM"
    checksum_url = url + ".CHECKSUM"
    if path.is_file() and checksum_path.is_file():
        expected = _parse_checksum(checksum_path.read_text(encoding="utf-8"))
        payload = path.read_bytes()
        if _sha256(payload) == expected:
            rows = _zip_row_count(payload)
            return payload, ArchivedFile(url=url, sha256=expected, rows=rows, checksum_ok=True)
    payload = http_get(url)
    expected = _parse_checksum(http_get(checksum_url).decode("utf-8"))
    digest = _sha256(payload)
    if digest != expected:
        raise ResearchSafetyError(f"checksum mismatch for {url}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    checksum_path.write_text(f"{expected}  {path.name}\n", encoding="utf-8")
    return payload, ArchivedFile(url=url, sha256=digest, rows=_zip_row_count(payload), checksum_ok=True)


def _zip_row_count(payload: bytes) -> int:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        name = archive.namelist()[0]
        text = archive.read(name).decode("utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    if lines and not lines[0].split(",")[0].strip().isdigit():
        return max(len(lines) - 1, 0)
    return len(lines)


def parse_vision_kline_csv(text: str) -> list[dict[str, Decimal | datetime]]:
    """Parse a Binance Vision kline CSV (trade, mark, or index)."""
    rows: list[dict[str, Decimal | datetime]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = [item.strip() for item in line.split(",")]
        if not parts[0].isdigit():
            continue
        if len(parts) < 6:
            raise ResearchSafetyError("vision kline row is short")
        open_time = datetime.fromtimestamp(int(parts[0]) / 1000, tz=timezone.utc)
        open_, high, low, close = (Decimal(parts[1]), Decimal(parts[2]), Decimal(parts[3]), Decimal(parts[4]))
        volume = Decimal(parts[5])
        if min(open_, high, low, close) <= 0:
            raise ResearchSafetyError(f"non-positive vision price at {open_time.isoformat()}")
        if high < max(open_, close) or low > min(open_, close) or low > high:
            raise ResearchSafetyError(f"inconsistent vision OHLC at {open_time.isoformat()}")
        rows.append(
            {
                "timestamp": open_time,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )
    rows.sort(key=lambda row: row["timestamp"])  # type: ignore[arg-type, return-value]
    for prev, nxt in zip(rows, rows[1:]):
        if prev["timestamp"] == nxt["timestamp"]:
            raise ResearchSafetyError(f"duplicate vision timestamp {prev['timestamp']}")
    return rows


def parse_vision_funding_csv(text: str) -> list[tuple[datetime, Decimal]]:
    events: list[tuple[datetime, Decimal]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = [item.strip() for item in line.split(",")]
        if not parts[0].isdigit():
            continue
        if len(parts) < 3:
            raise ResearchSafetyError("vision funding row is short")
        # Some settlement timestamps are 1ms past the hour. Snap to the second.
        stamp = datetime.fromtimestamp(int(parts[0]) / 1000, tz=timezone.utc).replace(microsecond=0)
        rate = Decimal(parts[2])
        events.append((stamp, rate))
    events.sort(key=lambda item: item[0])
    deduped: list[tuple[datetime, Decimal]] = []
    for stamp, rate in events:
        if deduped and deduped[-1][0] == stamp:
            continue
        deduped.append((stamp, rate))
    return deduped


def zip_csv_text(payload: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        if len(names) != 1:
            raise ResearchSafetyError(f"expected one CSV in vision zip, found {names}")
        return archive.read(names[0]).decode("utf-8")


def _month_starts(start: date, end: date) -> list[date]:
    cursor = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    months: list[date] = []
    while cursor <= last:
        months.append(cursor)
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return months


def _vision_monthly_url(kind: str, symbol: str, interval: str, month: date) -> str:
    stamp = f"{month.year:04d}-{month.month:02d}"
    if kind == "fundingRate":
        name = f"{symbol}-fundingRate-{stamp}.zip"
        return f"{VISION_ROOT}/monthly/fundingRate/{symbol}/{name}"
    folder = {"trade": "klines", "mark": "markPriceKlines", "index": "indexPriceKlines"}[kind]
    name = f"{symbol}-{interval}-{stamp}.zip"
    return f"{VISION_ROOT}/monthly/{folder}/{symbol}/{interval}/{name}"


def _calendar_gaps(timestamps: list[datetime]) -> list[str]:
    if not timestamps:
        return []
    found = {item.date() for item in timestamps}
    missing: list[str] = []
    cursor = timestamps[0].date()
    while cursor <= timestamps[-1].date():
        if cursor not in found:
            missing.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return missing


def load_btc_usdt_perp(
    *,
    symbol: str = "BTCUSDT",
    start: date,
    end: date,
    cache_root: Path = CACHE_ROOT,
) -> tuple[list[ResearchBar], list[FundingSettlement], dict[str, Any]]:
    if symbol != "BTCUSDT":
        raise ResearchSafetyError("this acquisition path only loads the BTCUSDT USD-M perpetual")
    months = _month_starts(start, end)
    urls = []
    for month in months:
        for kind in ("trade", "mark", "index", "fundingRate"):
            interval = "8h" if kind == "mark" else "1d"
            if kind == "fundingRate":
                urls.append(_vision_monthly_url("fundingRate", symbol, "1d", month))
            elif kind == "mark":
                urls.append(_vision_monthly_url("mark", symbol, "1d", month))
                urls.append(_vision_monthly_url("mark", symbol, "8h", month))
            else:
                urls.append(_vision_monthly_url(kind, symbol, interval, month))
    files: list[ArchivedFile] = []
    payloads: dict[str, bytes] = {}

    def _one(url: str) -> tuple[str, bytes, ArchivedFile]:
        data, meta = fetch_vision_zip(url, cache_root)
        return url, data, meta

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_one, url) for url in urls]
        for future in as_completed(futures):
            url, data, meta = future.result()
            payloads[url] = data
            files.append(meta)

    trade_rows: list[dict[str, Any]] = []
    mark_rows: list[dict[str, Any]] = []
    index_rows: list[dict[str, Any]] = []
    mark_8h: dict[datetime, Decimal] = {}
    funding_rows: list[tuple[datetime, Decimal]] = []
    for month in months:
        trade_rows.extend(parse_vision_kline_csv(zip_csv_text(payloads[_vision_monthly_url("trade", symbol, "1d", month)])))
        mark_rows.extend(parse_vision_kline_csv(zip_csv_text(payloads[_vision_monthly_url("mark", symbol, "1d", month)])))
        index_rows.extend(parse_vision_kline_csv(zip_csv_text(payloads[_vision_monthly_url("index", symbol, "1d", month)])))
        for row in parse_vision_kline_csv(zip_csv_text(payloads[_vision_monthly_url("mark", symbol, "8h", month)])):
            mark_8h[row["timestamp"]] = row["open"]  # type: ignore[index]
        funding_rows.extend(parse_vision_funding_csv(zip_csv_text(payloads[_vision_monthly_url("fundingRate", symbol, "1d", month)])))

    def _in_range(stamp: datetime) -> bool:
        day = stamp.date()
        return start <= day <= end

    trade_rows = [row for row in trade_rows if _in_range(row["timestamp"])]  # type: ignore[arg-type]
    mark_by_time = {row["timestamp"]: row for row in mark_rows if _in_range(row["timestamp"])}  # type: ignore[arg-type]
    index_by_time = {row["timestamp"]: row for row in index_rows if _in_range(row["timestamp"])}  # type: ignore[arg-type]
    bars: list[ResearchBar] = []
    missing_mark = 0
    missing_index = 0
    for row in trade_rows:
        stamp = row["timestamp"]
        mark = mark_by_time.get(stamp)
        index = index_by_time.get(stamp)
        if mark is None:
            missing_mark += 1
        if index is None:
            missing_index += 1
        bars.append(
            ResearchBar(
                timestamp=stamp,  # type: ignore[arg-type]
                open=row["open"],  # type: ignore[arg-type]
                high=row["high"],  # type: ignore[arg-type]
                low=row["low"],  # type: ignore[arg-type]
                close=row["close"],  # type: ignore[arg-type]
                volume=row["volume"],  # type: ignore[arg-type]
                mark_open=None if mark is None else mark["open"],  # type: ignore[arg-type]
                mark_high=None if mark is None else mark["high"],  # type: ignore[arg-type]
                mark_low=None if mark is None else mark["low"],  # type: ignore[arg-type]
                mark_close=None if mark is None else mark["close"],  # type: ignore[arg-type]
                index_close=None if index is None else index["close"],  # type: ignore[arg-type]
            )
        )
    if len(bars) < 3:
        raise ResearchSafetyError("BTCUSDT vision sample is empty")
    settlements: list[FundingSettlement] = []
    missing_funding_mark = 0
    for stamp, rate in funding_rows:
        if not _in_range(stamp):
            continue
        mark = mark_8h.get(stamp)
        if mark is None:
            # Funding files sometimes stamp 1s late; the 8h candle opens on the hour.
            mark = mark_8h.get(stamp.replace(minute=0, second=0))
        if mark is None:
            missing_funding_mark += 1
        settlements.append(FundingSettlement(timestamp=stamp, rate=rate, mark=mark))
    gaps = _calendar_gaps([bar.timestamp for bar in bars])
    provenance = {
        "symbol": symbol,
        "product": PRODUCT_PERP,
        "source": "binance_public_data_vision_usd_m",
        "source_url_root": VISION_ROOT,
        "documentation": "https://github.com/binance/binance-public-data",
        "what_it_is": "USD-M perpetual BTCUSDT trade, mark, and index klines plus funding settlements. Not spot and not a TradFi synthetic.",
        "fapi": "not_called_http_451_from_this_location",
        "proxy": "disabled",
        "retrieved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "interval": "1d",
        "funding_interval": "8h",
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "bar_count": len(bars),
        "first_bar": bars[0].timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_bar": bars[-1].timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "calendar_gaps": gaps,
        "missing_daily_mark_bars": missing_mark,
        "missing_daily_index_bars": missing_index,
        "funding_settlements": len(settlements),
        "funding_settlements_missing_mark": missing_funding_mark,
        "funding_complete": missing_funding_mark == 0 and len(settlements) > 0,
        "price_convention": "exchange_trade_klines",
        "mark_convention": "exchange_mark_price_klines_for_triggers_and_funding_notional",
        "index_convention": "exchange_index_klines_recorded_not_used_for_pnl",
        "files": [
            {"url": item.url, "sha256": item.sha256, "rows": item.rows, "checksum_ok": item.checksum_ok}
            for item in sorted(files, key=lambda item: item.url)
        ],
    }
    if gaps:
        provenance["gap_policy"] = "missing days are reported and not fabricated; the next available bar is the next fill bar"
    return bars, settlements, provenance


def parse_yahoo_chart(payload: dict[str, Any], *, symbol: str, start: date, end: date) -> tuple[list[ResearchBar], list[Dividend], list[Split], dict[str, Any]]:
    chart = payload.get("chart") or {}
    error = chart.get("error")
    if error:
        raise ResearchSafetyError(f"Yahoo chart error for {symbol}: {error}")
    result = (chart.get("result") or [None])[0]
    if not isinstance(result, dict):
        raise ResearchSafetyError(f"Yahoo chart payload for {symbol} has no result")
    meta = result.get("meta") or {}
    if str(meta.get("symbol", "")).upper() != symbol.upper():
        raise ResearchSafetyError(f"Yahoo returned {meta.get('symbol')!r} for requested {symbol}")
    instrument = str(meta.get("instrumentType", "")).upper()
    if instrument in {"OPTION", "FUTURE", "FUTURES", "INDEX"}:
        raise ResearchSafetyError(f"{symbol} instrumentType {instrument} is not a cash equity/ETF series")
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    events = result.get("events") or {}
    dividends: list[Dividend] = []
    for item in (events.get("dividends") or {}).values():
        stamp = datetime.fromtimestamp(int(item["date"]), tz=timezone.utc)
        dividends.append(Dividend(timestamp=stamp, amount=Decimal(str(item["amount"]))))
    splits: list[Split] = []
    for item in (events.get("splits") or {}).values():
        numerator = Decimal(str(item.get("numerator", "1")))
        denominator = Decimal(str(item.get("denominator", "1")))
        if denominator <= 0 or numerator <= 0:
            raise ResearchSafetyError(f"invalid split for {symbol}")
        stamp = datetime.fromtimestamp(int(item["date"]), tz=timezone.utc)
        splits.append(Split(timestamp=stamp, ratio=numerator / denominator, already_in_prices=False))
    bars: list[ResearchBar] = []
    skipped_null = 0
    for index, raw_ts in enumerate(timestamps):
        stamp = datetime.fromtimestamp(int(raw_ts), tz=timezone.utc)
        session_day = stamp.astimezone(NY).date()
        if session_day < start or session_day > end:
            continue
        try:
            open_ = Decimal(str(quote["open"][index]))
            high = Decimal(str(quote["high"][index]))
            low = Decimal(str(quote["low"][index]))
            close = Decimal(str(quote["close"][index]))
            volume = Decimal(str(quote["volume"][index] or "0"))
        except (TypeError, KeyError, IndexError, ArithmeticError):
            skipped_null += 1
            continue
        if min(open_, high, low, close) <= 0:
            raise ResearchSafetyError(f"non-positive Yahoo price for {symbol} on {session_day}")
        if high < max(open_, close) or low > min(open_, close):
            raise ResearchSafetyError(f"inconsistent Yahoo OHLC for {symbol} on {session_day}")
        if not is_nyse_session(session_day):
            raise ResearchSafetyError(f"Yahoo bar for {symbol} falls on a non-session date {session_day}")
        bars.append(
            ResearchBar(
                timestamp=stamp,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=volume,
                session_daily=True,
            )
        )
    bars.sort(key=lambda bar: bar.timestamp)
    for prev, nxt in zip(bars, bars[1:]):
        if prev.timestamp == nxt.timestamp:
            raise ResearchSafetyError(f"duplicate Yahoo bar for {symbol}")
    convention, reapply = _split_price_convention(bars, splits)
    splits = [replace(item, already_in_prices=not reapply) for item in splits]
    if bars:
        found_days = {bar.timestamp.astimezone(NY).date() for bar in bars}
        missing = [
            day.isoformat()
            for day in expected_nyse_sessions(bars[0].timestamp.astimezone(NY).date(), bars[-1].timestamp.astimezone(NY).date())
            if day not in found_days
        ]
        early = [
            day.isoformat()
            for day in found_days
            if day in nyse_early_closes(day.year)
        ]
    else:
        missing = []
        early = []
    info = {
        "symbol": symbol,
        "instrument_type": instrument or None,
        "exchange_timezone": meta.get("exchangeTimezoneName"),
        "currency": meta.get("currency"),
        "interval": meta.get("dataGranularity") or "1d",
        "bar_count": len(bars),
        "first_bar": bars[0].timestamp.strftime("%Y-%m-%dT%H:%M:%SZ") if bars else None,
        "last_bar": bars[-1].timestamp.strftime("%Y-%m-%dT%H:%M:%SZ") if bars else None,
        "skipped_null_bars": skipped_null,
        "missing_sessions": missing,
        "early_close_sessions_present": sorted(early),
        "dividends": len(dividends),
        "splits": [
            {"timestamp": item.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"), "ratio": format(item.ratio, "f")}
            for item in splits
        ],
        "price_convention": convention,
        "splits_reapplied": reapply,
        "dividend_convention": "cash_credited_once_on_ex_date_if_already_long; not taken from adjclose",
        "total_return_limitation": "Split-adjusted Yahoo OHLC plus cash dividends. Not a broker confirmation, and daily bars do not model the 13:00 early-close clock.",
    }
    return bars, dividends, splits, info


def _split_price_convention(bars: list[ResearchBar], splits: list[Split]) -> tuple[str, bool]:
    """Return (convention, reapply_split_to_quantity)."""
    if not splits:
        return "no_split_events_in_sample", False
    by_time = {bar.timestamp: bar for bar in bars}
    stamps = [bar.timestamp for bar in bars]
    adjusted = 0
    raw = 0
    for split in splits:
        earlier = [stamp for stamp in stamps if stamp < split.timestamp]
        later = [stamp for stamp in stamps if stamp >= split.timestamp]
        if not earlier or not later:
            continue
        before = by_time[earlier[-1]].close
        after = by_time[later[0]].close
        ratio = after / before
        if abs(ratio - Decimal("1")) <= Decimal("0.35"):
            adjusted += 1
        inverse = Decimal("1") / split.ratio
        if abs(ratio - inverse) <= abs(inverse) * Decimal("0.35"):
            raw += 1
    if adjusted and not raw:
        return "yahoo_ohlc_already_split_adjusted_not_dividend_adjusted", False
    if raw and not adjusted:
        return "raw_ohlc_splits_applied_in_engine", True
    return "split_adjustment_ambiguous_splits_not_reapplied", False


def fetch_yahoo_chart(symbol: str, start: date, end: date, cache_root: Path = CACHE_ROOT) -> tuple[list[ResearchBar], list[Dividend], list[Split], dict[str, Any]]:
    end_exclusive = end + timedelta(days=1)
    period1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    period2 = int(datetime(end_exclusive.year, end_exclusive.month, end_exclusive.day, tzinfo=timezone.utc).timestamp())
    url = (
        f"https://{YAHOO_HOST}/v8/finance/chart/{symbol}"
        f"?period1={period1}&period2={period2}&interval=1d&events=div%7Csplit&includeAdjustedClose=true"
    )
    cache_path = cache_root / "yahoo" / f"{symbol}-{start.isoformat()}-{end.isoformat()}.json"
    if cache_path.is_file():
        payload_bytes = cache_path.read_bytes()
    else:
        payload_bytes = http_get(url)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(payload_bytes)
    payload = json.loads(payload_bytes.decode("utf-8"))
    bars, dividends, splits, info = parse_yahoo_chart(payload, symbol=symbol, start=start, end=end)
    info.update(
        {
            "source": "yahoo_chart_v8_public_http",
            "source_url": url,
            "documentation": "Public Yahoo chart JSON. Not an exchange-official archive and not a listed-derivative feed.",
            "sha256": _sha256(payload_bytes),
            "retrieved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "proxy": "disabled",
            "product_scope": "cash equity or ETF",
        }
    )
    if not bars:
        raise ResearchSafetyError(f"Yahoo returned no session bars for {symbol}")
    return bars, dividends, splits, info


def _parse_day(value: str) -> date:
    return date.fromisoformat(value)


def acquire(config: ResearchConfig, cache_root: Path = CACHE_ROOT) -> tuple[dict[str, list[ResearchBar]], MarketAux, list[dict[str, Any]]]:
    """Download or reload cached public history for assets that declare a source."""
    # Make sure a developer proxy in the environment cannot redirect this fetch.
    os.environ.pop("http_proxy", None)
    os.environ.pop("https_proxy", None)
    os.environ.pop("HTTP_PROXY", None)
    os.environ.pop("HTTPS_PROXY", None)
    os.environ.pop("ALL_PROXY", None)
    os.environ.pop("all_proxy", None)
    series: dict[str, list[ResearchBar]] = {}
    aux = MarketAux()
    provenance: list[dict[str, Any]] = []
    for asset in config.assets:
        if not asset.source_type:
            continue
        start = _parse_day(asset.source_start)
        end = _parse_day(asset.source_end)
        if asset.source_type == "binance_vision_um":
            bars, funding, info = load_btc_usdt_perp(symbol=asset.symbol, start=start, end=end, cache_root=cache_root)
            series[asset.symbol] = bars
            aux.funding[asset.symbol] = funding
            aux.funding_complete[asset.symbol] = bool(info["funding_complete"])
            provenance.append(info)
        elif asset.source_type == "yahoo_chart":
            bars, dividends, splits, info = fetch_yahoo_chart(asset.symbol, start, end, cache_root)
            series[asset.symbol] = bars
            aux.dividends[asset.symbol] = dividends
            aux.splits[asset.symbol] = splits
            provenance.append(info)
        elif asset.source_type in {"binance_fapi", "alpaca"}:
            raise ResearchSafetyError(f"{asset.source_type} is not a credential-free archive for this path")
        else:
            raise ResearchSafetyError(f"unknown historical source {asset.source_type}")
    return series, aux, provenance


def _slim(report: dict[str, Any]) -> dict[str, Any]:
    books = []
    for book in report["books"]:
        item = dict(book)
        books.append(item)
    assets = []
    for asset in report["assets"]:
        assets.append(dict(asset))
    return {
        "mode": report["mode"],
        "execution": report["execution"],
        "paper_only": True,
        "live_trading": False,
        "orders_sent": 0,
        "leverage_changes_sent": 0,
        "strategy": report.get("strategy"),
        "parameters": report.get("parameters"),
        "data_label": report["data_label"],
        "profitability_claim": "none",
        "sample_split_timestamp": report.get("sample_split_timestamp"),
        "books": books,
        "assets": assets,
        "fill_count": len(report.get("fills") or []),
        "rejection_counts": _count_reasons(report.get("rejections") or []),
        "pending_signals": report.get("pending_signals") or [],
        "limitations": report.get("limitations") or [],
    }


def _count_reasons(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[str(row.get("reason"))] += 1
    return dict(sorted(counts.items()))


def _section_metrics(report: dict[str, Any], book_name: str, section: str) -> dict[str, Any]:
    book = next(item for item in report["books"] if item["book"] == book_name)
    block = book.get(section)
    if not block:
        return {}
    starting = Decimal(book["starting_equity"])
    net = Decimal(block["net_pnl"])
    return {
        "net_pnl": block["net_pnl"],
        "net_return": format((net / starting) if starting else Decimal("0"), "f"),
        "fees": block["fees"],
        "funding": block["funding"],
        "net_pnl_includes_funding": block["net_pnl_includes_funding"],
        "max_drawdown": block["max_drawdown"],
        "fills": block["fills"],
        "period_start": block["period_start"],
        "period_end": block["period_end"],
    }


def run_historical_study(config: ResearchConfig, cache_root: Path = CACHE_ROOT) -> dict[str, Any]:
    if config.mode not in {"backtest", "paper"}:
        raise ResearchSafetyError("historical study is paper/backtest only")
    sourced = [asset for asset in config.assets if asset.source_type]
    if not sourced:
        raise ResearchSafetyError("historical study needs source blocks, not only synthetic csv")
    if not config.parameters_locked:
        raise ResearchSafetyError("historical config must set parameters_locked true before the held-out run")
    series, aux, provenance = acquire(config, cache_root)
    for asset in config.assets:
        if asset.symbol not in series:
            raise ResearchSafetyError(f"{asset.symbol} was not acquired")
    weights_by_book: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for asset in config.assets:
        book = "perp" if asset.product == PRODUCT_PERP else "cash"
        if asset.weight is None:
            raise ResearchSafetyError(f"{asset.symbol} needs an allocation weight so cash is not copied per symbol")
        weights_by_book[book] += asset.weight
    for book, total in weights_by_book.items():
        if abs(total - Decimal("1")) > Decimal("0.0000001"):
            raise ResearchSafetyError(f"{book} weights must sum to 1")

    def _tagged(report: dict[str, Any]) -> dict[str, Any]:
        report = dict(report)
        report["parameters"] = {
            "name": config.strategy.name,
            "fast_window": config.strategy.fast_window,
            "slow_window": config.strategy.slow_window,
            "momentum_window": config.strategy.momentum_window,
            "momentum_threshold": format(config.strategy.momentum_threshold, "f"),
            "stop_pct": format(config.strategy.stop_pct, "f"),
            "risk_per_trade_pct": format(config.risk.risk_per_trade_pct, "f"),
            "max_position_pct_of_sleeve": format(config.risk.max_position_pct, "f"),
            "max_drawdown_pct": format(config.risk.max_drawdown_pct, "f"),
            "parameters_locked_before_oos": True,
            "optimized_on_test": False,
            "signal_uses_only_closed_bars": True,
            "fills": "next_bar_open",
        }
        return report

    baseline = _tagged(run_research(config, series, aux=aux, signal_mode="baseline"))
    benchmark = _tagged(run_research(config, series, aux=aux, signal_mode="buy_and_hold"))
    stressed_assets = tuple(
        replace(
            asset,
            fee_bps=asset.fee_bps * config.stress_multiplier,
            slippage_bps=asset.slippage_bps * config.stress_multiplier,
        )
        for asset in config.assets
    )
    stressed_config = replace(config, assets=stressed_assets)
    stress = _tagged(run_research(stressed_config, series, aux=aux, signal_mode="baseline"))
    books = sorted({("perp" if asset.product == PRODUCT_PERP else "cash") for asset in config.assets})
    comparison: dict[str, Any] = {"books": {}, "symbols": {}}
    for book in books:
        comparison["books"][book] = {
            "in_sample": {
                "baseline": _section_metrics(baseline, book, "in_sample"),
                "buy_and_hold": _section_metrics(benchmark, book, "in_sample"),
                "cost_stress": _section_metrics(stress, book, "in_sample"),
            },
            "out_of_sample": {
                "baseline": _section_metrics(baseline, book, "out_of_sample"),
                "buy_and_hold": _section_metrics(benchmark, book, "out_of_sample"),
                "cost_stress": _section_metrics(stress, book, "out_of_sample"),
            },
        }
    for asset in config.assets:
        base_asset = next(item for item in baseline["assets"] if item["symbol"] == asset.symbol)
        bench_asset = next(item for item in benchmark["assets"] if item["symbol"] == asset.symbol)
        stress_asset = next(item for item in stress["assets"] if item["symbol"] == asset.symbol)
        allocated = config.starting_equity * asset.weight if asset.weight is not None else None
        comparison["symbols"][asset.symbol] = {
            "product": asset.product,
            "allocated_starting_equity": format(allocated, "f") if allocated is not None else None,
            "out_of_sample": {
                "baseline": base_asset.get("out_of_sample"),
                "buy_and_hold": bench_asset.get("out_of_sample"),
                "cost_stress": stress_asset.get("out_of_sample"),
            },
        }
    return {
        "study": "historical_public_data",
        "paper_only": True,
        "live_trading": False,
        "orders_sent": 0,
        "leverage_changes_sent": 0,
        "profitability_claim": "none",
        "parameters_locked_before_oos": True,
        "sample_split_timestamp": baseline.get("sample_split_timestamp"),
        "provenance": provenance,
        "baseline": _slim(baseline),
        "buy_and_hold": _slim(benchmark),
        "cost_stress": _slim(stress),
        "comparison": comparison,
        "baseline_fills": baseline["fills"],
        "limitations": baseline["limitations"]
        + [
            "FAPI 在此环境返回 HTTP 451，研究程序没有使用代理绕过，改为 Binance 公开数据归档中的 U 本位永续文件。",
            "美股/ETF 价格来自 Yahoo 公开日线。OHLC 若已含拆股，就不再把拆股乘到股数上；股息按除息日现金计入一次，不用调整收盘价重复计算。",
            "这不是券商成交回报。提前收市日仍使用当天日线，没有把 13:00 的盘中路径单独模拟出来。",
            "费用和滑点是事先写死的假设。压力情形只把同一组费用和滑点乘以设定倍数，不改信号参数。",
            "买入持有基准用同一资金切片、同一费用和同一预热长度，从可以交易的第一根下一开盘买入并持有，没有根据样本外结果挑选。",
        ],
    }


def write_study_files(study: dict[str, Any], report_path: str | None, fills_path: str | None) -> None:
    fills = study.get("baseline_fills") or []
    public = dict(study)
    public["baseline_fills"] = {"count": len(fills), "path": fills_path}
    if report_path:
        target = Path(report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(public, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        provenance_path = target.with_name("historical-provenance.json")
        provenance_path.write_text(json.dumps(study["provenance"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if fills_path:
        target = Path(fills_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(json.dumps(fill, ensure_ascii=False) + "\n" for fill in fills), encoding="utf-8")
        csv_path = target.with_suffix(".csv")
        fields = [
            "timestamp",
            "symbol",
            "product",
            "action",
            "reason",
            "qty",
            "raw_price",
            "fill_price",
            "fee",
            "slippage_cost",
            "signal_timestamp",
            "mark_price",
            "index_price",
            "realized_price_pnl",
        ]
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(fills)


def main(argv: list[str] | None = None) -> int:
    """Read-only data command. It downloads public archives and does not trade."""
    import argparse
    import sys

    from research_backtest import load_config

    parser = argparse.ArgumentParser(description="Fetch public research history. No orders.")
    parser.add_argument("command", choices=["fetch", "describe"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--provenance", default="artifacts/research/historical-provenance.json")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        _series, _aux, provenance = acquire(config)
    except ResearchSafetyError as exc:
        print(f"研究数据拒绝执行: {exc}", file=sys.stderr)
        return 2
    target = Path(args.provenance)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for info in provenance:
        gaps = info.get("calendar_gaps") or info.get("missing_sessions") or []
        print(
            f"{info.get('symbol')} {info.get('source') or info.get('source_url_root')} "
            f"bars={info.get('bar_count')} {info.get('first_bar')}..{info.get('last_bar')} gaps={len(gaps)}"
        )
    print(f"provenance: {target}")
    return 0


def format_study(study: dict[str, Any]) -> str:
    lines = [
        "历史研究（纸面/回测）",
        f"样本切分: {study.get('sample_split_timestamp')}",
        "参数已在看样本外之前锁定，没有用测试集寻优。",
        "实盘订单: 0；杠杆修改: 0；收益承诺: 无",
    ]
    for book, sections in study["comparison"]["books"].items():
        oos = sections["out_of_sample"]
        lines.append(
            f"账本 {book} 样本外 基线净盈亏={oos['baseline'].get('net_pnl')} "
            f"收益={oos['baseline'].get('net_return')} 费用={oos['baseline'].get('fees')} "
            f"资金费={oos['baseline'].get('funding')} 回撤={oos['baseline'].get('max_drawdown')} "
            f"成交={oos['baseline'].get('fills')} | 买入持有净盈亏={oos['buy_and_hold'].get('net_pnl')} "
            f"| 费用压力净盈亏={oos['cost_stress'].get('net_pnl')}"
        )
    for symbol, item in study["comparison"]["symbols"].items():
        oos = (item.get("out_of_sample") or {}).get("baseline") or {}
        lines.append(
            f"{symbol} {item['product']} 分配资金={item['allocated_starting_equity']} "
            f"样本外净盈亏={oos.get('net_pnl')} 费用={oos.get('fees')} 资金费={oos.get('funding')} "
            f"成交={oos.get('fills')}"
        )
    for info in study["provenance"]:
        lines.append(
            f"数据 {info.get('symbol')} {info.get('source') or info.get('source_url_root')} "
            f"区间={info.get('first_bar')}..{info.get('last_bar')} bars={info.get('bar_count')} "
            f"缺口={len(info.get('calendar_gaps') or info.get('missing_sessions') or [])}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())

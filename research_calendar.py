"""NYSE session calendar for cash-equity research bars.

Full-day holidays follow the published NYSE weekday rules (weekend observance,
Good Friday, and Juneteenth from 2022). Early closes stay tradable because a
daily bar already contains that session's official OHLC; they are listed so
reports do not pretend the session ran until 16:00.
"""

from __future__ import annotations

from datetime import date, timedelta


def easter_sunday(year: int) -> date:
    """Gregorian Easter Sunday (Anonymous algorithm)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed_fixed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    shift = (weekday - first.weekday()) % 7
    return first + timedelta(days=shift + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        cursor = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    while cursor.weekday() != weekday:
        cursor -= timedelta(days=1)
    return cursor


# One-off full closures that are not part of the repeating holiday rules.
EXTRA_FULL_CLOSURES = {
    date(2025, 1, 9),  # National Day of Mourning
}


def nyse_full_holidays(year: int) -> set[date]:
    """NYSE full closures for one calendar year."""
    holidays = {
        _observed_fixed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),  # MLK
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        easter_sunday(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),  # Memorial Day
        _observed_fixed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed_fixed(date(year, 12, 25)),
    }
    if year >= 2022:
        holidays.add(_observed_fixed(date(year, 6, 19)))
    holidays.update(day for day in EXTRA_FULL_CLOSURES if day.year == year)
    return holidays


def nyse_early_closes(year: int) -> set[date]:
    """Common 13:00 ET closes. The daily bar is still a real session."""
    holidays = nyse_full_holidays(year)
    closes: set[date] = set()
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    black_friday = thanksgiving + timedelta(days=1)
    if black_friday.weekday() < 5 and black_friday not in holidays:
        closes.add(black_friday)
    christmas_eve = date(year, 12, 24)
    if christmas_eve.weekday() < 5 and christmas_eve not in holidays:
        closes.add(christmas_eve)
    july3 = date(year, 7, 3)
    july4 = date(year, 7, 4)
    if july3.weekday() < 5 and july3 not in holidays and july4.weekday() < 5:
        closes.add(july3)
    return closes


def is_nyse_session(day: date) -> bool:
    return day.weekday() < 5 and day not in nyse_full_holidays(day.year)


def expected_nyse_sessions(start: date, end: date) -> list[date]:
    if end < start:
        return []
    days: list[date] = []
    cursor = start
    while cursor <= end:
        if is_nyse_session(cursor):
            days.append(cursor)
        cursor += timedelta(days=1)
    return days

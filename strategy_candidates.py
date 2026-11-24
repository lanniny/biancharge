"""Predeclared 4-hour momentum and trend signals.

The grids below are the whole search. Walk-forward may pick among them on a
training window. It must not add new parameter values after seeing a test
window. Long and short are both allowed. Stops are ATR fractions known at the
signal bar.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from research_backtest import StrategySpec, desired_side


def baseline_side(closes: list[Decimal]) -> str | None:
    strategy = StrategySpec(
        name="baseline_sma_momentum",
        fast_window=20,
        slow_window=50,
        momentum_window=20,
        momentum_threshold=Decimal("0"),
        stop_pct=Decimal("0.08"),
    )
    # The daily baseline stays long-only. Swing presets below are long and short.
    return desired_side(closes, strategy, allow_short=False)


def true_range(high: Decimal, low: Decimal, previous_close: Decimal | None) -> Decimal:
    span = high - low
    if previous_close is None:
        return span
    return max(span, abs(high - previous_close), abs(low - previous_close))


def atr_at(highs: list[Decimal], lows: list[Decimal], closes: list[Decimal], index: int, window: int = 14) -> Decimal | None:
    """ATR ending at ``index`` using only bars up to that index."""
    if index + 1 < window or index >= len(closes):
        return None
    total = Decimal("0")
    for cursor in range(index - window + 1, index + 1):
        previous = closes[cursor - 1] if cursor else None
        total += true_range(highs[cursor], lows[cursor], previous)
    return total / Decimal(window)


# Frozen grids. Do not extend these after looking at test results.
ATR_BREAKOUT_GRID = (
    {"name": "atr_breakout_4h", "allow_short": True, "breakout": 20, "atr_multiple": Decimal("2.5"), "daily_sma": 50},
    {"name": "atr_breakout_4h", "allow_short": True, "breakout": 20, "atr_multiple": Decimal("3"), "daily_sma": 50},
    {"name": "atr_breakout_4h", "allow_short": True, "breakout": 55, "atr_multiple": Decimal("2.5"), "daily_sma": 50},
    {"name": "atr_breakout_4h", "allow_short": True, "breakout": 55, "atr_multiple": Decimal("3"), "daily_sma": 50},
)
TSMOM_GRID = (
    {"name": "tsmom_4h", "allow_short": True, "ema": 34, "atr_multiple": Decimal("2")},
    {"name": "tsmom_4h", "allow_short": True, "ema": 34, "atr_multiple": Decimal("3")},
    {"name": "tsmom_4h", "allow_short": True, "ema": 55, "atr_multiple": Decimal("2")},
    {"name": "tsmom_4h", "allow_short": True, "ema": 55, "atr_multiple": Decimal("3")},
)
DONCHIAN_GRID = (
    {"name": "donchian_trail_4h", "allow_short": True, "entry": 20, "exit": 10, "atr_multiple": Decimal("3"), "daily_sma": 100},
    {"name": "donchian_trail_4h", "allow_short": True, "entry": 55, "exit": 20, "atr_multiple": Decimal("3"), "daily_sma": 100},
)
SWING_GRIDS = {
    "atr_breakout_4h": ATR_BREAKOUT_GRID,
    "tsmom_4h": TSMOM_GRID,
    "donchian_trail_4h": DONCHIAN_GRID,
}

# Round 2 was specified after round 1 missed the gates. These six values are
# frozen before that run. Do not add or retune them after reading the result.
# Shorts require a completed weekly downtrend. Symmetric longs require a
# completed weekly uptrend. Asymmetric longs keep the daily SMA(50) filter.
# Confirmation and a minimum hold are there to cut turnover. ATR breakout is
# not in this grid.
_SLOW_LOOKBACKS = (48, 180, 540)
_SLOW_WEIGHTS = (Decimal("0.2"), Decimal("0.3"), Decimal("0.5"))
_SLOW_COMMON = {
    "allow_short": True,
    "atr_multiple": Decimal("3"),
    "weekly_sma": 20,
    "daily_sma": 50,
    "confirm_bars": 3,
    "min_hold_bars": 30,
    "ema": 180,
    "lookbacks": _SLOW_LOOKBACKS,
    "weights": _SLOW_WEIGHTS,
    "entry": 120,
    "exit": 40,
}


def _slow(name: str, trend_filter: str) -> dict:
    preset = dict(_SLOW_COMMON)
    preset["name"] = name
    preset["filter"] = trend_filter
    return preset


ROUND2_GRIDS = {
    "tsmom_slow_4h": (
        _slow("tsmom_slow_4h", "weekly_both"),
        _slow("tsmom_slow_4h", "daily_long_weekly_short"),
    ),
    "donchian_slow_4h": (
        _slow("donchian_slow_4h", "weekly_both"),
        _slow("donchian_slow_4h", "daily_long_weekly_short"),
    ),
    "combo_slow_4h": (
        _slow("combo_slow_4h", "weekly_both"),
        _slow("combo_slow_4h", "daily_long_weekly_short"),
    ),
}
SWING_GRIDS = {**SWING_GRIDS, **ROUND2_GRIDS}
SWING_STRATEGY_NAMES = frozenset(SWING_GRIDS)


def materialize_preset(raw: dict) -> dict:
    """Restore Decimals after a preset has been written to JSON."""
    preset = dict(raw)
    if "atr_multiple" in preset:
        preset["atr_multiple"] = Decimal(str(preset["atr_multiple"]))
    if "weights" in preset:
        preset["weights"] = tuple(Decimal(str(item)) for item in preset["weights"])
    if "lookbacks" in preset:
        preset["lookbacks"] = tuple(int(item) for item in preset["lookbacks"])
    for key in ("ema", "weekly_sma", "daily_sma", "confirm_bars", "min_hold_bars", "entry", "exit", "breakout"):
        if key in preset and preset[key] is not None:
            preset[key] = int(preset[key])
    if "allow_short" in preset:
        preset["allow_short"] = bool(preset["allow_short"])
    return preset


def preset_matches(grid_item: dict, raw: dict) -> bool:
    if set(grid_item) != set(raw):
        return False
    for key, value in grid_item.items():
        if not _value_eq(value, raw[key]):
            return False
    return True


def _value_eq(value, raw) -> bool:
    if isinstance(value, Decimal):
        return Decimal(str(raw)) == value
    if isinstance(value, tuple):
        if not isinstance(raw, (list, tuple)) or len(raw) != len(value):
            return False
        return all(_value_eq(left, right) for left, right in zip(value, raw))
    return raw == value


def daily_trend_up(daily_closes_before_today: list[Decimal], window: int = 50) -> bool:
    if len(daily_closes_before_today) < window:
        return False
    sample = daily_closes_before_today[-window:]
    average = sum(sample, Decimal("0")) / Decimal(window)
    return daily_closes_before_today[-1] > average


def atr_breakout_decision(
    highs: list[Decimal],
    lows: list[Decimal],
    closes: list[Decimal],
    index: int,
    *,
    breakout_window: int = 20,
    atr_window: int = 14,
    atr_multiple: Decimal = Decimal("2.5"),
    daily_closes_before_today: list[Decimal] | None = None,
) -> tuple[str | None, Decimal | None]:
    """Return (side, stop fraction) from completed bars through ``index`` only.

    ``side`` is ``long``, ``flat``, ``hold``, or None when the window is short.
    ``hold`` means the open position is left unchanged.
    """
    if index < 0 or index >= len(closes):
        return None, None
    if index < max(breakout_window, atr_window):
        return None, None
    volatility = atr_at(highs, lows, closes, index, atr_window)
    if volatility is None or volatility <= 0 or closes[index] <= 0:
        return None, None
    stop_fraction = (volatility * atr_multiple) / closes[index]
    prior_high = max(highs[index - breakout_window : index])
    slow = sum(closes[index - breakout_window + 1 : index + 1], Decimal("0")) / Decimal(breakout_window)
    trend_ok = daily_trend_up(daily_closes_before_today or [])
    if closes[index] > prior_high and trend_ok:
        return "long", stop_fraction
    if closes[index] < slow:
        return "flat", stop_fraction
    return "hold", stop_fraction


class SwingBook:
    """Causal 4h state. Completed UTC daily closes exclude the current day."""

    def __init__(self, preset: dict) -> None:
        if preset.get("allow_short") is not True:
            raise ValueError("4h momentum/trend presets must set allow_short=True")
        self.preset = preset
        self.highs: list[Decimal] = []
        self.lows: list[Decimal] = []
        self.closes: list[Decimal] = []
        self.completed_daily: list[Decimal] = []
        self.completed_weekly: list[Decimal] = []
        self._day = None
        self._day_close = None
        self._week = None
        self._week_close: Decimal | None = None
        self._ema: Decimal | None = None
        self._raw_history: list[str] = []
        self._held_side: str | None = None
        self._hold_until = 0

    def on_bar(self, ts: datetime, high: Decimal, low: Decimal, close: Decimal) -> tuple[str | None, Decimal | None]:
        day = ts.date()
        if self._day is None:
            self._day = day
        elif day != self._day:
            if self._day_close is not None:
                self.completed_daily.append(self._day_close)
            self._day = day
        week = day.isocalendar()[:2]
        if self._week is None:
            self._week = week
        elif week != self._week:
            if self._week_close is not None:
                self.completed_weekly.append(self._week_close)
            self._week = week
        self._day_close = close
        self._week_close = close
        self.highs.append(high)
        self.lows.append(low)
        self.closes.append(close)
        kind = self.preset["name"]
        if kind == "atr_breakout_4h":
            return self._breakout()
        if kind == "tsmom_4h":
            return self._tsmom()
        if kind == "donchian_trail_4h":
            return self._donchian()
        if kind == "tsmom_slow_4h":
            return self._finalize(*self._tsmom_raw())
        if kind == "donchian_slow_4h":
            return self._finalize(*self._donchian_raw())
        if kind == "combo_slow_4h":
            return self._finalize(*self._combo_raw())
        raise ValueError(kind)

    def _stop_fraction(self, multiple: Decimal) -> Decimal | None:
        index = len(self.closes) - 1
        volatility = atr_at(self.highs, self.lows, self.closes, index, 14)
        if volatility is None or self.closes[index] <= 0:
            return None
        return (volatility * multiple) / self.closes[index]

    def _daily_up(self, window: int) -> bool:
        return daily_trend_up(self.completed_daily, window)

    def _daily_down(self, window: int) -> bool:
        if len(self.completed_daily) < window:
            return False
        sample = self.completed_daily[-window:]
        average = sum(sample, Decimal("0")) / Decimal(window)
        return self.completed_daily[-1] < average

    def _breakout(self) -> tuple[str | None, Decimal | None]:
        window = int(self.preset["breakout"])
        multiple = Decimal(self.preset["atr_multiple"])
        daily_n = int(self.preset["daily_sma"])
        fraction = self._stop_fraction(multiple)
        index = len(self.closes) - 1
        if fraction is None or index < window:
            return None, None
        prior_high = max(self.highs[index - window : index])
        prior_low = min(self.lows[index - window : index])
        close = self.closes[index]
        if close > prior_high and self._daily_up(daily_n):
            return "long", fraction
        if close < prior_low and self._daily_down(daily_n):
            return "short", fraction
        return "flat", fraction

    def _tsmom(self) -> tuple[str | None, Decimal | None]:
        multiple = Decimal(self.preset["atr_multiple"])
        ema_n = int(self.preset["ema"])
        fraction = self._stop_fraction(multiple)
        closes = self.closes
        if fraction is None or len(closes) <= 120 or len(closes) < ema_n:
            return None, None
        weights = (Decimal("0.2"), Decimal("0.3"), Decimal("0.5"))
        lookbacks = (6, 30, 120)
        score = Decimal("0")
        for weight, lookback in zip(weights, lookbacks):
            previous = closes[-1 - lookback]
            if previous <= 0:
                return None, None
            change = closes[-1] - previous
            sign = Decimal("1") if change > 0 else Decimal("-1") if change < 0 else Decimal("0")
            score += weight * sign
        ema = self._ema_update(ema_n)
        if ema is None:
            return None, None
        if score > 0 and closes[-1] > ema:
            return "long", fraction
        if score < 0 and closes[-1] < ema:
            return "short", fraction
        return "flat", fraction

    def _ema_update(self, window: int) -> Decimal | None:
        if len(self.closes) < window:
            return None
        close = self.closes[-1]
        if len(self.closes) == window or self._ema is None:
            self._ema = sum(self.closes[-window:], Decimal("0")) / Decimal(window)
            return self._ema
        k = Decimal(2) / Decimal(window + 1)
        self._ema = close * k + self._ema * (Decimal(1) - k)
        return self._ema

    def _donchian(self) -> tuple[str | None, Decimal | None]:
        entry_n = int(self.preset["entry"])
        exit_n = int(self.preset["exit"])
        multiple = Decimal(self.preset["atr_multiple"])
        daily_n = int(self.preset["daily_sma"])
        fraction = self._stop_fraction(multiple)
        index = len(self.closes) - 1
        if fraction is None or index < entry_n:
            return None, None
        close = self.closes[index]
        prior_high = max(self.highs[index - entry_n : index])
        prior_low = min(self.lows[index - entry_n : index])
        exit_low = min(self.lows[index - exit_n : index])
        exit_high = max(self.highs[index - exit_n : index])
        if close > prior_high and self._daily_up(daily_n):
            return "long", fraction
        if close < prior_low and self._daily_down(daily_n):
            return "short", fraction
        if close < exit_low or close > exit_high:
            return "flat", fraction
        return "hold", fraction

    def _weekly_up(self) -> bool:
        window = int(self.preset["weekly_sma"])
        if len(self.completed_weekly) < window:
            return False
        sample = self.completed_weekly[-window:]
        average = sum(sample, Decimal("0")) / Decimal(window)
        return self.completed_weekly[-1] > average

    def _weekly_down(self) -> bool:
        window = int(self.preset["weekly_sma"])
        if len(self.completed_weekly) < window:
            return False
        sample = self.completed_weekly[-window:]
        average = sum(sample, Decimal("0")) / Decimal(window)
        return self.completed_weekly[-1] < average

    def _side_allowed(self, side: str) -> bool:
        mode = self.preset["filter"]
        if side == "short":
            return self._weekly_down()
        if side == "long" and mode == "weekly_both":
            return self._weekly_up()
        if side == "long":
            return self._daily_up(int(self.preset["daily_sma"]))
        return True

    def _finalize(self, raw: str | None, fraction: Decimal | None) -> tuple[str | None, Decimal | None]:
        """Confirm the filtered side, then keep a new position for min_hold_bars."""
        if raw is None or fraction is None:
            return None, None
        if raw in {"long", "short"} and not self._side_allowed(raw):
            raw = "flat"
        self._raw_history.append(raw)
        confirm = int(self.preset["confirm_bars"])
        recent = self._raw_history[-confirm:]
        if len(recent) < confirm or any(item != recent[-1] for item in recent):
            confirmed = "hold"
        else:
            confirmed = recent[-1]
        if confirmed == "hold":
            return "hold", fraction
        if self._held_side is not None and len(self.closes) <= self._hold_until and confirmed != self._held_side:
            return "hold", fraction
        if confirmed in {"long", "short"} and confirmed != self._held_side:
            self._held_side = confirmed
            self._hold_until = len(self.closes) + int(self.preset["min_hold_bars"]) - 1
        if confirmed == "flat":
            self._held_side = None
            self._hold_until = 0
        return confirmed, fraction

    def _tsmom_raw(self) -> tuple[str | None, Decimal | None]:
        multiple = Decimal(self.preset["atr_multiple"])
        ema_n = int(self.preset["ema"])
        fraction = self._stop_fraction(multiple)
        closes = self.closes
        lookbacks = tuple(int(item) for item in self.preset["lookbacks"])
        weights = tuple(Decimal(item) for item in self.preset["weights"])
        needed = max(lookbacks)
        if fraction is None or len(closes) <= needed or len(closes) < ema_n:
            return None, None
        score = Decimal("0")
        for weight, lookback in zip(weights, lookbacks):
            previous = closes[-1 - lookback]
            if previous <= 0:
                return None, None
            change = closes[-1] - previous
            sign = Decimal("1") if change > 0 else Decimal("-1") if change < 0 else Decimal("0")
            score += weight * sign
        ema = self._ema_update(ema_n)
        if ema is None:
            return None, None
        if score > 0 and closes[-1] > ema:
            return "long", fraction
        if score < 0 and closes[-1] < ema:
            return "short", fraction
        return "flat", fraction

    def _donchian_raw(self) -> tuple[str | None, Decimal | None]:
        entry_n = int(self.preset["entry"])
        exit_n = int(self.preset["exit"])
        fraction = self._stop_fraction(Decimal(self.preset["atr_multiple"]))
        index = len(self.closes) - 1
        if fraction is None or index < entry_n:
            return None, None
        close = self.closes[index]
        prior_high = max(self.highs[index - entry_n : index])
        prior_low = min(self.lows[index - entry_n : index])
        exit_low = min(self.lows[index - exit_n : index])
        exit_high = max(self.highs[index - exit_n : index])
        if close > prior_high:
            return "long", fraction
        if close < prior_low:
            return "short", fraction
        if close < exit_low or close > exit_high:
            return "flat", fraction
        return "hold", fraction

    def _combo_raw(self) -> tuple[str | None, Decimal | None]:
        trend_side, trend_fraction = self._tsmom_raw()
        channel_side, channel_fraction = self._donchian_raw()
        fraction = trend_fraction or channel_fraction
        if trend_side is None or channel_side is None or fraction is None:
            return None, None
        if trend_side == channel_side == "long":
            return "long", fraction
        if trend_side == channel_side == "short":
            return "short", fraction
        if trend_side == "flat" and channel_side == "flat":
            return "flat", fraction
        return "hold", fraction



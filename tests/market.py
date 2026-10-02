"""Synthetic multi-day market for feature / signal / look-ahead tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
import polars as pl

from orb.config import Config
from orb.context import ContextBuilder
from orb.data.schema import TZ, conform_minute
from orb.features import daily_context
from orb.synthetic import minute_session

ACTIONS_SCHEMA = {
    "symbol": pl.String,
    "ex_date": pl.Date,
    "action_type": pl.String,
    "price_factor": pl.Float64,
}


def weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def bars_from_arrays(symbol: str, day: date, o, h, lo, c, v) -> pl.DataFrame:
    start = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=15)
    n = len(c)
    return conform_minute(
        pl.DataFrame(
            {
                "symbol": symbol,
                "ts": [start + timedelta(minutes=i) for i in range(n)],
                "open": np.asarray(o, float),
                "high": np.asarray(h, float),
                "low": np.asarray(lo, float),
                "close": np.asarray(c, float),
                "volume": np.asarray(v, np.int64),
            }
        ).with_columns(pl.col("ts").dt.replace_time_zone(TZ))
    )


def flat_daily(symbol: str, days: list[date], close=100.0, rng=3.0) -> pl.DataFrame:
    """Constant bars: TR == rng every day -> Wilder ATR == rng exactly."""
    n = len(days)
    return pl.DataFrame(
        {
            "symbol": symbol,
            "date": days,
            "open": [close] * n,
            "high": [close + rng / 2] * n,
            "low": [close - rng / 2] * n,
            "close": [close] * n,
            "volume": [100_000] * n,
        }
    )


@dataclass
class Market:
    cfg: Config
    calendar: list[date]
    daily: pl.DataFrame
    minute: pl.DataFrame
    index_minute: pl.DataFrame
    actions: pl.DataFrame = field(default_factory=lambda: pl.DataFrame(schema=ACTIONS_SCHEMA))
    excluded: pl.DataFrame = field(
        default_factory=lambda: pl.DataFrame(schema={"symbol": pl.String, "date": pl.Date})
    )
    invalid_daily: pl.DataFrame = field(
        default_factory=lambda: pl.DataFrame(schema={"symbol": pl.String, "date": pl.Date})
    )
    index_invalid: set = field(default_factory=set)

    @property
    def symbols(self) -> list[str]:
        return sorted(self.minute["symbol"].unique().to_list())

    def membership(self) -> pl.DataFrame:
        return pl.DataFrame(
            {"symbol": self.symbols, "valid_from": self.calendar[0], "valid_to": None},
            schema_overrides={"valid_to": pl.Date},
        )

    def builder(self) -> ContextBuilder:
        ctx = daily_context(
            self.daily, self.actions, self.invalid_daily, self.calendar, self.cfg.features
        )
        m, im = self.minute, self.index_minute

        def load(frame):
            def f(symbols, a, b):
                return frame.filter(
                    pl.col("symbol").is_in(symbols) & pl.col("ts").dt.date().is_between(a, b)
                )

            return f

        return ContextBuilder(
            self.cfg,
            self.calendar,
            self.membership(),
            ctx,
            self.excluded,
            load(m),
            load(im),
            self.index_invalid,
        )


def random_market(
    cfg: Config, n_days=130, minute_days=36, symbols=("AAA", "BBB", "CCC", "DDD"), seed=7
) -> Market:
    cal = weekdays(date(2024, 1, 1), n_days)
    rng = np.random.default_rng(seed)
    daily, minute = [], []
    for i, s in enumerate(symbols):
        px = 200.0 * (i + 1)
        closes = px * np.exp(np.cumsum(rng.normal(0, 0.015, n_days)))
        rngs = closes * np.abs(rng.normal(0.025, 0.006, n_days))
        daily.append(
            pl.DataFrame(
                {
                    "symbol": s,
                    "date": cal,
                    "open": closes,
                    "high": closes + rngs / 2,
                    "low": closes - rngs / 2,
                    "close": closes,
                    "volume": 100_000,
                }
            )
        )
        for j, d in enumerate(cal[-minute_days:]):
            minute.append(
                minute_session(
                    s,
                    d,
                    open_price=float(closes[-minute_days + j - 1]),
                    seed=1000 * i + j,
                    vol_pct=0.0012,
                    base_volume=1000 + 300 * (j % 5),
                )
            )
    idx = [
        minute_session(n, d, open_price=20000.0, seed=50_000 + 7 * j + k, vol_pct=0.0004)
        for j, d in enumerate(cal[-minute_days:])
        for k, n in enumerate(("NIFTY 200", "NIFTY 50"))
    ]
    return Market(cfg, cal, pl.concat(daily), pl.concat(minute), pl.concat(idx))

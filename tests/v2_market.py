"""Synthetic one-day markets for V2 tests (prev close 100, ATR14 3 -> atr_pct 3%)."""

from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl

from orb.features import SessionArrays
from orb.v2.context import V2ContextBuilder
from tests.market import Market, bars_from_arrays, flat_daily, weekdays

N = 375  # 09:15..15:29
T = date(2024, 6, 28)  # tick 0.01 below Rs 250 from 2024-06-10


def slot_of(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m - (9 * 60 + 15)


def flat(price: float = 100.0) -> dict[str, np.ndarray]:
    o = np.full(N, price)
    return {"o": o.copy(), "h": o + 0.1, "l": o - 0.1, "c": o.copy()}


def candle(b: dict, hhmm: str, o: float, h: float, lo: float, c: float) -> None:
    i = slot_of(hhmm)
    b["o"][i], b["h"][i], b["l"][i], b["c"][i] = o, h, lo, c


def drop(b: dict, *hhmm: str) -> None:
    for t in hhmm:
        i = slot_of(t)
        for k in b:
            b[k][i] = np.nan


def arrays(b: dict) -> SessionArrays:
    return SessionArrays(b["o"], b["h"], b["l"], b["c"], np.where(np.isnan(b["c"]), 0, 1000.0))


CAL = weekdays(date(2024, 1, 1), 130)
assert CAL[-1] == T


def _minute(sym: str, day: date, b: dict) -> pl.DataFrame:
    ok = ~np.isnan(b["c"])
    return bars_from_arrays(sym, day, b["o"], b["h"], b["l"], b["c"], np.full(N, 1000)).filter(
        pl.Series(ok)
    )


def market_days(
    cfg,
    days: dict[date, dict[str, dict]],
    excluded: list[tuple[str, date]] = (),
    index: dict[date, dict] | None = None,
):
    """Minute bars on ``days`` only; flat daily bars (close 100, ATR 3) for every symbol."""
    syms = sorted({s for st in days.values() for s in st})
    daily = pl.concat([flat_daily(s, CAL, close=100.0, rng=3.0) for s in syms])
    minute = pl.concat([_minute(s, d, b) for d, st in days.items() for s, b in st.items()])
    imin = []
    for d in days:
        ib = (index or {}).get(d) or flat(20000.0)
        imin.append(
            bars_from_arrays("NIFTY 200", d, ib["o"], ib["h"], ib["l"], ib["c"], np.zeros(N))
        )
    m = Market(cfg, CAL, daily, minute, pl.concat(imin))
    m.excluded = pl.DataFrame(
        {
            "symbol": [s for s, _ in excluded],
            "date": [d for _, d in excluded],
            "reason": ["FNO_BAN"] * len(excluded),
        },
        schema={"symbol": pl.String, "date": pl.Date, "reason": pl.String},
    )
    return m


def market(cfg, stocks: dict[str, dict], excluded: list[str] = (), index: dict | None = None):
    return market_days(cfg, {T: stocks}, [(s, T) for s in excluded], {T: index} if index else None)


def builder(m: Market) -> V2ContextBuilder:
    return V2ContextBuilder.from_builder(m.builder())

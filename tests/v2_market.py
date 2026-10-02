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


def market(cfg, stocks: dict[str, dict], excluded: list[str] = (), index: dict | None = None):
    cal = weekdays(date(2024, 1, 1), 130)
    assert cal[-1] == T
    daily, minute = [], []
    for sym, b in stocks.items():
        daily.append(flat_daily(sym, cal, close=100.0, rng=3.0))
        ok = ~np.isnan(b["c"])
        minute.append(
            bars_from_arrays(sym, T, b["o"], b["h"], b["l"], b["c"], np.full(N, 1000)).filter(
                pl.Series(ok)
            )
        )
    ib = index or flat(20000.0)
    imin = bars_from_arrays("NIFTY 200", T, ib["o"], ib["h"], ib["l"], ib["c"], np.zeros(N))
    m = Market(cfg, cal, pl.concat(daily), pl.concat(minute), imin)
    m.excluded = pl.DataFrame(
        {
            "symbol": list(excluded),
            "date": [T] * len(excluded),
            "reason": ["FNO_BAN"] * len(excluded),
        },
        schema={"symbol": pl.String, "date": pl.Date, "reason": pl.String},
    )
    return m


def builder(m: Market) -> V2ContextBuilder:
    return V2ContextBuilder.from_builder(m.builder())

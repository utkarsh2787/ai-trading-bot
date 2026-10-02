"""Deterministic synthetic bars for tests and golden-day fixtures."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import polars as pl

from orb.data.schema import TZ, conform_daily, conform_minute


def minute_session(
    symbol: str,
    day: date,
    open_price: float = 1000.0,
    seed: int = 0,
    n: int = 375,
    base_volume: int = 1000,
    vol_pct: float = 0.0005,
) -> pl.DataFrame:
    """A clean 09:15..15:29 session (``n`` candles) as a random walk."""
    rng = np.random.default_rng(seed)
    closes = open_price * np.exp(np.cumsum(rng.normal(0, vol_pct, n)))
    opens = np.concatenate([[open_price], closes[:-1]])
    wick = np.abs(rng.normal(0, vol_pct, n)) * opens
    highs = np.maximum(opens, closes) + wick
    lows = np.minimum(opens, closes) - wick
    start = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=15)
    ts = [start + timedelta(minutes=i) for i in range(n)]
    df = pl.DataFrame(
        {
            "symbol": symbol,
            "ts": ts,
            "open": opens.round(2),
            "high": highs.round(2),
            "low": lows.round(2),
            "close": closes.round(2),
            "volume": rng.integers(base_volume // 2, base_volume * 2, n),
        }
    ).with_columns(pl.col("ts").dt.replace_time_zone(TZ))
    return conform_minute(df)


def daily_bars(
    symbol: str, start: date, n: int, price: float = 1000.0, seed: int = 0, vol_pct: float = 0.02
) -> pl.DataFrame:
    """``n`` consecutive weekday bars starting at ``start``."""
    rng = np.random.default_rng(seed)
    days: list[date] = []
    d = start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    closes = price * np.exp(np.cumsum(rng.normal(0, vol_pct, n)))
    opens = np.concatenate([[price], closes[:-1]]) * np.exp(rng.normal(0, vol_pct / 4, n))
    rng_ = np.abs(rng.normal(0, vol_pct, n))
    df = pl.DataFrame(
        {
            "symbol": symbol,
            "date": days,
            "open": opens.round(2),
            "high": (np.maximum(opens, closes) * (1 + rng_)).round(2),
            "low": (np.minimum(opens, closes) * (1 - rng_)).round(2),
            "close": closes.round(2),
            "volume": rng.integers(100_000, 1_000_000, n),
        }
    )
    return conform_daily(df)

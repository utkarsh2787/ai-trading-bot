"""Causal feature building blocks.

A session is represented as fixed-length arrays indexed by *slot* = minutes
since the first candle (09:15 -> 0, 15:29 -> 374). Candle ``t`` closes at
``t + 1 min``; every feature "at t" uses slots ``<= t`` of the trade date and
daily bars strictly before the trade date. Nothing here reads beyond that.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time

import numpy as np
import polars as pl

from orb.config import FeaturesConfig, SessionConfig
from orb.data.adjust import adjust_daily, with_adj_factor


def _mod(t: time) -> int:
    return t.hour * 60 + t.minute


def n_slots(s: SessionConfig) -> int:
    return _mod(s.last_candle) - _mod(s.first_candle) + 1


def slot(s: SessionConfig, t: time) -> int:
    return _mod(t) - _mod(s.first_candle)


# ------------------------------------------------------------- session arrays


@dataclass(frozen=True)
class SessionArrays:
    """OHLC (NaN where no candle) and volume (0 where no candle) by slot."""

    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray

    @property
    def present(self) -> np.ndarray:
        return ~np.isnan(self.close)

    def cum_volume(self) -> np.ndarray:
        return np.cumsum(self.volume)


def session_arrays(day_bars: pl.DataFrame, s: SessionConfig) -> SessionArrays:
    """One symbol-day of canonical minute bars -> slot arrays (out-of-session dropped)."""
    n = n_slots(s)
    arr = {k: np.full(n, np.nan) for k in ("open", "high", "low", "close")}
    vol = np.zeros(n)
    if day_bars.height:
        idx = (
            day_bars["ts"].dt.hour().cast(pl.Int32) * 60
            + day_bars["ts"].dt.minute().cast(pl.Int32)
            - _mod(s.first_candle)
        ).to_numpy()
        ok = (idx >= 0) & (idx < n)
        idx = idx[ok]
        for k in arr:
            arr[k][idx] = day_bars[k].to_numpy()[ok]
        vol[idx] = np.nan_to_num(day_bars["volume"].cast(pl.Float64).to_numpy()[ok])
    return SessionArrays(arr["open"], arr["high"], arr["low"], arr["close"], vol)


# ------------------------------------------------------------- opening range


@dataclass(frozen=True)
class OpeningRange:
    high: float
    low: float
    candles: int

    @property
    def width(self) -> float:
        return self.high - self.low


def opening_range(a: SessionArrays, s: SessionConfig) -> OpeningRange | None:
    lo, hi = slot(s, s.or_start), slot(s, s.or_end) + 1
    h, low = a.high[lo:hi], a.low[lo:hi]
    n = int(np.sum(~np.isnan(h)))
    if n == 0:
        return None
    return OpeningRange(float(np.nanmax(h)), float(np.nanmin(low)), n)


def first_breakout(a: SessionArrays, rng: OpeningRange, s: SessionConfig) -> tuple[int, str] | None:
    """First candle in the entry window closing strictly above OR_H (long) or
    strictly below OR_L (short). Returns (slot, side)."""
    for i in range(slot(s, s.entry_start), slot(s, s.entry_end) + 1):
        c = a.close[i]
        if np.isnan(c):
            continue
        if c > rng.high:
            return i, "long"
        if c < rng.low:
            return i, "short"
    return None


# ---------------------------------------------------------------------- index


def index_return(a: SessionArrays, t: int) -> float | None:
    """close_t / open_0915 - 1, using the last index close at or before t."""
    o = a.open[0]
    closes = a.close[: t + 1]
    seen = closes[~np.isnan(closes)]
    if np.isnan(o) or o <= 0 or seen.size == 0:
        return None
    return float(seen[-1] / o - 1)


# ------------------------------------------------------------------------ ATR


def wilder(tr: np.ndarray, n: int) -> np.ndarray:
    """Wilder smoothing seeded with the simple mean of the first n values."""
    out = np.full(tr.shape, np.nan)
    if tr.size >= n:
        out[n - 1] = tr[:n].mean()
        for i in range(n, tr.size):
            out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    pc = np.concatenate([[np.nan], close[:-1]])
    tr = np.fmax(high - low, np.fmax(np.abs(high - pc), np.abs(low - pc)))
    tr[0] = high[0] - low[0]
    return tr


def daily_context(
    daily_raw: pl.DataFrame,
    actions: pl.DataFrame,
    invalid_daily: pl.DataFrame,
    calendar: list[date],
    f: FeaturesConfig,
) -> pl.DataFrame:
    """Per (symbol, trade date T in calendar): ATR14 and previous close in T's
    own price terms, the factor F(T), and whether ATR may be used.

    * Bars with daily DQ errors (``invalid_daily``) are dropped before the
      Wilder recursion and count as missing.
    * ``atr_ok`` requires >= ``min_daily_bars`` valid bars and a valid bar on
      every one of the ``atr_period`` market trading days before T.
    """
    out_schema = {
        "symbol": pl.String,
        "date": pl.Date,
        "atr14": pl.Float64,
        "prev_close": pl.Float64,
        "adj_factor_t": pl.Float64,
        "n_bars": pl.Int64,
        "atr_ok": pl.Boolean,
        "atr_reason": pl.String,
    }
    valid = daily_raw.join(
        invalid_daily.select("symbol", "date"), on=["symbol", "date"], how="anti"
    )
    if valid.height == 0 or not calendar:
        return pl.DataFrame(schema=out_schema)
    adj = adjust_daily(valid, actions).sort("symbol", "date")
    parts = []
    for _, g in adj.group_by("symbol", maintain_order=True):
        tr = true_range(
            g["adj_high"].to_numpy(), g["adj_low"].to_numpy(), g["adj_close"].to_numpy()
        )
        parts.append(
            g.select("symbol", "date", "adj_close").with_columns(
                atr_adj=pl.Series(wilder(tr, f.atr_period)),
                n_bars=pl.int_range(1, pl.len() + 1, dtype=pl.Int64),
            )
        )
    bars = pl.concat(parts).sort("date")

    cal = pl.DataFrame({"date": calendar}, schema={"date": pl.Date}).with_row_index("ci")
    symbols = bars["symbol"].unique().sort()
    t_rows = (
        cal.join(pl.DataFrame({"symbol": symbols}), how="cross")
        .with_columns(_key=pl.col("date") - pl.duration(days=1))
        .sort("_key")
    )
    # last valid bar strictly before T
    last = t_rows.join_asof(
        bars.select(
            "symbol", _key=pl.col("date"), adj_close="adj_close", atr_adj="atr_adj", n_bars="n_bars"
        ),
        on="_key",
        by="symbol",
        strategy="backward",
        check_sortedness=False,
    )
    # valid bars in the atr_period trading days before T
    flags = bars.join(cal, on="date").select("symbol", "ci")
    n_cal = cal.height
    counts = []
    for (sym,), g in flags.group_by("symbol"):
        ind = np.zeros(n_cal, dtype=np.int64)
        ind[g["ci"].to_numpy()] = 1
        cs = np.concatenate([[0], np.cumsum(ind)])  # cs[k] = valid bars with ci < k
        k = np.arange(n_cal)
        lo = np.clip(k - f.atr_period, 0, None)
        cnt = cs[k] - cs[lo]
        full = (k >= f.atr_period) & (cnt == f.atr_period)
        counts.append(pl.DataFrame({"symbol": sym, "ci": k.astype(np.uint32), "_win_ok": full}))
    win = (
        pl.concat(counts)
        if counts
        else pl.DataFrame(schema={"symbol": pl.String, "ci": pl.UInt32, "_win_ok": pl.Boolean})
    )
    t = last.join(win, on=["symbol", "ci"], how="left")
    t = with_adj_factor(t.drop("_key"), actions).rename({"adj_factor": "adj_factor_t"})
    reason = (
        pl.when(pl.col("n_bars").is_null() | (pl.col("n_bars") < f.min_daily_bars))
        .then(pl.lit("INSUFFICIENT_HISTORY:daily_bars"))
        .when(~pl.col("_win_ok").fill_null(False))
        .then(pl.lit("INSUFFICIENT_HISTORY:missing_daily_bar"))
        .when(pl.col("atr_adj").is_null())
        .then(pl.lit("INSUFFICIENT_HISTORY:daily_bars"))
        .otherwise(None)
    )
    return (
        t.with_columns(
            atr14=pl.col("atr_adj") / pl.col("adj_factor_t"),
            prev_close=pl.col("adj_close") / pl.col("adj_factor_t"),
            atr_reason=reason,
        )
        .with_columns(atr_ok=pl.col("atr_reason").is_null())
        .filter(pl.col("n_bars").is_not_null())
        .select(list(out_schema))
        .sort("date", "symbol")
    )


# ------------------------------------------------------------------------- RV


def select_baseline_sessions(
    window_days: list[date], valid: set[date], f: FeaturesConfig
) -> list[date] | None:
    """Most recent ``rv_lookback`` valid sessions within ``window_days`` (the last
    ``rv_window_trading_days`` market trading days before T), or None if fewer
    than ``rv_min_valid_sessions``."""
    chosen = [d for d in window_days if d in valid][-f.rv_lookback :]
    return chosen if len(chosen) >= f.rv_min_valid_sessions else None


def rv_baseline(curves: list[np.ndarray], scales: list[float]) -> np.ndarray:
    """Mean cumulative-volume curve; each session rescaled to T's share basis."""
    return np.mean([c * s for c, s in zip(curves, scales, strict=True)], axis=0)


def relative_volume(today_cum: np.ndarray, baseline: np.ndarray, t: int) -> float | None:
    b = baseline[t]
    return None if b <= 0 else float(today_cum[t] / b)

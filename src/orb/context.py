"""Assemble everything the scanner needs for one trade date, causally.

``ContextBuilder.day(T)`` returns ``DayInputs`` built from
  * raw minute bars of T (stocks + index) - the scanner itself only reads
    slots <= the signal candle;
  * raw minute bars of past sessions (RV baseline), never T or later;
  * ATR / previous close from daily bars strictly before T (``daily_context``);
  * exclusions known for T (ban list, corporate actions, DQ, calendar).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import polars as pl

from orb.config import Config
from orb.data.reference import members_on
from orb.features import SessionArrays, rv_baseline, select_baseline_sessions, session_arrays

MinuteLoader = Callable[[list[str], date, date], pl.DataFrame]


@dataclass(frozen=True)
class StockDay:
    symbol: str
    bars: SessionArrays
    atr14: float | None
    prev_close: float | None
    history_reason: str | None  # None = ATR and RV baseline usable
    rv_base: np.ndarray | None
    rv_sessions: int


@dataclass(frozen=True)
class IndexDay:
    name: str
    bars: SessionArrays
    substituted: bool  # fallback index used instead of the primary


@dataclass(frozen=True)
class DayInputs:
    day: date
    stocks: list[StockDay]
    index: IndexDay | None
    no_data: list[str] = field(default_factory=list)  # members without minute bars on T


class ContextBuilder:
    def __init__(
        self,
        cfg: Config,
        calendar: list[date],
        membership: pl.DataFrame,
        daily_ctx: pl.DataFrame,
        excluded: pl.DataFrame,
        minute_loader: MinuteLoader,
        index_loader: MinuteLoader,
        index_invalid: set[tuple[str, date]] = frozenset(),
    ):
        """
        calendar:   market trading days (ascending)
        daily_ctx:  output of ``features.daily_context``
        excluded:   (symbol, date) stock-days that are neither traded nor used as
                    RV baseline sessions; whole-market excluded days must be
                    expanded to all symbols (or listed with symbol '*')
        """
        self.cfg = cfg
        self.calendar = calendar
        self.cal_index = {d: i for i, d in enumerate(calendar)}
        self.membership = membership
        self.ctx = {(r["symbol"], r["date"]): r for r in daily_ctx.to_dicts()}
        ex = excluded.select("symbol", "date")
        self.excluded_all = set(ex.filter(pl.col("symbol") == "*")["date"].to_list())
        self.excluded = {(s, d) for s, d in ex.filter(pl.col("symbol") != "*").iter_rows()}
        self.minute_loader = minute_loader
        self.index_loader = index_loader
        self.index_invalid = set(index_invalid)
        self._sessions: dict[tuple[str, date], SessionArrays | None] = {}
        self._loaded: set[tuple[str, tuple[int, int]]] = set()

    # ------------------------------------------------------------- caching
    def _ensure(self, symbols: list[str], days: list[date]) -> None:
        """Load whole calendar months of minute bars for ``symbols``. Bars of days
        after the trade date may sit in the cache but are never read for T."""
        for m in sorted({(d.year, d.month) for d in days}):
            need = [s for s in symbols if (s, m) not in self._loaded]
            if not need:
                continue
            mdays = [d for d in self.calendar if (d.year, d.month) == m]
            df = self.minute_loader(need, mdays[0], mdays[-1])
            by_key = {
                k: g for k, g in df.with_columns(_d=pl.col("ts").dt.date()).group_by("symbol", "_d")
            }
            for s in need:
                for d in mdays:
                    g = by_key.get((s, d))
                    self._sessions[(s, d)] = (
                        session_arrays(g.drop("_d"), self.cfg.session) if g is not None else None
                    )
                self._loaded.add((s, m))

    def _session(self, s: str, d: date) -> SessionArrays | None:
        if (s, d) not in self._sessions:  # evicted or never loaded
            self._loaded.discard((s, (d.year, d.month)))
            self._ensure([s], [d])
        return self._sessions.get((s, d))

    def _evict(self, before: date) -> None:
        for k in [k for k in self._sessions if k[1] < before]:
            del self._sessions[k]

    def _valid_session(self, s: str, d: date) -> bool:
        return d not in self.excluded_all and (s, d) not in self.excluded

    # ---------------------------------------------------------------- index
    def _index(self, day: date) -> IndexDay | None:
        names = [self.cfg.data.index.primary, self.cfg.data.index.fallback]
        for i, name in enumerate(names):
            if (name, day) in self.index_invalid:
                continue
            bars = session_arrays(self.index_loader([name], day, day), self.cfg.session)
            if not np.isnan(bars.open[0]):  # needs the 09:15 candle
                return IndexDay(name, bars, substituted=i > 0)
        return None

    # ------------------------------------------------------------------ day
    def day(self, day: date) -> DayInputs:
        k = self.cal_index[day]
        f = self.cfg.features
        window = self.calendar[max(0, k - f.rv_window_trading_days) : k]
        if window:
            self._evict(window[0])
        members = [s for s in members_on(self.membership, day) if self._valid_session(s, day)]
        if day in self.excluded_all:
            members = []
        self._ensure(members, window + [day])
        stocks, no_data = [], []
        for s in members:
            bars = self._session(s, day)
            if bars is None or not bars.present.any():
                no_data.append(s)
                continue
            c = self.ctx.get((s, day))
            reason = None
            atr = prev = None
            if c is None:
                reason = "INSUFFICIENT_HISTORY:daily_bars"
            else:
                atr, prev = c["atr14"], c["prev_close"]
                reason = c["atr_reason"]
            valid = {
                d for d in window if self._valid_session(s, d) and self._session(s, d) is not None
            }
            chosen = select_baseline_sessions(window, valid, f)
            base = None
            if chosen is None:
                reason = reason or "INSUFFICIENT_HISTORY:rv_sessions"
            else:
                f_t = c["adj_factor_t"] if c else 1.0
                curves, scales = [], []
                for d in chosen:
                    cd = self.ctx.get((s, d))
                    f_d = cd["adj_factor_t"] if cd else f_t
                    curves.append(self._session(s, d).cum_volume())
                    scales.append(f_t / f_d)  # volume_T(d) = volume(d) * F(T) / F(d)
                base = rv_baseline(curves, scales)
            stocks.append(
                StockDay(s, bars, atr, prev, reason, base, len(chosen) if chosen else len(valid))
            )
        return DayInputs(day, stocks, self._index(day), no_data)

"""Memoised market views shared by every V3 book and every null draw.

The actual books populate the caches; the 2,000 random-selection draws reuse
them, so every draw sees exactly the same fills, locks and valuations.
"""

from __future__ import annotations

from datetime import date

import polars as pl

from orb.features import slot
from orb.v3.data import V3Data
from orb.v3.fills import Exec, _first_present, buy_exec, sell_exec, value_price
from orb.v3.signal import rebalance_days, scan_rebalance


class Market:
    def __init__(self, data: V3Data):
        self.data = data
        self.cfg = data.cfg
        self.all_rebalances = rebalance_days(data.trading_days)
        self.prev_rebalance = {
            d: (self.all_rebalances[i - 1] if i else None)
            for i, d in enumerate(self.all_rebalances)
        }
        self._scan: dict[date, pl.DataFrame] = {}
        self._targets: dict[date, list[str]] = {}
        self._eligible: dict[date, list[str]] = {}
        self._buy: dict[tuple, Exec] = {}
        self._sell: dict[tuple, Exec] = {}
        self._value: dict[tuple, tuple] = {}
        self._open: dict[tuple, float | None] = {}

    def rebalances(self, start: date, end: date) -> list[date]:
        return [d for d in self.all_rebalances if start <= d <= end]

    def scan(self, d: date) -> pl.DataFrame:
        if d not in self._scan:
            df = scan_rebalance(self.data, d, self.prev_rebalance.get(d))
            self._scan[d] = df
            self._targets[d] = df.filter(pl.col("target")).sort("rank")["symbol"].to_list()
            self._eligible[d] = df.filter(pl.col("eligible")).sort("rank")["symbol"].to_list()
        return self._scan[d]

    def targets(self, d: date) -> list[str]:
        self.scan(d)
        return self._targets[d]

    def eligible(self, d: date) -> list[str]:
        self.scan(d)
        return self._eligible[d]

    def buy(self, s: str, d: date) -> Exec:
        k = (s, d)
        if k not in self._buy:
            self._buy[k] = buy_exec(self.data.session(s, d), self.data.guard(s, d), self.cfg)
        return self._buy[k]

    def sell(self, s: str, d: date, carry: bool = False, guard: bool = True) -> Exec:
        k = (s, d, carry, guard)
        if k not in self._sell:
            g = guard and self.data.guard(s, d)
            self._sell[k] = sell_exec(
                self.data.session(s, d), self.data.close(s, d), g, self.cfg, carry_window=carry
            )
        return self._sell[k]

    def value(self, s: str, d: date) -> tuple[float | None, str]:
        k = (s, d)
        if k not in self._value:
            prev = self.data.last_close_before(s, d)
            self._value[k] = value_price(
                self.data.session(s, d), prev[1] if prev else None, self.cfg
            )
        return self._value[k]

    def open_raw(self, s: str, d: date) -> float | None:
        """Label price: the 15:00 open (else the first candle to 15:05), no guard;
        the official close when the stock has no candle in that window."""
        k = (s, d)
        if k not in self._open:
            a = self.data.session(s, d)
            sl = self.cfg.session
            i = _first_present(a, slot(sl, sl.exec_candle), slot(sl, sl.exec_last)) if a else None
            self._open[k] = float(a.open[i]) if i is not None else self.data.close(s, d)
        return self._open[k]

    def precompute(self, days: list[date], forced_day: date | None = None) -> None:
        """Scan every rebalance day and memoise every member's fills, valuation and
        label price; 1-min sessions are evicted as the scan moves on, so memory
        holds about one month. Later misses (carried / deferred exits, stocks that
        left the index) are loaded lazily."""
        for d in days:
            df = self.scan(d)
            for s in df["symbol"].to_list():
                if self.data.session(s, d) is None:
                    continue
                self.buy(s, d)
                self.sell(s, d)
                self.value(s, d)
                self.open_raw(s, d)
                if d == forced_day:
                    self.sell(s, d, guard=False)
            self.data.evict_before(d)

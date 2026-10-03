"""Memoised market views shared by every V3 book and every null draw.

The actual books populate the caches; the 2,000 random-selection draws reuse
them, so every draw sees exactly the same fills, locks and valuations.
"""

from __future__ import annotations

from datetime import date

import polars as pl

from orb.v3.data import V3Data
from orb.v3.fills import Exec, buy_exec, sell_exec, value_price
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

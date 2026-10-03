"""Everything V3 reads, behind one object (``V3Data``).

* trading days   = the market calendar minus whole-market excluded sessions
                   (special sessions, calendar exceptions: never trading days)
* minute bars    = raw 1-min sessions via the shared ``ContextBuilder`` cache
* official bars  = raw bhavcopy daily bars (across renames)
* exclusions     = the V1/V2 reasons per stock-day, FNO_BAN dropped
* corporate actions (symbols mapped forward through renames): split/bonus
  factors, dividends (Rs per share parsed from the subject), demerger and rights
  ex-dates
"""

from __future__ import annotations

import bisect
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

import polars as pl

from orb.context import ContextBuilder, exclusion_category
from orb.data.reference import members_on
from orb.features import SessionArrays
from orb.refdata.fno import FnoCalendar, Renamer
from orb.v3.config import ConfigV3

FNO_BAN = "FNO_BAN"
_AMOUNT = re.compile(
    r"(?:(?:rs|re|s)\.?\s*)?(\d+(?:\.\d+)?)\s*(?:/-)?\s*per\s*sh", flags=re.IGNORECASE
)


def dividend_amount(subject: str | None) -> float | None:
    """Rs per share from an NSE corporate-action subject (sum of every amount);
    None when no amount is stated."""
    found = _AMOUNT.findall(subject or "")
    return sum(float(x) for x in found) if found else None


@dataclass
class V3Data:
    cfg: ConfigV3
    base: ContextBuilder
    daily: pl.DataFrame  # raw official bars: symbol, date, open, high, low, close
    actions: pl.DataFrame  # symbol, ex_date, action_type, price_factor, subject
    ticks: dict
    fno: FnoCalendar | None = None
    renames: pl.DataFrame | None = None
    bars: dict = field(init=False)
    hist: dict = field(init=False)
    splits: dict = field(init=False)
    dividends: dict = field(init=False)
    demergers: set = field(init=False)
    rights: set = field(init=False)
    trading_days: list = field(init=False)

    def __post_init__(self) -> None:
        self.bars = {
            (s, d): (o, h, lo, c)
            for s, d, o, h, lo, c in self.daily.select(
                "symbol", "date", "open", "high", "low", "close"
            ).iter_rows()
        }
        hist = defaultdict(list)
        for s, d in self.daily.select("symbol", "date").sort("symbol", "date").iter_rows():
            hist[s].append(d)
        self.hist = dict(hist)
        canon = Renamer(self.renames) if self.renames is not None and self.renames.height else None
        self.splits, self.dividends = defaultdict(list), {}
        self.demergers, self.rights = set(), set()
        cols = ["symbol", "ex_date", "action_type", "price_factor", "subject"]
        a = self.actions
        if "subject" not in a.columns:
            a = a.with_columns(subject=pl.lit(None, pl.String))
        for s, ex, kind, pf, subject in a.select(cols).iter_rows():
            if ex is None:
                continue
            s = canon(s, ex) if canon else s
            if kind in ("split", "bonus") and pf:
                self.splits[s].append((ex, float(pf)))
            elif kind == "dividend":
                amt = dividend_amount(subject)
                prev_amt, prev_ok = self.dividends.get((s, ex), (0.0, True))
                self.dividends[(s, ex)] = (prev_amt + (amt or 0.0), prev_ok and amt is not None)
            elif kind == "demerger":
                self.demergers.add((s, ex))
            elif kind == "rights":
                self.rights.add((s, ex))
        for s in self.splits:
            self.splits[s].sort()
        self.splits = dict(self.splits)
        self.trading_days = [d for d in self.base.calendar if d not in self.base.excluded_all]
        self.day_pos = {d: i for i, d in enumerate(self.trading_days)}

    # ----------------------------------------------------------- calendar
    def next_trading_day(self, d: date) -> date | None:
        i = bisect.bisect_right(self.trading_days, d)
        return self.trading_days[i] if i < len(self.trading_days) else None

    def prev_trading_day(self, d: date) -> date | None:
        i = bisect.bisect_left(self.trading_days, d) - 1
        return self.trading_days[i] if i >= 0 else None

    @property
    def data_end(self) -> date:
        return self.trading_days[-1]

    # ------------------------------------------------------------ universe
    def members(self, d: date) -> list[str]:
        return members_on(self.base.membership, d)

    def reasons(self, s: str, d: date) -> list[str]:
        return [r for r in self.base.reasons.get((s, d), []) if r != FNO_BAN]

    def dq_excluded(self, s: str, d: date) -> bool:
        return any(exclusion_category(r) == "EXCLUDED_DQ" for r in self.reasons(s, d))

    def blocks_buy(self, s: str, d: date) -> str | None:
        """Exclusion category that keeps ``s`` out of ranking / buys on ``d``."""
        cats = {exclusion_category(r) for r in self.reasons(s, d)}
        if not self.cfg.universe.corp_action_days_block_buys:
            cats.discard("EXCLUDED_CORP_ACTION")
        for c in ("EXCLUDED_SESSION", "EXCLUDED_DQ", "EXCLUDED_CORP_ACTION"):
            if c in cats:
                return c
        return None

    def guard(self, s: str, d: date) -> bool:
        if not self.cfg.circuit_guard.enabled:
            return False
        return not (self.fno is not None and self.fno.is_fno(s, d))

    # -------------------------------------------------------------- prices
    def prepare(self, symbols: list[str], d: date) -> None:
        """Batch-load the month of 1-min bars for ``symbols`` (cache)."""
        self.base._ensure(symbols, [d])

    def session(self, s: str, d: date) -> SessionArrays | None:
        a = self.base._session(s, d)
        return a if a is not None and a.present.any() else None

    def evict_before(self, d: date) -> None:
        self.base._evict(d)

    def close(self, s: str, d: date) -> float | None:
        b = self.bars.get((s, d))
        return b[3] if b else None

    def bars_before(self, s: str, d: date) -> int:
        return bisect.bisect_left(self.hist.get(s, []), d)

    def last_close_before(self, s: str, d: date) -> tuple[date, float] | None:
        h = self.hist.get(s, [])
        i = bisect.bisect_left(h, d) - 1
        return (h[i], self.bars[(s, h[i])][3]) if i >= 0 else None

    def factor_between(self, s: str, a: date, b: date) -> float:
        """Product of split/bonus price factors with ex-date in (a, b]."""
        f = 1.0
        for ex, pf in self.splits.get(s, []):
            if a < ex <= b:
                f *= pf
        return f

    def last_traded(self, s: str) -> date | None:
        h = self.hist.get(s)
        return h[-1] if h else None

    def delisted_on(self, s: str) -> date | None:
        """Last traded day when the daily history ends before the data does."""
        last = self.last_traded(s)
        return last if last is not None and last < self.data_end else None

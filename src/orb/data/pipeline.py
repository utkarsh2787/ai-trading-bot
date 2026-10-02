"""Build the canonical RAW store from vendor data.

Layout under ``data.root``::

    vendor/<provider>/...   as delivered (Kite: adjusted)
    raw/daily/...           NSE bhavcopy (stocks) / vendor daily (indices)
    raw/minute/...          raw minute bars (de-adjusted when the vendor is adjusted)

Everything downstream (DQ, features, backtest) reads ``raw/`` only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import polars as pl

from orb.data.deadjust import deadjust_factors, deadjust_minute
from orb.data.quality import concat_issues
from orb.data.store import ParquetStore


@dataclass
class Stores:
    vendor: ParquetStore
    raw: ParquetStore

    @classmethod
    def under(cls, root: str | Path, provider: str) -> Stores:
        root = Path(root)
        return cls(vendor=ParquetStore(root / "vendor" / provider), raw=ParquetStore(root / "raw"))


@dataclass
class BuildReport:
    symbols: int = 0
    minute_rows: int = 0
    issues: list[pl.DataFrame] = field(default_factory=list)

    def issue_frame(self) -> pl.DataFrame:
        return concat_issues(self.issues)


def build_raw_symbol(
    symbol: str,
    stores: Stores,
    start: date,
    end: date,
    price_basis: str,
    is_index: bool,
    tolerance: float,
    report: BuildReport,
) -> None:
    minute = stores.vendor.read_minute(symbol, start, end)
    if is_index:
        # indices have no corporate actions: vendor daily and minute are raw
        stores.raw.write_daily(stores.vendor.read_daily(symbol, start, end))
        stores.raw.write_minute(minute)
    elif price_basis == "raw":
        stores.raw.write_minute(minute)
    else:
        bhav = stores.raw.read_daily(symbol, start, end)
        vendor_daily = stores.vendor.read_daily(symbol, start, end)
        factors, issues = deadjust_factors(vendor_daily, bhav, tolerance)
        report.issues.append(issues)
        minute = deadjust_minute(minute, factors)
        stores.raw.write_minute(minute)
    report.symbols += 1
    report.minute_rows += minute.height

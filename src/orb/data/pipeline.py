"""Build the canonical RAW store from vendor data.

Layout under ``data.root``::

    vendor/<provider>/snapshots/<id>/...   as delivered (Kite: adjusted), immutable once frozen
    raw/daily/...           NSE bhavcopy (stocks) / vendor daily (indices)
    raw/minute/...          raw minute bars (de-adjusted when the vendor is adjusted)

Everything downstream (DQ, features, backtest) reads ``raw/`` only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import polars as pl

from orb.data.deadjust import DRIFT_SCHEMA, deadjust_factors, deadjust_minute, factor_drift
from orb.data.quality import concat_issues
from orb.data.store import ParquetStore


@dataclass
class Stores:
    vendor: ParquetStore
    raw: ParquetStore

    @classmethod
    def under(cls, root: str | Path, provider: str, snapshot: str = "default") -> Stores:
        root = Path(root)
        vendor = root / "vendor" / provider / "snapshots" / snapshot
        return cls(vendor=ParquetStore(vendor), raw=ParquetStore(root / "raw"))

    @classmethod
    def raw_only(cls, root: str | Path) -> ParquetStore:
        return ParquetStore(Path(root) / "raw")


BUILD_FILE = "BUILD.json"


def write_build_record(raw_root: str | Path, snapshot_id: str, snapshot_hash: str) -> None:
    """Pin the raw store to the frozen vendor snapshot it was built from."""
    import json
    from datetime import datetime

    p = Path(raw_root) / BUILD_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "snapshot_id": snapshot_id,
                "snapshot_hash": snapshot_hash,
                "built_at": datetime.now().isoformat(timespec="seconds"),
            },
            indent=1,
        )
    )


def read_build_record(raw_root: str | Path) -> dict | None:
    import json

    p = Path(raw_root) / BUILD_FILE
    return json.loads(p.read_text()) if p.exists() else None


@dataclass
class BuildReport:
    symbols: int = 0
    minute_rows: int = 0
    issues: list[pl.DataFrame] = field(default_factory=list)
    drift: list[pl.DataFrame] = field(default_factory=list)

    def issue_frame(self) -> pl.DataFrame:
        return concat_issues(self.issues)

    def drift_frame(self) -> pl.DataFrame:
        parts = [d for d in self.drift if d.height]
        return pl.concat(parts) if parts else pl.DataFrame(schema=DRIFT_SCHEMA)


def build_raw_symbol(
    symbol: str,
    stores: Stores,
    start: date,
    end: date,
    price_basis: str,
    is_index: bool,
    tolerance: float,
    report: BuildReport,
    actions: pl.DataFrame | None = None,
    drift_tolerance: float | None = None,
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
        if actions is not None and drift_tolerance is not None:
            d_issues, d_runs = factor_drift(
                factors, actions.filter(pl.col("symbol") == symbol), drift_tolerance
            )
            report.issues.append(d_issues)  # drifting days stay in raw but are DQ errors
            report.drift.append(d_runs)
        minute = deadjust_minute(minute, factors)
        stores.raw.write_minute(minute)
    report.symbols += 1
    report.minute_rows += minute.height

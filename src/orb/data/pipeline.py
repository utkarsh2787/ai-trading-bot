"""Build the canonical RAW store from vendor data.

Layout under ``data.root``::

    vendor/<provider>/snapshots/<id>/...   as delivered (Kite: adjusted), immutable once frozen
    raw/daily/...           NSE bhavcopy (stocks) / vendor daily (indices)
    raw/minute/...          raw minute bars (de-adjusted when the vendor is adjusted)

Everything downstream (DQ, features, backtest) reads ``raw/`` only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
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


def name_windows(symbol: str, renames: pl.DataFrame | None) -> list[tuple[str, date, date]]:
    """Which exchange symbol was in force when, along the rename chain through
    ``symbol``: [(name, from, to)] covering all time (inclusive bounds).

    Kite returns a renamed stock's whole history under today's symbol, while the
    bhavcopy lists each day under the name in force that day, so the bhavcopy
    must be read per window (e.g. LTI -> LTIM -> LTM)."""
    lo, hi = date(1900, 1, 1), date(2100, 12, 31)
    if renames is None or renames.height == 0:
        return [(symbol, lo, hi)]
    r = renames.select("old_symbol", "new_symbol", "effective_date")
    back = {n: (o, e) for o, n, e in r.iter_rows()}
    fwd = {o: (n, e) for o, n, e in r.iter_rows()}
    root, seen = symbol, {symbol}
    while root in back and back[root][0] not in seen:  # oldest name
        root = back[root][0]
        seen.add(root)
    chain = [(root, lo)]
    while chain[-1][0] in fwd and fwd[chain[-1][0]][0] not in {c for c, _ in chain}:
        new, eff = fwd[chain[-1][0]]
        chain.append((new, eff))
    out = []
    for i, (name, start) in enumerate(chain):
        end = chain[i + 1][1] - timedelta(days=1) if i + 1 < len(chain) else hi
        out.append((name, start, end))
    return out


def bhav_for(
    symbol: str, raw: ParquetStore, start: date, end: date, renames: pl.DataFrame | None
) -> pl.DataFrame:
    """Raw bhavcopy bars for ``symbol`` across renames, labelled ``symbol``."""
    parts = []
    for name, a, b in name_windows(symbol, renames):
        a, b = max(a, start), min(b, end)
        if a <= b:
            d = raw.read_daily(name, a, b)
            if d.height:
                parts.append(d.with_columns(symbol=pl.lit(symbol)))
    return pl.concat(parts).sort("date") if parts else raw.read_daily(symbol, start, end)


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
    renames: pl.DataFrame | None = None,
) -> None:
    minute = stores.vendor.read_minute(symbol, start, end)
    if is_index:
        # indices have no corporate actions: vendor daily and minute are raw
        stores.raw.write_daily(stores.vendor.read_daily(symbol, start, end))
        stores.raw.write_minute(minute)
    elif price_basis == "raw":
        stores.raw.write_minute(minute)
    else:
        bhav = bhav_for(symbol, stores.raw, start, end, renames)
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

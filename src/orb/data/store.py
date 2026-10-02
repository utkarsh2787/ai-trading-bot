"""Local Parquet cache.

Layout::

    <root>/minute/<symbol>/<year>.parquet
    <root>/daily/<symbol>.parquet

Symbols are percent-encoded for the filesystem ("M&M" -> "M%26M"). Writes merge
with existing data (new rows win on the same key) and are atomic.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from urllib.parse import quote, unquote

import polars as pl

from orb.data.schema import DAILY_SCHEMA, MINUTE_SCHEMA, conform_daily, conform_minute, empty


def _enc(symbol: str) -> str:
    return quote(symbol, safe="")


def _atomic_write(df: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.write_parquet(tmp, compression="zstd", statistics=True)
    os.replace(tmp, path)


def _merge(existing: Path, new: pl.DataFrame, key: str) -> pl.DataFrame:
    if existing.exists():
        new = pl.concat([pl.read_parquet(existing), new], how="vertical")
    return new.unique(subset=["symbol", key], keep="last", maintain_order=True).sort(key)


class ParquetStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    # ------------------------------------------------------------------ paths
    def minute_dir(self, symbol: str) -> Path:
        return self.root / "minute" / _enc(symbol)

    def daily_path(self, symbol: str) -> Path:
        return self.root / "daily" / f"{_enc(symbol)}.parquet"

    # ------------------------------------------------------------------ write
    def write_minute(self, df: pl.DataFrame) -> None:
        df = conform_minute(df)
        if df.height == 0:
            return
        df = df.with_columns(pl.col("ts").dt.year().alias("_year"))
        for (symbol, year), part in df.group_by("symbol", "_year", maintain_order=True):
            path = self.minute_dir(symbol) / f"{year}.parquet"
            _atomic_write(_merge(path, part.drop("_year"), "ts"), path)

    def write_daily(self, df: pl.DataFrame) -> None:
        df = conform_daily(df)
        for (symbol,), part in df.group_by("symbol", maintain_order=True):
            path = self.daily_path(symbol)
            _atomic_write(_merge(path, part, "date"), path)

    # ------------------------------------------------------------------- read
    def read_minute(self, symbol: str, start: date, end: date) -> pl.DataFrame:
        files = [self.minute_dir(symbol) / f"{y}.parquet" for y in range(start.year, end.year + 1)]
        files = [f for f in files if f.exists()]
        if not files:
            return empty(MINUTE_SCHEMA)
        return (
            pl.scan_parquet(files)
            .filter(pl.col("ts").dt.date().is_between(start, end))
            .sort("ts")
            .collect()
        )

    def read_daily(self, symbol: str, start: date, end: date) -> pl.DataFrame:
        path = self.daily_path(symbol)
        if not path.exists():
            return empty(DAILY_SCHEMA)
        return pl.read_parquet(path).filter(pl.col("date").is_between(start, end)).sort("date")

    def minute_dates(self, symbol: str) -> list[date]:
        """Distinct trade dates with at least one minute bar (reads only ``ts``)."""
        files = sorted(self.minute_dir(symbol).glob("*.parquet"))
        if not files:
            return []
        return (
            pl.scan_parquet(files)
            .select(pl.col("ts").dt.date().unique())
            .collect()
            .to_series()
            .sort()
            .to_list()
        )

    def symbols(self, kind: str) -> list[str]:
        base = self.root / kind
        if not base.exists():
            return []
        names = (p.name if kind == "minute" else p.stem for p in base.iterdir())
        return sorted(unquote(n) for n in names if not n.endswith(".tmp"))

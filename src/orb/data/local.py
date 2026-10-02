"""Vendor CSV/Parquet import, normalised to the canonical schema."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from orb import csvio
from orb.config import LocalConfig
from orb.data.provider import DataProvider, SymbolNotFound
from orb.data.schema import conform_daily, conform_minute


def _read_any(path: Path) -> pl.DataFrame:
    if path.suffix == ".parquet":
        return pl.read_parquet(path)
    if path.suffix in (".csv", ".txt"):
        return csvio.read_csv(path, try_parse_dates=False, infer_schema_length=10_000)
    raise ValueError(f"unsupported file type: {path}")


class LocalProvider(DataProvider):
    """One file per symbol, located via ``minute_glob`` / ``daily_glob``.

    Vendor columns are renamed with ``cfg.columns``; naive timestamps are
    interpreted in ``cfg.timezone``.
    """

    name = "local"

    def __init__(self, cfg: LocalConfig):
        self.cfg = cfg
        self.root = Path(cfg.root)

    def _file(self, pattern: str, symbol: str) -> Path:
        matches = sorted(self.root.glob(pattern.format(symbol=symbol)))
        if not matches:
            raise SymbolNotFound(f"no local file for {symbol!r} ({pattern})")
        if len(matches) > 1:
            raise ValueError(f"ambiguous local files for {symbol!r}: {matches}")
        return matches[0]

    def _load(self, pattern: str, symbol: str) -> pl.DataFrame:
        df = _read_any(self._file(pattern, symbol))
        df = df.rename({k: v for k, v in self.cfg.columns.items() if k in df.columns})
        if "symbol" not in df.columns:
            df = df.with_columns(symbol=pl.lit(symbol))
        return df

    def minute_bars(self, symbol: str, start: date, end: date) -> pl.DataFrame:
        df = self._load(self.cfg.minute_glob, symbol)
        if df.schema["ts"] == pl.String:
            df = df.with_columns(pl.col("ts").str.to_datetime(time_unit="us"))
        if df.schema["ts"].time_zone is None:  # type: ignore[union-attr]
            df = df.with_columns(pl.col("ts").dt.replace_time_zone(self.cfg.timezone))
        df = conform_minute(df)
        return df.filter(pl.col("ts").dt.date().is_between(start, end))

    def daily_bars(self, symbol: str, start: date, end: date) -> pl.DataFrame:
        df = conform_daily(self._load(self.cfg.daily_glob, symbol))
        return df.filter(pl.col("date").is_between(start, end))

"""NSE capital-market bhavcopy: official raw (unadjusted) daily OHLCV.

Two formats:
  * legacy  ``content/historical/EQUITIES/YYYY/MON/cmDDMONYYYYbhav.csv.zip``
    (last published 2024-07-05)
  * UDiFF   ``content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip``
    (from 2024-07-08; also available for some earlier dates)
"""

from __future__ import annotations

import io
import zipfile
from datetime import date

import polars as pl

from orb.data.schema import DAILY_SCHEMA, conform_daily, empty


def legacy_url(base: str, day: date) -> str:
    mon = day.strftime("%b").upper()
    return (
        f"{base}/content/historical/EQUITIES/{day.year}/{mon}/cm{day:%d}{mon}{day.year}bhav.csv.zip"
    )


def udiff_url(base: str, day: date) -> str:
    return f"{base}/content/cm/BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv.zip"


def urls_for(base: str, day: date, udiff_from: date) -> list[str]:
    """Preferred URL first, the other format as fallback."""
    a, b = legacy_url(base, day), udiff_url(base, day)
    return [b, a] if day >= udiff_from else [a, b]


def unzip_single(blob: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"expected one csv in bhavcopy zip, got {names}")
        return z.read(names[0])


def _keep_series(df: pl.DataFrame, series: list[str]) -> pl.DataFrame:
    # one row per symbol-day: first series in priority order
    prio = {s: i for i, s in enumerate(series)}
    return (
        df.filter(pl.col("series").is_in(series))
        .with_columns(_p=pl.col("series").replace_strict(prio, default=99))
        .sort("symbol", "date", "_p")
        .unique(subset=["symbol", "date"], keep="first", maintain_order=True)
        .drop("_p", "series")
    )


def parse(csv: bytes, series: list[str]) -> pl.DataFrame:
    """Either format -> canonical daily bars (raw prices) for the given series."""
    df = pl.read_csv(io.BytesIO(csv), infer_schema_length=0, truncate_ragged_lines=True)
    df = df.rename({c: c.strip() for c in df.columns})
    if "TckrSymb" in df.columns:  # UDiFF
        df = df.select(
            symbol=pl.col("TckrSymb").str.strip_chars(),
            series=pl.col("SctySrs").str.strip_chars(),
            date=pl.col("TradDt").str.strip_chars().str.to_date("%Y-%m-%d"),
            open=pl.col("OpnPric"),
            high=pl.col("HghPric"),
            low=pl.col("LwPric"),
            close=pl.col("ClsPric"),
            volume=pl.col("TtlTradgVol"),
        )
    elif "SYMBOL" in df.columns:  # legacy
        df = df.select(
            symbol=pl.col("SYMBOL").str.strip_chars(),
            series=pl.col("SERIES").str.strip_chars(),
            date=pl.col("TIMESTAMP").str.strip_chars().str.to_date("%d-%b-%Y"),
            open=pl.col("OPEN"),
            high=pl.col("HIGH"),
            low=pl.col("LOW"),
            close=pl.col("CLOSE"),
            volume=pl.col("TOTTRDQTY"),
        )
    else:
        raise ValueError(f"unrecognised bhavcopy columns: {df.columns[:8]}")
    df = df.with_columns(
        *(pl.col(c).str.strip_chars().cast(pl.Float64) for c in ("open", "high", "low", "close")),
        pl.col("volume").str.strip_chars().cast(pl.Float64).cast(pl.Int64),
    )
    out = _keep_series(df, series)
    return conform_daily(out) if out.height else empty(DAILY_SCHEMA)

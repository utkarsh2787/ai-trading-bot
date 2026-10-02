from datetime import date

import polars as pl

from orb.data.schema import MINUTE_SCHEMA
from orb.data.store import ParquetStore
from orb.synthetic import daily_bars, minute_session


def test_minute_roundtrip_partitions_by_year(tmp_path):
    store = ParquetStore(tmp_path)
    df = pl.concat(
        [
            minute_session("M&M", date(2023, 12, 29), seed=1),
            minute_session("M&M", date(2024, 1, 1), seed=2),
        ]
    )
    store.write_minute(df)
    files = sorted(p.name for p in store.minute_dir("M&M").iterdir())
    assert files == ["2023.parquet", "2024.parquet"]
    back = store.read_minute("M&M", date(2023, 12, 29), date(2024, 1, 1))
    assert back.schema == pl.Schema(MINUTE_SCHEMA)
    assert back.equals(df)
    assert store.symbols("minute") == ["M&M"]


def test_minute_merge_dedupes_with_new_rows_winning(tmp_path):
    store = ParquetStore(tmp_path)
    day = date(2024, 3, 1)
    df = minute_session("TCS", day, seed=1)
    store.write_minute(df)
    revised = df.head(10).with_columns(pl.col("volume") + 1)
    store.write_minute(revised)
    back = store.read_minute("TCS", day, day)
    assert back.height == df.height
    assert back.head(10)["volume"].to_list() == revised["volume"].to_list()
    assert back.tail(5).equals(df.tail(5))


def test_read_filters_date_range(tmp_path):
    store = ParquetStore(tmp_path)
    store.write_minute(
        pl.concat([minute_session("X", date(2024, 3, d), seed=d) for d in (4, 5, 6)])
    )
    got = store.read_minute("X", date(2024, 3, 5), date(2024, 3, 5))
    assert got["ts"].dt.date().unique().to_list() == [date(2024, 3, 5)]
    assert store.read_minute("NOPE", date(2024, 1, 1), date(2024, 1, 2)).height == 0


def test_daily_roundtrip_and_merge(tmp_path):
    store = ParquetStore(tmp_path)
    d = daily_bars("NIFTY 200", date(2024, 1, 1), 30)
    store.write_daily(d.head(20))
    store.write_daily(d.tail(15))  # overlaps 5 rows
    back = store.read_daily("NIFTY 200", date(2024, 1, 1), date(2024, 12, 31))
    assert back.equals(d)
    assert store.symbols("daily") == ["NIFTY 200"]

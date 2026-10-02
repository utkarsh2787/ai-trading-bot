"""Item 3: Kite candles are already split/bonus adjusted (prices AND volume).

These tests use REAL NSE bhavcopy prices for two Nifty 200 events:
  * IRCTC 1:5 split (face value 10 -> 2), ex-date 2021-10-28
  * SRF   4:1 bonus,                      ex-date 2021-10-13
The vendor series is simulated the way Kite delivers it (latest price basis,
2-decimal values); after de-adjustment the RAW store must hold the prices that
actually traded on each pre-event date.
"""

import os
from datetime import date, datetime
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from orb.data.adjust import adjust_daily
from orb.data.deadjust import deadjust_factors, deadjust_minute
from orb.data.pipeline import BuildReport, Stores, build_raw_symbol
from orb.data.schema import TZ, conform_minute
from orb.refdata import bhavcopy

FIX = Path(__file__).parent / "fixtures" / "nse"
ACTIONS = pl.DataFrame(
    {
        "symbol": ["IRCTC", "SRF"],
        "ex_date": [date(2021, 10, 28), date(2021, 10, 13)],
        "action_type": ["split", "bonus"],
        "price_factor": [0.2, 0.2],
    }
)


@pytest.fixture(scope="module")
def bhav() -> pl.DataFrame:
    return bhavcopy.parse((FIX / "real_events_bhav.csv").read_bytes(), ["EQ"])


def kite_like_daily(raw: pl.DataFrame) -> pl.DataFrame:
    """Latest-basis adjusted prices and volumes, as Kite returns them."""
    a = adjust_daily(raw, ACTIONS)
    return a.select(
        "symbol",
        "date",
        *(pl.col(f"adj_{c}").round(2).alias(c) for c in ("open", "high", "low", "close")),
        pl.col("adj_volume").round(0).cast(pl.Int64).alias("volume"),
    )


def raw_minute_for(day_row: dict, symbol: str, seed: int = 0) -> pl.DataFrame:
    """A 375-candle session whose OHLC/volume aggregate to the real daily bar."""
    rng = np.random.default_rng(seed)
    o, h, lo, c = (day_row[k] for k in ("open", "high", "low", "close"))
    path = np.interp(np.linspace(0, 1, 375), [0, 0.3, 0.6, 1], [o, h, lo, c])
    path = np.clip(np.round(path / 0.05) * 0.05, lo, h)
    path[112], path[225], path[-1] = h, lo, c  # hit the real extremes exactly
    opens = np.concatenate([[o], path[:-1]])
    vol = rng.multinomial(day_row["volume"], np.ones(375) / 375)
    start = datetime.combine(day_row["date"], datetime.min.time()).replace(hour=9, minute=15)
    return conform_minute(
        pl.DataFrame(
            {
                "symbol": symbol,
                "ts": pl.datetime_range(start, start.replace(hour=15, minute=29), "1m", eager=True),
                "open": opens,
                "high": np.maximum(opens, path),
                "low": np.minimum(opens, path),
                "close": path,
                "volume": vol,
            }
        ).with_columns(pl.col("ts").dt.replace_time_zone(TZ))
    )


def test_bhavcopy_fixture_is_the_real_event(bhav):
    irctc = bhav.filter(pl.col("symbol") == "IRCTC").sort("date")
    pre, ex = (
        irctc.filter(pl.col("date") == d).row(0, named=True)
        for d in (date(2021, 10, 27), date(2021, 10, 28))
    )
    assert pre["close"] == 4130.15 and ex["open"] == 817.0  # raw 1:5 drop
    srf = bhav.filter(pl.col("symbol") == "SRF")
    assert srf.filter(pl.col("date") == date(2021, 10, 12))["close"][0] == 12456.85


def test_kite_like_series_is_adjusted(bhav):
    k = kite_like_daily(bhav)
    pre = k.filter((pl.col("symbol") == "IRCTC") & (pl.col("date") == date(2021, 10, 27)))
    assert pre["close"][0] == pytest.approx(826.03)  # 4130.15 * 0.2


def test_factors_recover_event_ratio(bhav, cfg):
    factors, issues = deadjust_factors(kite_like_daily(bhav), bhav, cfg.data.deadjust_tolerance)
    assert issues.height == 0
    f = {(r["symbol"], r["date"]): r["ratio"] for r in factors.to_dicts()}
    assert f[("IRCTC", date(2021, 10, 27))] == pytest.approx(5.0, rel=1e-4)
    assert f[("IRCTC", date(2021, 10, 28))] == pytest.approx(1.0)
    assert f[("SRF", date(2021, 10, 12))] == pytest.approx(5.0, rel=1e-4)
    assert f[("SRF", date(2021, 10, 13))] == pytest.approx(1.0)


@pytest.mark.parametrize(
    "symbol, pre_day", [("IRCTC", date(2021, 10, 27)), ("SRF", date(2021, 10, 12))]
)
def test_raw_store_holds_actually_traded_prices(tmp_path, bhav, cfg, symbol, pre_day):
    stores = Stores.under(tmp_path, "kite")
    real = bhav.filter(pl.col("symbol") == symbol)
    kite_daily = kite_like_daily(real)
    day_row = real.filter(pl.col("date") == pre_day).row(0, named=True)
    raw_min = raw_minute_for(day_row, symbol)
    # vendor (Kite) minute = raw * latest-basis factor, as delivered
    f = adjust_daily(real, ACTIONS).filter(pl.col("date") == pre_day)["adj_factor"][0]
    kite_min = raw_min.with_columns(
        *((pl.col(c) * f).round(2) for c in ("open", "high", "low", "close")),
        (pl.col("volume") / f).round(0).cast(pl.Int64),
    )
    stores.vendor.write_daily(kite_daily)
    stores.vendor.write_minute(kite_min)
    stores.raw.write_daily(real)  # bhavcopy = raw daily
    report = BuildReport()
    build_raw_symbol(
        symbol,
        stores,
        date(2021, 10, 1),
        date(2021, 10, 31),
        "adjusted",
        False,
        cfg.data.deadjust_tolerance,
        report,
    )
    assert report.issue_frame().height == 0
    got = stores.raw.read_minute(symbol, pre_day, pre_day)
    # pre-event prices are in that date's own (pre-split/bonus) price terms
    assert got["open"][0] == pytest.approx(day_row["open"], abs=0.05)
    assert got["high"].max() == pytest.approx(day_row["high"], abs=0.05)
    assert got["low"].min() == pytest.approx(day_row["low"], abs=0.05)
    assert got["volume"].sum() == pytest.approx(day_row["volume"], rel=1e-3)
    assert np.allclose(got["close"].to_numpy(), raw_min["close"].to_numpy(), atol=0.05)


def test_inconsistent_vendor_day_flagged(bhav, cfg):
    k = kite_like_daily(bhav).with_columns(
        high=pl.when(pl.col("date") == date(2021, 10, 26))
        .then(pl.col("high") * 1.05)
        .otherwise("high")
    )
    factors, issues = deadjust_factors(k, bhav, cfg.data.deadjust_tolerance)
    assert set(issues["check"]) == {"deadjust_inconsistent"}
    assert set(issues["date"]) == {date(2021, 10, 26)}
    # the day is dropped from minute output rather than written with a bad factor
    assert date(2021, 10, 26) not in factors["date"].to_list()
    m = raw_minute_for(
        bhav.filter(pl.col("date") == date(2021, 10, 26)).row(0, named=True), "IRCTC"
    )
    assert deadjust_minute(m, factors.filter(pl.col("symbol") == "IRCTC")).height == 0


def test_unadjusted_vendor_gives_unit_ratio(bhav, cfg):
    factors, issues = deadjust_factors(bhav, bhav, cfg.data.deadjust_tolerance)
    assert issues.height == 0 and factors["ratio"].to_list() == pytest.approx([1.0] * bhav.height)


@pytest.mark.skipif("ORB_DATA_ROOT" not in os.environ, reason="needs real downloaded data")
def test_integration_real_kite_store_matches_bhavcopy(bhav):
    """Run after `orb download && orb build-raw`: ORB_DATA_ROOT=data uv run pytest -k integration"""
    raw = Stores.under(os.environ["ORB_DATA_ROOT"], "kite").raw
    for symbol, pre_day in (("IRCTC", date(2021, 10, 27)), ("SRF", date(2021, 10, 12))):
        m = raw.read_minute(symbol, pre_day, pre_day)
        if m.height == 0:
            pytest.skip(f"{symbol} {pre_day} not in store")
        real = bhav.filter((pl.col("symbol") == symbol) & (pl.col("date") == pre_day)).row(
            0, named=True
        )
        assert m["high"].max() == pytest.approx(real["high"], rel=0.002)
        assert m["low"].min() == pytest.approx(real["low"], rel=0.002)

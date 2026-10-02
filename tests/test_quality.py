from datetime import date

import polars as pl
import pytest

from orb.data.quality import (
    check_daily,
    check_minute,
    excluded_stock_days,
    reconcile_daily_minute,
)
from orb.synthetic import daily_bars, minute_session

DAY = date(2024, 3, 5)
NO_ACTIONS = pl.DataFrame(
    schema={
        "symbol": pl.String,
        "ex_date": pl.Date,
        "action_type": pl.String,
        "price_factor": pl.Float64,
    }
)


def _checks(df, cfg, **kw):
    issues = check_minute(df, cfg.dq, cfg.session, **kw)
    return {(r["check"], r["severity"]) for r in issues.to_dicts()}, issues


def test_clean_session_has_no_issues(cfg):
    found, _ = _checks(minute_session("A", DAY, seed=1), cfg)
    assert found == set()


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda d: pl.concat([d, d.slice(100, 1)]), ("duplicate_timestamp", "error")),
        (
            lambda d: d.with_columns(pl.col("ts") + pl.duration(hours=-5, minutes=-30)),
            ("outside_session", "error"),
        ),
        (
            lambda d: d.with_columns(pl.col("ts") + pl.duration(seconds=30)),
            ("off_minute_timestamp", "error"),
        ),
        (lambda d: d.filter(pl.int_range(pl.len()) % 10 != 3), ("missing_minutes", "warn")),
        (lambda d: d.filter(pl.int_range(pl.len()) % 3 != 0), ("missing_minutes", "error")),
        (lambda d: d.slice(10), ("late_start", "warn")),
        (lambda d: d.head(300), ("early_end", "warn")),
        (
            lambda d: d.with_columns(
                high=pl.when(pl.int_range(pl.len()) == 5).then(pl.col("low") - 1).otherwise("high")
            ),
            ("ohlc_inconsistent", "error"),
        ),
        (
            lambda d: d.with_columns(
                close=pl.when(pl.int_range(pl.len()) == 5).then(0.0).otherwise("close")
            ),
            ("nonpositive_price", "error"),
        ),
        (
            lambda d: d.with_columns(
                volume=pl.when(pl.int_range(pl.len()) == 5).then(-1).otherwise("volume")
            ),
            ("negative_volume", "error"),
        ),
        (
            lambda d: d.with_columns(
                volume=pl.when(pl.int_range(pl.len()) == 5).then(10**8).otherwise("volume")
            ),
            ("volume_spike", "warn"),
        ),
        (lambda d: d.with_columns(volume=pl.lit(0, pl.Int64)), ("no_volume_day", "error")),
        (
            lambda d: d.with_columns(
                volume=pl.when(pl.int_range(pl.len()) == 5).then(None).otherwise("volume")
            ),
            ("null_values", "error"),
        ),
    ],
)
def test_each_defect_detected(cfg, mutate, expected):
    found, _ = _checks(mutate(minute_session("A", DAY, seed=1)), cfg)
    assert expected in found


@pytest.mark.parametrize("volume", [pl.lit(0, pl.Int64), pl.lit(None, pl.Int64)])
def test_valid_index_day_not_excluded(cfg, volume):
    """Nifty 200 / Nifty 50 candles carry zero or missing volume; that is valid."""
    df = minute_session("NIFTY 200", DAY, seed=1).with_columns(volume=volume)
    found, issues = _checks(df, cfg, is_index=True)
    assert found == set()
    assert excluded_stock_days(issues).height == 0
    daily = pl.DataFrame(
        {
            "symbol": ["NIFTY 200"],
            "date": [DAY],
            "open": [df["open"][0]],
            "high": [df["high"].max()],
            "low": [df["low"].min()],
            "close": [df["close"][-1]],
            "volume": [0],
        }
    )
    assert check_daily(daily, NO_ACTIONS, cfg.dq, is_index=True).height == 0


def test_same_day_with_null_volume_is_error_for_stocks(cfg):
    df = minute_session("A", DAY, seed=1).with_columns(volume=pl.lit(None, pl.Int64))
    found, _ = _checks(df, cfg)
    assert ("null_values", "error") in found


def test_special_session_skips_completeness(cfg):
    df = minute_session("A", DAY, seed=1).head(60)
    found, _ = _checks(df, cfg, special_dates={DAY})
    assert found == set()


def test_excluded_stock_days_only_errors(cfg):
    good = minute_session("A", DAY, seed=1).slice(10)  # warn only
    bad = minute_session("B", DAY, seed=2)
    bad = pl.concat([bad, bad.head(1)])  # error
    _, issues = _checks(pl.concat([good, bad]), cfg)
    assert excluded_stock_days(issues).to_dicts() == [{"symbol": "B", "date": DAY}]


def _gapped(mult: float):
    d = daily_bars("A", date(2024, 1, 1), 10, vol_pct=0.001)
    gap_day = d["date"][5]
    d = d.with_columns(
        *(
            pl.when(pl.col("date") >= gap_day).then(pl.col(c) * mult).otherwise(pl.col(c)).alias(c)
            for c in ("open", "high", "low", "close")
        )
    )
    return d, gap_day


def _acts(day, factor, kind="bonus"):
    return pl.DataFrame(
        {"symbol": ["A"], "ex_date": [day], "action_type": [kind], "price_factor": [factor]}
    )


def test_news_gap_is_warning_and_not_excluded(cfg):
    d, gap_day = _gapped(1.15)  # +15% news gap, no corporate action
    issues = check_daily(d, NO_ACTIONS, cfg.dq)
    assert issues.filter(pl.col("check") == "news_gap")["date"].to_list() == [gap_day]
    assert set(issues["severity"]) == {"warn"}
    assert excluded_stock_days(issues).height == 0


def test_extreme_gap_without_action_is_error(cfg):
    d, gap_day = _gapped(0.70)  # -30%
    issues = check_daily(d, NO_ACTIONS, cfg.dq)
    assert issues.filter(pl.col("check") == "extreme_gap")["date"].to_list() == [gap_day]
    assert excluded_stock_days(issues)["date"].to_list() == [gap_day]


def test_known_bonus_explains_raw_gap(cfg):
    d, gap_day = _gapped(0.5)  # raw 1:1 bonus drop
    assert (
        check_daily(d, _acts(gap_day, 0.5), cfg.dq).filter(pl.col("severity") == "error").height
        == 0
    )


def test_price_looks_unadjusted_relative_to_known_ratio(cfg):
    d, gap_day = _gapped(0.5)  # 1:1 bonus in the data ...
    issues = check_daily(d, _acts(gap_day, 0.2), cfg.dq)  # ... but file says 1:4
    assert issues.filter(pl.col("check") == "adjustment_mismatch")["date"].to_list() == [gap_day]
    # data already adjusted by the vendor while our file applies the ratio again
    d_adj, _ = _gapped(1.0)
    issues = check_daily(d_adj, _acts(gap_day, 0.5), cfg.dq)
    assert issues.filter(pl.col("check") == "adjustment_mismatch").height == 1


def test_volume_spike_is_warning_only(cfg):
    df = minute_session("A", DAY, seed=1).with_columns(
        volume=pl.when(pl.int_range(pl.len()) == 20).then(10**8).otherwise("volume")
    )
    _, issues = _checks(df, cfg)
    assert issues["check"].to_list() == ["volume_spike"]
    assert excluded_stock_days(issues).height == 0


def test_daily_duplicates_and_bad_ohlc(cfg):
    d = daily_bars("A", date(2024, 1, 1), 5, vol_pct=0.001)
    d = pl.concat([d, d.tail(1)]).with_columns(
        low=pl.when(pl.col("date") == d["date"][1]).then(pl.col("high") * 2).otherwise("low")
    )
    checks = set(check_daily(d, NO_ACTIONS, cfg.dq)["check"])
    assert {"duplicate_date", "ohlc_inconsistent"} <= checks


def test_reconcile_daily_vs_minute(cfg):
    m = minute_session("A", DAY, seed=1)
    daily = pl.DataFrame(
        {
            "symbol": ["A"],
            "date": [DAY],
            "open": [m["open"][0]],
            "high": [m["high"].max()],
            "low": [m["low"].min()],
            "close": [m["close"][-1]],
            "volume": [m["volume"].sum()],
        }
    )
    assert reconcile_daily_minute(daily, m, cfg.dq).height == 0
    off = daily.with_columns(pl.col("high") * 1.05)
    assert reconcile_daily_minute(off, m, cfg.dq)["check"].to_list() == ["daily_minute_mismatch"]


def test_rebuilt_minute_hl_vs_official_is_an_error_beyond_half_percent(cfg):
    m = minute_session("A", DAY, seed=1)
    official = pl.DataFrame(
        {
            "symbol": ["A"],
            "date": [DAY],
            "open": [m["open"][0]],
            "high": [m["high"].max()],
            "low": [m["low"].min()],
            "close": [m["close"][-1]],
            "volume": [m["volume"].sum()],
        }
    )
    near = official.with_columns(pl.col("high") * 1.004)  # 0.4%: fine
    far = official.with_columns(pl.col("low") * 0.994)  # 0.6%: error
    assert reconcile_daily_minute(near, m, cfg.dq).height == 0
    r = reconcile_daily_minute(far, m, cfg.dq)
    assert r["severity"].to_list() == ["error"]
    assert excluded_stock_days(r).to_dicts() == [{"symbol": "A", "date": DAY}]

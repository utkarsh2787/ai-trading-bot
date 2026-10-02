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


def test_index_skips_volume_checks(cfg):
    df = minute_session("NIFTY 200", DAY, seed=1).with_columns(volume=pl.lit(0, pl.Int64))
    found, _ = _checks(df, cfg, is_index=True)
    assert found == set()


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


def test_daily_unexplained_gap_vs_corporate_action(cfg):
    d = daily_bars("A", date(2024, 1, 1), 10, vol_pct=0.001)
    gap_day = d["date"][5]
    d = d.with_columns(
        *(
            pl.when(pl.col("date") >= gap_day).then(pl.col(c) * 0.5).otherwise(pl.col(c)).alias(c)
            for c in ("open", "high", "low", "close")
        )
    )
    issues = check_daily(d, NO_ACTIONS, cfg.dq)
    assert issues.filter(pl.col("check") == "unexplained_gap")["date"].to_list() == [gap_day]
    acts = pl.DataFrame(
        {"symbol": ["A"], "ex_date": [gap_day], "action_type": ["bonus"], "price_factor": [0.5]}
    )
    assert check_daily(d, acts, cfg.dq).filter(pl.col("check") == "unexplained_gap").height == 0


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

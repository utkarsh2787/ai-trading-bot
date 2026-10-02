"""Decision 65: Kite 1-min volume is unadjusted before some splits/bonuses.

Real daily totals around two Nifty 200 splits (bhavcopy quantity and the sum of
Kite 1-min volume, from the frozen 2026-10-02 snapshot):
  * EICHERMOT 1:10 split (price factor 0.1), ex-date 2020-08-24
  * CHOLAFIN  1:5  split (price factor 0.2), ex-date 2019-06-14
"""

from datetime import date, datetime

import polars as pl

from orb.data.deadjust import fix_unadjusted_volume
from orb.data.quality import VOLUME_BAND, check_volume
from orb.data.schema import conform_minute

EICHER = [  # date, bhav qty, sum of 1-min volume
    (date(2020, 8, 19), 285484, 28478),
    (date(2020, 8, 20), 224320, 22322),
    (date(2020, 8, 21), 261318, 26025),
    (date(2020, 8, 24), 11491732, 11481991),
    (date(2020, 8, 25), 4757114, 4738349),
]
CHOLA = [
    (date(2019, 6, 11), 273246, 54054),
    (date(2019, 6, 12), 279403, 55456),
    (date(2019, 6, 13), 370098, 73940),
    (date(2019, 6, 14), 1035738, 1033164),
    (date(2019, 6, 17), 605090, 599626),
]
ACTIONS = pl.DataFrame(
    {
        "symbol": ["EICHERMOT", "CHOLAFIN", "EICHERMOT"],
        "ex_date": [date(2020, 8, 24), date(2019, 6, 14), date(2020, 8, 10)],
        "action_type": ["split", "split", "dividend"],
        "price_factor": [0.1, 0.2, 0.99],
    }
)


def frames(sym, rows):
    minute, bhav = [], []
    for d, qty, mvol in rows:
        a, b = mvol // 3, mvol - 2 * (mvol // 3)  # three candles
        for i, v in enumerate((a, a, b)):
            ts = datetime(d.year, d.month, d.day, 9, 15 + i)
            minute.append((sym, ts, 100.0, 101.0, 99.0, 100.0, v))
        bhav.append((sym, d, 100.0, 101.0, 99.0, 100.0, qty))
    m = pl.DataFrame(
        minute,
        orient="row",
        schema={
            "symbol": pl.String,
            "ts": pl.Datetime("us"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
        },
    )
    m = conform_minute(m)
    d = pl.DataFrame(
        bhav,
        orient="row",
        schema={
            "symbol": pl.String,
            "date": pl.Date,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
        },
    )
    return m, d


def ratios(m, d):
    agg = m.group_by(date=pl.col("ts").dt.date()).agg(mvol=pl.col("volume").sum())
    j = d.join(agg, on="date").sort("date")
    return dict(zip(j["date"], (j["mvol"] / j["volume"]).to_list(), strict=True))


def run(sym, rows):
    m, d = frames(sym, rows)
    assert check_volume(d, m).height == 3  # the three pre-split days fail the band
    fixed, log = fix_unadjusted_volume(m, d, ACTIONS.filter(pl.col("symbol") == sym))
    return m, d, fixed, log


def test_eichermot_pre_split_days_corrected():
    m, d, fixed, log = run("EICHERMOT", EICHER)
    assert log["date"].to_list() == [r[0] for r in EICHER[:3]]
    assert log["F"].to_list() == [0.1] * 3
    r = ratios(fixed, d)
    for day, *_ in EICHER[:3]:
        assert abs(r[day] - 1) < 0.01
    # ex-date and later untouched; the dividend factor plays no part
    after = fixed.filter(pl.col("ts").dt.date() >= date(2020, 8, 24))
    assert after.equals(m.filter(pl.col("ts").dt.date() >= date(2020, 8, 24)))
    assert check_volume(d, fixed).height == 0


def test_cholafin_pre_split_days_corrected():
    m, d, fixed, log = run("CHOLAFIN", CHOLA)
    assert log["date"].to_list() == [r[0] for r in CHOLA[:3]]
    assert log["F"].to_list() == [0.2] * 3
    r = ratios(fixed, d)
    for day, *_ in CHOLA[:3]:
        assert abs(r[day] - 1) < 0.015
    assert fixed.filter(pl.col("ts").dt.date() >= date(2019, 6, 14)).equals(
        m.filter(pl.col("ts").dt.date() >= date(2019, 6, 14))
    )
    assert check_volume(d, fixed).height == 0


def test_pre_split_day_already_adjusted_is_left_alone():
    # Kite DID adjust this day's volume: ratio ~1, far from F=0.1 -> no correction
    rows = [(date(2020, 8, 19), 285484, 284000), (date(2020, 8, 24), 11491732, 11481991)]
    m, d = frames("EICHERMOT", rows)
    fixed, log = fix_unadjusted_volume(m, d, ACTIONS.filter(pl.col("symbol") == "EICHERMOT"))
    assert log.height == 0 and fixed.equals(m)


def test_ratio_more_than_5pct_from_factor_not_corrected():
    rows = [(date(2020, 8, 19), 285484, 31000)]  # 0.1086: 8.6% off F
    m, d = frames("EICHERMOT", rows)
    fixed, log = fix_unadjusted_volume(m, d, ACTIONS.filter(pl.col("symbol") == "EICHERMOT"))
    assert log.height == 0
    assert check_volume(d, fixed)["check"].to_list() == ["volume_mismatch"]


def test_cumulative_factor_over_two_later_events():
    acts = pl.DataFrame(
        {
            "symbol": ["X", "X"],
            "ex_date": [date(2021, 1, 4), date(2022, 1, 4)],
            "action_type": ["split", "bonus"],
            "price_factor": [0.5, 0.5],
        }
    )
    rows = [(date(2020, 12, 1), 1000, 250), (date(2021, 6, 1), 1000, 500)]
    m, d = frames("X", rows)
    fixed, log = fix_unadjusted_volume(m, d, acts)
    assert log["F"].to_list() == [0.25, 0.5]
    assert all(abs(v - 1) < 0.01 for v in ratios(fixed, d).values())


def test_volume_band_edges():
    lo, hi = VOLUME_BAND
    assert (lo, hi) == (0.80, 1.02)
    rows = [
        (date(2023, 1, 2), 1000, 800),
        (date(2023, 1, 3), 1000, 1020),
        (date(2023, 1, 4), 1000, 790),
        (date(2023, 1, 5), 1000, 1030),
    ]
    m, d = frames("X", rows)
    bad = check_volume(d, m)
    assert bad["date"].to_list() == [date(2023, 1, 4), date(2023, 1, 5)]
    assert set(bad["severity"]) == {"error"}

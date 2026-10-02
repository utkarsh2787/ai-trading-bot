from datetime import date, timedelta

import polars as pl
import pytest

from orb.sim.ticks import daily_ticks, round_to_tick, tick_for


def _daily(closes: dict[date, float], symbol="A"):
    return pl.DataFrame({"symbol": symbol, "date": list(closes), "close": list(closes.values())})


@pytest.mark.parametrize(
    "day, ref, tick",
    [
        (date(2024, 6, 7), 100.0, 0.05),  # before 2024-06-10: flat 0.05
        (date(2024, 6, 7), 30000.0, 0.05),
        (date(2024, 6, 10), 249.99, 0.01),
        (date(2024, 6, 10), 250.0, 0.05),
        (date(2025, 4, 14), 3000.0, 0.05),  # last day of the 2024-06-10 regime
        (date(2025, 4, 15), 249.0, 0.01),
        (date(2025, 4, 15), 250.0, 0.05),
        (date(2025, 4, 15), 1000.0, 0.05),
        (date(2025, 4, 15), 1000.05, 0.10),
        (date(2025, 4, 15), 5000.0, 0.10),
        (date(2025, 4, 15), 5000.5, 0.50),
        (date(2025, 4, 15), 10000.0, 0.50),
        (date(2025, 4, 15), 20000.0, 1.00),
        (date(2025, 4, 15), 20001.0, 5.00),
    ],
)
def test_bands_and_regimes(cfg, day, ref, tick):
    assert tick_for(cfg.ticks, day, ref) == tick


def test_band_crossing_mid_month_waits_for_next_month(cfg):
    # May 2025 closes below 1000; crosses above 1000 on 2025-06-12.
    days = [date(2025, 5, 29), date(2025, 5, 30)]
    days += [
        date(2025, 6, 2) + timedelta(days=i)
        for i in range(29)
        if (date(2025, 6, 2) + timedelta(days=i)).weekday() < 5
    ]
    days += [date(2025, 7, 1), date(2025, 7, 2)]
    closes = {d: (990.0 if d < date(2025, 6, 12) else 1100.0) for d in days}
    t = daily_ticks(_daily(closes), cfg.ticks)
    june = t.filter(pl.col("date").dt.month() == 6)
    assert june["tick"].unique().to_list() == [0.05]  # whole June: May-end close 990
    assert june["ref_date"].unique().to_list() == [date(2025, 5, 30)]
    july = t.filter(pl.col("date").dt.month() == 7)
    assert july["tick"].unique().to_list() == [0.10]  # July: June-end close 1100
    assert july["ref_close"].unique().to_list() == [1100.0]


def test_reference_uses_raw_previous_month_even_after_gap_month(cfg):
    # no trading in June (suspension): July uses the last close before July (May)
    t = daily_ticks(_daily({date(2025, 5, 30): 300.0, date(2025, 7, 1): 200.0}), cfg.ticks)
    jul = t.filter(pl.col("date") == date(2025, 7, 1)).row(0, named=True)
    assert jul["ref_date"] == date(2025, 5, 30) and jul["tick"] == 0.05
    first = t.filter(pl.col("date") == date(2025, 5, 30)).row(0, named=True)
    assert first["tick"] is None  # no prior close


def test_round_to_tick_adverse():
    assert round_to_tick(100.031, 0.05, "buy") == 100.05
    assert round_to_tick(100.031, 0.05, "sell") == 100.0
    assert round_to_tick(100.05, 0.05, "buy") == 100.05  # already on grid
    assert round_to_tick(1234.56, 0.10, "sell") == 1234.5

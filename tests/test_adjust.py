from datetime import date

import polars as pl
import pytest

from orb.data.adjust import adjust_daily, rebase_price, rebase_volume, with_adj_factor

ACTIONS_SCHEMA = {
    "symbol": pl.String,
    "ex_date": pl.Date,
    "action_type": pl.String,
    "price_factor": pl.Float64,
}


def _daily(prices, vols, start_day=1):
    n = len(prices)
    return pl.DataFrame(
        {
            "symbol": ["A"] * n,
            "date": [date(2024, 1, start_day + i) for i in range(n)],
            "open": prices,
            "high": prices,
            "low": prices,
            "close": prices,
            "volume": vols,
        }
    )


def _actions(rows):
    return pl.DataFrame(rows, schema=ACTIONS_SCHEMA, orient="row")


def test_split_and_bonus_factors():
    # 1:5 split ex 2024-01-03, 1:1 bonus ex 2024-01-05
    daily = _daily([1000, 1000, 200, 200, 100], [10, 10, 50, 50, 100])
    acts = _actions(
        [
            ("A", date(2024, 1, 3), "split", 0.2),
            ("A", date(2024, 1, 5), "bonus", 0.5),
            ("A", date(2024, 1, 2), "dividend", None),
            ("B", date(2024, 1, 2), "split", 0.1),
        ]
    )
    adj = adjust_daily(daily, acts)
    assert adj["adj_factor"].to_list() == pytest.approx([0.1, 0.1, 0.5, 0.5, 1.0])
    assert adj["adj_close"].to_list() == pytest.approx([100.0] * 5)
    assert adj["adj_volume"].to_list() == pytest.approx([100.0] * 5)
    assert adj["date"].to_list() == daily["date"].to_list()  # order preserved


def test_rebase_to_trade_date_basis_is_point_in_time():
    """A split after T must not change T-basis prices: future info cannot leak."""
    daily = _daily([1000, 1000, 1000, 200, 200], [10, 10, 10, 50, 50])
    t = date(2024, 1, 3)  # before the split (ex 2024-01-04)

    def t_basis(acts):
        a = adjust_daily(daily, acts)
        f_t = a.filter(pl.col("date") == t)["adj_factor"][0]
        hist = a.filter(pl.col("date") <= t)
        return (
            hist.select(rebase_price(pl.col("adj_close"), f_t))["adj_close"].to_list(),
            hist.select(rebase_volume(pl.col("adj_volume"), f_t))["adj_volume"].to_list(),
        )

    no_split = t_basis(_actions([]))
    with_future_split = t_basis(_actions([("A", date(2024, 1, 4), "split", 0.2)]))
    assert with_future_split[0] == pytest.approx(no_split[0])
    assert no_split[0] == pytest.approx([1000.0] * 3)
    assert with_future_split[1] == pytest.approx(no_split[1])


def test_with_adj_factor_on_minute_like_rows_no_actions():
    df = _daily([1, 2], [1, 1])
    assert with_adj_factor(df, _actions([]))["adj_factor"].to_list() == [1.0, 1.0]

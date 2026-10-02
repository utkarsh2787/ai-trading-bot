from datetime import date

import numpy as np
import polars as pl
import pytest

from orb.features import (
    daily_context,
    first_breakout,
    index_return,
    opening_range,
    relative_volume,
    rv_baseline,
    select_baseline_sessions,
    session_arrays,
    true_range,
    wilder,
)
from tests.market import ACTIONS_SCHEMA, bars_from_arrays, flat_daily, weekdays

N = 375


# ------------------------------------------------------------------- ATR


def test_wilder_hand_computed():
    out = wilder(np.array([1.0, 2.0, 3.0, 6.0, 9.0]), 3)
    assert np.isnan(out[:2]).all()
    assert out[2] == pytest.approx(2.0)  # seed = mean(1,2,3)
    assert out[3] == pytest.approx((2.0 * 2 + 6.0) / 3)  # 3.3333
    assert out[4] == pytest.approx((out[3] * 2 + 9.0) / 3)


def test_true_range_uses_previous_close():
    tr = true_range(
        np.array([10.0, 12.0, 11.0]), np.array([9.0, 11.0, 7.0]), np.array([9.5, 11.5, 8.0])
    )
    assert tr.tolist() == [1.0, 2.5, 4.5]  # max(H-L, |H-pc|, |L-pc|)


def _ctx(daily, cal, cfg, actions=None, invalid=None):
    actions = actions if actions is not None else pl.DataFrame(schema=ACTIONS_SCHEMA)
    invalid = (
        invalid
        if invalid is not None
        else pl.DataFrame(schema={"symbol": pl.String, "date": pl.Date})
    )
    return daily_context(daily, actions, invalid, cal, cfg.features)


def test_daily_context_constant_atr_and_prev_close(cfg):
    cal = weekdays(date(2024, 1, 1), 120)
    ctx = _ctx(flat_daily("A", cal), cal, cfg)
    last = ctx.filter(pl.col("date") == cal[-1]).row(0, named=True)
    assert last["atr14"] == pytest.approx(3.0) and last["prev_close"] == 100.0
    assert last["atr_ok"] and last["n_bars"] == 119  # bars strictly before T
    early = ctx.filter(pl.col("date") == cal[50]).row(0, named=True)
    assert not early["atr_ok"] and early["atr_reason"] == "INSUFFICIENT_HISTORY:daily_bars"


def test_daily_context_missing_or_invalid_bar_blocks_atr(cfg):
    cal = weekdays(date(2024, 1, 1), 130)
    gap = cal[-5]
    daily = flat_daily("A", cal).filter(pl.col("date") != gap)
    ctx = _ctx(daily, cal, cfg)
    bad = ctx.filter(pl.col("date").is_in(cal[-4:]))  # windows containing the gap
    assert set(bad["atr_reason"]) == {"INSUFFICIENT_HISTORY:missing_daily_bar"}
    invalid = pl.DataFrame({"symbol": ["A"], "date": [cal[-20]]})
    ctx2 = _ctx(flat_daily("A", cal), cal, cfg, invalid=invalid)
    blocked = ctx2.filter(pl.col("atr_reason") == "INSUFFICIENT_HISTORY:missing_daily_bar")[
        "date"
    ].to_list()
    assert blocked == cal[-19:-5]  # the next 14 trading days
    assert ctx2.filter(pl.col("date") == cal[-1])["atr_ok"][0]


def test_atr_in_trade_date_price_terms_across_split(cfg):
    cal = weekdays(date(2024, 1, 1), 130)
    ex = cal[60]
    pre = flat_daily("A", cal[:60], close=500.0, rng=15.0)  # raw pre-split prices
    post = flat_daily("A", cal[60:], close=100.0, rng=3.0)  # 1:5 split
    acts = pl.DataFrame(
        {"symbol": ["A"], "ex_date": [ex], "action_type": ["split"], "price_factor": [0.2]}
    )
    ctx = _ctx(pl.concat([pre, post]), cal, cfg, actions=acts)
    after = ctx.filter(pl.col("date") == cal[-1]).row(0, named=True)
    assert after["atr14"] == pytest.approx(3.0) and after["prev_close"] == pytest.approx(100.0)
    # ex-date gap is absorbed by the adjustment: TR on ex-date is not inflated
    on_ex_next = ctx.filter(pl.col("date") == cal[61]).row(0, named=True)
    assert on_ex_next["atr14"] == pytest.approx(3.0)


def test_future_split_does_not_change_atr(cfg):
    cal = weekdays(date(2024, 1, 1), 130)
    daily = flat_daily("A", cal)
    t = cal[110]
    base = _ctx(daily, cal, cfg).filter(pl.col("date") == t).row(0, named=True)
    acts = pl.DataFrame(
        {"symbol": ["A"], "ex_date": [cal[120]], "action_type": ["split"], "price_factor": [0.5]}
    )
    fut = _ctx(daily, cal, cfg, actions=acts).filter(pl.col("date") == t).row(0, named=True)
    assert fut["atr14"] == pytest.approx(base["atr14"])
    assert fut["prev_close"] == pytest.approx(base["prev_close"])


# ------------------------------------------------- opening range / breakout


def _session(closes, highs=None, lows=None, vol=1000):
    c = np.asarray(closes, float)
    h = c + 0.1 if highs is None else np.asarray(highs, float)
    lo = c - 0.1 if lows is None else np.asarray(lows, float)
    return bars_from_arrays("A", date(2024, 6, 3), c, h, lo, c, np.full(c.size, vol))


def test_opening_range_uses_0915_to_0929_only(cfg):
    c = np.full(N, 100.0)
    c[14] = 102.0  # 09:29 candle -> in OR
    c[15] = 105.0  # 09:30 candle -> not in OR (and is the breakout)
    a = session_arrays(_session(c), cfg.session)
    r = opening_range(a, cfg.session)
    assert (r.high, r.low, r.candles) == (pytest.approx(102.1), pytest.approx(99.9), 15)
    assert first_breakout(a, r, cfg.session) == (15, "long")


def test_breakout_rules(cfg):
    c = np.full(N, 100.0)
    a0 = session_arrays(_session(c), cfg.session)
    r = opening_range(a0, cfg.session)  # 99.9 .. 100.1
    c1 = c.copy()
    c1[20] = 100.1  # equal to OR_H: not a breakout (strict)
    c1[30] = 99.5  # first close beyond -> short
    c1[40] = 101.0  # later long breakout ignored
    assert first_breakout(session_arrays(_session(c1), cfg.session), r, cfg.session) == (
        30,
        "short",
    )
    c2 = c.copy()
    c2[316] = 101.0  # 14:31 candle: outside the entry window
    assert first_breakout(session_arrays(_session(c2), cfg.session), r, cfg.session) is None
    c3 = c.copy()
    c3[315] = 101.0  # 14:30 candle: inside (inclusive)
    assert first_breakout(session_arrays(_session(c3), cfg.session), r, cfg.session) == (
        315,
        "long",
    )


def test_missing_candles_are_nan_and_zero_volume(cfg):
    df = _session(np.full(N, 100.0)).filter(pl.int_range(pl.len()) % 2 == 0)
    a = session_arrays(df, cfg.session)
    assert np.isnan(a.close[1]) and a.volume[1] == 0 and a.present.sum() == 188


# ----------------------------------------------------------------- index


def test_index_return_asof_last_close(cfg):
    c = np.full(N, 101.0)
    df = bars_from_arrays(
        "NIFTY 200", date(2024, 6, 3), np.full(N, 100.0), c + 1, c - 1, c, np.zeros(N)
    ).filter(pl.int_range(pl.len()) != 20)
    a = session_arrays(df, cfg.session)
    assert index_return(a, 20) == pytest.approx(0.01)  # uses 09:34 close (as-of)
    a_no_open = session_arrays(df.slice(1), cfg.session)
    assert index_return(a_no_open, 20) is None


# -------------------------------------------------------------------- RV


def test_baseline_sessions_skip_excluded_and_need_15(cfg):
    window = weekdays(date(2024, 1, 1), 30)
    valid = set(window) - {window[-1], window[-2]}  # two most recent excluded
    chosen = select_baseline_sessions(window, valid, cfg.features)
    assert len(chosen) == 20 and chosen[-1] == window[-3] and chosen[0] == window[-22]
    assert select_baseline_sessions(window, set(window[:14]), cfg.features) is None
    fifteen = select_baseline_sessions(window, set(window[:15]), cfg.features)
    assert len(fifteen) == 15


def test_rv_baseline_rescales_volume():
    c1, c2 = np.array([10.0, 20.0]), np.array([30.0, 60.0])
    base = rv_baseline([c1, c2], [2.0, 1.0])  # c1 is pre-split: x2 shares
    assert base.tolist() == [25.0, 50.0]
    assert relative_volume(np.array([50.0, 100.0]), base, 1) == pytest.approx(2.0)
    assert relative_volume(np.array([1.0, 1.0]), np.zeros(2), 1) is None

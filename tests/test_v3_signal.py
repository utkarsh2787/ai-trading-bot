"""V3 deliverable 3: schedule, signal, ranking, exclusions, look-ahead."""

from datetime import date

import polars as pl
import pytest

from orb.v3.data import dividend_amount
from orb.v3.signal import (
    EXCLUDED,
    INSUFFICIENT_HISTORY,
    NO_PREV_PRICE,
    NO_PRICE,
    RANKED,
    rebalance_days,
    scan_rebalance,
)
from tests.v3_market import CAL, day_bars, drop, make_data

PREV, DAY = date(2024, 6, 21), date(2024, 6, 28)  # Fridays


def test_rebalance_days_last_trading_day_of_week():
    days = [d for d in CAL if d != date(2024, 3, 29)]  # Good Friday holiday
    rb = rebalance_days(days)
    assert date(2024, 3, 28) in rb and date(2024, 3, 29) not in rb  # Thursday instead
    assert all(d.weekday() == 4 for d in rb if d.isocalendar()[1] != 13)
    assert len(rb) == len({d.isocalendar()[:2] for d in days})


def test_whole_market_excluded_friday_moves_rebalance(cfg_v3):
    data = make_data(cfg_v3, ["A"], {}, whole_days=[date(2024, 6, 14)])
    assert date(2024, 6, 14) not in data.trading_days
    assert date(2024, 6, 13) in rebalance_days(data.trading_days)


def sig_data(cfg_v3, extra_minute=None, **kw):
    p = {"A": 95.0, "B": 97.0, "C": 103.0, "D": 95.0, "F": 90.0, "G": 96.0, "H": 80.0}
    minute = {
        (s, DAY): day_bars(100.0, **{"14_59": (100, 100.5, v - 0.5, v)}) for s, v in p.items()
    }
    # E: 1:2 split (price factor 0.5) ex 2024-06-26; prev close 200 raw -> 100 adjusted
    minute[("E", DAY)] = day_bars(48.0, **{"14_59": (48, 48.5, 47.5, 48.0)})
    closes = {("E", d): (200.0 if d < date(2024, 6, 26) else 50.0) for d in CAL}
    # I: only 3 bars before DAY; J: no bar on the previous rebalance day
    closes.update({("I", d): None for d in CAL if d < date(2024, 6, 25)})
    closes[("J", PREV)] = None
    for s in "IJ":
        minute[(s, DAY)] = day_bars(100.0, **{"14_59": (100, 100, 90, 90.0)})
    k = day_bars(100.0)  # K: 14:59 missing -> 14:57 close
    drop(k, "14:58", "14:59")
    k["c"][342] = 94.0  # 14:57
    minute[("K", DAY)] = k
    m = day_bars(100.0)  # M: nothing 14:55..14:59
    drop(m, "14:55", "14:56", "14:57", "14:58", "14:59")
    minute[("M", DAY)] = m
    minute.update(extra_minute or {})
    syms = sorted({s for s, _ in minute})
    excluded = [
        ("F", DAY, "DAILY_MINUTE_MISMATCH"),  # DQ error -> excluded
        ("G", DAY, "FNO_BAN"),  # the ban does NOT exclude in V3
        ("H", DAY, "CORPORATE_ACTION_BONUS"),  # CA ex-day blocks ranking / buys
    ]
    actions = [("E", date(2024, 6, 26), "split", 0.5, "Face Value Split")]
    return make_data(cfg_v3, syms, minute, closes, excluded, actions, **kw)


def scan(data):
    return {r["symbol"]: r for r in scan_rebalance(data, DAY, PREV).to_dicts()}


def test_signal_ranking_and_reasons(cfg_v3):
    r = scan(sig_data(cfg_v3))
    assert r["A"]["r_week"] == pytest.approx(-0.05) and r["C"]["r_week"] > 0
    assert r["E"]["p_prev"] == 100.0 and r["E"]["adj_factor"] == 0.5
    assert r["E"]["r_week"] == pytest.approx(-0.52)
    assert r["F"]["decision"] == EXCLUDED and r["F"]["reason"] == "EXCLUDED_DQ"
    assert r["G"]["decision"] == RANKED  # F&O ban ignored
    assert r["H"]["decision"] == EXCLUDED and r["H"]["reason"] == "EXCLUDED_CORP_ACTION"
    assert r["I"]["decision"] == INSUFFICIENT_HISTORY
    assert r["J"]["decision"] == NO_PREV_PRICE
    assert r["K"]["p_now"] == 94.0 and r["K"]["p_now_substituted"]
    assert r["M"]["decision"] == NO_PRICE and r["M"]["rank"] is None
    # r_week: E -0.52, K -0.06, A -0.05 = D -0.05 (A first), G -0.04, B -0.03, C +0.03
    order = sorted((v["rank"], s) for s, v in r.items() if v["rank"])
    assert [s for _, s in order] == ["E", "K", "A", "D", "G", "B", "C"]
    assert {s for s, v in r.items() if v["target"]} == {"E", "K"}


def test_lookahead_truncate_after_1459(cfg_v3):
    full = scan(sig_data(cfg_v3))
    # absurd prices from 15:00 on, and a different official close for the rebalance day
    cut = sig_data(cfg_v3)
    cut.prepare(cut.members(DAY), DAY)
    for (_s, d), a in cut.base._sessions.items():
        if d == DAY and a is not None:
            for arr in (a.open, a.high, a.low, a.close):
                arr[345:] = 1.0  # absurd prices after 14:59
    for s in cut.members(DAY):
        if (s, DAY) in cut.bars:
            cut.bars[(s, DAY)] = (5.0, 5.0, 5.0, 5.0)  # official close is known only after 15:30
    got = scan(cut)
    for s in full:
        for k in ("r_week", "rank", "target", "decision"):
            assert got[s][k] == full[s][k], (s, k)


def test_future_prices_never_change_this_weeks_targets(cfg_v3):
    nxt = date(2024, 7, 5)
    cal = CAL + [date(2024, 7, 1), date(2024, 7, 2), date(2024, 7, 3), date(2024, 7, 4), nxt]
    base = scan(sig_data(cfg_v3, cal=cal))
    later = {(s, nxt): day_bars(500.0) for s in "ABCDE"}  # wild prices next week
    closes_extra = sig_data(cfg_v3, extra_minute=later, cal=cal)
    for d in cal[-5:]:
        for s in "ABCDE":
            closes_extra.bars[(s, d)] = (1.0, 1.0, 1.0, 1.0)
    got = scan(closes_extra)
    assert {s for s, v in got.items() if v["target"]} == {s for s, v in base.items() if v["target"]}


def test_dividend_amount_parsing():
    assert dividend_amount("Annual General Meeting/Dividend - Rs  1.20 Per Share") == 1.2
    assert dividend_amount("Interim Dividend - Re 0.375/- Per Share") == 0.375
    assert dividend_amount("Interim Dividend - Rs 0.36 Per Sh") == 0.36
    assert dividend_amount("Dividend - 2.50 Per Share") == 2.5
    assert dividend_amount("Interim Dividend - S 5/- Per Share (Purpose Revised)") == 5.0
    assert (
        dividend_amount("Interim Dividend - Rs 2 Per Share And Special Dividend - Rs 3 Per Share")
        == 5.0
    )
    assert dividend_amount("Interim Dividend") is None


def test_renamed_actions_map_forward(cfg_v3):
    data = make_data(
        cfg_v3, ["NEW"], {}, actions=[("OLD", date(2024, 3, 1), "dividend", None, "Rs 2 Per Share")]
    )
    assert data.dividends == {("OLD", date(2024, 3, 1)): (2.0, True)}
    data2 = make_data(
        cfg_v3, ["NEW"], {}, actions=[("OLD", date(2024, 3, 1), "dividend", None, "Rs 2 Per Share")]
    )
    data2.renames = pl.DataFrame(
        {"old_symbol": ["OLD"], "new_symbol": ["NEW"], "effective_date": [date(2024, 5, 1)]}
    )
    data2.__post_init__()
    assert ("NEW", date(2024, 3, 1)) in data2.dividends

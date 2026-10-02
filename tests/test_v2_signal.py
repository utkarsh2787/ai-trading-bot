"""V2 deliverable 3: signal, selection, entry/exit rules, circuit guard, look-ahead.

prev close 100, ATR14 3 -> atr_pct 0.03; z = (P_0945 / 100 - 1) / 0.03.
"""

from datetime import date

import numpy as np
import polars as pl
import pytest

from orb.refdata.fno import FnoCalendar, sample_date, stock_futures
from orb.v2 import trade as tr
from orb.v2.signal import (
    BELOW_THRESHOLD,
    EXCLUDED,
    NO_P0945,
    QUALIFIED,
    index_selection,
    index_signal,
    scan_day_v2,
)
from tests.v2_market import T, arrays, builder, candle, drop, flat, market


def sig_market(cfg_v2):
    s = {}
    for sym, p in {"A": 101.5, "B": 99.0, "C": 100.6, "D": 101.5, "E": 102.4, "F": 103.0}.items():
        b = flat()
        candle(b, "09:44", 100.0, p + 0.1, min(p, 100.0) - 0.1, p)
        s[sym] = b
    g = flat()  # 09:44 missing -> 09:42 close
    drop(g, "09:43", "09:44")
    candle(g, "09:42", 100.0, 101.3, 99.9, 101.2)
    s["G"] = g
    h = flat()  # nothing 09:40..09:44
    drop(h, "09:40", "09:41", "09:42", "09:43", "09:44")
    s["H"] = h
    return market(cfg_v2, s, excluded=["F"])


@pytest.fixture(scope="module")
def scanned(cfg_v2):
    m = sig_market(cfg_v2)
    inputs = builder(m).day(T)
    return m, inputs, {r["symbol"]: r for r in scan_day_v2(inputs, cfg_v2).to_dicts()}


def test_z_values_and_decisions(scanned):
    _, _, r = scanned
    assert r["A"]["z"] == pytest.approx(0.015 / 0.03)
    assert r["B"]["z"] == pytest.approx(-0.01 / 0.03) and r["B"]["side"] == "short"
    assert r["C"]["decision"] == BELOW_THRESHOLD and not r["C"]["qualified"]  # z = 0.2
    assert r["E"]["z"] == pytest.approx(0.8) and r["E"]["side"] == "long"
    assert r["F"]["decision"] == EXCLUDED and r["F"]["z"] is None and r["F"]["rank"] is None
    assert r["G"]["p0945_substituted"] and r["G"]["p0945"] == pytest.approx(101.2)
    assert r["G"]["reason"] == "P0945_SUBSTITUTED" and r["G"]["decision"] == QUALIFIED
    assert r["H"]["decision"] == NO_P0945 and r["H"]["rank"] is None
    assert r["A"]["prev_close"] == pytest.approx(100.0) and r["A"]["atr_pct"] == pytest.approx(0.03)


def test_ranking_ties_and_top3(scanned):
    _, _, r = scanned
    # |z|: E 0.8, A 0.5 = D 0.5 (A before D), G 0.4, B 0.333
    assert [r[s]["rank"] for s in "EADGB"] == [1, 2, 3, 4, 5]
    assert {s for s in r if r[s]["selected"]} == {"E", "A", "D"}
    assert r["C"]["rank"] is None


def test_lookahead_truncate_after_0945(cfg_v2, scanned):
    m, _, full = scanned
    cut = m.minute.filter(pl.col("ts").dt.strftime("%H:%M") <= "09:44")
    rng = np.random.default_rng(1)
    noisy = m.minute.with_columns(  # wild prices after 09:44 must change nothing
        pl.when(pl.col("ts").dt.strftime("%H:%M") > "09:44")
        .then(pl.col(c) * float(rng.uniform(0.5, 1.5)))
        .otherwise(pl.col(c))
        .alias(c)
        for c in ("open", "high", "low", "close")
    )
    for minute in (cut, noisy):
        m2 = type(m)(m.cfg, m.calendar, m.daily, minute, m.index_minute)
        m2.excluded = m.excluded
        got = {r["symbol"]: r for r in scan_day_v2(builder(m2).day(T), cfg_v2).to_dicts()}
        for s in full:
            for k in ("z", "r1", "rank", "selected", "side", "decision"):
                assert got[s][k] == full[s][k], (s, k)


def trade_bars(**candles):
    b = flat()
    for t, ohlc in candles.items():
        candle(b, t.replace("_", ":"), *ohlc)
    return b


def sim(cfg_v2, b, side="long", guard=True, book="primary", slot_value=2666.6667):
    return tr.simulate_v2(
        arrays(b), T, side, slot_value, 0.01, cfg_v2.execution.books[book], guard, cfg_v2
    )


def test_lookahead_truncate_after_1430(cfg_v2):
    b = trade_bars(
        **{"14_30": (100.40, 100.6, 100.3, 100.5), "15_10": (101.0, 101.1, 100.9, 101.0)}
    )
    full = sim(cfg_v2, b)
    cut = {k: v.copy() for k, v in b.items()}
    for k in cut:
        cut[k][tr.slot(cfg_v2.session, cfg_v2.session.entry_candle) + 1 :] = np.nan
    t2 = sim(cfg_v2, cut)
    for k in ("entry_slot", "entry_raw", "entry_price", "qty", "notional"):
        assert getattr(t2, k) == getattr(full, k), k
    assert full.entry_price == pytest.approx(100.41)  # 1 tick, no rounding


def test_entry_fallback_and_missing(cfg_v2):
    b = flat()
    drop(b, "14:30", "14:31")
    t = sim(cfg_v2, b)
    assert (
        t.status == tr.OK
        and t.entry_slot == tr.slot(cfg_v2.session, cfg_v2.session.entry_candle) + 2
    )
    assert "ENTRY_DELAYED" in t.flags
    drop(b, "14:32", "14:33", "14:34", "14:35")
    assert sim(cfg_v2, b).status == tr.ENTRY_MISSING


def test_exit_substituted(cfg_v2):
    b = flat()
    drop(b, "15:10")
    candle(b, "15:09", 100.0, 100.3, 99.9, 100.2)
    t = sim(cfg_v2, b)
    assert t.exit_reason == tr.EXIT_SUBSTITUTED and t.exit_raw == pytest.approx(100.2)
    assert t.exit_at == "close" and t.exit_slot < tr.slot(
        cfg_v2.session, cfg_v2.session.exit_candle
    )


def test_circuit_guard_entry(cfg_v2):
    # upper circuit at 14:30: high == low == day's high so far -> a long can't buy
    b = trade_bars(**{"14_30": (105.0, 105.0, 105.0, 105.0)})
    assert sim(cfg_v2, b, "long").status == tr.LOCKED_CIRCUIT
    assert sim(cfg_v2, b, "long", guard=False).status == tr.OK  # F&O stock: no guard
    assert sim(cfg_v2, b, "short").status == tr.OK  # selling into an upper circuit is fine
    # H == L but not at the day's extreme: not locked
    b2 = trade_bars(**{"10_00": (100, 106, 99.9, 100), "14_30": (105.0, 105.0, 105.0, 105.0)})
    assert sim(cfg_v2, b2, "long").status == tr.OK


def test_circuit_guard_exit(cfg_v2):
    # lower circuit from 15:08: a long can't sell at 15:10 -> close of 15:07 (unlocked)
    b = trade_bars(
        **{
            "15_07": (99.0, 99.2, 98.8, 98.9),
            "15_08": (95.0, 95.0, 95.0, 95.0),
            "15_09": (95.0, 95.0, 95.0, 95.0),
            "15_10": (95.0, 95.0, 95.0, 95.0),
        }
    )
    t = sim(cfg_v2, b, "long")
    assert (
        t.exit_reason == tr.EXIT_LOCKED
        and t.exit_raw == pytest.approx(98.9)
        and t.exit_at == "close"
    )
    assert sim(cfg_v2, b, "long", guard=False).exit_raw == pytest.approx(95.0)
    # a short buys back: a lower circuit doesn't block it
    assert sim(cfg_v2, b, "short").exit_reason == tr.HARD_EXIT
    # locked all the way back to the entry -> the 15:10 open, still EXIT_LOCKED
    b2 = flat()
    i0 = tr.slot(cfg_v2.session, cfg_v2.session.entry_candle)
    b2["o"][i0 + 1 :], b2["h"][i0 + 1 :], b2["l"][i0 + 1 :], b2["c"][i0 + 1 :] = (
        90.0,
        90.0,
        90.0,
        90.0,
    )
    b2["c"][i0] = 90.0
    b2["l"][i0] = 90.0
    t2 = sim(cfg_v2, b2, "long")
    assert t2.exit_reason == tr.EXIT_LOCKED and t2.exit_at in ("open", "close")


def test_slippage_models(cfg_v2):
    b = trade_bars(
        **{"14_30": (100.403, 100.6, 100.3, 100.5), "15_10": (101.0, 101.1, 100.9, 101.0)}
    )
    p = sim(cfg_v2, b, "long", book="primary")
    assert p.entry_price == pytest.approx(100.413)  # off-grid raw open stays off-grid
    s = sim(cfg_v2, b, "long", book="v1_model")
    # 100.403 + 0.01 + 0.0002 x 100.403 = 100.433081 -> buy rounds up -> 100.44
    assert s.entry_price == pytest.approx(100.44)
    # 101.0 - (0.01 + 0.0202) = 100.9698 -> sell rounds down -> 100.96
    assert s.exit_price == pytest.approx(100.96)
    two = sim(cfg_v2, b, "long", book="primary_2x")
    assert two.entry_price == pytest.approx(100.423)
    assert p.gross_pnl == pytest.approx(p.raw_move - p.slippage_paid)


def test_qty_zero(cfg_v2):
    assert sim(cfg_v2, flat(), slot_value=50.0).status == tr.QTY_ZERO


def test_index_variant_alignment(cfg_v2, scanned):
    m, inputs, _ = scanned
    sig = scan_day_v2(inputs, cfg_v2)
    up = {"qualified": True, "r1": 0.01, "side": "long"}
    top = index_selection(sig, up, 3)
    assert top["symbol"].to_list() == ["E", "A", "D"] and set(top["side"]) == {"long"}
    down = {"qualified": True, "r1": -0.01, "side": "short"}
    top = index_selection(sig, down, 3)
    # alignment = -z: B +0.333, C -0.2 (z 0.2, not qualifying itself), G -0.4
    assert top["symbol"].to_list() == ["B", "C", "G"] and set(top["side"]) == {"short"}
    assert index_selection(sig, {"qualified": False}, 3).height == 0


def test_index_signal(cfg_v2):
    ib = flat(20000.0)
    candle(ib, "09:44", 20000, 20101, 19999, 20100)
    m = sig_market(cfg_v2)
    m.index_minute = m.index_minute.filter(pl.lit(False))
    from tests.market import bars_from_arrays

    m.index_minute = bars_from_arrays(
        "NIFTY 200", T, ib["o"], ib["h"], ib["l"], ib["c"], np.zeros(375)
    )
    inputs = builder(m).day(T)
    ctx = {("NIFTY 200", T): {"atr14": 300.0, "prev_close": 20000.0, "atr_reason": None}}
    out = index_signal(inputs, ctx, cfg_v2)
    assert out["z"] == pytest.approx(0.005 / 0.015) and out["qualified"] and out["side"] == "long"


def test_fno_calendar_bracketing():
    d1, d2, d3 = date(2024, 1, 1), date(2024, 1, 8), date(2024, 1, 15)
    s = pl.DataFrame({"sample_date": [d1, d1, d2, d2, d3], "symbol": ["X", "Y", "X", "Z", "X"]})
    c = FnoCalendar(s, pl.DataFrame({"symbol": ["W"], "date": [date(2024, 1, 3)]}))
    assert c.is_fno("X", date(2024, 1, 3))  # in both bracketing samples
    assert not c.is_fno("Y", date(2024, 1, 3))  # dropped by the next sample -> guard applies
    assert not c.is_fno("Z", date(2024, 1, 3))  # added in the next sample -> guard applies
    assert c.is_fno("Z", d2)  # sample day itself
    assert c.is_fno("X", date(2024, 2, 1))  # after the last sample
    assert c.is_fno("W", date(2024, 1, 3))  # on the ban list that day
    assert not c.is_fno("X", date(2023, 12, 1))  # before the first sample


def test_fno_parsers():
    assert sample_date("abc_fo30APR2019bhav.csv.zip") == date(2019, 4, 30)
    assert sample_date("x_BhavCopy_NSE_FO_0_0_0_20250818_F_0000.csv.zip") == date(2025, 8, 18)
    legacy = (
        b"INSTRUMENT,SYMBOL,EXPIRY_DT\nFUTSTK,ACC,30-May-2019\n"
        b"OPTSTK,BEL,30-May-2019\nFUTIDX,NIFTY,30-May-2019\n"
    )
    assert stock_futures(legacy) == ["ACC"]
    udiff = b"FinInstrmTp,TckrSymb,XpryDt\nSTF,TCS,2025-08-28\nSTO,INFY,2025-08-28\n"
    assert stock_futures(udiff) == ["TCS"]

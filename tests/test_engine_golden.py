"""Engine golden day: four signals, every fill / qty / cost line computed by hand.

Day T = 2024-06-28 (Friday). Tick regime from 2024-06-10: price < 250 -> 0.01
(reference close = May-end close 100). Slippage per fill = 0.01 + 0.02% x price.
Every stock: ATR14 = 3, prev close = 100, OR (09:15-09:29) = 100.00 .. 101.00,
RV = 2.5 (2,500 vs 1,000 shares/min), Nifty 200 +0.20% all day.

  A long   signal 09:44 close 101.30 -> score 100; entry 09:45 open 101.40
           stop OR_L 100 hit at 12:35 (open 100.20, low 99.90)
  B short  signal 09:44 close 99.70  -> score 85 (market factor 0: index up);
           entry open 99.60; no stop; hard exit 15:10 open 99.00
  C long   like A; 11:55 candle gaps through the stop: open 99.20 -> fill at the open
  D long   signal 09:45 close 101.30 (score 100) -> entry 09:46: 3 slots full -> SKIPPED

Fills (adverse rounding: buys up, sells down to 0.01):
  A buy  101.40 + (0.01 + 0.020280) = 101.430280 -> 101.44
  A sell min(100, 100.20)=100 - 0.030000 = 99.970000 -> 99.97
  B sell 99.60 - (0.01 + 0.019920) = 99.570080 -> 99.57
  B buy  99.00 + (0.01 + 0.019800) = 99.029800 -> 99.03
  C sell min(100, 99.20)=99.20 - 0.029840 = 99.170160 -> 99.17
Qty = floor(min(8000/3 x score/100 / entry, 100 / risk)):
  A, C: min(2666.67/101.44 = 26.29, 100/1.44 = 69.4)  -> 26
  B:    min(2266.67/99.57  = 22.76, 100/1.43 = 69.9)  -> 22
Charges per order (value V): brokerage min(20, 0.03% V); STT 0.025% V sell;
exchange 0.00297% V; stamp 0.003% V buy; SEBI 10/crore = 0.0001% V;
GST 18% x (brokerage + exchange + SEBI).
"""

from datetime import date

import numpy as np
import polars as pl
import pytest

from orb.engine import Engine
from orb.portfolio import SKIPPED, TAKEN
from orb.scoring import RuleScorer
from orb.sim.ticks import daily_ticks
from tests.market import Market, bars_from_arrays, flat_daily, weekdays

N = 375
P = 0.01  # rupee tolerance

# ---------------------------------------------------------------- expected
EXPECTED = {
    "A": dict(
        side="long",
        entry=101.44,
        qty=26,
        exit=99.97,
        exit_reason="STOP",
        exit_slot=200,
        score=100.0,
        gross=-38.22,
        slippage=26 * ((101.44 - 101.40) + (100.00 - 99.97)),
        entry_order=dict(
            value=2637.44,
            brokerage=0.791232,
            stt=0.0,
            exchange=0.07833197,
            stamp=0.0791232,
            sebi=0.00263744,
            gst=0.15699625,
        ),
        exit_order=dict(
            value=2599.22,
            brokerage=0.779766,
            stt=0.649805,
            exchange=0.07719683,
            stamp=0.0,
            sebi=0.00259922,
            gst=0.15472117,
        ),
        costs=2.772409,
        net=-40.992409,
        costs_rounded=2.749007,
        net_rounded=-40.969007,
    ),
    "B": dict(
        side="short",
        entry=99.57,
        qty=22,
        exit=99.03,
        exit_reason="HARD_EXIT",
        exit_slot=355,
        score=85.0,
        gross=11.88,
        slippage=22 * ((99.60 - 99.57) + (99.03 - 99.00)),
        entry_order=dict(
            value=2190.54,
            brokerage=0.657162,
            stt=0.547635,
            exchange=0.06505904,
            stamp=0.0,
            sebi=0.00219054,
            gst=0.13039408,
        ),
        exit_order=dict(
            value=2178.66,
            brokerage=0.653598,
            stt=0.0,
            exchange=0.0647062,
            stamp=0.0653598,
            sebi=0.00217866,
            gst=0.12968692,
        ),
        costs=2.31797,
        net=9.56203,
        costs_rounded=2.29957,
        net_rounded=9.58043,
    ),
    "C": dict(
        side="long",
        entry=101.44,
        qty=26,
        exit=99.17,
        exit_reason="STOP",
        exit_slot=160,
        score=100.0,
        gross=-59.02,
        slippage=26 * ((101.44 - 101.40) + (99.20 - 99.17)),
        entry_order=dict(
            value=2637.44,
            brokerage=0.791232,
            stt=0.0,
            exchange=0.07833197,
            stamp=0.0791232,
            sebi=0.00263744,
            gst=0.15699625,
        ),
        exit_order=dict(
            value=2578.42,
            brokerage=0.773526,
            stt=0.644605,
            exchange=0.07657907,
            stamp=0.0,
            sebi=0.00257842,
            gst=0.15348303,
        ),
        costs=2.759092,
        net=-61.779092,
        costs_rounded=2.735244,
        net_rounded=-61.755244,
    ),
}
# contract note for the day: STT 0.649805 + 0.547635 + 0.644605 = 1.842045 -> Rs 2
# (allocated pro rata: A 0.705526, B 0.594595, C 0.699880); stamp 0.0791232 +
# 0.0653598 + 0.0791232 = 0.2236062 -> Rs 0.
DAY_STT, DAY_STT_ROUNDED, DAY_STAMP, DAY_STAMP_ROUNDED = 1.842045, 2.0, 0.2236062, 0.0
DAY_NET = -40.992409 + 9.56203 - 61.779092  # -93.209471
DAY_NET_ROUNDED = -40.969007 + 9.58043 - 61.755244  # -93.143821


def golden_day(cfg):
    cal = weekdays(date(2024, 1, 1), 130)
    T = cal[-1]
    assert T == date(2024, 6, 28)
    minute, daily = [], []
    for sym in "ABCD":
        daily.append(flat_daily(sym, cal, close=100.0, rng=3.0))
        for d in cal[-31:-1]:
            c = np.full(N, 100.5)
            minute.append(bars_from_arrays(sym, d, c, c + 0.2, c - 0.2, c, np.full(N, 1000)))
        o = np.full(N, 100.5)
        c, h, lo = o.copy(), o + 0.1, o - 0.1
        h[:15], lo[:15] = 101.0, 100.0  # opening range 100 .. 101

        def candle(i, op, hi, low, cl, o=o, h=h, lo=lo, c=c):
            o[i], h[i], lo[i], c[i] = op, hi, low, cl

        if sym in "AC":
            candle(29, 100.5, 101.35, 100.4, 101.30)  # long signal
            candle(30, 101.40, 101.50, 101.30, 101.40)  # entry candle
        if sym == "A":
            candle(200, 100.20, 100.30, 99.90, 100.00)  # stop touched
        if sym == "C":
            candle(160, 99.20, 99.30, 99.10, 99.20)  # gap through the stop
        if sym == "B":
            candle(29, 100.5, 100.6, 99.65, 99.70)  # short signal
            candle(30, 99.60, 99.70, 99.50, 99.60)
            candle(355, 99.00, 99.10, 98.90, 99.00)  # 15:10
        if sym == "D":
            candle(30, 100.5, 101.35, 100.4, 101.30)  # long signal one minute later
        minute.append(bars_from_arrays(sym, T, o, h, lo, c, np.full(N, 2500)))
    idx = []
    for d in cal[-31:]:
        io = np.full(N, 20000.0)
        ic = io * (1.002 if d == T else 1.0)
        idx.append(
            bars_from_arrays(
                "NIFTY 200", d, io, np.maximum(io, ic) + 1, np.minimum(io, ic) - 1, ic, np.zeros(N)
            )
        )
    return Market(cfg, cal, pl.concat(daily), pl.concat(minute), pl.concat(idx)), T


@pytest.fixture(scope="module")
def run(cfg, tmp_path_factory):
    m, T = golden_day(cfg)
    ticks = {
        (r["symbol"], r["date"]): r["tick"] for r in daily_ticks(m.daily, cfg.ticks).to_dicts()
    }
    res = Engine(cfg, m.builder(), ticks, RuleScorer(cfg.scoring)).run(
        T, T, ledger=tmp_path_factory.mktemp("l") / "l.jsonl"
    )
    book = res.book.filter((pl.col("variant") == "default") & (pl.col("slippage_mult") == 1.0))
    return res, {r["symbol"]: r for r in book.to_dicts()}, T


def test_signals_and_book(run):
    res, book, _ = run
    assert res.signals.height == 4
    assert {s: book[s]["book_decision"] for s in "ABCD"} == {
        "A": TAKEN,
        "B": TAKEN,
        "C": TAKEN,
        "D": SKIPPED,
    }
    assert book["D"]["book_reason"] == "SLOTS_FULL"


@pytest.mark.parametrize("sym", ["A", "B", "C"])
def test_fills_qty_pnl(run, sym):
    _, book, _ = run
    r, e = book[sym], EXPECTED[sym]
    assert r["side"] == e["side"] and r["score"] == pytest.approx(e["score"])
    assert r["entry_price"] == pytest.approx(e["entry"], abs=P)
    assert r["qty"] == e["qty"]
    assert r["exit_price"] == pytest.approx(e["exit"], abs=P)
    assert (r["exit_reason"], r["exit_slot"]) == (e["exit_reason"], e["exit_slot"])
    assert r["gross_pnl"] == pytest.approx(e["gross"], abs=P)
    assert r["slippage_paid"] == pytest.approx(e["slippage"], abs=P)
    assert r["costs"] == pytest.approx(e["costs"], abs=P)
    assert r["net_pnl"] == pytest.approx(e["net"], abs=P)
    assert r["costs_rounded"] == pytest.approx(e["costs_rounded"], abs=P)
    assert r["net_pnl_rounded"] == pytest.approx(e["net_rounded"], abs=P)


@pytest.mark.parametrize("sym", ["A", "B", "C"])
def test_every_cost_line(run, cfg, sym):
    from orb.sim.costs import Order, order_charges

    _, book, T = run
    r, e = book[sym], EXPECTED[sym]
    sides = ("buy", "sell") if e["side"] == "long" else ("sell", "buy")
    for leg, side, price in (
        ("entry_order", sides[0], r["entry_price"]),
        ("exit_order", sides[1], r["exit_price"]),
    ):
        got = order_charges(Order(T, side, r["qty"], price), cfg.costs)
        exp = e[leg]
        assert r["qty"] * price == pytest.approx(exp["value"], abs=P)
        assert got["brokerage"] == pytest.approx(exp["brokerage"], abs=1e-6)
        assert got["stt"] == pytest.approx(exp["stt"], abs=1e-6)
        assert got["exchange_txn"] == pytest.approx(exp["exchange"], abs=1e-6)
        assert got["stamp"] == pytest.approx(exp["stamp"], abs=1e-6)
        assert got["sebi_fee"] == pytest.approx(exp["sebi"], abs=1e-6)
        assert got["gst"] == pytest.approx(exp["gst"], abs=1e-6)


def test_day_totals_and_contract_note_rounding(run):
    _, book, _ = run
    taken = [book[s] for s in "ABC"]
    assert sum(t["net_pnl"] for t in taken) == pytest.approx(DAY_NET, abs=P)
    assert sum(t["net_pnl_rounded"] for t in taken) == pytest.approx(DAY_NET_ROUNDED, abs=P)
    # rounding moves exactly (2 - 1.842045) + (0 - 0.2236062) of costs
    delta = sum(t["costs"] - t["costs_rounded"] for t in taken)
    assert delta == pytest.approx(
        (DAY_STT - DAY_STT_ROUNDED) + (DAY_STAMP - DAY_STAMP_ROUNDED), abs=1e-6
    )

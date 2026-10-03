"""V3 deliverable 4: the portfolio ledger and its invariants.

Synthetic weeks in June 2024 (tick 0.01, prices < Rs 250). Rebalance Fridays:
06-07, 06-14, 06-21 and 06-28 (the last one on or before the run end = FORCED_END).
The selector is fixed per test, so each rule is exercised directly. Every run
checks the invariants on every day.
"""

from datetime import date

import numpy as np
import pytest

from orb.v3 import invariants as inv
from orb.v3 import ledger as lg
from orb.v3.market import Market
from tests.v3_market import CAL, day_bars, make_data

F1, F2, F3, F4 = date(2024, 6, 7), date(2024, 6, 14), date(2024, 6, 21), date(2024, 6, 28)
JUNE = [d for d in CAL if d >= date(2024, 6, 3)]


def market(cfg, syms, minute=None, closes=None, **kw):
    """Minute sessions flat at the day's close on every June day unless overridden."""
    closes = closes or {}
    m = {
        (s, d): day_bars(closes.get((s, d), 100.0))
        for s in syms
        for d in JUNE
        if closes.get((s, d), 100.0) is not None
    }
    m.update(minute or {})
    return Market(make_data(cfg, syms, m, closes, **kw))


def run(cfg, mk, picks: dict, book="primary", end=F4):
    sel = lambda d: picks.get(d, [])  # noqa: E731
    return lg.Ledger(mk, cfg.execution.books[book], book, sel).run(F1, end)


def trade(res, sym):
    return next(t for t in res.trades if t["symbol"] == sym)


def test_keep_sell_buy_and_sizing(cfg_v3):
    mk = market(cfg_v3, ["A", "B", "C"])
    res = run(cfg_v3, mk, {F1: ["A", "B"], F2: ["A", "C"], F3: ["A", "C"]})
    buys = [o for o in res.orders if o["side"] == "buy"]
    assert [(o["day"], o["symbol"]) for o in buys] == [(F1, "A"), (F1, "B"), (F2, "C")]
    # Slot_w = 0.9 x 10,000 / 2 = 4,500 -> floor(4500 / 100.01) = 44
    assert buys[0]["qty"] == 44 and buys[0]["fill"] == pytest.approx(100.01)
    b = trade(res, "B")
    assert (b["exit_date"], b["exit_cause"], b["exit_time"]) == (F2, "REBALANCE", "15:00")
    a = trade(res, "A")  # kept through F2 and F3, sold at the forced end
    assert (a["entry_date"], a["exit_date"], a["exit_cause"]) == (F1, F4, "FORCED_END")
    assert a["holding_days"] == 15
    assert res.counts["ROUND_TRIPS"] == 3
    # flat prices: the P&L is just slippage + costs (DP included)
    assert b["gross_pnl"] == pytest.approx(-44 * 0.02)
    assert b["dp"] == 15.34 and b["net_pnl_rounded"] < b["gross_pnl"] - 15.34
    assert res.final_equity == pytest.approx(10_000 + sum(t["net_pnl_rounded"] for t in res.trades))


def test_split_and_bonus_during_hold(cfg_v3):
    # A 1:2 split ex 06-12 (pf 0.5); B 1:3 bonus ex 06-12 (pf 0.75): 44 / 0.75 = 58.67 -> 58
    closes = {
        (s, d): (50.0 if s == "A" else 75.0) for s in "AB" for d in JUNE if d >= date(2024, 6, 12)
    }
    acts = [
        ("A", date(2024, 6, 12), "split", 0.5, "Split"),
        ("B", date(2024, 6, 12), "bonus", 0.75, "Bonus"),
    ]
    res = run(
        cfg_v3, market(cfg_v3, ["A", "B"], closes=closes, actions=acts), {F1: ["A", "B"]}, end=F2
    )
    a, b = trade(res, "A"), trade(res, "B")
    assert (a["entry_qty"], a["exit_qty"]) == (44, 88) and a["dropped_fraction"] == 0
    assert (b["entry_qty"], b["exit_qty"]) == (44, 58)
    assert b["dropped_fraction"] == pytest.approx(44 / 0.75 - 58)
    assert res.counts[lg.FRACTION_DROPPED] == 1
    assert a["gross_pnl"] == pytest.approx(88 * 49.99 - 44 * 100.01)  # value kept


def test_dividend_credited_on_ex_date(cfg_v3):
    acts = [
        ("A", date(2024, 6, 11), "dividend", None, "Dividend - Rs 2.50 Per Share"),
        ("B", date(2024, 6, 11), "dividend", None, "Interim Dividend"),
    ]
    res = run(cfg_v3, market(cfg_v3, ["A", "B"], actions=acts), {F1: ["A", "B"]}, end=F2)
    assert trade(res, "A")["dividends"] == pytest.approx(44 * 2.5)
    assert trade(res, "B")["dividends"] == 0 and res.counts[lg.DIVIDEND_NO_AMOUNT] == 1
    day = next(r for r in res.daily if r["date"] == date(2024, 6, 11))
    assert day["dividends"] == pytest.approx(110.0)


def test_rights_held_through(cfg_v3):
    acts = [("A", date(2024, 6, 11), "rights", None, "Rights 1:5")]
    res = run(cfg_v3, market(cfg_v3, ["A", "B"], actions=acts), {F1: ["A", "B"]}, end=F2)
    assert trade(res, "A")["rights_held"] == 1 and res.counts[lg.RIGHTS_HELD_THROUGH] == 1


def test_demerger_exit_previous_day_and_no_rebuy(cfg_v3):
    # demerger ex 06-17 (Mon): sell A at the 15:00 open on Fri 06-14 (a rebalance day);
    # A is a target again that day, but isn't bought back
    acts = [("A", date(2024, 6, 17), "demerger", None, "Demerger")]
    res = run(
        cfg_v3, market(cfg_v3, ["A", "B"], actions=acts), {F1: ["A", "B"], F2: ["A", "B"]}, end=F3
    )
    a = trade(res, "A")
    assert (a["exit_date"], a["exit_cause"], a["exit_time"]) == (F2, "EXIT_CORP_ACTION", "15:00")
    assert res.counts[lg.CORP_ACTION_NEXT_DAY] == 1
    assert [o["day"] for o in res.orders if o["symbol"] == "A" and o["side"] == "buy"] == [F1]


def test_delisting_exits_at_last_close(cfg_v3):
    # B's history ends 06-12: sold at its official close that day
    closes = {("B", d): None for d in CAL if d > date(2024, 6, 12)}
    closes[("B", date(2024, 6, 12))] = 90.0
    res = run(cfg_v3, market(cfg_v3, ["A", "B"], closes=closes), {F1: ["A", "B"]}, end=F2)
    b = trade(res, "B")
    assert (b["exit_date"], b["exit_cause"], b["exit_time"]) == (
        date(2024, 6, 12),
        "EXIT_DELISTED",
        "close",
    )
    assert b["exit_raw"] == 90.0 and b["exit_fill"] == pytest.approx(89.99)


def test_deferred_exit_on_dq_excluded_day(cfg_v3):
    ex = [("B", F2, "DAILY_MINUTE_MISMATCH"), ("B", date(2024, 6, 17), "VOLUME_MISMATCH")]
    res = run(cfg_v3, market(cfg_v3, ["A", "B"], excluded=ex), {F1: ["A", "B"], F2: ["A"]}, end=F3)
    b = trade(res, "B")  # deferred past 06-14 and 06-17 (both DQ-excluded) -> 06-18 15:00
    assert b["exit_date"] == date(2024, 6, 18) and "EXIT_DEFERRED" in b["flags"]
    assert b["exit_time"] == "15:00" and res.counts["exit:EXIT_DEFERRED"] == 1


def locked_day(price=95.0, unlock: str | None = None):
    """Lower circuit from 15:00 (H = L = the day's low so far)."""
    b = day_bars(100.0)
    for k in ("o", "h", "l", "c"):
        b[k][345:] = price
    if unlock:
        i = int(unlock[:2]) * 60 + int(unlock[3:]) - 555
        b["o"][i], b["h"][i], b["l"][i], b["c"][i] = price + 0.4, price + 1.0, price, price + 0.5
    return b


def test_exit_locked_forward_and_carried(cfg_v3):
    m = {("B", F2): locked_day(unlock="15:03")}
    res = run(cfg_v3, market(cfg_v3, ["A", "B"], m), {F1: ["A", "B"], F2: ["A"]}, end=F3)
    b = trade(res, "B")
    assert (b["exit_date"], b["exit_time"], b["exit_reason"]) == (F2, "15:03", "EXIT_LOCKED")
    assert b["exit_raw"] == pytest.approx(95.4)
    # locked to the close -> carried to Monday 09:20
    m2 = {("B", F2): locked_day()}
    res2 = run(cfg_v3, market(cfg_v3, ["A", "B"], m2), {F1: ["A", "B"], F2: ["A"]}, end=F3)
    b2 = trade(res2, "B")
    assert (b2["exit_date"], b2["exit_time"]) == (date(2024, 6, 17), "09:20")
    assert "EXIT_CARRIED" in b2["flags"] and res2.counts["exit:EXIT_CARRIED"] == 1
    # an F&O stock gets no guard: sold at the locked 15:00 open
    import polars as pl

    from orb.refdata.fno import FnoCalendar

    fno = FnoCalendar(pl.DataFrame({"sample_date": [date(2024, 6, 3)], "symbol": ["B"]}))
    res3 = run(cfg_v3, market(cfg_v3, ["A", "B"], m2, fno=fno), {F1: ["A", "B"], F2: ["A"]}, end=F3)
    assert trade(res3, "B")["exit_date"] == F2


def test_entry_skips(cfg_v3):
    up = day_bars(100.0)
    for k in ("o", "h", "l", "c"):
        up[k][345:] = 105.0  # upper circuit from 15:00
    gone = day_bars(100.0)
    for k in gone:
        gone[k][345:351] = np.nan  # no candle 15:00..15:05
    m = {("B", F1): up, ("C", F1): gone}
    res = run(cfg_v3, market(cfg_v3, ["A", "B", "C"], m), {F1: ["B", "C"]}, end=F2)
    assert res.counts["LOCKED_CIRCUIT"] == 1 and res.counts["ENTRY_MISSING"] == 1
    assert not [o for o in res.orders if o["side"] == "buy"]
    reb = res.rebalances[0]
    assert reb["skipped"] == "B:LOCKED_CIRCUIT;C:ENTRY_MISSING"


def test_qty_zero_and_cash_limited(cfg_v3):
    closes = {("X", d): 5000.0 for d in JUNE}  # Slot_w 4,500 < one share
    res = run(cfg_v3, market(cfg_v3, ["X", "A"], closes=closes), {F1: ["X", "A"]}, end=F2)
    assert res.counts[lg.QTY_ZERO] == 1
    # A doubles while held: Slot_w grows above the cash available for the second slot
    closes = {("A", d): 200.0 for d in JUNE if d > F1}
    res2 = run(
        cfg_v3,
        market(cfg_v3, ["A", "B", "C"], closes=closes),
        {F1: ["A", "B"], F2: ["A", "C"]},
        end=F3,
    )
    c = trade(res2, "C")
    assert "CASH_LIMITED" in c["flags"] and res2.counts[lg.CASH_LIMITED] == 1
    assert c["entry_qty"] < res2.rebalances[1]["slot_w"] / 100.01


def test_ruin_stops_new_buys(cfg_v3):
    closes = {(s, d): 30.0 for s in "AB" for d in JUNE if d > F1}  # -70% on both slots
    res = run(
        cfg_v3,
        market(cfg_v3, ["A", "B", "C", "D"], closes=closes),
        {F1: ["A", "B"], F2: ["C", "D"], F3: ["C", "D"]},
    )
    assert res.ruin_date == F2
    assert [o["day"] for o in res.orders if o["side"] == "buy"] == [F1, F1]
    assert res.counts[lg.RUIN] == 4
    assert {t["exit_date"] for t in res.trades} == {F2}  # holdings still sold


def test_null_selector_same_rules(cfg_v3):
    import random

    mk = market(cfg_v3, ["A", "B", "C", "D"])
    for wk in (F1, F2, F3):
        mk._eligible[wk] = ["A", "B", "C", "D"]
    rng = random.Random(1)
    res = lg.Ledger(
        mk,
        cfg_v3.execution.books["primary"],
        "null",
        lambda d: rng.sample(mk.eligible(d), 2),
        record=False,
    ).run(F1, F4)
    assert res.trades == [] and res.counts["ROUND_TRIPS"] >= 2  # invariants checked daily
    assert res.final_equity < 10_000


# ----------------------------------------------------------- invariants


def order(side="buy", slot=345, cause="REBALANCE", flags=(), qty=10, fill=100.0, cost=1.0, sym="A"):
    return dict(
        symbol=sym,
        side=side,
        slot=slot,
        cause=cause,
        flags=list(flags),
        qty=qty,
        fill=fill,
        cost_r=cost,
    )


def v(
    orders, cash_start=10_000.0, cash_end=None, reb=True, ruined=False, npos=1, divs=0.0, cfg=None
):
    if cash_end is None:
        cash_end = (
            cash_start
            + divs
            + sum(
                (-1 if o["side"] == "buy" else 1) * o["qty"] * o["fill"] - o["cost_r"]
                for o in orders
            )
        )
    return inv.violations(F1, reb, ruined, orders, divs, cash_start, cash_end, npos, cfg)


def test_invariants(cfg_v3):
    assert v([order()], cfg=cfg_v3) == []
    assert v([order(), order(sym="B"), order(sym="C")], npos=3, cfg=cfg_v3)[0].endswith("> 2")
    assert "outside the rebalance window" in v([order(slot=400)], cfg=cfg_v3)[0]
    assert "outside the rebalance window" in v([order()], reb=False, cfg=cfg_v3)[0]
    assert "after ruin" in v([order()], ruined=True, cfg=cfg_v3)[0]
    assert "unflagged sell" in v([order("sell", slot=5)], reb=False, cfg=cfg_v3)[0]
    assert v([order("sell", slot=5, flags=["EXIT_CARRIED"])], reb=False, cfg=cfg_v3) == []
    assert v([order("sell", slot=-1, cause="EXIT_DELISTED")], reb=False, cfg=cfg_v3) == []
    assert "cash negative" in v([order(qty=200)], cfg=cfg_v3)[0]
    assert "does not reconcile" in v([order()], cash_end=9_000.0, cfg=cfg_v3)[0]

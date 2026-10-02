"""V2 deliverable 4: equity-tracking sizing, ruin stop, books, labels, invariants.

Flat daily history (prev close 100, ATR 3); all stocks are non-F&O unless noted,
tick 0.01 (price < 250, from 2024-06-10). Primary slippage = 1 tick per fill.
"""

from datetime import date

import pytest

from orb.v2 import invariants as inv
from orb.v2.engine import INDEX_BOOK, RUIN, SKIPPED, TAKEN, EngineV2
from orb.v2.sizing import DayLimits, EquityBook
from tests.v2_market import CAL, builder, candle, flat, market_days

D1, D2, D3 = CAL[-3:]


def move(p0945: float, entry: float, exit_: float) -> dict:
    b = flat()
    candle(b, "09:44", 100.0, max(p0945, 100.0) + 0.1, min(p0945, 100.0) - 0.1, p0945)
    candle(b, "14:30", entry, entry + 0.1, entry - 0.1, entry)
    candle(b, "15:10", exit_, exit_ + 0.1, exit_ - 0.1, exit_)
    return b


def ticks_for(m):
    return {(s, d): 0.01 for s in m.symbols for d in CAL}


def engine(cfg_v2, m, fno=None, index_ctx=None):
    return EngineV2(cfg_v2, builder(m), ticks_for(m), fno, index_ctx or {})


@pytest.fixture(scope="module")
def three_days(cfg_v2):
    # D1: A, B, C, D qualify long (z 0.8, 0.7, 0.6, 0.5); top 3 = A, B, C; all +1%
    # D2: A long, entry 100 exit 90 (a 10% loss); D3: A long flat
    days = {
        D1: {
            "A": move(102.4, 100.0, 101.0),
            "B": move(102.1, 100.0, 101.0),
            "C": move(101.8, 100.0, 101.0),
            "D": move(101.5, 100.0, 101.0),
        },
        D2: {"A": move(102.4, 100.0, 90.0)},
        D3: {"A": move(102.4, 100.0, 100.0)},
    }
    m = market_days(cfg_v2, days)
    return engine(cfg_v2, m).run(D1, D3)


def test_equity_path_and_slot_sizing(three_days, cfg_v2):
    eq = three_days.equity.filter(three_days.equity["book"] == "primary").sort("date").to_dicts()
    assert eq[0]["equity_start"] == 10_000
    assert eq[0]["slot_value"] == pytest.approx(0.8 * 10_000 / 3)
    for prev, cur in zip(eq, eq[1:], strict=False):
        assert cur["equity_start"] == pytest.approx(prev["equity_end"])
        assert cur["slot_value"] == pytest.approx(0.8 * cur["equity_start"] / 3)
    b = three_days.book.filter(
        (three_days.book["book"] == "primary") & (three_days.book["book_decision"] == TAKEN)
    )
    d1 = b.filter(b["date"] == D1)
    assert sorted(d1["symbol"]) == ["A", "B", "C"]  # D (rank 4) not taken
    # qty = floor(2666.67 / 100.01) = 26
    assert set(d1["qty"]) == {26}
    d2 = b.filter(b["date"] == D2).row(0, named=True)
    assert d2["qty"] == int(eq[1]["slot_value"] // 100.01)  # sized from day-2 equity


def test_books_have_their_own_equity(three_days):
    e = three_days.equity
    ends = {r["book"]: r["equity_end"] for r in e.filter(e["date"] == D3).to_dicts()}
    assert set(ends) == {"primary", "primary_2x", "v1_model", INDEX_BOOK}
    assert ends["primary"] > ends["primary_2x"]  # more slippage, less equity
    assert ends["primary"] > ends["v1_model"]


def test_labels_cover_every_qualifier(three_days):
    lb = three_days.labels.filter(three_days.labels["date"] == D1)
    assert sorted(lb["symbol"]) == ["A", "B", "C", "D"]
    d = lb.filter(lb["symbol"] == "D").row(0, named=True)
    assert d["status"] == "OK" and d["ret_net"] == pytest.approx(d["net_pnl"] / d["notional"])


def test_ruin_stop(cfg_v2):
    # D1: three longs lose 70% (100 -> 30): equity ~ 10,000 - 0.7 x 8,000 < 5,000
    days = {
        D1: {s: move(102.4 - i * 0.3, 100.0, 30.0) for i, s in enumerate("ABC")},
        D2: {"A": move(102.4, 100.0, 101.0)},
        D3: {"A": move(102.4, 100.0, 101.0)},
    }
    res = engine(cfg_v2, market_days(cfg_v2, days)).run(D1, D3)
    e = res.equity.filter(res.equity["book"] == "primary").sort("date")
    assert e["equity_end"][0] < 5_000
    assert res.ruin["primary"] == D2
    b = res.book.filter((res.book["book"] == "primary") & (res.book["date"] > D1))
    assert set(b["book_decision"]) == {SKIPPED} and set(b["book_reason"]) == {RUIN}
    assert e["equity_end"][-1] == pytest.approx(e["equity_end"][0])  # no trades after ruin


def test_fno_stock_skips_guard(cfg_v2):
    import polars as pl

    from orb.refdata.fno import FnoCalendar

    b = move(102.4, 105.0, 101.0)
    candle(b, "14:30", 105.0, 105.0, 105.0, 105.0)  # upper circuit at the entry
    m = market_days(cfg_v2, {D1: {"A": b}})
    res = engine(cfg_v2, m).run(D1, D1)
    p = res.book.filter(res.book["book"] == "primary").row(0, named=True)
    assert (p["book_decision"], p["book_reason"]) == (SKIPPED, "LOCKED_CIRCUIT")
    fno = FnoCalendar(pl.DataFrame({"sample_date": [D1], "symbol": ["A"]}))
    p = (
        engine(cfg_v2, m, fno)
        .run(D1, D1)
        .book.filter(pl.col("book") == "primary")
        .row(0, named=True)
    )
    assert p["book_decision"] == TAKEN and p["guard"] is False


def test_index_book_trades_index_direction(cfg_v2):
    ix = flat(20000.0)
    candle(ix, "09:44", 20000, 19899, 19899, 19900)  # index -0.5% -> z = -0.333
    days = {D1: {"A": move(102.4, 100.0, 101.0), "B": move(98.5, 100.0, 99.0)}}
    m = market_days(cfg_v2, days, index={D1: ix})
    ctx = {("NIFTY 200", D1): {"atr14": 300.0, "prev_close": 20000.0, "atr_reason": None}}
    res = engine(cfg_v2, m, index_ctx=ctx).run(D1, D1)
    ib = res.book.filter(res.book["book"] == INDEX_BOOK)
    assert ib["symbol"].to_list() == ["B", "A"]  # alignment -z: B 0.5, A -0.8
    assert set(ib["side"]) == {"short"}
    assert res.index["qualified"][0] and res.index["side"][0] == "short"


# ------------------------------------------------------------- invariants


def lim(equity=10_000.0, ruined=False):
    return DayLimits(D1, equity, 0.8 * equity, 0.8 * equity / 3, ruined)


def trade(sym="A", **kw):
    t = dict(
        symbol=sym,
        notional=2600.0,
        entry_costs=1.0,
        entry_slot=315,  # 14:30
        exit_slot=355,  # 15:10
        exit_at="open",
        qty=26,
        entry_price=100.0,
    )
    return {**t, **kw}


def test_invariants_clean(cfg_v2):
    assert inv.violations([trade("A"), trade("B"), trade("C")], lim(), "primary", cfg_v2) == []


@pytest.mark.parametrize(
    "taken,limits,needle",
    [
        ([trade(s) for s in "ABCD"], lim(), "trades > 3"),
        (
            [trade("A", notional=5000.0, qty=50), trade("B", notional=3100.0, qty=31)],
            lim(),
            "deployed",
        ),
        ([trade("A"), trade("A")], lim(), "more than one trade"),
        ([trade("A")], lim(4_000.0, ruined=True), "after ruin"),
        ([trade("A", entry_slot=314)], lim(), "outside 14:30-14:35"),
        ([trade("A", entry_slot=321)], lim(), "outside 14:30-14:35"),
        ([trade("A", exit_slot=356)], lim(), "exit"),
        ([trade("A", exit_slot=355, exit_at="close")], lim(), "exit"),
        ([trade("A", qty=0, notional=0.0)], lim(), "qty"),
        ([trade("A", exit_slot=375, exit_reason="EXIT_LOCKED")], lim(), "exit"),  # past 15:29
    ],
)
def test_invariants_fire(cfg_v2, taken, limits, needle):
    v = inv.violations(taken, limits, "primary", cfg_v2)
    assert any(needle in x for x in v), v


def test_cash_invariant(cfg_v2):
    # deployable 8,000 of 10,000; 3 x 2,666 notional + huge entry charges -> cash < 0
    t = [trade(s, notional=2666.0, entry_costs=700.0) for s in "ABC"]
    assert any("cash negative" in x for x in inv.violations(t, lim(), "primary", cfg_v2))


def test_equity_path_check(cfg_v2):
    rows = [
        {
            "date": D1,
            "book": "p",
            "equity_start": 10_000.0,
            "day_net_rounded": -10.0,
            "equity_end": 9_990.0,
        },
        {
            "date": D2,
            "book": "p",
            "equity_start": 9_990.0,
            "day_net_rounded": 5.0,
            "equity_end": 9_995.0,
        },
    ]
    inv.check_equity_path(rows, cfg_v2)
    rows[1]["equity_start"] = 10_000.0
    with pytest.raises(inv.V2InvariantError):
        inv.check_equity_path(rows, cfg_v2)


def test_equity_book_order(cfg_v2):
    eb = EquityBook("p", cfg_v2)
    eb.limits(D2)
    eb.close_day(D2, 0.0)
    with pytest.raises(ValueError):
        eb.limits(D1)
    assert date(2024, 6, 26) == D1


def test_late_exit_allowed_only_when_locked(cfg_v2):
    ok = [
        trade("A", exit_slot=358, exit_reason="EXIT_LOCKED"),
        trade("B", exit_slot=374, exit_at="close", exit_reason="EXIT_LOCKED_UNFILLED"),
    ]
    assert inv.violations(ok, lim(), "primary", cfg_v2) == []
    bad = [trade("A", exit_slot=358, exit_reason="HARD_EXIT")]
    assert any("exit" in x for x in inv.violations(bad, lim(), "primary", cfg_v2))

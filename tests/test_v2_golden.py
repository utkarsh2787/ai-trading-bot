"""V2 golden day: fills, qty, every cost line and P&L computed by hand.

Day T = 2024-06-28 (Friday): tick 0.01 (price < Rs 250, regime from 2024-06-10).
Every stock: prev close 100, ATR14 3 -> atr_pct 0.03. Non-F&O, nothing locked.
equity_d = 10,000 -> Deployable 8,000 -> Slot_d = 2,666.666667.

  A long  P_0945 102.40 -> z +0.80 (rank 1); entry 14:30 open 101.20; exit 15:10 open 102.00
  B short P_0945  97.90 -> z -0.70 (rank 2); entry 14:30 open  98.10; 15:10 and 15:11
          LOCKED at an upper circuit (H = L = 104.00 = day's high so far) -> a short can't
          buy back -> forward to 15:12 (H 104.00, L 103.40: unlocked), open 103.50 (EXIT_LOCKED)
  C long  P_0945 101.80 -> z +0.60 (rank 3); 14:30 missing -> 14:31 open 101.75 (ENTRY_DELAYED);
          15:10 missing -> 15:09 close 101.10 (EXIT_SUBSTITUTED)
  D long  P_0945 101.50 -> z +0.50 (rank 4): not selected

PRIMARY slippage: 1 tick per fill, no rounding
  A buy 101.20 + 0.01 = 101.21; qty floor(2666.67 / 101.21 = 26.35) = 26;
    sell 102.00 - 0.01 = 101.99
    gross (101.99 - 101.21) x 26 = 20.28
  B sell 98.10 - 0.01 = 98.09; qty floor(2666.67 / 98.09 = 27.19) = 27; buy 103.50 + 0.01 = 103.51
    gross (98.09 - 103.51) x 27 = -146.34
  C buy 101.75 + 0.01 = 101.76; qty floor(26.21) = 26; sell 101.10 - 0.01 = 101.09
    gross (101.09 - 101.76) x 26 = -17.42
STRESS (V1 model): 1 tick + 0.02% of price, rounded against the trade to 0.01
  A buy 101.20 + 0.01 + 0.020240 = 101.230240 -> 101.24; qty 26;
    sell 102.00 - 0.030400 = 101.9696 -> 101.96
    gross (101.96 - 101.24) x 26 = 18.72
  B sell 98.10 - 0.029620 = 98.070380 -> 98.07; qty 27; buy 103.50 + 0.030700 = 103.5307 -> 103.54
    gross (98.07 - 103.54) x 27 = -147.69
  C buy 101.75 + 0.030350 = 101.780350 -> 101.79; qty 26;
    sell 101.10 - 0.030220 = 101.06978 -> 101.06
    gross (101.06 - 101.79) x 26 = -18.98

Charges per order (value V): brokerage min(20, 0.03% V); STT 0.025% V on the sell;
exchange 0.00297% V; stamp 0.003% V on the buy; SEBI Rs 10/crore; GST 18% x
(brokerage + exchange + SEBI). Contract note (per day, per book): STT and stamp day
totals rounded half-up to the rupee, allocated back pro rata.
  primary: STT 0.662935 + 0.6621075 + 0.657085 = 1.9821275 -> 2; stamp 0.2421597 -> 0
  stress:  STT 0.66274 + 0.6619725 + 0.65689 = 1.9816025 -> 2;   stamp 0.2422308 -> 0
"""

import pytest

from orb.sim.costs import Order, order_charges
from orb.v2.engine import TAKEN, EngineV2
from tests.v2_market import T, builder, candle, drop, flat, market

P = 1e-6

EXPECTED = {
    "primary": {
        "A": dict(
            side="long",
            entry=101.21,
            exit=101.99,
            qty=26,
            gross=20.28,
            slip=0.52,
            costs=2.8035208,
            net=17.4764792,
            costs_r=2.73055457,
            net_r=17.54944543,
            reason="HARD_EXIT",
            entry_order=dict(
                value=2631.46,
                brokerage=0.789438,
                stt=0.0,
                exchange=0.07815436,
                stamp=0.0789438,
                sebi=0.00263146,
                gst=0.15664029,
            ),
            exit_order=dict(
                value=2651.74,
                brokerage=0.795522,
                stt=0.662935,
                exchange=0.07875668,
                stamp=0.0,
                sebi=0.00265174,
                gst=0.15784748,
            ),
        ),
        "B": dict(
            side="short",
            entry=98.09,
            exit=103.51,
            qty=27,
            gross=-146.34,
            slip=0.54,
            costs=2.87002876,
            net=-149.21002876,
            costs_r=2.79215577,
            net_r=-149.13215577,
            reason="EXIT_LOCKED",
            entry_order=dict(
                value=2648.43,
                brokerage=0.794529,
                stt=0.6621075,
                exchange=0.07865837,
                stamp=0.0,
                sebi=0.00264843,
                gst=0.15765044,
            ),
            exit_order=dict(
                value=2794.77,
                brokerage=0.838431,
                stt=0.0,
                exchange=0.08300467,
                stamp=0.0838431,
                sebi=0.00279477,
                gst=0.16636148,
            ),
        ),
        "C": dict(
            side="long",
            entry=101.76,
            exit=101.09,
            qty=26,
            gross=-17.42,
            slip=0.52,
            costs=2.79454875,
            net=-20.21454875,
            costs_r=2.72110077,
            net_r=-20.14110077,
            reason="EXIT_SUBSTITUTED",
            entry_order=dict(
                value=2645.76,
                brokerage=0.793728,
                stt=0.0,
                exchange=0.07857907,
                stamp=0.0793728,
                sebi=0.00264576,
                gst=0.15749151,
            ),
            exit_order=dict(
                value=2628.34,
                brokerage=0.788502,
                stt=0.657085,
                exchange=0.0780617,
                stamp=0.0,
                sebi=0.00262834,
                gst=0.15645457,
            ),
        ),
        "equity_end": 9848.27618889,
    },
    "v1_model": {
        "A": dict(
            side="long",
            entry=101.24,
            exit=101.96,
            qty=26,
            gross=18.72,
            slip=2.08,
            costs=2.8033492,
            net=15.9166508,
            costs_r=2.73053498,
            net_r=15.98946502,
            reason="HARD_EXIT",
            entry_order=dict(
                value=2632.24,
                brokerage=0.789672,
                stt=0.0,
                exchange=0.07817753,
                stamp=0.0789672,
                sebi=0.00263224,
                gst=0.15668672,
            ),
            exit_order=dict(
                value=2650.96,
                brokerage=0.795288,
                stt=0.66274,
                exchange=0.07873351,
                stamp=0.0,
                sebi=0.00265096,
                gst=0.15780104,
            ),
        ),
        "B": dict(
            side="short",
            entry=98.07,
            exit=103.54,
            qty=27,
            gross=-147.69,
            slip=1.89,
            costs=2.87002342,
            net=-150.56002342,
            costs_r=2.79230188,
            net_r=-150.48230188,
            reason="EXIT_LOCKED",
            entry_order=dict(
                value=2647.89,
                brokerage=0.794367,
                stt=0.6619725,
                exchange=0.07864233,
                stamp=0.0,
                sebi=0.00264789,
                gst=0.1576183,
            ),
            exit_order=dict(
                value=2795.58,
                brokerage=0.838674,
                stt=0.0,
                exchange=0.08302873,
                stamp=0.0838674,
                sebi=0.00279558,
                gst=0.1664097,
            ),
        ),
        "C": dict(
            side="long",
            entry=101.79,
            exit=101.06,
            qty=26,
            gross=-18.98,
            slip=2.08,
            costs=2.79437715,
            net=-21.77437715,
            costs_r=2.72107961,
            net_r=-21.70107961,
            reason="EXIT_SUBSTITUTED",
            entry_order=dict(
                value=2646.54,
                brokerage=0.793962,
                stt=0.0,
                exchange=0.07860224,
                stamp=0.0793962,
                sebi=0.00264654,
                gst=0.15753794,
            ),
            exit_order=dict(
                value=2627.56,
                brokerage=0.788268,
                stt=0.65689,
                exchange=0.07803853,
                stamp=0.0,
                sebi=0.00262756,
                gst=0.15640814,
            ),
        ),
        "equity_end": 9843.80608353,
    },
}


def golden_market(cfg):
    s = {}
    for sym, (p, e, x) in {
        "A": (102.40, 101.20, 102.00),
        "B": (97.90, 98.10, 97.50),
        "C": (101.80, None, None),
        "D": (101.50, 100.0, 100.0),
    }.items():
        b = flat()
        candle(b, "09:44", 100.0, max(p, 100) + 0.1, min(p, 100) - 0.1, p)
        if e is not None:
            candle(b, "14:30", e, e + 0.1, e - 0.1, e)
            candle(b, "15:10", x, x + 0.1, x - 0.1, x)
        s[sym] = b
    for t in ("15:10", "15:11"):  # B: upper circuit, H = L = 104.00 = the day's high so far
        candle(s["B"], t, 104.00, 104.00, 104.00, 104.00)
    candle(s["B"], "15:12", 103.50, 104.00, 103.40, 103.60)  # unlocked: H != L
    drop(s["C"], "14:30", "15:10")
    candle(s["C"], "14:31", 101.75, 101.85, 101.65, 101.75)
    candle(s["C"], "15:09", 101.05, 101.15, 101.00, 101.10)
    return market(cfg, s)


@pytest.fixture(scope="module")
def golden(cfg_v2):
    m = golden_market(cfg_v2)
    ticks = {(sym, T): 0.01 for sym in m.symbols}
    return EngineV2(cfg_v2, builder(m), ticks, None, {}).run(T, T)


def test_signals(golden):
    r = {x["symbol"]: x for x in golden.signals.to_dicts()}
    assert [round(r[s]["z"], 9) for s in "ABCD"] == [0.8, -0.7, 0.6, 0.5]
    assert [r[s]["rank"] for s in "ABCD"] == [1, 2, 3, 4]
    assert [r[s]["selected"] for s in "ABCD"] == [True, True, True, False]


@pytest.mark.parametrize("book", ["primary", "v1_model"])
@pytest.mark.parametrize("sym", ["A", "B", "C"])
def test_fills_qty_pnl(golden, cfg_v2, book, sym):
    b = golden.book.filter((golden.book["book"] == book) & (golden.book["symbol"] == sym))
    r, e = b.row(0, named=True), EXPECTED[book][sym]
    assert r["book_decision"] == TAKEN and r["side"] == e["side"]
    assert r["entry_price"] == pytest.approx(e["entry"], abs=P)
    assert r["exit_price"] == pytest.approx(e["exit"], abs=P)
    assert r["qty"] == e["qty"] and r["exit_reason"] == e["reason"]
    assert r["gross_pnl"] == pytest.approx(e["gross"], abs=P)
    assert r["slippage_paid"] == pytest.approx(e["slip"], abs=P)
    assert r["costs"] == pytest.approx(e["costs"], abs=P)
    assert r["net_pnl"] == pytest.approx(e["net"], abs=P)
    assert r["costs_rounded"] == pytest.approx(e["costs_r"], abs=P)
    assert r["net_pnl_rounded"] == pytest.approx(e["net_r"], abs=P)
    sides = ("buy", "sell") if e["side"] == "long" else ("sell", "buy")
    for leg, side, px in (
        ("entry_order", sides[0], e["entry"]),
        ("exit_order", sides[1], e["exit"]),
    ):
        got = order_charges(Order(T, side, e["qty"], px), cfg_v2.costs)
        x = e[leg]
        assert e["qty"] * px == pytest.approx(x["value"], abs=P)
        for k, ck in (
            ("brokerage", "brokerage"),
            ("stt", "stt"),
            ("exchange", "exchange_txn"),
            ("stamp", "stamp"),
            ("sebi", "sebi_fee"),
            ("gst", "gst"),
        ):
            assert got[ck] == pytest.approx(x[k], abs=1e-7), (leg, k)


def test_c_flags(golden):
    r = golden.book.filter((golden.book["book"] == "primary") & (golden.book["symbol"] == "C")).row(
        0, named=True
    )
    assert "ENTRY_DELAYED" in r["flags"] and "EXIT_FALLBACK" in r["flags"]
    assert r["exit_at"] == "close"


@pytest.mark.parametrize("book", ["primary", "v1_model"])
def test_equity_end(golden, book):
    e = golden.equity.filter(golden.equity["book"] == book).row(0, named=True)
    assert e["equity_start"] == 10_000 and e["slot_value"] == pytest.approx(8000 / 3)
    assert e["equity_end"] == pytest.approx(EXPECTED[book]["equity_end"], abs=P)
    assert e["taken"] == 3


def test_d_not_in_book(golden):
    assert "D" not in golden.book.filter(golden.book["book"] == "primary")["symbol"].to_list()


def test_b_forward_locked_exit(golden):
    for book in ("primary", "v1_model"):
        r = golden.book.filter((golden.book["book"] == book) & (golden.book["symbol"] == "B")).row(
            0, named=True
        )
        assert r["exit_reason"] == "EXIT_LOCKED" and r["exit_at"] == "open"
        assert r["exit_slot"] == 357 and r["exit_raw"] == pytest.approx(103.50)  # 15:12 open

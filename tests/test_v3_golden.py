"""V3 golden weeks: every fill, qty, CNC cost line, DP, dividend, split and the
cash path computed by hand. Primary book (1 tick = Rs 0.01 per fill, no rounding).

Prices (minute sessions flat at the day's price; official close = the same):
  A 200.00 to 06-10; 1:2 split ex 06-11 (price factor 0.5) -> 105.00; 110.00 on 06-21
  B 150.00 on 06-07; 155.00 after; dividend Rs 2 per share ex 06-12
  C 120.00 to 06-17; 125.00 from 06-18; demerger ex 06-19 (Wed)

Fri 06-07 rebalance 1: targets A, B. equity_w 10,000 -> Slot_w = 0.9 x 10,000 / 2 = 4,500
  A buy 200.00 + 0.01 = 200.01; qty floor(4500 / 200.01 = 22.50) = 22; value 4,400.22
  B buy 150.00 + 0.01 = 150.01; qty floor(4500 / 150.01 = 29.998) = 29; value 4,350.29
Tue 06-11: A split -> 44 shares (kept: no trade)
Wed 06-12: B dividend 29 x 2 = 58.00
Fri 06-14 rebalance 2: targets A, C -> A kept; B sold; C bought
  equity_w = cash 1,297.17300402 + 44 x 105 + 29 x 155 = 10,412.17300402
  Slot_w = 0.9 x 10,412.17300402 / 2 = 4,685.47785181
  B sell 155.00 - 0.01 = 154.99 (x 29)
  C buy 120.01; qty floor(4685.48 / 120.01 = 39.04) = 39 (cash 5,771.30 covers it)
Tue 06-18: C demerger tomorrow -> sold at the 15:00 open, 125.00 - 0.01 = 124.99 (EXIT_CORP_ACTION)
Fri 06-21: last rebalance of the run -> A sold, 110.00 - 0.01 = 109.99 x 44 (FORCED_END)

CNC charges per order (value V): brokerage 0; STT 0.1% V (buy and sell); exchange
0.00297% V; stamp 0.015% V (buy); SEBI Rs 10/crore; GST 18% x (exchange + SEBI);
DP Rs 15.93 per stock per sell day. Contract note per day: STT and stamp totals
rounded half-up to the rupee, allocated pro rata:
  06-07: STT 4.40022 + 4.35029 = 8.75051 -> 9; stamp 0.660033 + 0.6525435 = 1.3125765 -> 1
  06-14: STT 4.49471 + 4.68039 = 9.1751 -> 9;   stamp 0.7020585 -> 1
  06-18: STT 4.87461 -> 5.   06-21: STT 4.83956 -> 5
"""

from datetime import date

import pytest

from orb.v3 import ledger as lg
from orb.v3.costs import CnCOrder, order_charges
from orb.v3.market import Market
from tests.v3_market import CAL, day_bars, make_data

F1, F2, F3 = date(2024, 6, 7), date(2024, 6, 14), date(2024, 6, 21)
D18 = date(2024, 6, 18)
P = 1e-6


def price(s: str, d: date) -> float:
    if s == "A":
        return 200.0 if d < date(2024, 6, 11) else (110.0 if d >= F3 else 105.0)
    if s == "B":
        return 150.0 if d <= F1 else 155.0
    return 120.0 if d < D18 else 125.0


ORDERS = {  # value, stt, exchange_txn, stamp, sebi_fee, gst, cost rounded (incl. DP)
    ("A", "buy"): (
        22,
        200.01,
        4400.22,
        4.40022,
        0.13068653,
        0.660033,
        0.00440022,
        0.02431562,
        5.18793214,
    ),
    ("B", "buy"): (
        29,
        150.01,
        4350.29,
        4.35029,
        0.12920361,
        0.6525435,
        0.00435029,
        0.0240397,
        5.12906384,
    ),
    ("B", "sell"): (
        29,
        154.99,
        4494.71,
        4.49471,
        0.13349289,
        0.0,
        0.00449471,
        0.02483777,
        20.50175715,
    ),
    ("C", "buy"): (
        39,
        120.01,
        4680.39,
        4.68039,
        0.13900758,
        0.7020585,
        0.00468039,
        0.02586384,
        5.76062003,
    ),
    ("C", "sell"): (
        39,
        124.99,
        4874.61,
        4.87461,
        0.14477592,
        0.0,
        0.00487461,
        0.02693709,
        21.10658762,
    ),
    ("A", "sell"): (
        44,
        109.99,
        4839.56,
        4.83956,
        0.14373493,
        0.0,
        0.00483956,
        0.02674341,
        21.1053179,
    ),
}
TRADES = {  # gross, dividends, net rounded, net unrounded, slippage, exit cause, exit date
    "A": (439.34, 0.0, 413.04674996, 413.17546673, 0.66, "FORCED_END", F3),
    "B": (144.42, 58.0, 176.78917901, 176.67203753, 0.58, "REBALANCE", F2),
    "C": (194.22, 0.0, 167.35279235, 167.68680207, 0.78, "EXIT_CORP_ACTION", D18),
}
CASH = {
    F1: 1239.17300402,
    date(2024, 6, 12): 1297.17300402,
    F2: 1085.23062685,
    D18: 5938.73403923,
    F3: 10757.18872133,
}


@pytest.fixture(scope="module")
def golden(cfg_v3):
    june = [d for d in CAL if d >= date(2024, 6, 3)]
    syms = ["A", "B", "C"]
    closes = {(s, d): price(s, d) for s in syms for d in CAL}
    minute = {(s, d): day_bars(price(s, d)) for s in syms for d in june}
    acts = [
        ("A", date(2024, 6, 11), "split", 0.5, "Face Value Split From Rs 10 To Rs 5"),
        ("B", date(2024, 6, 12), "dividend", None, "Interim Dividend - Rs 2 Per Share"),
        ("C", date(2024, 6, 19), "demerger", None, "Demerger"),
    ]
    mk = Market(make_data(cfg_v3, syms, minute, closes, actions=acts))
    picks = {F1: ["A", "B"], F2: ["A", "C"]}
    return lg.Ledger(
        mk, cfg_v3.execution.books["primary"], "primary", lambda d: picks.get(d, [])
    ).run(F1, F3)


def test_orders_and_every_cost_line(golden, cfg_v3):
    got = {(o["symbol"], o["side"]): o for o in golden.orders}
    assert set(got) == set(ORDERS)
    for k, (qty, fill, value, stt, ex, stamp, sebi, gst, cost_r) in ORDERS.items():
        o = got[k]
        assert (o["qty"], o["fill"]) == (qty, pytest.approx(fill, abs=P)), k
        ch = order_charges(CnCOrder(o["day"], k[1], qty, fill), cfg_v3.costs)
        assert qty * fill == pytest.approx(value, abs=P)
        for name, want in (
            ("brokerage", 0.0),
            ("stt", stt),
            ("exchange_txn", ex),
            ("stamp", stamp),
            ("sebi_fee", sebi),
            ("gst", gst),
        ):
            assert ch[name] == pytest.approx(want, abs=1e-7), (k, name)
            assert o["charges"][name] == pytest.approx(want, abs=1e-7), (k, name)
        assert o["dp"] == (15.93 if k[1] == "sell" else 0.0)
        assert o["cost_r"] == pytest.approx(cost_r, abs=P), k


def test_trades(golden):
    t = {x["symbol"]: x for x in golden.trades}
    for s, (gross, div, net_r, net_u, slip, cause, exit_day) in TRADES.items():
        assert t[s]["gross_pnl"] == pytest.approx(gross, abs=P), s
        assert t[s]["dividends"] == pytest.approx(div, abs=P)
        assert t[s]["net_pnl_rounded"] == pytest.approx(net_r, abs=P), s
        assert t[s]["net_pnl"] == pytest.approx(net_u, abs=P), s
        assert t[s]["slippage_paid"] == pytest.approx(slip, abs=P)
        assert (t[s]["exit_cause"], t[s]["exit_date"]) == (cause, exit_day)
    assert (t["A"]["entry_qty"], t["A"]["exit_qty"]) == (22, 44)  # split during the hold


def test_kept_position_has_no_trade_at_rebalance_2(golden):
    assert [o for o in golden.orders if o["symbol"] == "A" and o["day"] == F2] == []
    r2 = next(r for r in golden.rebalances if r["date"] == F2)
    assert r2["equity_w"] == pytest.approx(10412.17300402, abs=P)
    assert r2["slot_w"] == pytest.approx(4685.47785181, abs=P)
    assert r2["held_after"] == "A;C"


def test_cash_path(golden):
    cash = {r["date"]: r["cash"] for r in golden.daily}
    for d, want in CASH.items():
        assert cash[d] == pytest.approx(want, abs=P), d
    assert golden.final_equity == pytest.approx(10757.18872133, abs=P)
    assert golden.final_equity == pytest.approx(10_000 + sum(TRADES[s][2] for s in TRADES), abs=P)

from datetime import date

import pytest

from orb.sim.costs import Order, charges_frame, order_charges

D = date(2025, 1, 6)


def test_buy_order_charges(cfg):
    c = order_charges(Order(D, "buy", 100, 500.0), cfg.costs)  # value 50,000
    assert c["brokerage"] == pytest.approx(15.0)  # 0.03% < Rs 20
    assert c["stt"] == 0.0
    assert c["exchange_txn"] == pytest.approx(1.485)
    assert c["stamp"] == pytest.approx(1.5)
    assert c["sebi_fee"] == pytest.approx(0.05)
    assert c["gst"] == pytest.approx(0.18 * (15.0 + 1.485 + 0.05))  # no STT / stamp


def test_sell_order_charges_and_brokerage_cap(cfg):
    c = order_charges(Order(D, "sell", 1000, 510.0), cfg.costs)  # value 5.1 lakh
    assert c["brokerage"] == 20.0
    assert c["stt"] == pytest.approx(127.5)
    assert c["stamp"] == 0.0
    assert c["gst"] == pytest.approx(0.18 * (20.0 + 510000 * 0.0000297 + 0.51))


def test_contract_note_rounding_aggregates_per_day(cfg):
    orders = [
        Order(D, "sell", 10, 1000.0, "a"),  # STT 2.50
        Order(D, "sell", 10, 1040.0, "b"),  # STT 2.60 -> day 5.10 -> 5
        Order(date(2025, 1, 7), "sell", 1, 1000.0, "c"),  # STT 0.25 -> 0
        Order(D, "buy", 20, 1000.0, "d"),  # stamp 0.60 -> 1
    ]
    raw = charges_frame(orders, cfg.costs, round_stt_stamp=False)
    rnd = charges_frame(orders, cfg.costs, round_stt_stamp=True)
    day = rnd.filter(rnd["day"] == D)
    assert day["stt"].sum() == pytest.approx(5.0)
    assert day["stamp"].sum() == pytest.approx(1.0)
    assert rnd.filter(rnd["order_id"] == "c")["stt"][0] == 0.0
    a, b = (rnd.filter(rnd["order_id"] == k)["stt"][0] for k in "ab")
    assert a / b == pytest.approx(2.5 / 2.6)  # pro-rata allocation
    # rounding touches only STT and stamp
    for col in ("brokerage", "exchange_txn", "sebi_fee", "gst"):
        assert rnd[col].to_list() == pytest.approx(raw[col].to_list())
    assert raw["stt"].sum() == pytest.approx(5.35)


def test_costs_are_nse_only(cfg):
    assert cfg.costs.exchange == "NSE" and cfg.execution.exchange == "NSE"

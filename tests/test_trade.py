"""Golden fills: every number below is computed by hand from the rules."""

from datetime import date

import numpy as np
import pytest

from orb.features import SessionArrays
from orb.sim.costs import Order, order_charges
from orb.sim.trade import INVALID_ENTRY_GAP, NO_ENTRY_DATA, OK, simulate

N = 375
D = date(2024, 1, 5)
TICK = 0.05
SIG = 29  # signal candle 09:44 -> entry at slot 30 (09:45)
HARD = 355  # 15:10


def bars(o=100.0, lo_pad=0.2, hi_pad=0.2, edits=None):
    op = np.full(N, o)
    cl = np.full(N, o)
    hi = op + hi_pad
    lo = op - lo_pad
    for i, kv in (edits or {}).items():
        for k, v in kv.items():
            {"o": op, "c": cl, "h": hi, "l": lo}[k][i] = v
    return SessionArrays(op, hi, lo, cl, np.full(N, 1000.0))


def sim(a, side="long", or_h=99.9, or_l=99.0, score=100.0, variant="default", mult=1.0, cfg=None):
    return simulate(a, D, SIG, side, or_h, or_l, score, TICK, cfg, variant, mult)


def test_long_hard_exit_golden(cfg):
    a = bars(edits={HARD: {"o": 102.0}})
    r = sim(a, cfg=cfg)
    # entry: 100 + (0.05 + 0.0002*100) = 100.07 -> buy rounds UP to 100.10
    assert r.status == OK and r.entry_slot == 30 and r.entry_price == 100.10
    # exit: 102 - (0.05 + 0.0002*102) = 101.9296 -> sell rounds DOWN to 101.90
    assert r.exit_reason == "HARD_EXIT" and r.exit_slot == HARD and r.exit_price == 101.90
    assert r.stop == 99.0 and r.risk_per_share == pytest.approx(1.10)
    # qty = floor(min(8000/3 * 1.0 / 100.10, 100 / 1.10)) = floor(min(26.64, 90.9)) = 26
    assert r.qty == 26 and r.notional == pytest.approx(2602.60)
    assert r.gross_pnl == pytest.approx(26 * 1.80)
    assert r.slippage_paid == pytest.approx(26 * (0.10 + 0.10))
    costs = sum(order_charges(Order(D, "buy", 26, 100.10), cfg.costs).values()) + sum(
        order_charges(Order(D, "sell", 26, 101.90), cfg.costs).values()
    )
    assert r.costs == pytest.approx(costs)
    assert r.net_pnl == pytest.approx(26 * 1.80 - costs)
    assert r.r_gross == pytest.approx(1.80 / 1.10)
    assert r.hold_minutes == HARD - 30


def test_long_stop_gap_fills_at_open(cfg):
    a = bars(edits={31: {"o": 98.5, "l": 98.3}})
    r = sim(a, cfg=cfg)
    # min(stop 99, open 98.5) - (0.05 + 0.0197) = 98.4303 -> 98.40
    assert (r.exit_reason, r.exit_slot, r.exit_price) == ("STOP", 31, 98.40)
    assert r.gross_per_share == pytest.approx(98.40 - 100.10)
    assert r.mae_per_share == pytest.approx(100.10 - 98.3)


def test_entry_candle_stop_only_in_conservative_variant(cfg):
    a = bars(edits={30: {"l": 98.9}})  # entry candle trades through the stop
    d = sim(a, cfg=cfg)
    c = sim(a, variant="conservative", cfg=cfg)
    assert d.exit_reason == "HARD_EXIT"
    # stop - slippage: 99 - (0.05 + 0.0198) = 98.9302 -> 98.90
    assert (c.exit_reason, c.exit_slot, c.exit_price) == ("STOP", 30, 98.90)


def test_short_mirror(cfg):
    a = bars(edits={40: {"o": 100.5, "h": 101.2}})
    r = sim(a, side="short", or_h=101.0, or_l=100.2, cfg=cfg)
    # entry sell: 100 - 0.07 = 99.93 -> round down 99.90; stop 101.0, risk 1.10
    assert r.entry_price == 99.90 and r.risk_per_share == pytest.approx(1.10)
    # stop: max(101, 100.5) + (0.05 + 0.0202) = 101.0702 -> buy rounds up 101.10
    assert (r.exit_reason, r.exit_slot, r.exit_price) == ("STOP", 40, 101.10)
    assert r.gross_per_share == pytest.approx(99.90 - 101.10)


def test_invalid_entry_gap(cfg):
    a = bars(edits={30: {"o": 98.0}})  # opens below the long stop
    r = sim(a, cfg=cfg)
    assert r.status == INVALID_ENTRY_GAP and r.qty is None


def test_risk_cap_binds_and_qty_below_one(cfg):
    a = bars()
    wide = sim(a, or_l=90.0, cfg=cfg)  # risk 10.10 -> 100/10.10 = 9.9 -> 9
    assert wide.qty == 9 and wide.initial_risk == pytest.approx(9 * 10.10)
    pricey = simulate(bars(o=5000.0), D, SIG, "long", 4990.0, 4900.0, 65.0, TICK, cfg)
    # 8000/3*0.65 = 1733 < 5001 -> qty 0: per-share results only
    assert pricey.status == OK and pricey.qty == 0 and pricey.net_pnl is None
    assert pricey.gross_per_share is not None


def test_missing_entry_and_exit_candles(cfg):
    a = bars(edits={HARD - 1: {"c": 101.0}, HARD + 1: {"o": 105.0}})
    for k in (a.open, a.high, a.low, a.close):
        k[30] = np.nan  # no entry candle
        k[HARD] = np.nan  # no 15:10 candle
    r = sim(a, cfg=cfg)
    assert r.entry_slot == 31 and "ENTRY_DELAYED" in r.flags
    # never held past 15:10: exits at the 15:09 close, not the 15:11 open
    assert r.exit_slot == HARD - 1 and r.exit_raw == 101.0 and "EXIT_FALLBACK" in r.flags
    assert r.exit_reason == "EXIT_SUBSTITUTED"
    late = bars()
    for k in (late.open, late.high, late.low, late.close):
        k[316:330] = np.nan  # signal 14:30 (slot 315), no 14:31 candle
    assert simulate(late, D, 315, "long", 99.9, 99.0, 100.0, TICK, cfg).status == NO_ENTRY_DATA
    empty = bars()
    for k in (empty.open, empty.high, empty.low, empty.close):
        k[30:] = np.nan
    assert sim(empty, cfg=cfg).status == NO_ENTRY_DATA


def test_slippage_stress_scales_both_parts(cfg):
    a = bars(edits={HARD: {"o": 102.0}})
    r2 = sim(a, mult=2.0, cfg=cfg)
    # 100 + 2*(0.05 + 0.02) = 100.14 -> 100.15 ; 102 - 2*(0.0704) = 101.8592 -> 101.85
    assert (r2.entry_price, r2.exit_price) == (100.15, 101.85)

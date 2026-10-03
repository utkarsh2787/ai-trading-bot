"""Zerodha delivery (CNC) charges on NSE equity (``config/costs_cnc.yaml``).

Per order (value V): brokerage min(cap, pct x V) (Rs 0 for delivery); STT on the
buy AND the sell; exchange txn; stamp duty on the buy; SEBI fee; GST on the
configured base. DP charge: flat per stock per sell day, GST included.
Contract note: the day's STT and stamp totals rounded half-up to the rupee and
allocated back pro rata (as V1 / V2).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from orb.v3.config import CnCCostTable, CnCSchedule

CHARGES = ["brokerage", "stt", "exchange_txn", "stamp", "sebi_fee", "gst"]


@dataclass(frozen=True)
class CnCOrder:
    day: date
    side: str  # "buy" | "sell"
    qty: int
    price: float


def schedule_on(table: CnCCostTable, day: date) -> CnCSchedule:
    eligible = [s for s in table.schedules if s.effective_from <= day]
    if not eligible:
        raise ValueError(f"no CNC cost schedule in force on {day}")
    return eligible[-1]


def order_charges(o: CnCOrder, table: CnCCostTable) -> dict[str, float]:
    s = schedule_on(table, o.day)
    v = o.qty * o.price
    c = {
        "brokerage": min(s.brokerage_cap, s.brokerage_pct * v),
        "stt": (s.stt_buy_pct if o.side == "buy" else s.stt_sell_pct) * v,
        "exchange_txn": s.exchange_txn_pct * v,
        "stamp": s.stamp_buy_pct * v if o.side == "buy" else 0.0,
        "sebi_fee": s.sebi_per_crore * v / 1e7,
    }
    c["gst"] = s.gst_pct * sum(c[k] for k in table.gst_on)
    return c


def dp_charge(table: CnCCostTable, day: date) -> float:
    return schedule_on(table, day).dp_per_scrip_sell_day


def round_day(charges: list[dict[str, float]]) -> list[float]:
    """Totals per order after contract-note rounding of the day's STT and stamp."""
    out = [sum(c[k] for k in CHARGES) for c in charges]
    for col in ("stt", "stamp"):
        tot = sum(c[col] for c in charges)
        if tot <= 0:
            continue
        rounded = math.floor(tot + 0.5)
        for i, c in enumerate(charges):
            out[i] += rounded * c[col] / tot - c[col]
    return out

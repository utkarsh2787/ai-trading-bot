"""Zerodha intraday equity charges on NSE.

Per executed order: brokerage, STT (sell), exchange txn, stamp duty (buy), SEBI
fee, GST on the configured base. With ``round_stt_stamp_to_rupee`` the day's
STT and stamp duty totals are rounded to the nearest rupee (as on a contract
note, which covers one trading day) and the rounded total is allocated back to
the day's orders pro rata, so per-trade costs still sum to the contract note.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

import polars as pl

from orb.config import CostSchedule, CostTable

CHARGES = ["brokerage", "stt", "exchange_txn", "stamp", "sebi_fee", "gst"]


@dataclass(frozen=True)
class Order:
    day: date
    side: Literal["buy", "sell"]
    qty: int
    price: float
    order_id: str = ""


def schedule_on(table: CostTable, day: date) -> CostSchedule:
    eligible = [s for s in table.schedules if s.effective_from <= day]
    if not eligible:
        raise ValueError(f"no cost schedule in force on {day}")
    return eligible[-1]


def order_charges(order: Order, table: CostTable) -> dict[str, float]:
    """Unrounded charges for one executed order."""
    s = schedule_on(table, order.day)
    value = order.qty * order.price
    c = {
        "brokerage": min(s.brokerage_cap, s.brokerage_pct * value),
        "stt": s.stt_sell_pct * value if order.side == "sell" else 0.0,
        "exchange_txn": s.exchange_txn_pct * value,
        "stamp": s.stamp_buy_pct * value if order.side == "buy" else 0.0,
        "sebi_fee": s.sebi_per_crore * value / 1e7,
    }
    c["gst"] = s.gst_pct * sum(c[k] for k in table.gst_on)
    return c


def charges_frame(orders: list[Order], table: CostTable, round_stt_stamp: bool) -> pl.DataFrame:
    """One row per order with every charge and ``total``."""
    rows = [
        {
            "order_id": o.order_id,
            "day": o.day,
            "side": o.side,
            "value": o.qty * o.price,
            **order_charges(o, table),
        }
        for o in orders
    ]
    schema = {
        "order_id": pl.String,
        "day": pl.Date,
        "side": pl.String,
        "value": pl.Float64,
        **{k: pl.Float64 for k in CHARGES},
    }
    df = pl.DataFrame(rows, schema=schema)
    if round_stt_stamp and df.height:
        for col in ("stt", "stamp"):
            day_total = pl.col(col).sum().over("day")
            rounded = (day_total + 0.5).floor()  # half-up to the rupee
            share = pl.when(day_total > 0).then(pl.col(col) / day_total).otherwise(0.0)
            df = df.with_columns((rounded * share).alias(col))
    return df.with_columns(total=pl.sum_horizontal(CHARGES))

"""V2 single-trade simulation on one stock-day's slot arrays (raw prices).

Entry  open of the 14:30 candle + slippage; missing -> first candle 14:31..14:35;
       none -> ENTRY_MISSING.
Exit   open of the 15:10 candle - slippage; missing -> close of the last candle
       before 15:10 (EXIT_SUBSTITUTED).
No stop, no target.

Circuit-lock guard (stocks NOT in F&O that day). A candle is locked for a BUY
order if high == low == the day's high so far (from 09:15, incl. that candle),
and for a SELL order if high == low == the day's low so far.
  * entry candle locked for the entry order -> LOCKED_CIRCUIT, no trade;
  * exit candle locked for the exit order -> close of the first unlocked candle
    going back from 15:10 (not before the entry candle), else the 15:10 open;
    both EXIT_LOCKED.

Slippage per fill = ticks x tick + pct x price, against the trade; the fill is
rounded against the trade to the tick only when the model says so (V1 model).
qty = floor(slot value / entry fill); < 1 -> QTY_ZERO.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date

import numpy as np

from orb.features import SessionArrays, slot
from orb.sim.costs import Order, order_charges
from orb.sim.ticks import round_to_tick
from orb.v2.config import ConfigV2, SlippageModel

OK = "OK"
ENTRY_MISSING = "ENTRY_MISSING"
LOCKED_CIRCUIT = "LOCKED_CIRCUIT"
QTY_ZERO = "QTY_ZERO"
NO_TICK = "NO_TICK"

HARD_EXIT = "HARD_EXIT"
EXIT_SUBSTITUTED = "EXIT_SUBSTITUTED"
EXIT_LOCKED = "EXIT_LOCKED"

EPS = 1e-9


@dataclass
class TradeV2:
    status: str
    side: str
    entry_slot: int | None = None
    entry_raw: float | None = None
    entry_price: float | None = None
    exit_slot: int | None = None  # the candle used
    exit_at: str | None = None  # "open" | "close" of exit_slot
    exit_raw: float | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    flags: str = ""
    qty: int | None = None
    notional: float | None = None
    raw_move: float | None = None  # side x (exit_raw - entry_raw) x qty
    slippage_paid: float | None = None
    gross_pnl: float | None = None  # raw_move - slippage_paid
    costs: float | None = None  # unrounded, both orders
    entry_costs: float | None = None
    net_pnl: float | None = None
    hold_minutes: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def fill(raw: float, order_side: str, tick: float, m: SlippageModel) -> float:
    slip = m.ticks * tick + m.pct * raw
    px = raw + slip if order_side == "buy" else raw - slip
    if m.round_to_tick:
        return round_to_tick(px, tick, order_side)
    return round(px, 6)  # float hygiene only; no tick rounding


def locked(a: SessionArrays, i: int, order_side: str) -> bool:
    """Upper circuit blocks a buy, lower circuit blocks a sell (heuristic, 1-min bars)."""
    h, lo = a.high[i], a.low[i]
    if np.isnan(h) or abs(h - lo) > EPS:
        return False
    if order_side == "buy":
        return abs(h - np.nanmax(a.high[: i + 1])) <= EPS
    return abs(lo - np.nanmin(a.low[: i + 1])) <= EPS


def entry_slot(a: SessionArrays, cfg: ConfigV2) -> int | None:
    s = cfg.session
    for i in range(slot(s, s.entry_candle), slot(s, s.entry_last) + 1):
        if not np.isnan(a.open[i]):
            return i
    return None


def simulate_v2(
    a: SessionArrays,
    day: date,
    side: str,
    slot_value: float,
    tick: float | None,
    model: SlippageModel,
    guard: bool,
    cfg: ConfigV2,
) -> TradeV2:
    """``guard``: apply the circuit-lock check (stock not in F&O that day)."""
    if tick is None:
        return TradeV2(NO_TICK, side)
    buy, sell = ("buy", "sell") if side == "long" else ("sell", "buy")  # entry, exit orders
    e = entry_slot(a, cfg)
    if e is None:
        return TradeV2(ENTRY_MISSING, side)
    flags = []
    if e != slot(cfg.session, cfg.session.entry_candle):
        flags.append("ENTRY_DELAYED")
    if guard and locked(a, e, buy):
        return TradeV2(LOCKED_CIRCUIT, side, entry_slot=e, entry_raw=float(a.open[e]))
    entry_raw = float(a.open[e])
    entry = fill(entry_raw, buy, tick, model)
    qty = math.floor(slot_value / entry + EPS) if entry > 0 else 0
    if qty < 1:
        return TradeV2(
            QTY_ZERO, side, entry_slot=e, entry_raw=entry_raw, entry_price=entry, qty=qty
        )

    x = slot(cfg.session, cfg.session.exit_candle)
    if not np.isnan(a.open[x]):
        j, at, reason = x, "open", HARD_EXIT
    else:
        present = np.flatnonzero(~np.isnan(a.close[e:x])) + e
        j, at, reason = int(present[-1]), "close", EXIT_SUBSTITUTED
        flags.append("EXIT_FALLBACK")
    if guard and locked(a, j, sell):
        back = [
            i
            for i in range(j - 1, e - 1, -1)
            if not np.isnan(a.close[i]) and not locked(a, i, sell)
        ]
        if back:
            j, at = back[0], "close"
        reason = EXIT_LOCKED
    exit_raw = float(a.open[j] if at == "open" else a.close[j])
    exit_price = fill(exit_raw, sell, tick, model)

    sign = 1.0 if side == "long" else -1.0
    raw_move = sign * (exit_raw - entry_raw) * qty
    slip = qty * (abs(entry - entry_raw) + abs(exit_price - exit_raw))
    gross = sign * (exit_price - entry) * qty
    c_in = sum(order_charges(Order(day, buy, qty, entry), cfg.costs).values())
    c_out = sum(order_charges(Order(day, sell, qty, exit_price), cfg.costs).values())
    return TradeV2(
        OK,
        side,
        entry_slot=e,
        entry_raw=entry_raw,
        entry_price=entry,
        exit_slot=j,
        exit_at=at,
        exit_raw=exit_raw,
        exit_price=exit_price,
        exit_reason=reason,
        flags=";".join(flags),
        qty=qty,
        notional=qty * entry,
        raw_move=raw_move,
        slippage_paid=slip,
        gross_pnl=gross,
        costs=c_in + c_out,
        entry_costs=c_in,
        net_pnl=gross - c_in - c_out,
        hold_minutes=(j + (1 if at == "close" else 0)) - e,
    )

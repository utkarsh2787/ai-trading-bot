"""Single-trade simulation on one stock-day's slot arrays (raw prices).

Rules (spec + decisions 4, 5, 17, 20, 22, 25):
  * entry: open of the candle after the signal candle (t+1), +slippage (long)
    / -slippage (short), rounded adversely to the tick. If that candle is
    missing, the next available open before the hard exit (flag ENTRY_DELAYED).
  * stop: OR_L (long) / OR_H (short). Checked from the candle after entry
    (default) or from the entry candle itself (conservative variant), up to the
    candle before the hard exit. Long: low <= stop -> fill min(stop, open) -
    slippage; short: high >= stop -> fill max(stop, open) + slippage.
  * hard exit: open of the 15:10 candle -/+ slippage. Missing -> next open,
    else the last close (flag EXIT_FALLBACK).
  * slippage per fill = multiplier x (ticks x tick + pct x price).
  * MFE / MAE: from the entry candle through the exit candle, inclusive.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date

import numpy as np

from orb.config import Config, SessionConfig
from orb.features import SessionArrays, slot
from orb.sim.costs import Order, order_charges
from orb.sim.ticks import round_to_tick

# trade statuses
OK = "OK"
INVALID_ENTRY_GAP = "INVALID_ENTRY_GAP"  # entry at/through the stop: risk <= 0
NO_ENTRY_DATA = "NO_ENTRY_DATA"  # no candle to enter on before the hard exit
NO_TICK = "NO_TICK"  # no reference close for the tick size


@dataclass(frozen=True)
class TradeResult:
    status: str
    variant: str  # "default" | "conservative"
    slippage_mult: float
    entry_slot: int | None = None
    entry_raw: float | None = None
    entry_price: float | None = None
    stop: float | None = None
    risk_per_share: float | None = None
    exit_slot: int | None = None
    exit_raw: float | None = None
    exit_price: float | None = None
    exit_reason: str | None = None  # STOP | HARD_EXIT
    flags: str = ""
    # per share (always, when status OK)
    gross_per_share: float | None = None
    mfe_per_share: float | None = None
    mae_per_share: float | None = None
    # sized (qty >= 1)
    qty: int | None = None
    notional: float | None = None
    initial_risk: float | None = None
    gross_pnl: float | None = None
    slippage_paid: float | None = None
    costs: float | None = None
    net_pnl: float | None = None
    r_gross: float | None = None
    r_net: float | None = None
    mfe_r: float | None = None
    mae_r: float | None = None
    hold_minutes: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def slippage(price: float, tick: float, cfg: Config, mult: float) -> float:
    e = cfg.execution
    return mult * (e.slippage_ticks * tick + e.slippage_pct * price)


def _fill(raw: float, side_of_order: str, tick: float, cfg: Config, mult: float) -> float:
    slip = slippage(raw, tick, cfg, mult)
    px = raw + slip if side_of_order == "buy" else raw - slip
    return round_to_tick(px, tick, side_of_order)


def size(entry: float, risk_per_share: float, score: float | None, cfg: Config) -> int | None:
    """qty = floor(min(slot x score/100 / entry, risk_cap / risk)). None if no score."""
    if score is None or risk_per_share <= 0:
        return None
    p = cfg.portfolio
    notional = p.slot_size * score / 100.0
    return math.floor(min(notional / entry, p.risk_cap / risk_per_share) + 1e-9)


def _next_open(a: SessionArrays, start: int, stop_before: int) -> int | None:
    for i in range(start, stop_before):
        if not np.isnan(a.open[i]):
            return i
    return None


def simulate(
    a: SessionArrays,
    day: date,
    signal_slot: int,
    side: str,
    or_h: float,
    or_l: float,
    score: float | None,
    tick: float | None,
    cfg: Config,
    variant: str = "default",
    slippage_mult: float = 1.0,
) -> TradeResult:
    s: SessionConfig = cfg.session
    base = {"variant": variant, "slippage_mult": slippage_mult}
    if tick is None:
        return TradeResult(NO_TICK, **base)
    hard = slot(s, s.hard_exit)
    long = side == "long"
    buy, sell = ("buy", "sell") if long else ("sell", "buy")  # entry order, exit order
    flags = []

    e = _next_open(a, signal_slot + 1, hard)
    if e is None:
        return TradeResult(NO_ENTRY_DATA, **base)
    if e != signal_slot + 1:
        flags.append("ENTRY_DELAYED")
    entry_raw = float(a.open[e])
    entry = _fill(entry_raw, buy, tick, cfg, slippage_mult)
    stop = or_l if long else or_h
    risk = (entry - stop) if long else (stop - entry)
    common = dict(entry_slot=e, entry_raw=entry_raw, entry_price=entry, stop=stop, **base)
    if risk <= 0:
        return TradeResult(INVALID_ENTRY_GAP, risk_per_share=risk, flags=";".join(flags), **common)

    first_check = e if variant == "conservative" else e + 1
    exit_slot = exit_raw = None
    reason = None
    for i in range(first_check, hard):
        lo, hi, op = a.low[i], a.high[i], a.open[i]
        if np.isnan(lo):
            continue  # no candle: no stop check (decision 5)
        if long and lo <= stop:
            exit_raw = min(stop, op) if i != e else stop  # entry candle: we entered at its open
            exit_slot, reason = i, "STOP"
            break
        if not long and hi >= stop:
            exit_raw = max(stop, op) if i != e else stop
            exit_slot, reason = i, "STOP"
            break
    if exit_slot is None:
        reason = "HARD_EXIT"
        x = _next_open(a, hard, len(a.open))
        if x is not None:
            exit_slot, exit_raw = x, float(a.open[x])
            if x != hard:
                flags.append("EXIT_FALLBACK")
        else:
            present = np.flatnonzero(~np.isnan(a.close[:hard]))
            exit_slot = int(present[-1])
            exit_raw = float(a.close[exit_slot])
            flags.append("EXIT_FALLBACK")
    exit_price = _fill(float(exit_raw), sell, tick, cfg, slippage_mult)

    sign = 1.0 if long else -1.0
    gross_ps = sign * (exit_price - entry)
    hi = np.nanmax(a.high[e : exit_slot + 1])
    lo = np.nanmin(a.low[e : exit_slot + 1])
    mfe_ps = (hi - entry) if long else (entry - lo)
    mae_ps = (entry - lo) if long else (hi - entry)
    out = dict(
        risk_per_share=risk,
        exit_slot=exit_slot,
        exit_raw=float(exit_raw),
        exit_price=exit_price,
        exit_reason=reason,
        flags=";".join(flags),
        gross_per_share=gross_ps,
        mfe_per_share=float(mfe_ps),
        mae_per_share=float(mae_ps),
        hold_minutes=exit_slot - e,
        **common,
    )
    qty = size(entry, risk, score, cfg)
    if qty is None or qty < 1:
        return TradeResult(OK, qty=qty, **out)
    slip_paid = qty * (abs(entry - entry_raw) + abs(exit_price - float(exit_raw)))
    costs = sum(order_charges(Order(day, buy, qty, entry), cfg.costs).values()) + sum(
        order_charges(Order(day, sell, qty, exit_price), cfg.costs).values()
    )
    gross = qty * gross_ps
    init_risk = qty * risk
    return TradeResult(
        OK,
        qty=qty,
        notional=qty * entry,
        initial_risk=init_risk,
        gross_pnl=gross,
        slippage_paid=slip_paid,
        costs=costs,
        net_pnl=gross - costs,
        r_gross=gross / init_risk,
        r_net=(gross - costs) / init_risk,
        mfe_r=mfe_ps / risk,
        mae_r=mae_ps / risk,
        **out,
    )

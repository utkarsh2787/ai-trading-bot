"""V3 execution windows, the circuit guard and fills.

Rebalance trades: the 15:00 open, else the first candle 15:01..15:05.
  buy  : none -> ENTRY_MISSING; entry candle locked (upper circuit) -> LOCKED_CIRCUIT
  sell : no 1-min data that day, or no candle in the window -> the official close
         (EXIT_NO_MINUTE_DATA); window candle locked (lower circuit) -> forward to
         the open of the first unlocked candle up to 15:29 (EXIT_LOCKED); none ->
         CARRY to the next trading day
Carried exits: from the 09:20 candle (or the first after it) with the same guard
and forward search; locked all day -> carry again.
The lock test is V2's (``orb.v2.trade.locked``); fills are V2's ``fill``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from orb.features import SessionArrays, slot
from orb.v2.trade import locked
from orb.v3.config import ConfigV3

OFFICIAL = -1  # pseudo-slot: the official (bhavcopy) close

ENTRY_MISSING = "ENTRY_MISSING"
LOCKED_CIRCUIT = "LOCKED_CIRCUIT"
EXIT_LOCKED = "EXIT_LOCKED"
EXIT_NO_MINUTE_DATA = "EXIT_NO_MINUTE_DATA"
CARRY = "CARRY"


@dataclass(frozen=True)
class Exec:
    ok: bool  # False: no fill (buy skipped, or a sell carried)
    raw: float | None = None
    slot: int | None = None  # minute slot, or OFFICIAL
    reason: str | None = None  # ENTRY_MISSING / LOCKED_CIRCUIT / EXIT_LOCKED / ... / CARRY


def _first_present(a: SessionArrays, lo: int, hi: int) -> int | None:
    for i in range(lo, hi + 1):
        if not np.isnan(a.open[i]):
            return i
    return None


def buy_exec(a: SessionArrays | None, guard: bool, cfg: ConfigV3) -> Exec:
    s = cfg.session
    if a is None:
        return Exec(False, reason=ENTRY_MISSING)
    i = _first_present(a, slot(s, s.exec_candle), slot(s, s.exec_last))
    if i is None:
        return Exec(False, reason=ENTRY_MISSING)
    if guard and locked(a, i, "buy"):
        return Exec(False, raw=float(a.open[i]), slot=i, reason=LOCKED_CIRCUIT)
    return Exec(True, float(a.open[i]), i)


def sell_exec(
    a: SessionArrays | None,
    official_close: float | None,
    guard: bool,
    cfg: ConfigV3,
    carry_window: bool = False,
) -> Exec:
    """``carry_window``: a carried exit (09:20 onward) instead of the 15:00 window."""
    s = cfg.session
    last = slot(s, s.last_candle)
    if carry_window:
        lo, hi = slot(s, s.carry_candle), last
    else:
        lo, hi = slot(s, s.exec_candle), slot(s, s.exec_last)
    i = _first_present(a, lo, hi) if a is not None else None
    if i is None:
        if carry_window and official_close is None:
            return Exec(False, reason=CARRY)  # not traded that day at all
        if official_close is None:
            return Exec(False, reason=CARRY)
        return Exec(True, official_close, OFFICIAL, EXIT_NO_MINUTE_DATA)
    if not (guard and locked(a, i, "sell")):
        return Exec(True, float(a.open[i]), i)
    for j in range(i + 1, last + 1):
        if not np.isnan(a.open[j]) and not locked(a, j, "sell"):
            return Exec(True, float(a.open[j]), j, EXIT_LOCKED)
    return Exec(False, reason=CARRY)


def value_price(a: SessionArrays | None, prev_official: float | None, cfg: ConfigV3) -> tuple:
    """(price, source) for equity_w: the 14:59 close, else 14:55..14:58, else the
    previous official close."""
    s = cfg.session
    if a is not None:
        k, lo = slot(s, s.signal_candle), slot(s, s.signal_fallback_from)
        for i in range(k, lo - 1, -1):
            if not np.isnan(a.close[i]):
                return float(a.close[i]), "minute" if i == k else "minute_fallback"
    return prev_official, "prev_official"


def minute_time(cfg: ConfigV3, i: int) -> str:
    if i == OFFICIAL:
        return "close"
    m = cfg.session.first_candle.hour * 60 + cfg.session.first_candle.minute + i
    return f"{m // 60:02d}:{m % 60:02d}"

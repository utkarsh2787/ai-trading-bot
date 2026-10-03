"""V3 invariants, checked every trading day of every ledger (actual books and
every null draw). Any violation fails the run.

  1. positions <= slots (2)
  2. cash >= 0 after the day's contract-note rounding
  3. buys only on rebalance days, in the 15:00..15:05 window, never after ruin
  4. sells only at rebalance times (15:00..15:05 on a rebalance day), unless the
     exit is flagged (locked / carried / deferred / corporate action / delisted /
     no minute data / forced end)
  5. cash reconciles daily: cash_end = cash_start + dividends
     + sum(sell qty x fill - sell costs) - sum(buy qty x fill + buy costs)
"""

from __future__ import annotations

from datetime import date

from orb.features import slot
from orb.v3.config import ConfigV3

TOL = 0.01
UNFLAGGED_CAUSES = {"REBALANCE", "FORCED_END"}


class V3InvariantError(AssertionError):
    pass


def violations(
    day: date,
    is_rebalance: bool,
    ruined: bool,
    orders: list[dict],
    dividends: float,
    cash_start: float,
    cash_end: float,
    n_positions: int,
    cfg: ConfigV3,
) -> list[str]:
    s = cfg.session
    lo, hi = slot(s, s.exec_candle), slot(s, s.exec_last)
    out = []
    if n_positions > cfg.sizing.slots:
        out.append(f"{day}: {n_positions} positions > {cfg.sizing.slots}")
    if cash_end < -TOL:
        out.append(f"{day}: cash negative ({cash_end:.2f})")
    expected = cash_start + dividends
    for o in orders:
        in_window = lo <= o["slot"] <= hi
        if o["side"] == "buy":
            expected -= o["qty"] * o["fill"] + o["cost_r"]
            if not (is_rebalance and in_window):
                out.append(
                    f"{day}: buy {o['symbol']} outside the rebalance window (slot {o['slot']})"
                )
            if ruined:
                out.append(f"{day}: buy {o['symbol']} after ruin")
        else:
            expected += o["qty"] * o["fill"] - o["cost_r"]
            flagged = bool(o["flags"]) or o["cause"] not in UNFLAGGED_CAUSES
            if not flagged and not (is_rebalance and in_window):
                out.append(f"{day}: unflagged sell {o['symbol']} outside the rebalance window")
    if abs(expected - cash_end) > TOL:
        out.append(f"{day}: cash {cash_end:.4f} does not reconcile (expected {expected:.4f})")
    return out


def check(*args, **kw) -> None:
    v = violations(*args, **kw)
    if v:
        raise V3InvariantError("\n".join(v))

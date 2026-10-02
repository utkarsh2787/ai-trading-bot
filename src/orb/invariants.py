"""Engine invariants, checked for every (day, variant, slippage) book.

Any violation fails the run (``EngineInvariantError``). Positions are open on
the closed slot interval [entry_slot, exit_slot]; a cash ledger replays the
day: at entry the notional plus the entry-order charges leave cash, at exit
the notional plus gross P&L minus the remaining charges come back.

  1. open positions <= max_positions and deployed notional <= max_deployment
     at every slot
  2. no position open after the hard exit (15:10); no signal after entry_end
     (14:30) and no entry after the candle that follows it
  3. at most one trade per stock per day
  4. cash never negative
  5. per-trade risk at entry <= risk_cap
  6. sum of trade net P&L == the day's equity change (to Rs 0.01), with and
     without contract-note rounding
"""

from __future__ import annotations

import polars as pl

from orb.config import Config
from orb.features import slot
from orb.portfolio import TAKEN
from orb.sim.costs import Order, order_charges

TOL = 0.01


class EngineInvariantError(AssertionError):
    pass


def violations(book: pl.DataFrame, cfg: Config) -> list[str]:
    """``book``: one (day, variant, slippage) allocation incl. sims and costs."""
    t = book.filter(pl.col("book_decision") == TAKEN).to_dicts()
    if not t:
        return []
    p, s = cfg.portfolio, cfg.session
    hard, last_sig = slot(s, s.hard_exit), slot(s, s.entry_end)
    tag = f"{t[0]['date']} {t[0]['variant']} x{t[0]['slippage_mult']}"
    out: list[str] = []

    syms = [r["symbol"] for r in t]
    if len(syms) != len(set(syms)):
        out.append(f"{tag}: more than one trade for a stock: {sorted(syms)}")
    for r in t:
        if r["exit_slot"] > hard:
            out.append(f"{tag}: {r['symbol']} open after the hard exit (slot {r['exit_slot']})")
        if r["signal_slot"] > last_sig or r["entry_slot"] > last_sig + 1:
            out.append(
                f"{tag}: {r['symbol']} entry after the entry window "
                f"(signal {r['signal_slot']}, entry {r['entry_slot']})"
            )
        if r["initial_risk"] > p.risk_cap + 1e-9:
            out.append(f"{tag}: {r['symbol']} risk {r['initial_risk']:.2f} > {p.risk_cap}")

    for slot_i in sorted({r["entry_slot"] for r in t} | {r["exit_slot"] for r in t}):
        live = [r for r in t if r["entry_slot"] <= slot_i <= r["exit_slot"]]
        if len(live) > p.max_positions:
            out.append(f"{tag}: {len(live)} positions open at slot {slot_i}")
        dep = sum(r["notional"] for r in live)
        if dep > p.max_deployment + 1e-9:
            out.append(f"{tag}: deployed {dep:.2f} > {p.max_deployment} at slot {slot_i}")

    for costs_col, net_col in (("costs", "net_pnl"), ("costs_rounded", "net_pnl_rounded")):
        events = []
        for r in t:
            buy = "buy" if r["side"] == "long" else "sell"
            entry_cost = sum(
                order_charges(Order(r["date"], buy, r["qty"], r["entry_price"]), cfg.costs).values()
            )
            # entries before exits within a slot: cash freed in candle e is not usable at e
            events.append((r["entry_slot"], 0, -(r["notional"] + entry_cost)))
            events.append(
                (r["exit_slot"], 1, r["notional"] + r["gross_pnl"] - (r[costs_col] - entry_cost))
            )
        cash = p.capital
        for _, _, amount in sorted(events):
            cash += amount
            if cash < -TOL:
                out.append(f"{tag}: cash negative ({cash:.2f}) [{costs_col}]")
                break
        change = cash - p.capital
        total = sum(r[net_col] for r in t)
        if abs(change - total) > TOL:
            out.append(f"{tag}: sum {net_col} {total:.4f} != equity change {change:.4f}")
    return out


def check(book: pl.DataFrame, cfg: Config) -> None:
    v = violations(book, cfg)
    if v:
        raise EngineInvariantError("engine invariant violated:\n  " + "\n  ".join(v))

"""Chronological slot allocation for one trading day (one variant x slippage run).

* only QUALIFIED signals compete; others keep their scanner decision;
* a simulated trade must be OK and have qty >= 1;
* at most ``max_positions`` open at once; a slot freed by an exit during
  candle e can be used by an entry at slot > e (decision 17);
* same entry minute: higher score first, then symbol (decision 18);
* total open notional <= ``max_deployment``; no leverage, no compounding.

Only exits that happened strictly before an entry are visible to it, so the
allocation is causal.
"""

from __future__ import annotations

import polars as pl

from orb.config import PortfolioConfig
from orb.signals import QUALIFIED

TAKEN = "TAKEN"
SKIPPED = "SKIPPED"


def allocate(day: pl.DataFrame, p: PortfolioConfig) -> pl.DataFrame:
    """``day``: one row per signal with ``decision``, ``score``, ``symbol``, and the
    simulation columns ``status``, ``entry_slot``, ``exit_slot``, ``qty``,
    ``notional``. Adds ``book_decision`` and ``book_reason``."""
    rows = day.to_dicts()
    order = sorted(
        (i for i, r in enumerate(rows) if r["decision"] == QUALIFIED),
        key=lambda i: (
            rows[i]["entry_slot"] if rows[i]["entry_slot"] is not None else 10**6,
            -(rows[i]["score"] or 0.0),
            rows[i]["symbol"],
        ),
    )
    decision = [r["decision"] for r in rows]
    reason = [r.get("reason") for r in rows]
    open_pos: list[tuple[int, float]] = []  # (exit_slot, notional)
    for i in order:
        r = rows[i]
        if r["status"] != "OK":
            decision[i], reason[i] = SKIPPED, r["status"]
            continue
        if r["qty"] is None or r["qty"] < 1:
            decision[i], reason[i] = SKIPPED, "QTY_BELOW_ONE"
            continue
        # still open at this entry: exited at or after the entry candle
        open_pos = [x for x in open_pos if x[0] >= r["entry_slot"]]
        if len(open_pos) >= p.max_positions:
            decision[i], reason[i] = SKIPPED, "SLOTS_FULL"
            continue
        if sum(n for _, n in open_pos) + r["notional"] > p.max_deployment + 1e-9:
            decision[i], reason[i] = SKIPPED, "MAX_DEPLOYMENT"
            continue
        decision[i], reason[i] = TAKEN, None
        open_pos.append((r["exit_slot"], r["notional"]))
    return day.with_columns(
        book_decision=pl.Series(decision, dtype=pl.String),
        book_reason=pl.Series(reason, dtype=pl.String),
    )

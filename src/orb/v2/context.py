"""V2 day inputs: the V1 ``ContextBuilder`` without the RV baseline.

V2 uses only the trade date's raw 1-min bars plus ATR14 / previous close from
daily bars strictly before T (V1's ``daily_context``), and the same exclusions.
It has no volume warm-up (gap #4), so past sessions are never loaded.
"""

from __future__ import annotations

from datetime import date

from orb.context import (
    EXCLUSION_PRECEDENCE,
    ContextBuilder,
    DayInputs,
    StockDay,
    exclusion_category,
)
from orb.data.reference import members_on


class V2ContextBuilder(ContextBuilder):
    @classmethod
    def from_builder(cls, base: ContextBuilder) -> V2ContextBuilder:
        """Reuse a builder made by ``scan.load_from_disk`` / ``tests.market``."""
        obj = cls.__new__(cls)
        obj.__dict__.update(base.__dict__)
        return obj

    def day(self, day: date) -> DayInputs:
        self._evict(day)  # only T's session is ever needed
        members = members_on(self.membership, day)  # excluded ones too: they are logged
        self._ensure(members, [day])
        stocks, no_data = [], []
        for s in members:
            bars = self._session(s, day)
            if bars is None or not bars.present.any():
                no_data.append(s)
                continue
            c = self.ctx.get((s, day))
            if c is None:
                atr = prev = None
                atr_reason = "INSUFFICIENT_HISTORY:daily_bars"
            else:
                atr, prev, atr_reason = c["atr14"], c["prev_close"], c["atr_reason"]
            raw_reasons = self.reasons.get((s, day), []) + self.reasons.get(("*", day), [])
            cats = {exclusion_category(r) for r in raw_reasons}
            stocks.append(
                StockDay(
                    s,
                    bars,
                    atr,
                    prev,
                    atr_reason,
                    None,
                    0,
                    None,
                    next((c for c in EXCLUSION_PRECEDENCE if c in cats), None),
                    ";".join(sorted(set(raw_reasons))) or None,
                )
            )
        return DayInputs(day, stocks, self._index(day), no_data)

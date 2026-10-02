"""V2 sizing: equity tracking and the ruin stop, one ``EquityBook`` per book.

equity_d     = starting capital + cumulative rupee-rounded net P&L to d-1
Deployable_d = deploy_fraction x equity_d
Slot_d       = Deployable_d / max_positions (equal weight)
ruin         = no new trades once equity_d < ruin_equity; ruin_date = first such day
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from orb.v2.config import ConfigV2


@dataclass(frozen=True)
class DayLimits:
    day: date
    equity: float
    deployable: float
    slot_value: float
    ruined: bool


@dataclass
class EquityBook:
    name: str
    cfg: ConfigV2
    equity: float = field(init=False)
    ruin_date: date | None = None
    last_day: date | None = None

    def __post_init__(self) -> None:
        self.equity = self.cfg.sizing.starting_capital

    def limits(self, day: date) -> DayLimits:
        if self.last_day is not None and day <= self.last_day:
            raise ValueError(
                f"{self.name}: days must be booked in order ({day} <= {self.last_day})"
            )
        z = self.cfg.sizing
        ruined = self.equity < z.ruin_equity
        if ruined and self.ruin_date is None:
            self.ruin_date = day
        deployable = z.deploy_fraction * self.equity
        return DayLimits(
            day, self.equity, deployable, deployable / self.cfg.selection.max_positions, ruined
        )

    def close_day(self, day: date, day_net_rounded: float) -> float:
        self.equity += day_net_rounded
        self.last_day = day
        return self.equity

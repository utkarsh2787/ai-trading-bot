"""ConfigV3: the V3 schema (``config/v3.yaml``), the CNC cost table and the hash.

Shared sections reuse the V1 sub-models (run, data, reference, tick table) and
V2's ``SlippageModel`` / ``CircuitGuardV2``. V1 and V2 configs are untouched.
"""

from __future__ import annotations

import hashlib
import json
import warnings
from datetime import date, time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator

from orb.config import DataConfig, ReferenceConfig, RunConfig, TickTable, _Model
from orb.v2.config import CircuitGuardV2, SlippageModel


class SessionV3(_Model):
    first_candle: time
    last_candle: time
    signal_candle: time  # P_now = close of this candle (14:59, closes at 15:00:00)
    signal_fallback_from: time  # 14:59 missing -> last close from here (14:55)
    exec_candle: time  # 15:00: buys and sells at its open
    exec_last: time  # 15:00 missing -> first candle up to here (15:05)
    carry_candle: time  # EXIT_CARRIED: next trading day's 09:20 open

    @model_validator(mode="after")
    def _order(self) -> SessionV3:
        seq = [
            self.first_candle,
            self.carry_candle,
            self.signal_fallback_from,
            self.signal_candle,
            self.exec_candle,
            self.exec_last,
            self.last_candle,
        ]
        if any(a > b for a, b in zip(seq, seq[1:], strict=False)) or (
            self.signal_candle >= self.exec_candle
        ):
            raise ValueError(f"V3 session times out of order: {seq}")
        return self


class UniverseV3(_Model):
    warmup_sessions: int = Field(gt=0)  # >= this many prior daily bars
    fno_ban_excludes: Literal[False]  # the ban restricts derivatives only
    corp_action_days_block_buys: bool  # gap #2: CA ex-days excluded for ranking / buys
    dq_defers_exits: bool  # gap #3: EXIT_DEFERRED on a held stock's DQ-excluded day


class ScheduleV3(_Model):
    rebalance: Literal["last_trading_day_of_week"]


class SignalV3(_Model):
    lookback: Literal["previous_rebalance_close"]
    rank: Literal["r_week_ascending"]
    tie_break: Literal["symbol_asc"]
    target_size: int = Field(gt=0)


class SizingV3(_Model):
    starting_capital: float = Field(gt=0)
    slot_fraction: float = Field(gt=0, le=1)  # Slot_w = fraction x equity_w / slots
    slots: int = Field(gt=0)
    ruin_equity: float = Field(ge=0)
    equity_basis: Literal["net_rounded"]
    same_day_sell_proceeds: Literal[True]  # known limit
    leverage: Literal[False]


class ExecutionV3(_Model):
    books: dict[str, SlippageModel]
    primary_book: str
    stress_book: str
    exchange: Literal["NSE"]
    round_stt_stamp_to_rupee: bool
    tick_table: str
    cost_table: str

    @model_validator(mode="after")
    def _books(self) -> ExecutionV3:
        for b in (self.primary_book, self.stress_book):
            if b not in self.books:
                raise ValueError(f"book {b!r} not defined in execution.books")
        return self


class CorporateActionsV3(_Model):
    split_bonus: Literal["qty_floor_drop_fraction"]
    dividends: Literal["credit_on_ex_date"]
    demerger: Literal["exit_previous_trading_day"]
    rights: Literal["hold_through"]
    delisting: Literal["exit_last_traded_close"]


class ValidationV3(_Model):
    null_draws: int = Field(gt=0)
    vix_terciles_from: Literal["expanding"]
    vix_min_history: int = Field(gt=0)
    trend_day_threshold: float = Field(gt=0, lt=1)
    results_window_sessions: int = Field(ge=0)
    expiry_types: list[str]
    label_quantiles: int = Field(gt=1)  # criterion e: r_week quintiles, per week
    post_start: date
    dp_sensitivity: list[float]  # DP multipliers for the secondary diagnostic


class PreregV3(_Model):
    version: str
    strategies_tested: int = Field(gt=0)
    min_round_trips: int = Field(gt=0)
    null_p_max: float = Field(gt=0, lt=1)
    min_years_beating_null_share: float = Field(gt=0, le=1)
    ruin_never_hit: Literal[True]

    @model_validator(mode="after")
    def _bonferroni(self) -> PreregV3:
        if abs(self.null_p_max - 0.05 / self.strategies_tested) > 1e-12:
            raise ValueError("null_p_max must equal 0.05 / strategies_tested (Bonferroni)")
        return self


class CnCSchedule(_Model):
    effective_from: date
    brokerage_pct: float = Field(ge=0)
    brokerage_cap: float = Field(ge=0)
    stt_buy_pct: float = Field(ge=0)
    stt_sell_pct: float = Field(ge=0)
    exchange_txn_pct: float = Field(ge=0)
    stamp_buy_pct: float = Field(ge=0)
    sebi_per_crore: float = Field(ge=0)
    gst_pct: float = Field(ge=0)
    dp_per_scrip_sell_day: float = Field(ge=0)  # flat, GST included


class FieldVerification(_Model):
    verified: bool
    source: str


class CnCCostTable(_Model):
    exchange: Literal["NSE"]
    gst_on: list[Literal["brokerage", "exchange_txn", "sebi_fee"]]
    verification: dict[str, FieldVerification]
    schedules: list[CnCSchedule]

    @model_validator(mode="after")
    def _check(self) -> CnCCostTable:
        dates = [s.effective_from for s in self.schedules]
        if not dates or dates != sorted(set(dates)):
            raise ValueError("cost schedules must have strictly increasing effective_from")
        fields = set(CnCSchedule.model_fields) - {"effective_from"}
        if set(self.verification) != fields:
            raise ValueError(f"cost verification must cover exactly {sorted(fields)}")
        return self

    @property
    def unverified(self) -> list[str]:
        return sorted(k for k, v in self.verification.items() if not v.verified)


class ConfigV3(_Model):
    strategy: Literal["v3"]
    run: RunConfig
    data: DataConfig
    reference: ReferenceConfig
    session: SessionV3
    universe: UniverseV3
    schedule: ScheduleV3
    signal: SignalV3
    sizing: SizingV3
    execution: ExecutionV3
    circuit_guard: CircuitGuardV2
    corporate_actions: CorporateActionsV3
    validation: ValidationV3
    prereg: PreregV3
    ticks: TickTable
    costs: CnCCostTable

    @model_validator(mode="after")
    def _cross(self) -> ConfigV3:
        if self.signal.target_size != self.sizing.slots:
            raise ValueError("signal.target_size must equal sizing.slots")
        return self

    def hash(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def load_config_v3(path: str | Path) -> ConfigV3:
    path = Path(path)
    with path.open() as f:
        raw = yaml.safe_load(f)
    base = path.parent
    for key, name in (("ticks", "tick_table"), ("costs", "cost_table")):
        with (base / raw["execution"][name]).open() as f:
            raw[key] = yaml.safe_load(f)
    cfg = ConfigV3.model_validate(raw)
    if cfg.costs.unverified:
        warnings.warn(
            f"CNC cost table has unverified fields: {', '.join(cfg.costs.unverified)}",
            UserWarning,
            stacklevel=2,
        )
    return cfg

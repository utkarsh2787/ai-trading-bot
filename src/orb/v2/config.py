"""ConfigV2: the V2 schema (``config/v2.yaml``) and its canonical hash.

Shared sections reuse the V1 sub-models (run, data, reference, tick and cost
tables); V2-only sections are defined here. The V1 ``Config`` is untouched, so
its hash is unchanged.
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

from orb.config import (
    CostTable,
    DataConfig,
    ReferenceConfig,
    RunConfig,
    TickTable,
    _Model,
)


class SessionV2(_Model):
    first_candle: time
    last_candle: time
    signal_candle: time  # P_0945 = close of this candle (09:44, closes at 09:45:00)
    signal_fallback_from: time  # 09:44 missing -> last close from here to signal_candle
    entry_candle: time  # 14:30: entry at its open
    entry_last: time  # 14:30 missing -> first candle up to here (14:35)
    exit_candle: time  # 15:10: exit at its open

    @model_validator(mode="after")
    def _order(self) -> SessionV2:
        seq = [
            self.first_candle,
            self.signal_fallback_from,
            self.signal_candle,
            self.entry_candle,
            self.entry_last,
            self.exit_candle,
            self.last_candle,
        ]
        if any(a > b for a, b in zip(seq, seq[1:], strict=False)) or (
            self.signal_candle >= self.entry_candle or self.entry_last >= self.exit_candle
        ):
            raise ValueError(f"V2 session times out of order: {seq}")
        return self


class FeaturesV2(_Model):
    atr_period: int = Field(gt=0)  # Wilder ATR on daily bars up to the previous day
    min_daily_bars: int = Field(gt=0)  # warm-up (the only one in V2: no volume baseline)

    @model_validator(mode="after")
    def _warmup(self) -> FeaturesV2:
        if self.min_daily_bars <= self.atr_period:
            raise ValueError("min_daily_bars must exceed atr_period")
        return self


class SignalV2(_Model):
    z_min: float = Field(gt=0)  # qualifies if |z| >= z_min
    direction: Literal["sign_r1"]


class SelectionV2(_Model):
    max_positions: int = Field(gt=0)
    rank_by: Literal["abs_z_desc"]
    tie_break: Literal["symbol_asc"]
    replacements: Literal[False]  # no later additions / replacements that day


class SizingV2(_Model):
    starting_capital: float = Field(gt=0)
    deploy_fraction: float = Field(gt=0, le=1)  # Deployable_d = fraction x equity_d
    ruin_equity: float = Field(ge=0)  # no new trades once equity_d < this
    equity_basis: Literal["net_pnl_rounded"]  # what the contract note charges
    leverage: Literal[False]


class SlippageModel(_Model):
    ticks: float = Field(ge=0)  # ticks per fill, against the trade
    pct: float = Field(ge=0)  # extra fraction of price per fill
    round_to_tick: bool  # round the fill against the trade to the tick


class ExecutionV2(_Model):
    books: dict[str, SlippageModel]  # every book is simulated, each with its own equity
    primary_book: str
    stress_book: str  # criterion b's second check
    exchange: Literal["NSE"]
    round_stt_stamp_to_rupee: bool  # contract-note rounding, per day per book
    tick_table: str
    cost_table: str

    @model_validator(mode="after")
    def _books(self) -> ExecutionV2:
        for b in (self.primary_book, self.stress_book):
            if b not in self.books:
                raise ValueError(f"book {b!r} not defined in execution.books")
        return self


class CircuitGuardV2(_Model):
    enabled: bool
    apply_to: Literal["non_fno"]
    fno_membership: str  # (sample_date, symbol) built by `orb ref fno` from cached F&O bhavcopies
    # in F&O on d = a stock futures contract in BOTH weekly samples bracketing d
    # (only weekly samples are cached; a transition week counts as non-F&O, so the
    # guard applies), or on the F&O ban list that day
    fno_rule: Literal["both_bracketing_samples"]
    ban_list_implies_fno: bool


class IndexVariantV2(_Model):
    z_min: float = Field(gt=0)  # the index qualifies if |z_index| >= z_min
    rank_by: Literal["alignment_desc"]  # alignment = z x sign(r1_index)


class ValidationV2(_Model):
    null_bootstrap: int = Field(gt=0)
    vix_terciles_from: Literal["expanding"]
    vix_min_history: int = Field(gt=0)
    trend_day_threshold: float = Field(gt=0, lt=1)
    results_window_sessions: int = Field(ge=0)
    expiry_types: list[str]
    z_terciles: int = Field(gt=1)  # criterion e
    post_start: date  # secondary diagnostic: trades from this date on


class PreregV2(_Model):
    version: str
    strategies_tested: int = Field(gt=0)  # Bonferroni: null_p_max = 0.05 / strategies_tested
    min_trades: int = Field(gt=0)
    null_p_max: float = Field(gt=0, lt=1)
    min_positive_year_share: float = Field(gt=0, le=1)
    ruin_never_hit: Literal[True]

    @model_validator(mode="after")
    def _bonferroni(self) -> PreregV2:
        if abs(self.null_p_max - 0.05 / self.strategies_tested) > 1e-12:
            raise ValueError("null_p_max must equal 0.05 / strategies_tested (Bonferroni)")
        return self


class ConfigV2(_Model):
    strategy: Literal["v2"]
    run: RunConfig
    data: DataConfig
    reference: ReferenceConfig
    session: SessionV2
    features: FeaturesV2
    signal: SignalV2
    selection: SelectionV2
    sizing: SizingV2
    execution: ExecutionV2
    circuit_guard: CircuitGuardV2
    index_variant: IndexVariantV2
    validation: ValidationV2
    prereg: PreregV2
    ticks: TickTable
    costs: CostTable

    @model_validator(mode="after")
    def _cross(self) -> ConfigV2:
        if self.data.daily_history_start >= self.run.start_date:
            raise ValueError("daily_history_start must precede run.start_date")
        return self

    def hash(self) -> str:
        """Canonical sha256 of the fully-resolved config (same method as V1)."""
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def load_config_v2(path: str | Path) -> ConfigV2:
    path = Path(path)
    with path.open() as f:
        raw = yaml.safe_load(f)
    base = path.parent
    for key, name in (("ticks", "tick_table"), ("costs", "cost_table")):
        with (base / raw["execution"][name]).open() as f:
            raw[key] = yaml.safe_load(f)
    cfg = ConfigV2.model_validate(raw)
    if not cfg.ticks.verified:
        warnings.warn("tick table is marked unverified", UserWarning, stacklevel=2)
    if cfg.costs.unverified:
        warnings.warn(
            f"cost table has unverified fields: {', '.join(cfg.costs.unverified)}",
            UserWarning,
            stacklevel=2,
        )
    return cfg

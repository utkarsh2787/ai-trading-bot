"""Typed, validated configuration.

Every research parameter lives in YAML; this module only defines the schema,
loads it (including the referenced tick and cost tables) and computes a
canonical hash used for reproducibility.
"""

from __future__ import annotations

import hashlib
import json
import warnings
from datetime import date, time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- run


class RunConfig(_Model):
    name: str
    start_date: date
    end_date: date
    oos_start: date
    seed: int

    @model_validator(mode="after")
    def _dates(self) -> RunConfig:
        if not self.start_date < self.oos_start <= self.end_date:
            raise ValueError("require start_date < oos_start <= end_date")
        return self


# -------------------------------------------------------------------------- data


class IndexConfig(_Model):
    primary: str
    fallback: str
    vix: str


class KiteConfig(_Model):
    api_key_env: str
    access_token_env: str
    exchange: str
    index_exchange: str
    max_requests_per_sec: float = Field(gt=0)
    minute_chunk_days: int = Field(gt=0, le=60)
    day_chunk_days: int = Field(gt=0, le=2000)
    max_retries: int = Field(ge=0)
    backoff_base_sec: float = Field(ge=0)


class LocalConfig(_Model):
    root: str
    minute_glob: str
    daily_glob: str
    timezone: str
    columns: dict[str, str]


class DataConfig(_Model):
    root: str
    provider: Literal["kite", "local"]
    daily_history_start: date
    minute_history_start: date
    index: IndexConfig
    kite: KiteConfig
    local: LocalConfig


class ReferenceConfig(_Model):
    root: str
    membership: str
    ban_list: str
    corporate_actions: str
    special_sessions: str
    calendar_exceptions: str
    budget_days: str
    symbol_map: str
    expiries: str
    results_dates: str
    excluding_action_types: list[str]

    def path(self, name: str) -> Path:
        return Path(self.root) / getattr(self, name)


# ----------------------------------------------------------------------- session


class SessionConfig(_Model):
    first_candle: time
    last_candle: time
    or_start: time
    or_end: time
    entry_start: time
    entry_end: time
    hard_exit: time

    @model_validator(mode="after")
    def _order(self) -> SessionConfig:
        seq = [
            self.first_candle,
            self.or_start,
            self.or_end,
            self.entry_start,
            self.entry_end,
            self.hard_exit,
            self.last_candle,
        ]
        if (
            any(a > b for a, b in zip(seq, seq[1:], strict=False))
            or self.or_end >= self.entry_start
        ):
            raise ValueError(f"session times out of order: {seq}")
        return self


class FeaturesConfig(_Model):
    atr_period: int = Field(gt=0)
    min_daily_bars: int = Field(gt=0)
    rv_lookback: int = Field(gt=0)

    @model_validator(mode="after")
    def _warmup(self) -> FeaturesConfig:
        if self.min_daily_bars <= self.atr_period:
            raise ValueError("min_daily_bars must exceed atr_period")
        return self


# ----------------------------------------------------------------------- scoring


class BreakoutScore(_Model):
    weight: float = Field(ge=0)
    full_at_atr: float = Field(gt=0)


class RelVolumeScore(_Model):
    weight: float = Field(ge=0)


class MarketScore(_Model):
    weight: float = Field(ge=0)
    band: float = Field(ge=0)
    neutral_points: float = Field(ge=0)


class ORQualityScore(_Model):
    weight: float = Field(ge=0)
    filter_min: float
    flat_min: float
    flat_max: float
    filter_max: float

    @model_validator(mode="after")
    def _order(self) -> ORQualityScore:
        if not 0 <= self.filter_min < self.flat_min <= self.flat_max < self.filter_max:
            raise ValueError("or_quality breakpoints out of order")
        return self


class VolatilityScore(_Model):
    weight: float = Field(ge=0)
    ramp_min: float
    flat_min: float
    flat_max: float
    ramp_max: float

    @model_validator(mode="after")
    def _order(self) -> VolatilityScore:
        if not 0 <= self.ramp_min < self.flat_min <= self.flat_max < self.ramp_max:
            raise ValueError("volatility breakpoints out of order")
        return self


class ScoringConfig(_Model):
    threshold: float = Field(ge=0, le=100)
    breakout: BreakoutScore
    rel_volume: RelVolumeScore
    market: MarketScore
    or_quality: ORQualityScore
    volatility: VolatilityScore

    @model_validator(mode="after")
    def _weights(self) -> ScoringConfig:
        total = (
            self.breakout.weight
            + self.rel_volume.weight
            + self.market.weight
            + self.or_quality.weight
            + self.volatility.weight
        )
        if abs(total - 100) > 1e-9:
            raise ValueError(f"factor weights must sum to 100, got {total}")
        if self.market.neutral_points > self.market.weight:
            raise ValueError("market.neutral_points exceeds market.weight")
        return self


# --------------------------------------------------------------------- portfolio


class PortfolioConfig(_Model):
    capital: float = Field(gt=0)
    max_deployment: float = Field(gt=0)
    max_positions: int = Field(gt=0)
    risk_cap: float = Field(gt=0)
    compounding: Literal[False]
    slot_mode: Literal["concurrent"]

    @model_validator(mode="after")
    def _leverage(self) -> PortfolioConfig:
        if self.max_deployment > self.capital:
            raise ValueError("max_deployment > capital implies leverage")
        return self

    @property
    def slot_size(self) -> float:
        return self.max_deployment / self.max_positions


# ---------------------------------------------------------- tick and cost tables


class TickBand(_Model):
    min_price: float = Field(ge=0)
    tick: float = Field(gt=0)


class TickRegime(_Model):
    effective_from: date
    bands: list[TickBand]

    @model_validator(mode="after")
    def _bands(self) -> TickRegime:
        mins = [b.min_price for b in self.bands]
        if not mins or mins[0] != 0 or mins != sorted(set(mins)):
            raise ValueError("tick bands must start at 0 and strictly increase")
        return self


class TickTable(_Model):
    verified: bool
    regimes: list[TickRegime]

    @model_validator(mode="after")
    def _sorted(self) -> TickTable:
        dates = [r.effective_from for r in self.regimes]
        if not dates or dates != sorted(set(dates)):
            raise ValueError("tick regimes must have strictly increasing effective_from")
        return self


class CostSchedule(_Model):
    effective_from: date
    brokerage_pct: float = Field(ge=0)
    brokerage_cap: float = Field(ge=0)
    stt_sell_pct: float = Field(ge=0)
    exchange_txn_pct: float = Field(ge=0)
    stamp_buy_pct: float = Field(ge=0)
    sebi_per_crore: float = Field(ge=0)
    gst_pct: float = Field(ge=0)


class CostTable(_Model):
    verified: bool
    schedules: list[CostSchedule]

    @model_validator(mode="after")
    def _sorted(self) -> CostTable:
        dates = [s.effective_from for s in self.schedules]
        if not dates or dates != sorted(set(dates)):
            raise ValueError("cost schedules must have strictly increasing effective_from")
        return self


class ExecutionConfig(_Model):
    slippage_ticks: float = Field(ge=0)
    slippage_pct: float = Field(ge=0)
    stress_multipliers: list[float]
    tick_rounding: Literal["adverse"]
    entry_candle_stop_check_variants: list[bool]
    tick_table: str
    cost_table: str


# ---------------------------------------------------------------- dq, validation


class DQConfig(_Model):
    expected_candles: int = Field(gt=0)
    max_missing_minutes_warn: int = Field(ge=0)
    max_missing_minutes_error: int = Field(ge=0)
    volume_spike_mult: float = Field(gt=1)
    overnight_gap_error: float = Field(gt=0)
    daily_minute_tolerance: float = Field(ge=0)


class ValidationConfig(_Model):
    null_bootstrap: int = Field(gt=0)
    vix_terciles_from: Literal["in_sample"]
    trend_day_threshold: float = Field(gt=0, lt=1)
    results_window_sessions: int = Field(ge=0)
    expiry_types: list[str]
    score_buckets: list[float]
    factor_quantiles: int = Field(gt=1)


# -------------------------------------------------------------------------- root


class Config(_Model):
    run: RunConfig
    data: DataConfig
    reference: ReferenceConfig
    session: SessionConfig
    features: FeaturesConfig
    scoring: ScoringConfig
    portfolio: PortfolioConfig
    execution: ExecutionConfig
    dq: DQConfig
    validation: ValidationConfig
    # Resolved from execution.tick_table / cost_table at load time so the
    # config hash covers their contents.
    ticks: TickTable
    costs: CostTable

    @model_validator(mode="after")
    def _cross(self) -> Config:
        if self.data.daily_history_start >= self.run.start_date:
            raise ValueError("daily_history_start must precede run.start_date")
        if self.data.minute_history_start >= self.run.start_date:
            raise ValueError("minute_history_start must precede run.start_date")
        buckets = self.validation.score_buckets
        if not buckets or buckets[0] != self.scoring.threshold or buckets != sorted(buckets):
            raise ValueError("score_buckets must be ascending and start at scoring.threshold")
        return self

    def hash(self) -> str:
        """Canonical sha256 of the fully-resolved config."""
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def _read_yaml(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    raw = _read_yaml(path)
    execution = raw.get("execution", {})
    base = path.parent
    raw["ticks"] = _read_yaml(base / execution["tick_table"])
    raw["costs"] = _read_yaml(base / execution["cost_table"])
    cfg = Config.model_validate(raw)
    for name, table in (("tick table", cfg.ticks), ("cost table", cfg.costs)):
        if not table.verified:
            warnings.warn(f"{name} is marked unverified", UserWarning, stacklevel=2)
    return cfg

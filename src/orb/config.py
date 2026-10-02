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
    # Kite returns split/bonus/rights/spin-off/extraordinary-dividend adjusted
    # candles (prices and volume) and offers no raw series; see docs/DATA_SOURCES.md.
    price_basis: Literal["adjusted"]


class LocalConfig(_Model):
    root: str
    minute_glob: str
    daily_glob: str
    timezone: str
    columns: dict[str, str]
    price_basis: Literal["raw", "adjusted"]


class NSEConfig(_Model):
    """Public NSE / niftyindices.com endpoints for reference data and bhavcopies."""

    archives_base: str
    www_base: str
    niftyindices_base: str
    user_agent: str
    max_requests_per_sec: float = Field(gt=0)
    timeout_sec: float = Field(gt=0)
    max_retries: int = Field(ge=0)
    bhavcopy_udiff_from: date  # first date of the new (UDiFF) CM bhavcopy format
    series: list[str]  # equity series kept from bhavcopies / corporate actions


class DataConfig(_Model):
    root: str
    provider: Literal["kite", "local"]
    daily_history_start: date
    minute_history_start: date
    deadjust_tolerance: float = Field(gt=0)  # max OHLC ratio dispersion vs bhavcopy
    deadjust_drift_tolerance: float = Field(gt=0)  # factor drift between corporate actions
    index: IndexConfig
    kite: KiteConfig
    local: LocalConfig
    nse: NSEConfig

    def price_basis(self) -> str:
        return self.kite.price_basis if self.provider == "kite" else self.local.price_basis


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
    rv_lookback: int = Field(gt=0)  # valid sessions averaged for the RV baseline
    rv_window_trading_days: int = Field(gt=0)  # search window for valid sessions
    rv_min_valid_sessions: int = Field(gt=0)

    @model_validator(mode="after")
    def _warmup(self) -> FeaturesConfig:
        if self.min_daily_bars <= self.atr_period:
            raise ValueError("min_daily_bars must exceed atr_period")
        if not self.rv_min_valid_sessions <= self.rv_lookback <= self.rv_window_trading_days:
            raise ValueError("require rv_min_valid_sessions <= rv_lookback <= rv_window")
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
    """Applies to prices up to ``max_price`` (None = no upper bound)."""

    max_price: float | None = Field(default=None, gt=0)
    max_inclusive: bool = True
    tick: float = Field(gt=0)

    def contains_upper(self, price: float) -> bool:
        if self.max_price is None:
            return True
        return price <= self.max_price if self.max_inclusive else price < self.max_price


class TickRegime(_Model):
    effective_from: date
    bands: list[TickBand]

    @model_validator(mode="after")
    def _bands(self) -> TickRegime:
        caps = [b.max_price for b in self.bands]
        if not caps or caps[-1] is not None or None in caps[:-1]:
            raise ValueError("tick bands: only the last band may (and must) be unbounded")
        if caps[:-1] != sorted(set(caps[:-1])):
            raise ValueError("tick bands must have strictly increasing max_price")
        return self

    def tick_for(self, ref_price: float) -> float:
        return next(b.tick for b in self.bands if b.contains_upper(ref_price))


class TickTable(_Model):
    verified: bool
    reference_price: Literal["prev_month_last_close"]
    source: str
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


class FieldVerification(_Model):
    verified: bool
    source: str


class CostTable(_Model):
    exchange: Literal["NSE"]
    gst_on: list[Literal["brokerage", "exchange_txn", "sebi_fee"]]
    verification: dict[str, FieldVerification]
    schedules: list[CostSchedule]

    @model_validator(mode="after")
    def _sorted(self) -> CostTable:
        dates = [s.effective_from for s in self.schedules]
        if not dates or dates != sorted(set(dates)):
            raise ValueError("cost schedules must have strictly increasing effective_from")
        rate_fields = set(CostSchedule.model_fields) - {"effective_from"}
        if set(self.verification) != rate_fields:
            raise ValueError(f"cost verification must cover exactly {sorted(rate_fields)}")
        return self

    @property
    def unverified(self) -> list[str]:
        return sorted(k for k, v in self.verification.items() if not v.verified)


class ExecutionConfig(_Model):
    slippage_ticks: float = Field(ge=0)
    slippage_pct: float = Field(ge=0)
    stress_multipliers: list[float]
    tick_rounding: Literal["adverse"]
    # NSE only: fills and charges assume NSE. Any future live order must set
    # exchange=NSE explicitly (Kite's default routing could pick BSE).
    exchange: Literal["NSE"]
    round_stt_stamp_to_rupee: bool  # contract-note rounding, aggregated per day
    entry_candle_stop_check_variants: list[bool]
    tick_table: str
    cost_table: str


# ---------------------------------------------------------------- dq, validation


class DQConfig(_Model):
    expected_candles: int = Field(gt=0)
    max_missing_minutes_warn: int = Field(ge=0)
    max_missing_minutes_error: int = Field(ge=0)
    volume_spike_mult: float = Field(gt=1)  # warn only: high RV is a scoring input
    overnight_gap_warn: float = Field(gt=0)  # news gaps are kept (warn)
    overnight_gap_error: float = Field(gt=0)  # on the adjusted series
    daily_minute_tolerance: float = Field(ge=0)

    @model_validator(mode="after")
    def _gaps(self) -> DQConfig:
        if self.overnight_gap_warn >= self.overnight_gap_error:
            raise ValueError("overnight_gap_warn must be below overnight_gap_error")
        return self


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
    if not cfg.ticks.verified:
        warnings.warn("tick table is marked unverified", UserWarning, stacklevel=2)
    if cfg.costs.unverified:
        warnings.warn(
            f"cost table has unverified fields: {', '.join(cfg.costs.unverified)}",
            UserWarning,
            stacklevel=2,
        )
    return cfg

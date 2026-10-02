"""Run the signal scanner over the raw store (``orb scan``).

Inputs are the outputs of ``orb ref``, ``orb build-raw`` and ``orb dq``:
  * calendar        = dates of the primary index's daily bars
  * invalid_daily   = daily-level DQ errors (they break the ATR history)
  * excluded        = every exclusion (DQ errors, ban, corporate action, calendar)
  * index_invalid   = index-days with DQ errors
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import polars as pl

from orb.config import Config
from orb.context import ContextBuilder
from orb.data.pipeline import Stores
from orb.data.quality import ERROR
from orb.data.reference import load_reference
from orb.features import daily_context
from orb.scoring.base import Scorer
from orb.signals import CANDIDATE_SCHEMA, scan_day

log = logging.getLogger(__name__)


def builder_from_disk(cfg: Config) -> ContextBuilder:
    ref = load_reference(cfg.reference)
    raw = Stores.under(cfg.data.root, cfg.data.provider).raw
    dq = Path(cfg.data.root) / "_dq"
    end = cfg.run.end_date
    calendar = raw.read_daily(cfg.data.index.primary, cfg.data.daily_history_start, end)[
        "date"
    ].to_list()
    if not calendar:
        raise RuntimeError(f"no daily bars for {cfg.data.index.primary}: run download/build-raw")
    symbols = ref.all_symbols()
    daily = pl.concat([raw.read_daily(s, cfg.data.daily_history_start, end) for s in symbols])

    def errors(name: str) -> pl.DataFrame:
        p = dq / name
        if not p.exists():
            raise RuntimeError(f"{p} missing: run `orb dq` first")
        return pl.read_parquet(p).filter(pl.col("severity") == ERROR)

    invalid_daily = errors("daily_issues.parquet").select("symbol", "date").unique()
    excl = pl.read_parquet(dq / "exclusions.parquet").select("symbol", "date")
    whole_days = ref.day_exclusions().select(symbol=pl.lit("*"), date="date")
    indices = [cfg.data.index.primary, cfg.data.index.fallback]
    issues = errors("issues.parquet")
    index_invalid = {
        (s, d)
        for s, d in issues.filter(pl.col("symbol").is_in(indices))
        .select("symbol", "date")
        .iter_rows()
    }
    ctx = daily_context(daily, ref.corporate_actions, invalid_daily, calendar, cfg.features)

    def minute_loader(syms: list[str], a: date, b: date) -> pl.DataFrame:
        parts = [raw.read_minute(s, a, b) for s in syms]
        return pl.concat(parts) if parts else pl.DataFrame()

    return ContextBuilder(
        cfg,
        calendar,
        ref.membership,
        ctx,
        pl.concat([excl, whole_days]),
        minute_loader,
        minute_loader,
        index_invalid,
    )


def scan_range(
    builder: ContextBuilder, cfg: Config, scorer: Scorer, start: date, end: date
) -> pl.DataFrame:
    days = [d for d in builder.calendar if start <= d <= end]
    parts = []
    for d in days:
        inputs = builder.day(d)
        if inputs.index is not None and inputs.index.substituted:
            log.info("%s: %s unavailable, using %s", d, cfg.data.index.primary, inputs.index.name)
        parts.append(scan_day(inputs, cfg, scorer))
    return pl.concat(parts) if parts else pl.DataFrame(schema=CANDIDATE_SCHEMA)

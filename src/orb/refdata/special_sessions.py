"""Detect special / abnormal sessions for ``special_sessions.csv`` (human-reviewed).

Detection, market-wide:
  from the bhavcopy (available now)
    * weekend         a session on a Saturday or Sunday
    * low_turnover    total traded value < 30% of the median of the surrounding
                      +-10 sessions (Muhurat sessions are ~1 hour)
  from 1-min bars (after the Kite download)
    * late_start      median first candle later than 09:15 + 30 min
    * early_end       median last candle earlier than 15:30 - 30 min
    * few_candles     median candles per stock < 300 (a normal session has 375)
    * extended_end    median last candle after 15:29 (outage days run late)

Every detected date and every user-supplied candidate date becomes a draft row
(``needs_review=true``) with ``detection_reason`` and a ``proposed_action``:
  exclude      Muhurat / DR / mock / any truncated session
  tag_budget   a full-length Saturday budget session: traded and tagged as a budget
               day (decision 16), not excluded
"""

from __future__ import annotations

from datetime import date

import polars as pl

from orb.data.store import ParquetStore

LOW_TURNOVER = 0.30
NORMAL_FIRST = 9 * 60 + 15
NORMAL_LAST_CLOSE = 15 * 60 + 30
SLACK_MIN = 30
MIN_CANDLES = 300

DRAFT_SCHEMA = {
    "date": pl.Date,
    "session_type": pl.String,
    "proposed_action": pl.String,
    "detection_reason": pl.String,
    "in_candidate_list": pl.Boolean,
    "candidate_label": pl.String,
    "bhav_present": pl.Boolean,
    "turnover_ratio": pl.Float64,
    "median_first": pl.String,
    "median_last": pl.String,
    "median_candles": pl.Float64,
    "needs_review": pl.Boolean,
    "note": pl.String,
}


def market_turnover(raw: ParquetStore, symbols: list[str] | None = None) -> pl.DataFrame:
    """(date, turnover, n_symbols) from raw daily bhavcopy bars of all symbols."""
    parts = []
    for s in symbols or raw.symbols("daily"):
        d = raw.read_daily(s, date(1990, 1, 1), date(2100, 1, 1))
        if d.height:
            parts.append(d.select("date", tv=pl.col("close") * pl.col("volume")))
    df = pl.concat(parts)
    return df.group_by("date").agg(turnover=pl.col("tv").sum(), n_symbols=pl.len()).sort("date")


def bhav_flags(turnover: pl.DataFrame, window: int = 10) -> pl.DataFrame:
    t = (
        turnover.sort("date")
        .with_columns(
            _med=pl.col("turnover").rolling_median(
                window_size=2 * window + 1, center=True, min_samples=window
            )
        )
        .with_columns(turnover_ratio=pl.col("turnover") / pl.col("_med"))
    )
    return t.with_columns(
        weekend=pl.col("date").dt.weekday() >= 6,
        low_turnover=pl.col("turnover_ratio") < LOW_TURNOVER,
    ).drop("_med")


def minute_flags(store: ParquetStore, symbols: list[str]) -> pl.DataFrame:
    """Per date: median first / last candle (minutes since midnight) and median
    candle count across symbols, plus the three timing flags."""
    parts = []
    for s in symbols:
        files = sorted(store.minute_dir(s).glob("*.parquet"))
        if not files:
            continue
        m = (
            pl.scan_parquet(files)
            .select(
                date=pl.col("ts").dt.date(),
                mod=pl.col("ts").dt.hour().cast(pl.Int32) * 60
                + pl.col("ts").dt.minute().cast(pl.Int32),
            )
            .group_by("date")
            .agg(first=pl.col("mod").min(), last=pl.col("mod").max(), candles=pl.len())
            .collect()
        )
        parts.append(m)
    if not parts:
        return pl.DataFrame(schema={"date": pl.Date})
    per = (
        pl.concat(parts)
        .group_by("date")
        .agg(
            median_first=pl.col("first").median(),
            median_last=pl.col("last").median(),
            median_candles=pl.col("candles").median(),
        )
    )
    return per.with_columns(
        late_start=pl.col("median_first") > NORMAL_FIRST + SLACK_MIN,
        # last candle starts at 15:29 -> closes 15:30
        early_end=pl.col("median_last") + 1 < NORMAL_LAST_CLOSE - SLACK_MIN,
        few_candles=pl.col("median_candles") < MIN_CANDLES,
        # outage days sometimes run past the normal close (e.g. 2021-02-24)
        extended_end=pl.col("median_last") > NORMAL_LAST_CLOSE - 1,
    ).sort("date")


def _hhmm(v: float | None) -> str | None:
    if v is None:
        return None
    v = int(v)
    return f"{v // 60:02d}:{v % 60:02d}"


def draft(
    bhav: pl.DataFrame, minute: pl.DataFrame | None, candidates: dict[date, str]
) -> pl.DataFrame:
    flags = ["weekend", "low_turnover", "late_start", "early_end", "few_candles", "extended_end"]
    j = bhav.select("date", "turnover_ratio", "weekend", "low_turnover").with_columns(
        bhav_present=pl.lit(True)
    )
    if minute is not None and minute.height:
        j = j.join(minute, on="date", how="full", coalesce=True)
    cand = pl.DataFrame(
        {"date": list(candidates), "candidate_label": list(candidates.values())},
        schema={"date": pl.Date, "candidate_label": pl.String},
    )
    j = j.join(cand, on="date", how="full", coalesce=True)
    present = [f for f in flags if f in j.columns]
    j = j.with_columns(pl.col(present).fill_null(False))
    j = j.with_columns(
        detection_reason=pl.concat_str(
            [pl.when(pl.col(f)).then(pl.lit(f)) for f in present], separator=";", ignore_nulls=True
        ),
        in_candidate_list=pl.col("candidate_label").is_not_null(),
        bhav_present=pl.col("bhav_present").fill_null(False),
    ).with_columns(
        detection_reason=pl.when(pl.col("detection_reason") == "")
        .then(None)
        .otherwise(pl.col("detection_reason"))
    )
    j = j.filter(pl.col("detection_reason").is_not_null() | pl.col("in_candidate_list"))
    truncated = (
        pl.col("low_turnover") | pl.col("late_start") | pl.col("early_end") | pl.col("few_candles")
        if "few_candles" in j.columns
        else pl.col("low_turnover")
    )
    label = pl.col("candidate_label").fill_null("")
    session_type = (
        pl.when(label.str.contains("(?i)muhurat"))
        .then(pl.lit("muhurat"))
        .when(label.str.contains("(?i)budget"))
        .then(pl.lit("special"))
        .when(pl.col("weekend"))
        .then(pl.lit("special"))
        .otherwise(pl.lit("other"))
    )
    action = (
        pl.when(label.str.contains("(?i)budget") & ~truncated)
        .then(pl.lit("tag_budget"))
        .otherwise(pl.lit("exclude"))
    )
    notes = (
        pl.when(~pl.col("bhav_present"))
        .then(pl.lit("no bhavcopy on this date: no session?"))
        .when(pl.col("in_candidate_list") & pl.col("detection_reason").is_null())
        .then(pl.lit("candidate date not detected by any rule"))
        .when(~pl.col("in_candidate_list"))
        .then(pl.lit("detected; not in the candidate list"))
        .otherwise(pl.lit("candidate confirmed by detection"))
    )
    out = j.with_columns(
        session_type=session_type, proposed_action=action, note=notes, needs_review=pl.lit(True)
    )
    for c, f in (("median_first", _hhmm), ("median_last", _hhmm)):
        if c in out.columns:
            out = out.with_columns(pl.col(c).map_elements(f, return_dtype=pl.String))
    for c in DRAFT_SCHEMA:
        if c not in out.columns:
            out = out.with_columns(pl.lit(None, DRAFT_SCHEMA[c]).alias(c))
    return out.select([pl.col(c).cast(t) for c, t in DRAFT_SCHEMA.items()]).sort("date")

"""First-breakout detection, causal feature snapshot, hard filters and scoring.

One candidate row per stock-day whose first breakout falls in the entry window.
All computable features are filled first; then the decision, in order:

  0. EXCLUDED_*             stock-day excluded (SESSION > DQ > CORP_ACTION > BAN):
                            logged with its features, never traded
  1. INSUFFICIENT_HISTORY   ATR or RV baseline unusable (rejected)
  2. MARKET_DATA_MISSING    neither Nifty 200 nor Nifty 50 usable (rejected)
  3. FILTERED_OR_WIDTH      w outside [filter_min, filter_max] (hard filter)
  4. SCORE_BELOW_THRESHOLD  score < threshold (rejected; stock done for the day)
  5. QUALIFIED              goes to the portfolio (deliverable 4)

The scanner calls the scorer only through the ``Scorer`` protocol.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import polars as pl

from orb.config import Config
from orb.context import DayInputs, StockDay
from orb.data.schema import TZ
from orb.features import first_breakout, index_return, opening_range, relative_volume
from orb.scoring.base import Scorer

QUALIFIED = "QUALIFIED"
REJECTED = "REJECTED"
FILTERED = "FILTERED"
EXCLUDED = "EXCLUDED"  # stock-day excluded (DQ, ban, corporate action, session): log only

# fed to the scorer (side: +1 long / -1 short)
FEATURE_KEYS = ["side", "d", "rv", "r_idx", "w", "v"]

CANDIDATE_SCHEMA = {
    "date": pl.Date,
    "symbol": pl.String,
    "signal_ts": pl.Datetime("us", TZ),
    "signal_slot": pl.Int64,
    "side": pl.String,
    "or_h": pl.Float64,
    "or_l": pl.Float64,
    "or_w": pl.Float64,
    "or_candles": pl.Int64,
    "close_t": pl.Float64,
    "atr14": pl.Float64,
    "prev_close": pl.Float64,
    "d": pl.Float64,
    "rv": pl.Float64,
    "rv_sessions": pl.Int64,
    "r_idx": pl.Float64,
    "index_used": pl.String,
    "index_substituted": pl.Boolean,
    "w": pl.Float64,
    "v": pl.Float64,
    "s_breakout": pl.Float64,
    "s_rel_volume": pl.Float64,
    "s_market": pl.Float64,
    "s_or_quality": pl.Float64,
    "s_volatility": pl.Float64,
    "score": pl.Float64,
    "scorer": pl.String,
    "decision": pl.String,
    "reason": pl.String,
    "exclusion_detail": pl.String,
}


def _row(day, sd: StockDay, slot_i, side, rng, cfg: Config) -> dict:
    start = datetime.combine(day, cfg.session.first_candle, tzinfo=ZoneInfo(TZ))
    return {
        "date": day,
        "symbol": sd.symbol,
        "signal_ts": start + timedelta(minutes=slot_i),
        "signal_slot": slot_i,
        "side": side,
        "or_h": rng.high,
        "or_l": rng.low,
        "or_w": rng.width,
        "or_candles": rng.candles,
        "close_t": float(sd.bars.close[slot_i]),
        "atr14": sd.atr14,
        "prev_close": sd.prev_close,
        "rv_sessions": sd.rv_sessions,
    }


def scan_stock(sd: StockDay, inputs: DayInputs, cfg: Config, scorer: Scorer) -> dict | None:
    """Compute every feature that is computable at t, then decide (module doc)."""
    rng = opening_range(sd.bars, cfg.session)
    if rng is None:
        return None
    hit = first_breakout(sd.bars, rng, cfg.session)
    if hit is None:
        return None
    t, side = hit
    row = _row(inputs.day, sd, t, side, rng, cfg)
    row["exclusion_detail"] = sd.exclusion_detail
    c = row["close_t"]

    atr = sd.atr14 if (sd.atr_reason is None and sd.atr14 and sd.atr14 > 0) else None
    if atr is not None:
        row["d"] = ((c - rng.high) if side == "long" else (rng.low - c)) / atr
        row["w"] = rng.width / atr
        row["v"] = atr / sd.prev_close if sd.prev_close else None
    rv_reason = sd.rv_reason
    if sd.rv_base is not None:
        row["rv"] = relative_volume(sd.bars.cum_volume(), sd.rv_base, t)
        if row["rv"] is None:
            rv_reason = "INSUFFICIENT_HISTORY:rv_zero_baseline"
    idx = inputs.index
    r = index_return(idx.bars, t) if idx is not None else None
    if r is not None:
        row.update(r_idx=r, index_used=idx.name, index_substituted=idx.substituted)

    features = {
        "side": 1.0 if side == "long" else -1.0,
        "d": row.get("d"),
        "rv": row.get("rv"),
        "r_idx": row.get("r_idx"),
        "w": row.get("w"),
        "v": row.get("v"),
    }
    complete = all(v is not None for v in features.values())
    if complete:
        if hasattr(scorer, "explain"):
            row.update(scorer.explain(features))
        row["score"] = float(scorer.score(features))
        row["scorer"] = scorer.name

    def decide(decision: str, reason: str | None) -> dict:
        return {**row, "decision": decision, "reason": reason}

    if sd.excluded_reason is not None:  # logged with whatever was computable, never traded
        return decide(EXCLUDED, sd.excluded_reason)
    history = sd.atr_reason or rv_reason
    if history is not None or atr is None:
        return decide(REJECTED, history or "INSUFFICIENT_HISTORY")
    if r is None:
        return decide(REJECTED, "MARKET_DATA_MISSING")
    q = cfg.scoring.or_quality
    if not q.filter_min <= row["w"] <= q.filter_max:
        return decide(FILTERED, "OR_WIDTH")
    if row["score"] < cfg.scoring.threshold:
        return decide(REJECTED, "SCORE_BELOW_THRESHOLD")
    return decide(QUALIFIED, None)


def scan_day(inputs: DayInputs, cfg: Config, scorer: Scorer) -> pl.DataFrame:
    rows = [r for sd in inputs.stocks if (r := scan_stock(sd, inputs, cfg, scorer))]
    df = (
        pl.DataFrame(rows, schema=CANDIDATE_SCHEMA)
        if rows
        else pl.DataFrame(schema=CANDIDATE_SCHEMA)
    )
    return df.sort("signal_ts", "symbol")

"""Reference data: point-in-time universe, ban list, corporate actions, calendars.

All tables are CSV or Parquet. Required tables raise if missing; optional ones
(used only for regime tagging) load as empty frames.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import polars as pl

from orb import csvio
from orb.config import ReferenceConfig

ACTION_TYPES = {"split", "bonus", "rights", "demerger", "merger", "dividend", "other"}
# Splits and bonuses always carry a price_factor. Rights/demerger factors are
# optional (they need prices); without one the ex-date is still excluded and
# the DQ gap check flags any unadjusted jump.
REQUIRES_FACTOR = {"split", "bonus"}
SESSION_TYPES = {"muhurat", "mock", "dr", "special", "other"}

SCHEMAS: dict[str, dict[str, pl.DataType]] = {
    "ban_list": {"date": pl.Date(), "symbol": pl.String()},
    "corporate_actions": {
        "symbol": pl.String(),
        "ex_date": pl.Date(),
        "action_type": pl.String(),
        "price_factor": pl.Float64(),  # multiply pre-ex-date prices by this
    },
    "special_sessions": {"date": pl.Date(), "session_type": pl.String()},
    "calendar_exceptions": {"date": pl.Date(), "reason": pl.String()},
    "budget_days": {"date": pl.Date()},
    "symbol_map": {
        "old_symbol": pl.String(),
        "new_symbol": pl.String(),
        "effective_date": pl.Date(),
        "change_type": pl.String(),  # rename | merger (optional column, default rename)
    },
    "expiries": {"date": pl.Date(), "expiry_type": pl.String()},
    "results_dates": {"symbol": pl.String(), "date": pl.Date()},
}
MEMBERSHIP_SCHEMA = {"symbol": pl.String(), "valid_from": pl.Date(), "valid_to": pl.Date()}

REQUIRED = {"membership", "ban_list", "corporate_actions", "special_sessions"}


class ReferenceError(ValueError):
    pass


def _read(path: Path) -> pl.DataFrame:
    if path.suffix == ".parquet":
        return pl.read_parquet(path)
    return csvio.read_csv(path, infer_schema_length=0)  # all strings; cast explicitly


def _cast(df: pl.DataFrame, schema: dict[str, pl.DataType], name: str) -> pl.DataFrame:
    missing = set(schema) - set(df.columns)
    if missing:
        raise ReferenceError(f"{name}: missing columns {sorted(missing)}")
    exprs = []
    for col, dtype in schema.items():
        e = pl.col(col)
        if dtype == pl.Date and df.schema[col] == pl.String:
            e = e.str.strip_chars().str.to_date(strict=True)
        elif dtype == pl.String:
            e = e.cast(pl.String).str.strip_chars()
        else:
            e = e.cast(dtype, strict=True)
        exprs.append(e.alias(col))
    try:
        return df.select(exprs)
    except pl.exceptions.PolarsError as exc:
        raise ReferenceError(f"{name}: cannot parse columns: {exc}") from exc


def load_table(name: str, path: Path, required: bool) -> pl.DataFrame:
    if not path.exists():
        if required:
            raise ReferenceError(f"required reference table {name!r} not found at {path}")
        return pl.DataFrame(schema=SCHEMAS[name])
    raw = _read(path)
    if name == "symbol_map" and "change_type" not in raw.columns:
        raw = raw.with_columns(change_type=pl.lit("rename"))
    df = _cast(raw, SCHEMAS[name], f"{name} ({path})")
    return _validate(name, df)


def _validate(name: str, df: pl.DataFrame) -> pl.DataFrame:
    if name == "corporate_actions":
        df = df.with_columns(pl.col("action_type").str.to_lowercase())
        bad = set(df["action_type"].unique()) - ACTION_TYPES
        if bad:
            raise ReferenceError(f"corporate_actions: unknown action_type {sorted(bad)}")
        need = df.filter(
            pl.col("action_type").is_in(list(REQUIRES_FACTOR)) & pl.col("price_factor").is_null()
        )
        if need.height:
            raise ReferenceError(
                f"corporate_actions: price_factor required for {need.head(5).to_dicts()}"
            )
        if df.filter(pl.col("price_factor") <= 0).height:
            raise ReferenceError("corporate_actions: price_factor must be > 0")
    if name == "special_sessions":
        df = df.with_columns(pl.col("session_type").str.to_lowercase())
        bad = set(df["session_type"].unique()) - SESSION_TYPES
        if bad:
            raise ReferenceError(f"special_sessions: unknown session_type {sorted(bad)}")
    if name == "symbol_map":
        df = df.with_columns(pl.col("change_type").str.to_lowercase())
        bad = set(df["change_type"].unique()) - {"rename", "merger"}
        if bad:
            raise ReferenceError(f"symbol_map: unknown change_type {sorted(bad)}")
    if name in ("ban_list", "results_dates"):
        df = df.unique(maintain_order=True)
    return df.sort(df.columns[:2])


# -------------------------------------------------------------------- membership


def load_membership(path: Path) -> pl.DataFrame:
    """Normalise to non-overlapping ``(symbol, valid_from, valid_to)`` intervals.

    Accepts either
      * a snapshot file ``(date, symbol)``: a symbol listed in snapshot d_i is a
        member from d_i up to the day before the next snapshot date (works for
        both daily and reconstitution-only snapshots); or
      * a change log ``(symbol, valid_from, valid_to)`` (valid_to empty = open).

    ``valid_to`` is inclusive; null means still a member.
    """
    if not path.exists():
        raise ReferenceError(f"required reference table 'membership' not found at {path}")
    raw = _read(path)
    cols = set(raw.columns)
    if {"symbol", "valid_from", "valid_to"} <= cols:
        df = raw.select(
            pl.col("symbol").cast(pl.String).str.strip_chars(),
            pl.col("valid_from").cast(pl.String).str.strip_chars().str.to_date(),
            pl.col("valid_to").cast(pl.String).str.strip_chars().replace("", None).str.to_date(),
        )
    elif {"date", "symbol"} <= cols:
        snap = _cast(
            raw, {"date": pl.Date(), "symbol": pl.String()}, f"membership ({path})"
        ).unique()
        dates = snap.select(pl.col("date").unique().sort())
        nxt = dates.with_columns((pl.col("date").shift(-1) - pl.duration(days=1)).alias("_until"))
        df = (
            snap.join(nxt, on="date")
            .sort("symbol", "date")
            # start a new interval whenever the previous snapshot didn't include the symbol
            .with_columns(
                _prev_until=pl.col("_until").shift(1).over("symbol"),
            )
            .with_columns(
                _new=(
                    pl.col("_prev_until").is_null()
                    | (pl.col("_prev_until") + pl.duration(days=1) != pl.col("date"))
                )
                .cum_sum()
                .over("symbol")
            )
            .group_by("symbol", "_new")
            .agg(
                valid_from=pl.col("date").min(),
                # open-ended if the run reaches the last snapshot
                valid_to=pl.when(pl.col("_until").is_null().any())
                .then(None)
                .otherwise(pl.col("_until").max()),
            )
            .drop("_new")
        )
    else:
        raise ReferenceError(f"membership: unrecognised columns {sorted(cols)}")

    df = df.select(pl.col(c).cast(t) for c, t in MEMBERSHIP_SCHEMA.items()).sort(
        "symbol", "valid_from"
    )
    bad = df.filter(pl.col("valid_to").is_not_null() & (pl.col("valid_to") < pl.col("valid_from")))
    if bad.height:
        raise ReferenceError(f"membership: valid_to < valid_from for {bad.head(5).to_dicts()}")
    # a later interval overlaps if the previous one is open-ended or ends on/after its start
    overlap = df.with_columns(
        _prev_to=pl.col("valid_to").shift(1).over("symbol"),
        _has_prev=pl.int_range(pl.len()).over("symbol") > 0,
    ).filter(
        pl.col("_has_prev")
        & (pl.col("_prev_to").is_null() | (pl.col("valid_from") <= pl.col("_prev_to")))
    )
    if overlap.height:
        raise ReferenceError(f"membership: overlapping intervals {overlap.head(5).to_dicts()}")
    return df


def members_on(intervals: pl.DataFrame, day) -> list[str]:
    """Point-in-time constituents on ``day``."""
    return (
        intervals.filter(
            (pl.col("valid_from") <= day)
            & (pl.col("valid_to").is_null() | (pl.col("valid_to") >= day))
        )["symbol"]
        .unique()
        .sort()
        .to_list()
    )


# ---------------------------------------------------------------- symbol map


class SymbolResolver:
    """Follows ``symbol_map`` forward in time from a historical symbol.

    ``chain("LTI")`` -> ``[("LTIM", "rename", 2022-12-05), ("LTM", "rename", 2026-02-27)]``.
    A merger ends the chain: the old company's prices are NOT the acquirer's.
    """

    def __init__(self, symbol_map: pl.DataFrame):
        self._next: dict[str, tuple[str, str, object]] = {}
        for r in symbol_map.sort("effective_date").to_dicts():
            self._next[r["old_symbol"]] = (r["new_symbol"], r["change_type"], r["effective_date"])

    def chain(self, symbol: str) -> list[tuple[str, str, object]]:
        out, seen, cur = [], {symbol}, symbol
        while cur in self._next:
            new, kind, eff = self._next[cur]
            out.append((new, kind, eff))
            if kind == "merger" or new in seen:
                break
            seen.add(new)
            cur = new
        return out

    def classify(self, symbol: str) -> tuple[str, str | None]:
        """('renamed', latest_symbol) | ('merged', acquirer) | ('delisted', None)."""
        ch = self.chain(symbol)
        if not ch:
            return "delisted", None
        merged = [c for c in ch if c[1] == "merger"]
        if merged:
            return "merged", merged[0][0]
        return "renamed", ch[-1][0]


# ------------------------------------------------------------------------ bundle


@dataclass(frozen=True)
class ReferenceData:
    membership: pl.DataFrame
    ban_list: pl.DataFrame
    corporate_actions: pl.DataFrame
    special_sessions: pl.DataFrame
    calendar_exceptions: pl.DataFrame
    budget_days: pl.DataFrame
    symbol_map: pl.DataFrame
    expiries: pl.DataFrame
    results_dates: pl.DataFrame

    def all_symbols(self) -> list[str]:
        return self.membership["symbol"].unique().sort().to_list()

    def stock_day_exclusions(self, excluding_action_types: list[str]) -> pl.DataFrame:
        """``(symbol, date, reason)`` rows for per-stock exclusions."""
        ban = self.ban_list.select("symbol", "date", reason=pl.lit("FNO_BAN"))
        ca = self.corporate_actions.filter(
            pl.col("action_type").is_in(excluding_action_types)
        ).select(
            "symbol",
            pl.col("ex_date").alias("date"),
            reason=pl.lit("CORPORATE_ACTION_") + pl.col("action_type").str.to_uppercase(),
        )
        return pl.concat([ban, ca]).unique(maintain_order=True).sort("date", "symbol")

    def day_exclusions(self) -> pl.DataFrame:
        """``(date, reason)`` rows for whole-market exclusions."""
        ss = self.special_sessions.select(
            "date", reason=pl.lit("SPECIAL_SESSION_") + pl.col("session_type").str.to_uppercase()
        )
        ce = self.calendar_exceptions.select(
            "date", reason=pl.lit("CALENDAR_EXCEPTION: ") + pl.col("reason")
        )
        return pl.concat([ss, ce]).unique(maintain_order=True).sort("date")


def load_reference(cfg: ReferenceConfig) -> ReferenceData:
    tables = {name: load_table(name, cfg.path(name), name in REQUIRED) for name in SCHEMAS}
    return ReferenceData(membership=load_membership(cfg.path("membership")), **tables)

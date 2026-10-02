"""Parsers and downloaders for NSE reference data.

Sources (all public, fetched with ``orb ref ...``):
  * F&O ban list:    nsearchives  /archives/fo/sec_ban/fo_secban_DDMMYYYY.csv
  * corporate actions: www.nseindia.com /api/corporates-corporateActions (JSON)
  * symbol changes:  nsearchives  /content/equities/symbolchange.csv
  * bhavcopy (raw daily OHLCV): see ``orb.refdata.bhavcopy``
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import date, datetime, timedelta

import polars as pl

from orb.config import NSEConfig
from orb.data.schema import DAILY_SCHEMA, empty
from orb.refdata import bhavcopy
from orb.refdata.http import Fetcher

# ---------------------------------------------------------------- calendar util


def weekdays(start: date, end: date) -> Iterable[date]:
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def month_ranges(start: date, end: date) -> Iterable[tuple[date, date]]:
    cur = start.replace(day=1)
    while cur <= end:
        nxt = (cur + timedelta(days=32)).replace(day=1)
        yield max(cur, start), min(nxt - timedelta(days=1), end)
        cur = nxt


# --------------------------------------------------------------------- ban list

_BAN_HEADER = re.compile(r"Trade Date\s+(\d{2}-[A-Z]{3}-\d{4})", re.I)


def ban_url(cfg: NSEConfig, day: date) -> str:
    return f"{cfg.archives_base}/archives/fo/sec_ban/fo_secban_{day:%d%m%Y}.csv"


def parse_ban(text: str) -> tuple[date, list[str]]:
    """'Securities in Ban For Trade Date 01-AUG-2024:' then 'n,SYMBOL' rows (or ': NIL')."""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    m = _BAN_HEADER.search(lines[0]) if lines else None
    if not m:
        raise ValueError(f"unrecognised ban file header: {lines[:1]}")
    day = datetime.strptime(m.group(1).title(), "%d-%b-%Y").date()
    if "NIL" in lines[0].upper().split(":")[-1]:
        return day, []
    symbols = [ln.split(",", 1)[1].strip() for ln in lines[1:] if "," in ln]
    return day, symbols


def download_ban(fetch: Fetcher, cfg: NSEConfig, start: date, end: date) -> pl.DataFrame:
    rows = []
    for day in weekdays(start, end):
        body = fetch.get(ban_url(cfg, day))
        if body is None or body.lstrip().startswith(b"<"):  # holiday / no file
            continue
        file_day, syms = parse_ban(body.decode("utf-8", "replace"))
        rows += [{"date": file_day, "symbol": s} for s in syms]
    return (
        pl.DataFrame(rows, schema={"date": pl.Date, "symbol": pl.String})
        .unique()
        .sort("date", "symbol")
    )


# ------------------------------------------------------------ corporate actions

_SPLIT = re.compile(
    r"(?:split|sub-?division).*?from\s*(?:rs\.?|re\.?|inr)?\s*([\d.]+).*?"
    r"to\s*(?:rs\.?|re\.?|inr)?\s*([\d.]+)",
    re.I,
)
_BONUS = re.compile(r"bonus\s*(\d+)\s*:\s*(\d+)", re.I)
_RIGHTS = re.compile(
    r"rights\s*(\d+)\s*:\s*(\d+)(?:.*?premium\s*(?:rs\.?|re\.?)?\s*([\d.]+))?", re.I
)


def classify_action(subject: str) -> tuple[str, float | None, dict]:
    """Subject text -> (action_type, price_factor, extra).

    * split "From Rs 10/- To Rs 2/-"       -> factor new/old = 0.2
    * bonus "a:b" (a new for b held)        -> factor b/(a+b)
    * rights "a:b @ Premium Rs p"           -> factor needs prices (see fill_rights_factors)
    """
    s = subject.strip()
    low = s.lower()
    if m := _SPLIT.search(s):
        old, new = float(m.group(1)), float(m.group(2))
        if old > 0 and new > 0 and new != old:
            return "split", new / old, {}
    if m := _BONUS.search(s):
        a, b = int(m.group(1)), int(m.group(2))
        return "bonus", b / (a + b), {}
    if m := _RIGHTS.search(s):
        prem = float(m.group(3)) if m.group(3) else 0.0
        return (
            "rights",
            None,
            {"rights_new": int(m.group(1)), "rights_held": int(m.group(2)), "rights_premium": prem},
        )
    if "demerger" in low or "spin" in low:
        return "demerger", None, {}
    if "amalgamation" in low or "merger" in low:
        return "merger", None, {}
    if "dividend" in low or re.search(r"\bdiv\b", low):
        return "dividend", None, {}
    return "other", None, {}


def ca_url(cfg: NSEConfig, start: date, end: date) -> str:
    return (
        f"{cfg.www_base}/api/corporates-corporateActions?index=equities"
        f"&from_date={start:%d-%m-%Y}&to_date={end:%d-%m-%Y}"
    )


def parse_corporate_actions(records: list[dict], series: list[str]) -> pl.DataFrame:
    rows = []
    for r in records:
        if r.get("series") not in series:
            continue
        kind, factor, extra = classify_action(r.get("subject", ""))
        fv = r.get("faceVal")
        rows.append(
            {
                "symbol": r["symbol"].strip(),
                "ex_date": datetime.strptime(r["exDate"].strip(), "%d-%b-%Y").date(),
                "action_type": kind,
                "price_factor": factor,
                "subject": r.get("subject", "").strip(),
                "face_value": float(fv) if fv not in (None, "-", "") else None,
                "rights_new": extra.get("rights_new"),
                "rights_held": extra.get("rights_held"),
                "rights_premium": extra.get("rights_premium"),
            }
        )
    schema = {
        "symbol": pl.String,
        "ex_date": pl.Date,
        "action_type": pl.String,
        "price_factor": pl.Float64,
        "subject": pl.String,
        "face_value": pl.Float64,
        "rights_new": pl.Int64,
        "rights_held": pl.Int64,
        "rights_premium": pl.Float64,
    }
    return pl.DataFrame(rows, schema=schema).unique().sort("ex_date", "symbol", "action_type")


def fill_rights_factors(ca: pl.DataFrame, raw_daily: pl.DataFrame) -> pl.DataFrame:
    """Rights factor = TERP / cum price, with cum price = last raw close before ex-date:
    TERP = (held * P + new * (face_value + premium)) / (held + new)."""
    cum = raw_daily.select("symbol", pl.col("date").alias("_d"), _p=pl.col("close")).sort("_d")
    r = ca.with_columns(_key=pl.col("ex_date") - pl.duration(days=1)).sort("_key")
    r = r.join_asof(
        cum, left_on="_key", right_on="_d", by="symbol", strategy="backward", check_sortedness=False
    )
    issue = pl.col("face_value") + pl.col("rights_premium")
    terp = (pl.col("rights_held") * pl.col("_p") + pl.col("rights_new") * issue) / (
        pl.col("rights_held") + pl.col("rights_new")
    )
    fill = (
        pl.when(
            (pl.col("action_type") == "rights")
            & pl.col("price_factor").is_null()
            & pl.col("_p").is_not_null()
            & issue.is_not_null()
            & (issue < pl.col("_p"))
        )
        .then(terp / pl.col("_p"))
        .otherwise(pl.col("price_factor"))
    )
    return (
        r.with_columns(price_factor=fill)
        .drop("_key", "_d", "_p")
        .sort("ex_date", "symbol", "action_type")
    )


def download_corporate_actions(
    fetch: Fetcher,
    cfg: NSEConfig,
    start: date,
    end: date,
    fresh: Fetcher | None = None,
    today: date | None = None,
) -> pl.DataFrame:
    """Month by month. Months ending within a week of ``today`` use ``fresh``
    (uncached) since NSE keeps adding announcements for upcoming ex-dates."""
    parts = []
    for a, b in month_ranges(start, end):
        recent = today is not None and b >= today - timedelta(days=7)
        body = (fresh if recent and fresh is not None else fetch).get(ca_url(cfg, a, b))
        if body:
            parts.append(parse_corporate_actions(json.loads(body), cfg.series))
    return (
        pl.concat(parts).unique().sort("ex_date", "symbol")
        if parts
        else (parse_corporate_actions([], cfg.series))
    )


# --------------------------------------------------------------- symbol changes


def symbol_change_url(cfg: NSEConfig) -> str:
    return f"{cfg.archives_base}/content/equities/symbolchange.csv"


def parse_symbol_changes(text: str) -> pl.DataFrame:
    """Header-less 'company name, old, new, DD-MON-YYYY'. The company name may contain
    commas, so fields are taken from the right."""
    rows = []
    for ln in text.splitlines():
        parts = [p.strip() for p in ln.rsplit(",", 3)]
        if len(parts) != 4:
            continue
        try:
            d = datetime.strptime(parts[3].title(), "%d-%b-%Y").date()
        except ValueError:
            continue  # header or junk line
        rows.append(
            {
                "old_symbol": parts[1],
                "new_symbol": parts[2],
                "effective_date": d,
                "change_type": "rename",
            }
        )
    schema = {
        "old_symbol": pl.String,
        "new_symbol": pl.String,
        "effective_date": pl.Date,
        "change_type": pl.String,
    }
    return pl.DataFrame(rows, schema=schema).unique().sort("effective_date", "old_symbol")


# -------------------------------------------------------------------- bhavcopy


def download_bhavcopy(
    fetch: Fetcher, cfg: NSEConfig, start: date, end: date
) -> tuple[pl.DataFrame, list[date]]:
    """Raw daily bars for all EQ/BE symbols; also returns weekdays with no file
    (holidays, or genuinely missing -> check against the trading calendar)."""
    parts, missing = [], []
    for day in weekdays(start, end):
        body = None
        for url in bhavcopy.urls_for(cfg.archives_base, day, cfg.bhavcopy_udiff_from):
            body = fetch.get(url)
            if body is not None and body[:2] == b"PK":
                break
            body = None
        if body is None:
            missing.append(day)
            continue
        parts.append(bhavcopy.parse(bhavcopy.unzip_single(body), cfg.series))
    df = pl.concat(parts) if parts else empty(DAILY_SCHEMA)
    return df, missing

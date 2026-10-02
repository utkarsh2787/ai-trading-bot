"""Merger candidates for ``manual/mergers.csv`` (every row needs human review).

Public NSE data has no structured merger list and no share counts, so the
"target disappears while the acquirer's share count jumps" signal cannot be
measured directly. Instead we combine:

  1. niftyindices press releases: "... scheme of amalgamation of <target> with /
     into <acquirer> ..." (company names mapped to symbols through the
     "Company Name  SYMBOL" table rows found across all releases);
  2. the raw bhavcopy: the target's last trading day (it must stop trading,
     and it must not be a plain rename in ``symbol_map``).

Rows are written with ``needs_review=true``; ``orb ref symbols`` only applies
rows whose ``needs_review`` is false (or absent).
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import polars as pl

from orb.refdata.nifty200 import _parse_date

_ROW = re.compile(r"^\s*\d+\s+(.+?)\s+([A-Z][A-Z0-9&\-]{1,19})\*?\s*$")
_AMALG = re.compile(
    r"(?i)amalgamation\s+of\s+(?P<target>.{3,90}?)\s+(?:with|into)\s+(?P<acq>.{3,90}?)"
    r"(?:\s*\([A-Z0-9&\- ]+\))?\s*(?:[.,;]\s|\s+and\s|\s+on\s|\s+\(|$)"
)
_EFFECTIVE = re.compile(r"(?i)effective\s+(?:from\s+)?([A-Z][a-z]+\.?\s+\d{1,2}\s*,?\s*\d{4})")

CANDIDATE_SCHEMA = {
    "old_symbol": pl.String,
    "new_symbol": pl.String,
    "effective_date": pl.Date,  # day after the target's last bhavcopy trade
    "index_release_date": pl.Date,  # effective date quoted in the index release
    "old_last_traded": pl.Date,
    "source": pl.String,
    "evidence": pl.String,
    "needs_review": pl.Boolean,
}


def _norm_name(name: str) -> str:
    n = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    n = re.sub(r"\b(the|limited|ltd|co|company|corporation|corp|india|of|and)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def name_to_symbol(texts: dict[str, str]) -> dict[str, str]:
    """Normalised company name -> symbol, from table rows in all releases."""
    out: dict[str, str] = {}
    for t in texts.values():
        for line in t.splitlines():
            if m := _ROW.match(line):
                out.setdefault(_norm_name(m.group(1)), m.group(2))
    return out


def _page_of(text: str, pos: int) -> int:
    return text.count("=== page ", 0, pos) or 1


def find_candidates(
    texts: dict[str, str],
    last_traded: dict[str, date],
    renamed: set[str],
    last_bhav_date: date,
) -> pl.DataFrame:
    """``texts``: {pdf filename: page-marked text}. ``last_traded``: symbol -> last
    bhavcopy date. Targets still trading on ``last_bhav_date`` are dropped."""
    names = name_to_symbol(texts)
    rows = []
    for fname, t in texts.items():
        flat = re.sub(r"\s+", " ", t)
        for m in _AMALG.finditer(flat):
            tgt, acq = _norm_name(m.group("target")), _norm_name(m.group("acq"))
            old, new = names.get(tgt), names.get(acq)
            if not old or not new or old == new or old in renamed:
                continue
            last = last_traded.get(old)
            if last is not None and last >= last_bhav_date:
                continue  # still trading: not merged away (yet)
            eff = _EFFECTIVE.search(flat, m.start())
            pos = t.find(m.group("target").split()[0])
            rows.append(
                {
                    "old_symbol": old,
                    "new_symbol": new,
                    "effective_date": (last + timedelta(days=1)) if last else None,
                    "index_release_date": _parse_date(eff.group(1)) if eff else None,
                    "old_last_traded": last,
                    "source": f"{fname} p.{_page_of(t, max(pos, 0))}",
                    "evidence": m.group(0)[:160],
                    "needs_review": True,
                }
            )
    df = pl.DataFrame(rows, schema=CANDIDATE_SCHEMA)
    return df.sort("effective_date", "old_symbol").unique(
        subset=["old_symbol", "new_symbol"], keep="first", maintain_order=True
    )

"""Point-in-time Nifty 200 membership, reconstructed from public sources.

1. Current constituents: ``niftyindices.com/IndexConstituent/ind_nifty200list.csv``.
2. Change announcements: the niftyindices.com press-release list (HTML) links to
   one PDF per announcement. PDFs mentioning Nifty 200 are parsed for its
   excluded/included lists and the effective date.
3. Walk backwards from today's list, undoing each change (and symbol renames),
   to get ``(symbol, valid_from, valid_to)`` intervals.

Anything the parser cannot read with confidence goes to a review list; manual
corrections in ``manual/nifty200_changes_manual.csv`` override parsed changes
for the same (effective_date, symbol). A size check (the index must hold
exactly 200 names) catches missed or mis-parsed announcements.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import polars as pl

INDEX = "nifty 200"
CHANGE_SCHEMA = {
    "effective_date": pl.Date,
    "symbol": pl.String,
    "change": pl.String,  # add | remove
    "source": pl.String,
}

_ITEM = re.compile(
    r'data-date="([^"]+)"[^>]*>\s*<p>[^<]*</p>\s*<a href=\'([^\']+)\'[^>]*>([^<]+)</a>'
)
_SKIP_TITLE = re.compile(
    r"(?i)fixed income|bond|g-sec|gsec|sdl|t-bill|debt|money market|target maturity|"
    r"commodity|consultation|tracking error|benchmark code|webinar"
)
_HEADING = re.compile(r"(?im)^\s*(?:[a-z]{1,2}|\d{1,2})\)\s*(nifty[^\n]*?)\s*$")
_ROW = re.compile(r"^\s*\d+\s+(.+?)\s+([A-Z0-9][A-Z0-9&\-]*)\s*$")
_EFFECTIVE = re.compile(r"(?i)effective\s+(?:from\s+)?([A-Z][a-z]+\.?\s+\d{1,2}\s*,?\s*\d{4})")
_DATE_FMTS = ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b. %d, %Y", "%b %d %Y", "%B %d,%Y")


def _parse_date(s: str) -> date | None:
    s = re.sub(r"\s+", " ", s.replace(" ,", ",")).strip()
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _norm(name: str) -> str:
    """'NIFTY 200 Index' / 'Nifty200' / 'Nifty 200' -> 'nifty 200'."""
    n = re.sub(r"\s+", " ", re.sub(r"(?i)nifty\s*", "nifty ", name)).strip().lower()
    return re.sub(r"\s+index$", "", n)


# ---------------------------------------------------------------- press list


def parse_press_list(html: str, base: str) -> pl.DataFrame:
    rows = []
    for d, href, title in _ITEM.findall(html):
        pub = datetime.strptime(d.strip(), "%b %d, %Y").date()
        url = href if href.startswith("http") else base.rstrip("/") + href
        rows.append({"published": pub, "url": url, "title": title.strip()})
    return (
        pl.DataFrame(rows, schema={"published": pl.Date, "url": pl.String, "title": pl.String})
        .unique()
        .sort("published")
    )


def equity_releases(press: pl.DataFrame, since: date) -> pl.DataFrame:
    return press.filter(
        (pl.col("published") >= since) & ~pl.col("title").str.contains(_SKIP_TITLE.pattern)
    )


def pdf_text(blob: bytes) -> str:
    from pypdf import PdfReader

    return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(blob)).pages)


# -------------------------------------------------------------- PDF parsing


@dataclass
class ParsedRelease:
    effective_date: date | None
    adds: list[str] = field(default_factory=list)
    removes: list[str] = field(default_factory=list)
    method: str = ""
    mentions_index: bool = False
    spinoff: bool = False  # demerger-related: temporary inclusion/exclusion of a spun-off entity

    @property
    def needs_review(self) -> bool:
        return self.mentions_index and (
            self.effective_date is None or not (self.adds or self.removes)
        )


def mentions_index(text: str) -> bool:
    # 'Nifty 200' / 'NIFTY 200' but not 'Nifty 200 Momentum 30', 'Nifty200 Quality 30'
    return bool(
        re.search(r"(?i)nifty ?200(?![ \t]*(?:momentum|quality|value|alpha|low|equal|\d))", text)
    )


def _section_changes(text: str) -> tuple[list[str], list[str]] | None:
    heads = list(_HEADING.finditer(text))
    for i, h in enumerate(heads):
        if _norm(h.group(1)) != INDEX:
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        adds: list[str] = []
        removes: list[str] = []
        mode = None
        for line in text[h.end() : end].splitlines():
            low = line.lower()
            if "being excluded" in low:
                mode = removes
            elif "being included" in low:
                mode = adds
            elif mode is not None and (m := _ROW.match(line)):
                mode.append(m.group(2).rstrip("*"))
        if adds or removes:
            return adds, removes
    return None


def _single_exclusion(text: str) -> tuple[list[str], list[str]] | None:
    """'Exclusion of X Ltd. (SYMBOL ...) from Nifty indices' + a list of index names."""
    if not re.search(r"(?i)exclusion of .{3,120}? from (?:various |nifty )?ind", text[:600]):
        return None
    if not re.search(r"(?im)^\s*\d+\s+nifty 200\s*$", text):
        return None
    sym = re.search(r"\(([A-Z][A-Z0-9&\-]{1,19})(?:\s+or\b[^)]*)?\)", text)
    return ([], [sym.group(1)]) if sym else None


def parse_release(text: str) -> ParsedRelease:
    # background paragraphs can quote earlier effective dates: take the first one
    # at or after the committee's decision sentence
    decided = re.search(r"(?i)has decided", text)
    m = _EFFECTIVE.search(text, decided.start() if decided else 0) or _EFFECTIVE.search(text)
    eff = _parse_date(m.group(1)) if m else None
    out = ParsedRelease(
        effective_date=eff,
        mentions_index=mentions_index(text),
        spinoff=bool(re.search(r"(?i)demerg|spun[- ]?off|spin[- ]?off", text)),
    )
    for method, fn in (("section", _section_changes), ("single_exclusion", _single_exclusion)):
        res = fn(text)
        if res:
            out.adds, out.removes = res
            out.method = method
            break
    return out


# ----------------------------------------------------------- reconstruction


def classify_release(title: str, parsed: ParsedRelease) -> str:
    """'changes' | 'ignore' | 'review'.

    Spin-offs: Nifty adds the demerged entity temporarily (often before it even
    lists) and excludes it days later. Both steps are treated as non-events, so
    'Corporate (Action) Adjustment' releases and single exclusions of a spun-off
    entity are ignored, unless the title also announces replacements.
    """
    t = title.lower()
    if "launch" in t and not (parsed.adds or parsed.removes):
        return "ignore"  # a new index built on Nifty 200, no constituent change
    corp_adj = t.startswith("corporate adjustment") or t.startswith("corporate action adjustment")
    if corp_adj and "replacement" not in t:
        return "ignore"
    if parsed.method == "single_exclusion" and parsed.spinoff:
        return "ignore"
    if parsed.needs_review:
        return "review"
    return "changes" if (parsed.adds or parsed.removes) else "ignore"


@dataclass
class Reconstruction:
    membership: pl.DataFrame  # symbol, valid_from, valid_to
    sizes: pl.DataFrame  # date, n  (count in force from that date)
    inconsistencies: list[str]

    def size_violations(self, expected: int = 200) -> pl.DataFrame:
        return self.sizes.filter(pl.col("n") != expected)


def reconstruct(
    current: list[str],
    as_of: date,
    changes: pl.DataFrame,
    renames: pl.DataFrame,
    start: date,
) -> Reconstruction:
    """Undo changes newest-first. ``changes``: (effective_date, symbol, change);
    ``renames``: (old_symbol, new_symbol, effective_date) with change_type rename."""
    events: list[tuple[date, int, str, str, str]] = []  # (date, order, kind, a, b)
    for r in changes.filter(pl.col("effective_date").is_between(start, as_of)).to_dicts():
        events.append((r["effective_date"], 1, r["change"], r["symbol"], ""))
    for r in renames.filter(pl.col("effective_date").is_between(start, as_of)).to_dicts():
        events.append((r["effective_date"], 0, "rename", r["old_symbol"], r["new_symbol"]))
    # newest first; on the same date undo index changes (order 1) before renames (order 0)
    events.sort(key=lambda e: (e[0], e[1]), reverse=True)

    alias_new_to_old = {(r["new_symbol"]): r["old_symbol"] for r in renames.to_dicts()}
    alias_old_to_new = {v: k for k, v in alias_new_to_old.items()}
    state: dict[str, date | None] = {s: None for s in current}  # symbol -> valid_to
    intervals: list[dict] = []
    sizes: list[dict] = [{"date": as_of, "n": len(state)}]
    problems: list[str] = []

    def close(sym: str, valid_from: date) -> None:
        intervals.append({"symbol": sym, "valid_from": valid_from, "valid_to": state.pop(sym)})

    for d, _, kind, a, b in events:
        if kind == "rename":
            if b in state:  # member under the new name from d; under the old name before
                close(b, d)
                state[a] = d - timedelta(days=1)
        elif kind == "add":
            sym = a if a in state else alias_old_to_new.get(a, alias_new_to_old.get(a, a))
            if sym in state:
                close(sym, d)
            else:
                problems.append(f"{d}: add {a} but not a member after the change")
        elif kind == "remove":
            if a in state:
                problems.append(f"{d}: remove {a} but it is still a member after the change")
            else:
                state[a] = d - timedelta(days=1)
        sizes.append({"date": d - timedelta(days=1), "n": len(state)})
    for sym in list(state):
        close(sym, start)

    # an interval ending before it starts (renamed same day as added) is dropped
    mem = (
        pl.DataFrame(
            intervals, schema={"symbol": pl.String, "valid_from": pl.Date, "valid_to": pl.Date}
        )
        .filter(pl.col("valid_to").is_null() | (pl.col("valid_to") >= pl.col("valid_from")))
        .sort("symbol", "valid_from")
    )
    size_df = (
        pl.DataFrame(sizes, schema={"date": pl.Date, "n": pl.Int64})
        .group_by("date")
        .agg(pl.col("n").last())
        .sort("date")
    )
    return Reconstruction(mem, size_df, problems)


def merge_manual(auto: pl.DataFrame, manual: pl.DataFrame) -> pl.DataFrame:
    """Manual rows replace parsed rows for the same (effective_date, symbol).
    A manual row with change 'ignore' deletes the parsed row."""
    if manual.height == 0:
        return auto
    keys = manual.select("effective_date", "symbol")
    kept = auto.join(keys, on=["effective_date", "symbol"], how="anti")
    return (
        pl.concat([kept, manual.filter(pl.col("change") != "ignore").select(auto.columns)])
        .unique()
        .sort("effective_date", "symbol")
    )

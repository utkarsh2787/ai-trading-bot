"""F&O stock membership per date, from F&O bhavcopies already cached (V2 circuit guard).

Only weekly samples of the F&O bhavcopy are cached (``orb ref expiries`` fetched
the first trading day of every week); V2 forbids new downloads. So:

* ``samples_from_cache``: (sample_date, symbol) for every stock FUTURES contract
  (``FUTSTK`` legacy / ``STF`` UDiFF) in every cached file, symbols mapped forward
  through renames to today's names (the raw-store names).
* ``FnoCalendar.is_fno(symbol, d)``: listed in BOTH weekly samples that bracket d
  (the last sample alone after the final sample), or on the F&O ban list on d.
  A transition week therefore counts as non-F&O, so the circuit guard applies.
"""

from __future__ import annotations

import bisect
import io
import re
import zipfile
from datetime import date, datetime
from pathlib import Path

import polars as pl

from orb import csvio

STOCK_FUTURES = {"FUTSTK", "STF"}
SAMPLE_SCHEMA = {"sample_date": pl.Date, "symbol": pl.String}

_LEGACY = re.compile(r"fo(\d{2}[A-Z]{3}\d{4})bhav\.csv\.zip$")
_UDIFF = re.compile(r"BhavCopy_NSE_FO_0_0_0_(\d{8})_F_0000\.csv\.zip$")


def sample_date(name: str) -> date | None:
    if m := _LEGACY.search(name):
        return datetime.strptime(m.group(1).title(), "%d%b%Y").date()
    if m := _UDIFF.search(name):
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    return None


def stock_futures(csv: bytes, name: str = "F&O bhavcopy") -> list[str]:
    """Underlyings with a stock futures contract in one F&O bhavcopy (either format)."""
    df = csvio.read_csv(
        io.BytesIO(csv), name=name, infer_schema_length=0, truncate_ragged_lines=True
    )
    df = df.rename({c: c.strip() for c in df.columns})
    inst, sym = (
        ("FinInstrmTp", "TckrSymb") if "FinInstrmTp" in df.columns else ("INSTRUMENT", "SYMBOL")
    )
    return (
        df.select(i=pl.col(inst).str.strip_chars(), s=pl.col(sym).str.strip_chars())
        .filter(pl.col("i").is_in(list(STOCK_FUTURES)))["s"]
        .unique()
        .sort()
        .to_list()
    )


class Renamer:
    """Follow renames effective after a date forward to today's symbol."""

    def __init__(self, renames: pl.DataFrame):
        self.nxt = {
            r["old_symbol"]: (r["new_symbol"], r["effective_date"])
            for r in renames.sort("effective_date").to_dicts()
        }
        self._memo: dict[tuple[str, date], str] = {}

    def __call__(self, symbol: str, on: date) -> str:
        key = (symbol, on)
        if key not in self._memo:
            cur, seen = symbol, {symbol}
            while cur in self.nxt and self.nxt[cur][1] > on and self.nxt[cur][0] not in seen:
                cur = self.nxt[cur][0]
                seen.add(cur)
            self._memo[key] = cur
        return self._memo[key]


def samples_from_cache(cache_dir: str | Path, renames: pl.DataFrame) -> pl.DataFrame:
    rows, canonical = [], Renamer(renames)
    for p in sorted(Path(cache_dir).glob("*.zip")):
        d = sample_date(p.name)
        if d is None:
            continue
        with zipfile.ZipFile(p) as z:
            body = z.read(z.namelist()[0])
        rows += [(d, canonical(s, d)) for s in stock_futures(body, str(p))]
    return (
        pl.DataFrame(rows, schema=SAMPLE_SCHEMA, orient="row")
        .unique()
        .sort("sample_date", "symbol")
    )


class FnoCalendar:
    def __init__(self, samples: pl.DataFrame, ban: pl.DataFrame | None = None):
        self.dates: list[date] = samples["sample_date"].unique().sort().to_list()
        self.by_date: dict[date, set[str]] = {
            d: set(g["symbol"]) for (d,), g in samples.group_by("sample_date")
        }
        self.ban = (
            {(s, d) for s, d in ban.select("symbol", "date").iter_rows()}
            if ban is not None
            else set()
        )

    def is_fno(self, symbol: str, day: date) -> bool:
        if (symbol, day) in self.ban:
            return True
        i = bisect.bisect_right(self.dates, day) - 1  # last sample <= day
        if i < 0:
            return False
        prev = self.by_date[self.dates[i]]
        if self.dates[i] == day or i + 1 == len(self.dates):
            return symbol in prev
        return symbol in prev and symbol in self.by_date[self.dates[i + 1]]

    def on(self, day: date, symbols: list[str]) -> set[str]:
        return {s for s in symbols if self.is_fno(s, day)}

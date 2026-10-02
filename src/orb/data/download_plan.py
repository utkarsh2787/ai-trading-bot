"""What to download from Kite, how long it will take, and what we got.

* ``download_symbols``: the union of every symbol in ANY Nifty 200 membership
  version: the rebuilt membership, every parsed change, every manual change
  (including rows still at needs_review=true) and the current constituents,
  plus the index instruments. Pending reviews never shrink the list.
* ``plan``: chunk and request counts and a runtime estimate, without calling Kite.
* ``coverage``: per-symbol first/last 1-min date and the earliest date by which
  >= 95% of the list has 1-min data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from orb.config import Config
from orb.data.download import Manifest
from orb.data.kite import date_chunks
from orb.data.store import ParquetStore

# Kite historical API: ~3 requests/s; a sequential client also pays response
# latency, so the realistic pace is slower than the limit.
REALISTIC_SEC_PER_REQUEST = 0.6
BYTES_PER_MINUTE_ROW = 14  # zstd parquet, observed order of magnitude


def _col(path: Path, col: str) -> set[str]:
    if not path.exists():
        return set()
    df = pl.read_csv(path, infer_schema_length=0)
    return set(df[col].str.strip_chars().drop_nulls()) if col in df.columns else set()


def download_symbols(cfg: Config) -> tuple[list[str], dict[str, int]]:
    """(symbols, count per source). Index instruments are appended last."""
    root = Path(cfg.reference.root)
    sources = {
        "membership": _col(cfg.reference.path("membership"), "symbol"),
        "parsed_changes": _col(root / "nifty200_changes_parsed.csv", "symbol"),
        "manual_changes": _col(root / "manual" / "nifty200_changes_manual.csv", "symbol"),
        "current_constituents": set().union(
            *(
                _col(p, "Symbol")
                for p in (root / "_cache" / "nifty200").glob("*ind_nifty200list.csv")
            )
        ),
    }
    if not sources["membership"]:
        raise RuntimeError("no Nifty 200 membership yet: run `orb ref nifty200` first")
    stocks = sorted(set().union(*sources.values()))
    i = cfg.data.index
    indices = [i.primary, i.fallback, i.vix]
    counts = {k: len(v) for k, v in sources.items()} | {
        "stocks": len(stocks),
        "indices": len(indices),
    }
    return stocks + [s for s in indices if s not in stocks], counts


@dataclass
class Plan:
    symbols: int
    minute_range: tuple[date, date]
    daily_range: tuple[date, date]
    minute_requests: int
    daily_requests: int
    already_done: int
    est_minute_rows: int

    @property
    def requests(self) -> int:
        return self.minute_requests + self.daily_requests - self.already_done + 2  # + instruments

    def text(self, rate: float) -> str:
        lo = self.requests / rate
        hi = self.requests * max(REALISTIC_SEC_PER_REQUEST, 1 / rate)
        h = lambda s: f"{s / 3600:.1f} h" if s >= 3600 else f"{s / 60:.0f} min"  # noqa: E731
        return "\n".join(
            [
                f"symbols:          {self.symbols}",
                f"1-min range:      {self.minute_range[0]} .. {self.minute_range[1]}",
                f"daily range:      {self.daily_range[0]} .. {self.daily_range[1]}",
                f"requests:         {self.minute_requests} minute + {self.daily_requests} daily"
                f" + 2 instruments - {self.already_done} already done = {self.requests}",
                f"est. runtime:     {h(lo)} at the {rate:g} req/s limit, ~{h(hi)} realistic",
                f"est. 1-min rows:  ~{self.est_minute_rows / 1e6:.0f} M "
                f"(~{self.est_minute_rows * BYTES_PER_MINUTE_ROW / 1e9:.1f} GB on disk)",
            ]
        )


def _trading_days(a: date, b: date) -> int:
    weekdays = sum(1 for i in range((b - a).days + 1) if (a + timedelta(days=i)).weekday() < 5)
    return int(weekdays * 0.96)  # ~15 exchange holidays a year


def plan(
    cfg: Config, symbols: list[str], end: date, manifest: Manifest | None, provider: str
) -> Plan:
    k = cfg.data.kite
    mr = (cfg.data.minute_history_start, end)
    dr = (cfg.data.daily_history_start, end)
    m_chunks = list(date_chunks(*mr, k.minute_chunk_days))
    d_chunks = list(date_chunks(*dr, k.day_chunk_days))
    done = 0
    if manifest is not None:
        for s in symbols:
            done += sum(manifest.is_done(provider, "minute", s, a, b) for a, b in m_chunks)
            done += sum(manifest.is_done(provider, "daily", s, a, b) for a, b in d_chunks)
    n = len(symbols)
    return Plan(n, mr, dr, n * len(m_chunks), n * len(d_chunks), done, n * _trading_days(*mr) * 375)


def coverage(
    store: ParquetStore, symbols: list[str], threshold: float = 0.95
) -> tuple[pl.DataFrame, str]:
    rows = []
    for s in symbols:
        days = store.minute_dates(s)
        rows.append(
            {
                "symbol": s,
                "first_1min": days[0] if days else None,
                "last_1min": days[-1] if days else None,
                "days_1min": len(days),
            }
        )
    cov = pl.DataFrame(
        rows,
        schema={
            "symbol": pl.String,
            "first_1min": pl.Date,
            "last_1min": pl.Date,
            "days_1min": pl.Int64,
        },
    )
    n = cov.height
    firsts = sorted(d for d in cov["first_1min"].to_list() if d is not None)
    need = math.ceil(threshold * n)
    missing = n - len(firsts)
    if n == 0:
        line = "coverage: empty symbol list"
    elif len(firsts) >= need:
        line = (
            f"coverage: {threshold:.0%} of {n} symbols have 1-min data from "
            f"{firsts[need - 1]} ({missing} symbols have none)"
        )
    else:
        line = (
            f"coverage: {threshold:.0%} never reached: {len(firsts)}/{n} symbols "
            f"({len(firsts) / n:.1%}) have any 1-min data; {missing} have none "
            "(delisted/merged: see `orb download` warnings)"
        )
    return cov.sort("first_1min", "symbol", nulls_last=True), line

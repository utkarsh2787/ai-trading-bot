"""Resumable bulk download from a DataProvider into the ParquetStore.

Progress is an append-only JSONL manifest; each completed chunk is one line,
written (and fsynced) only after its data is safely in the store. A crash or
Ctrl-C loses at most the in-flight chunk. Chunks that touch ``today`` or later
are stored but never marked complete, so they are re-fetched next time.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from orb.data.kite import date_chunks
from orb.data.provider import DataProvider, FatalProviderError, ProviderError, SymbolNotFound
from orb.data.store import ParquetStore

log = logging.getLogger(__name__)

Kind = Literal["minute", "daily"]


@dataclass
class DownloadReport:
    fetched: list[tuple[str, date, date, int]] = field(default_factory=list)
    skipped: int = 0
    unresolved: list[str] = field(default_factory=list)
    failed: list[tuple[str, date, date, str]] = field(default_factory=list)


class Manifest:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._done: set[tuple[str, str, str, str]] = set()
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    r = json.loads(line)
                    self._done.add((r["provider"], r["kind"], r["symbol"], r["start"] + r["end"]))

    def is_done(self, provider: str, kind: str, symbol: str, start: date, end: date) -> bool:
        return (provider, kind, symbol, start.isoformat() + end.isoformat()) in self._done

    def mark(self, provider: str, kind: str, symbol: str, start: date, end: date, rows: int):
        rec = {
            "provider": provider,
            "kind": kind,
            "symbol": symbol,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "rows": rows,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._done.add((provider, kind, symbol, rec["start"] + rec["end"]))


class Downloader:
    def __init__(
        self,
        provider: DataProvider,
        store: ParquetStore,
        manifest: Manifest,
        chunk_days: dict[Kind, int],
    ):
        self.provider = provider
        self.store = store
        self.manifest = manifest
        self.chunk_days = chunk_days

    def run(
        self, symbols: list[str], kind: Kind, start: date, end: date, today: date
    ) -> DownloadReport:
        report = DownloadReport()
        fetch = self.provider.minute_bars if kind == "minute" else self.provider.daily_bars
        write = self.store.write_minute if kind == "minute" else self.store.write_daily
        pname = self.provider.name
        for symbol in symbols:
            for a, b in date_chunks(start, end, self.chunk_days[kind]):
                if self.manifest.is_done(pname, kind, symbol, a, b):
                    report.skipped += 1
                    continue
                try:
                    df = fetch(symbol, a, b)
                except FatalProviderError:
                    raise
                except SymbolNotFound:
                    log.warning("unresolved symbol %s", symbol)
                    report.unresolved.append(symbol)
                    break
                except ProviderError as exc:
                    log.error("failed %s %s %s..%s: %s", kind, symbol, a, b, exc)
                    report.failed.append((symbol, a, b, str(exc)))
                    continue
                write(df)
                if b < today:
                    self.manifest.mark(pname, kind, symbol, a, b, df.height)
                report.fetched.append((symbol, a, b, df.height))
        return report

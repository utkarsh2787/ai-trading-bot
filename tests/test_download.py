from datetime import date

import polars as pl
import pytest

from orb.data.download import Downloader, Manifest
from orb.data.provider import DataProvider, FatalProviderError, ProviderError, SymbolNotFound
from orb.data.store import ParquetStore
from orb.synthetic import daily_bars


class FakeProvider(DataProvider):
    name = "fake"

    def __init__(self, fail_on: set | None = None, fatal_on: set | None = None):
        self.calls: list[tuple[str, date, date]] = []
        self.fail_on = fail_on or set()
        self.fatal_on = fatal_on or set()

    def minute_bars(self, symbol, start, end):
        raise NotImplementedError

    def daily_bars(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        if symbol == "GONE":
            raise SymbolNotFound(symbol)
        if (symbol, start) in self.fatal_on:
            raise FatalProviderError("token expired")
        if (symbol, start) in self.fail_on:
            raise ProviderError("timeout")
        return daily_bars(symbol, date(2024, 1, 1), 60).filter(
            pl.col("date").is_between(start, end)
        )


def _dl(tmp_path, provider):
    return Downloader(
        provider,
        ParquetStore(tmp_path),
        Manifest(tmp_path / "m.jsonl"),
        {"daily": 30, "minute": 30},
    )


START, END, TODAY = date(2024, 1, 1), date(2024, 3, 15), date(2024, 6, 1)


def test_resume_skips_completed_chunks(tmp_path):
    p = FakeProvider()
    r1 = _dl(tmp_path, p).run(["A", "B"], "daily", START, END, TODAY)
    assert len(r1.fetched) == 6 and r1.skipped == 0
    p2 = FakeProvider()
    r2 = _dl(tmp_path, p2).run(["A", "B"], "daily", START, END, TODAY)  # fresh manifest load
    assert p2.calls == [] and r2.skipped == 6


def test_failed_chunk_is_retried_on_next_run(tmp_path):
    r1 = _dl(tmp_path, FakeProvider(fail_on={("A", date(2024, 1, 31))})).run(
        ["A"], "daily", START, END, TODAY
    )
    assert len(r1.failed) == 1 and len(r1.fetched) == 2
    p2 = FakeProvider()
    _dl(tmp_path, p2).run(["A"], "daily", START, END, TODAY)
    assert p2.calls == [("A", date(2024, 1, 31), date(2024, 2, 29))]
    stored = ParquetStore(tmp_path).read_daily("A", START, END)
    assert stored.height == daily_bars("A", START, 60).filter(pl.col("date") <= END).height


def test_chunk_touching_today_not_marked_complete(tmp_path):
    _dl(tmp_path, FakeProvider()).run(["A"], "daily", START, END, today=date(2024, 3, 10))
    p2 = FakeProvider()
    _dl(tmp_path, p2).run(["A"], "daily", START, END, today=date(2024, 3, 10))
    assert p2.calls == [("A", date(2024, 3, 1), END)]


def test_unresolved_symbol_reported_and_others_continue(tmp_path):
    r = _dl(tmp_path, FakeProvider()).run(["GONE", "A"], "daily", START, END, TODAY)
    assert r.delisted == ["GONE"]
    assert {s for s, *_ in r.fetched} == {"A"}


def test_fatal_error_stops_download_but_keeps_progress(tmp_path):
    p = FakeProvider(fatal_on={("B", START)})
    with pytest.raises(FatalProviderError):
        _dl(tmp_path, p).run(["A", "B"], "daily", START, END, TODAY)
    p2 = FakeProvider()
    _dl(tmp_path, p2).run(["A", "B"], "daily", START, END, TODAY)
    assert {s for s, *_ in p2.calls} == {"B"}


class RenamingProvider(FakeProvider):
    """Knows only current symbols, like Kite's instrument list."""

    known = {"LTM", "A"}

    def daily_bars(self, symbol, start, end):
        if symbol not in self.known:
            self.calls.append((symbol, start, end))
            raise SymbolNotFound(symbol)
        return super().daily_bars(symbol, start, end)


def test_unresolved_split_into_renamed_merged_delisted(tmp_path):
    import polars as pl

    from orb.data.reference import SymbolResolver

    sm = pl.DataFrame(
        {
            "old_symbol": ["LTI", "LTIM", "HDFC"],
            "new_symbol": ["LTIM", "LTM", "HDFCBANK"],
            "effective_date": [date(2022, 12, 5), date(2026, 2, 27), date(2023, 7, 13)],
            "change_type": ["rename", "rename", "merger"],
        }
    )
    dl = Downloader(
        RenamingProvider(),
        ParquetStore(tmp_path),
        Manifest(tmp_path / "m.jsonl"),
        {"daily": 30, "minute": 30},
        resolver=SymbolResolver(sm),
    )
    r = dl.run(["LTI", "HDFC", "GONE2", "A"], "daily", START, END, TODAY)
    assert set(r.renamed) == {("LTI", "LTM")}  # rename chain followed to the end
    assert r.merged == [("HDFC", "HDFCBANK")]  # acquirer's prices NOT used
    assert r.delisted == ["GONE2"]
    stored = ParquetStore(tmp_path).read_daily("LTI", START, END)
    assert stored.height > 0 and stored["symbol"].unique().to_list() == ["LTI"]
    assert ParquetStore(tmp_path).read_daily("HDFC", START, END).height == 0

"""The only way this project reads and writes CSV.

* ``write_csv``: explicit RFC-4180 quoting (any field containing a comma, quote
  or newline is quoted), atomic replace.
* ``read_csv``: every parse error names the file (or the label of an
  in-memory source such as a downloaded bhavcopy).
* ``ragged_rows`` / ``check_tree``: rows whose field count differs from the
  header, found with the stdlib ``csv`` module so correctly quoted commas are
  never false positives.
"""

from __future__ import annotations

import csv
import io
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import polars as pl


class CsvReadError(ValueError):
    pass


def read_csv(
    source: str | Path | bytes | io.BytesIO, *, name: str | None = None, **kw: Any
) -> pl.DataFrame:
    label = name or (str(source) if isinstance(source, (str, Path)) else "<in-memory csv>")
    if isinstance(source, bytes):
        source = io.BytesIO(source)
    try:
        return pl.read_csv(source, **kw)
    except Exception as exc:  # noqa: BLE001 - re-raised with the file name
        raise CsvReadError(f"{label}: {exc}") from exc


def write_csv(df: pl.DataFrame, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.write_csv(tmp, quote_style="necessary")
    os.replace(tmp, path)
    return path


def ragged_rows(path: str | Path) -> list[tuple[int, int, int]]:
    """[(line number, fields, expected fields)] for rows that don't match the header."""
    out = []
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            return out
        n = len(header)
        for row in reader:
            if row and len(row) != n:
                out.append((reader.line_num, len(row), n))
    return out


def check_tree(root: str | Path, exclude_dirs: Iterable[str] = ("_cache",)) -> pl.DataFrame:
    """Ragged rows in every CSV under ``root`` (skipping ``exclude_dirs``)."""
    root = Path(root)
    skip = set(exclude_dirs)
    rows = []
    for p in sorted(root.rglob("*.csv")):
        if skip & set(p.relative_to(root).parts[:-1]):
            continue
        for line, got, want in ragged_rows(p):
            rows.append({"file": str(p), "line": line, "fields": got, "expected": want})
    return pl.DataFrame(
        rows, schema={"file": pl.String, "line": pl.Int64, "fields": pl.Int64, "expected": pl.Int64}
    )

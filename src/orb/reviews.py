"""Human-review gate: no backtest while any manual reference row is unreviewed.

Every CSV under ``<reference.root>/manual/`` that has a ``needs_review`` column
is checked; a row counts as reviewed only when the value is ``false``/``0``.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl


def pending_reviews(ref_root: str | Path) -> dict[str, list[str]]:
    """{file name: [row labels still needing review]} (empty dict = all reviewed)."""
    out: dict[str, list[str]] = {}
    for p in sorted((Path(ref_root) / "manual").glob("*.csv")):
        df = pl.read_csv(p, infer_schema_length=0)
        if "needs_review" not in df.columns:
            continue
        v = df["needs_review"].fill_null("").str.strip_chars().str.to_lowercase()
        bad = df.filter(~v.is_in(["false", "0"]))
        if bad.height:
            key = [c for c in ("date", "effective_date", "old_symbol", "symbol") if c in df.columns]
            out[p.name] = [" ".join(str(r[c]) for c in key[:2]) for r in bad.iter_rows(named=True)]
    return out


def require_reviewed(ref_root: str | Path) -> None:
    pending = pending_reviews(ref_root)
    if pending:
        lines = [
            f"  {f}: {len(rows)} row(s): {', '.join(rows[:5])}" + (" ..." if len(rows) > 5 else "")
            for f, rows in pending.items()
        ]
        raise SystemExit(
            "backtest refused: manual reference rows still need review "
            "(set needs_review=false after checking):\n" + "\n".join(lines)
        )

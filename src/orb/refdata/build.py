"""Build the reference files in ``reference.root`` from public sources.

Order matters (``orb ref all``): symbols -> nifty200 -> bhavcopy -> corporate
actions (rights factors need raw closes) -> ban list. Raw downloads are cached
under ``<reference.root>/_cache/<source>/`` so every step is resumable.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import polars as pl

from orb.config import Config
from orb.data.reference import load_membership
from orb.data.store import ParquetStore
from orb.refdata import nifty200, nse
from orb.refdata.http import CachedFetcher, Fetcher, NSEHttp

log = logging.getLogger(__name__)

MANUAL_DIR = "manual"
MERGERS = "mergers.csv"  # old_symbol,new_symbol,effective_date (manual)
NIFTY_MANUAL = "nifty200_changes_manual.csv"  # effective_date,symbol,change,source
NIFTY_REVIEW = "nifty200_review.csv"
NIFTY_SIZES = "nifty200_sizes.csv"
NIFTY_PARSED = "nifty200_changes_parsed.csv"


class RefBuilder:
    def __init__(self, cfg: Config, fetcher: Fetcher | None = None, refresh: bool = False):
        self.cfg = cfg
        self.n = cfg.data.nse
        self.root = Path(cfg.reference.root)
        self._http = fetcher or NSEHttp(self.n)
        self.refresh = refresh

    def fetch(self, source: str, refresh: bool | None = None) -> CachedFetcher:
        return CachedFetcher(
            self._http, self.root / "_cache" / source, self.refresh if refresh is None else refresh
        )

    def _write(self, df: pl.DataFrame, name: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        df.write_csv(path)
        log.info("wrote %s (%d rows)", path, df.height)
        return path

    # ---------------------------------------------------------------- symbols
    def symbols(self) -> pl.DataFrame:
        body = self.fetch("symbols", refresh=True).get(nse.symbol_change_url(self.n))
        renames = nse.parse_symbol_changes(body.decode("utf-8", "replace") if body else "")
        mpath = self.root / MANUAL_DIR / MERGERS
        parts = [renames]
        if mpath.exists():
            m = pl.read_csv(mpath, infer_schema_length=0).select(
                pl.col("old_symbol").str.strip_chars(),
                pl.col("new_symbol").str.strip_chars(),
                pl.col("effective_date").str.strip_chars().str.to_date(),
                change_type=pl.lit("merger"),
            )
            parts.append(m)
        sm = pl.concat(parts).unique().sort("effective_date", "old_symbol")
        self._write(sm, self.cfg.reference.symbol_map)
        return sm

    # -------------------------------------------------------------- nifty 200
    def nifty200(self, since: date, today: date) -> nifty200.Reconstruction:
        base = self.n.niftyindices_base
        cur = self.fetch("nifty200", refresh=True).get(
            f"{base}/IndexConstituent/ind_nifty200list.csv"
        )
        if not cur:
            raise RuntimeError("could not fetch current Nifty 200 constituents")
        current = pl.read_csv(cur, infer_schema_length=0)["Symbol"].str.strip_chars().to_list()
        html = self.fetch("nifty200", refresh=True).get(f"{base}/press-release")
        press = nifty200.equity_releases(
            nifty200.parse_press_list(html.decode("utf-8", "ignore"), base), since
        )
        pdfs = self.fetch("nifty200_pdf", refresh=False)
        changes, review = [], []
        for r in press.to_dicts():
            blob = pdfs.get(r["url"])
            if not blob or blob[:4] != b"%PDF":
                review.append({**r, "reason": "could not download PDF"})
                continue
            try:
                parsed = nifty200.parse_release(nifty200.pdf_text(blob))
            except Exception as exc:  # noqa: BLE001 - malformed PDFs go to review
                review.append({**r, "reason": f"PDF text extraction failed: {exc}"})
                continue
            kind = nifty200.classify_release(r["title"], parsed)
            if kind == "review":
                review.append({**r, "reason": "mentions Nifty 200 but no change list parsed"})
            elif kind == "changes":
                for kind, syms in (("add", parsed.adds), ("remove", parsed.removes)):
                    changes += [
                        {
                            "effective_date": parsed.effective_date,
                            "symbol": s,
                            "change": kind,
                            "source": r["url"],
                        }
                        for s in syms
                    ]
        auto = pl.DataFrame(changes, schema=nifty200.CHANGE_SCHEMA).unique()
        self._write(auto.sort("effective_date", "symbol"), NIFTY_PARSED)
        mpath = self.root / MANUAL_DIR / NIFTY_MANUAL
        manual = (
            pl.read_csv(mpath, infer_schema_length=0).with_columns(
                pl.col("effective_date").str.to_date()
            )
            if mpath.exists()
            else pl.DataFrame(schema=nifty200.CHANGE_SCHEMA)
        )
        merged = nifty200.merge_manual(auto, manual)
        sm_path = self.cfg.reference.path("symbol_map")
        renames = (
            pl.read_csv(sm_path, try_parse_dates=True).filter(pl.col("change_type") == "rename")
            if sm_path.exists()
            else pl.DataFrame(
                schema={
                    "old_symbol": pl.String,
                    "new_symbol": pl.String,
                    "effective_date": pl.Date,
                    "change_type": pl.String,
                }
            )
        )
        rec = nifty200.reconstruct(current, today, merged, renames, since)
        self._write(rec.membership, self.cfg.reference.membership)
        self._write(
            pl.DataFrame(
                review,
                schema={
                    "published": pl.Date,
                    "url": pl.String,
                    "title": pl.String,
                    "reason": pl.String,
                },
            ),
            NIFTY_REVIEW,
        )
        self._write(rec.sizes, NIFTY_SIZES)
        for p in rec.inconsistencies:
            log.warning("nifty200: %s", p)
        bad = rec.size_violations()
        if bad.height:
            log.warning(
                "nifty200: %d dates where the index does not hold 200 names (see %s)",
                bad.height,
                NIFTY_SIZES,
            )
        return rec

    # --------------------------------------------------------------- bhavcopy
    def bhavcopy(self, raw: ParquetStore, start: date, end: date) -> list[date]:
        """Raw daily bars for universe symbols, written to the raw store year by year."""
        mpath = self.cfg.reference.path("membership")
        universe = set(load_membership(mpath)["symbol"]) if mpath.exists() else None
        missing: list[date] = []
        for year in range(start.year, end.year + 1):
            a, b = max(start, date(year, 1, 1)), min(end, date(year, 12, 31))
            df, miss = nse.download_bhavcopy(self.fetch("bhavcopy"), self.n, a, b)
            missing += miss
            if universe is not None:
                df = df.filter(pl.col("symbol").is_in(list(universe)))
            raw.write_daily(df)
            log.info("bhavcopy %d: %d rows, %d weekdays without a file", year, df.height, len(miss))
        self._write(
            pl.DataFrame({"date": missing}, schema={"date": pl.Date}),
            "bhavcopy_missing_weekdays.csv",
        )
        return missing

    # ------------------------------------------------------- corporate actions
    def corporate_actions(self, raw: ParquetStore, start: date, end: date) -> pl.DataFrame:
        ca = nse.download_corporate_actions(
            self.fetch("corporate_actions"),
            self.n,
            start,
            end,
            fresh=self.fetch("corporate_actions", refresh=True),
            today=date.today(),
        )
        rights = ca.filter(pl.col("action_type") == "rights")
        if rights.height:
            daily = pl.concat([raw.read_daily(s, start, end) for s in rights["symbol"].unique()])
            if daily.height:
                ca = nse.fill_rights_factors(ca, daily)
        self._write(ca, self.cfg.reference.corporate_actions)
        return ca

    # -------------------------------------------------------------------- ban
    def ban_list(self, start: date, end: date) -> pl.DataFrame:
        ban = nse.download_ban(self.fetch("ban"), self.n, start, end)
        self._write(ban, self.cfg.reference.ban_list)
        return ban

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
from orb.data.store import ParquetStore
from orb.refdata import nifty200, nse
from orb.refdata.http import CachedFetcher, Fetcher, NSEHttp

log = logging.getLogger(__name__)

MANUAL_DIR = "manual"
MERGERS = "mergers.csv"  # old_symbol,new_symbol,effective_date (manual)
NIFTY_MANUAL = "nifty200_changes_manual.csv"  # effective_date,symbol,change,source
NIFTY_REVIEW = "nifty200_review.csv"
NIFTY_SIZES = "nifty200_sizes.csv"
NIFTY_DAILY = "nifty200_daily_counts.csv"
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
            m = pl.read_csv(mpath, infer_schema_length=0)
            if "needs_review" in m.columns:  # only reviewed rows are applied
                m = m.filter(
                    pl.col("needs_review").str.to_lowercase().is_in(["false", "0", ""])
                    | pl.col("needs_review").is_null()
                )
            m = m.select(
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
                text = self._pdf_text(r["url"], blob)
            except Exception as exc:  # noqa: BLE001 - malformed PDFs go to review
                review.append({**r, "reason": f"PDF text extraction failed: {exc}"})
                continue
            if nifty200.is_image_only(text):
                ocr = nifty200.ocr_text(blob)
                name = r["url"].rsplit("/", 1)[-1]
                if ocr and nifty200.mentions_index(ocr):
                    ocr_dir = self.root / "_cache" / "nifty200_ocr"
                    ocr_dir.mkdir(parents=True, exist_ok=True)
                    (ocr_dir / f"{name}.txt").write_text(ocr)
                    review.append(
                        {
                            **r,
                            "reason": "image-only PDF: OCR text in "
                            f"_cache/nifty200_ocr/{name}.txt - transcribe into manual",
                        }
                    )
                elif ocr is None:
                    review.append({**r, "reason": "image-only PDF and no OCR available"})
                continue
            parsed = nifty200.parse_release(text)
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
        self._daily_counts(rec.membership)
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

    def _daily_counts(self, membership: pl.DataFrame) -> pl.DataFrame | None:
        """Members on EVERY trading day (bhavcopy dates), not only change dates."""
        raw = ParquetStore(Path(self.cfg.data.root) / "raw")
        days = raw.read_daily("RELIANCE", date(1990, 1, 1), date(2100, 1, 1))["date"]
        if days.len() == 0:
            log.warning("nifty200: no bhavcopy yet, daily member count skipped")
            return None
        start = membership["valid_from"].min()
        d = pl.DataFrame({"date": days.filter(days >= start)})
        counts = (
            d.join(membership, how="cross")
            .filter(
                (pl.col("date") >= pl.col("valid_from"))
                & (pl.col("valid_to").is_null() | (pl.col("date") <= pl.col("valid_to")))
            )
            .group_by("date")
            .agg(
                securities=pl.len(),
                companies=(~pl.col("symbol").is_in(list(nifty200.SECOND_CLASS))).sum(),
            )
        )
        counts = d.join(counts, on="date", how="left").fill_null(0).sort("date")
        self._write(counts, NIFTY_DAILY)
        bad = counts.filter(pl.col("companies") != 200)
        if bad.height:
            log.warning(
                "nifty200: %d trading days without exactly 200 companies (%s)",
                bad.height,
                NIFTY_DAILY,
            )
        return counts

    def _pdf_text(self, url: str, blob: bytes) -> str:
        """Page-marked text of a press-release PDF, cached next to the PDFs."""
        d = self.root / "_cache" / "nifty200_text"
        p = d / (url.rsplit("/", 1)[-1] + ".txt")
        if p.exists():
            return p.read_text()
        text = nifty200.pdf_text(blob)
        d.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return text

    def press_texts(self) -> dict[str, str]:
        """{pdf filename: text} for every cached release (OCR text where image-only)."""
        out = {}
        for d in ("nifty200_text", "nifty200_ocr"):
            for p in sorted((self.root / "_cache" / d).glob("*.txt")):
                out[p.name.removesuffix(".txt")] = p.read_text()
        return out

    # ---------------------------------------------------------------- mergers
    def merger_candidates(self, raw: ParquetStore) -> pl.DataFrame:
        from orb.refdata import mergers

        last = {}
        for s in raw.symbols("daily"):
            d = raw.read_daily(s, date(1990, 1, 1), date(2100, 1, 1))["date"]
            if d.len():
                last[s] = d.max()
        if not last:
            raise RuntimeError("no bhavcopy in the raw store: run `orb ref bhavcopy` first")
        sm = self.cfg.reference.path("symbol_map")
        renamed = (
            set(pl.read_csv(sm).filter(pl.col("change_type") == "rename")["old_symbol"])
            if sm.exists()
            else set()
        )
        cands = mergers.find_candidates(self.press_texts(), last, renamed, max(last.values()))
        mpath = self.cfg.reference.path("membership")
        if mpath.exists():
            from orb.data.reference import load_membership

            days = raw.read_daily("RELIANCE", date(1990, 1, 1), date(2100, 1, 1))["date"]
            cands = mergers.index_gaps(cands, load_membership(mpath), days.to_list())
        path = self.root / MANUAL_DIR / MERGERS
        if path.exists():  # keep the user's rows; add only new candidates
            have = pl.read_csv(path, infer_schema_length=0)
            known = set(zip(have["old_symbol"], have["new_symbol"], strict=True))
            new = cands.filter(
                ~pl.struct("old_symbol", "new_symbol").map_elements(
                    lambda r: (r["old_symbol"], r["new_symbol"]) in known, return_dtype=pl.Boolean
                )
            )
            merged = pl.concat([have, new.cast(pl.String)], how="diagonal")
        else:
            merged = cands
        path.parent.mkdir(parents=True, exist_ok=True)
        merged.write_csv(path)
        log.info("mergers: %d candidates (%s)", cands.height, path)
        return cands

    # --------------------------------------------------------------- bhavcopy
    def bhavcopy(self, raw: ParquetStore, start: date, end: date) -> list[date]:
        """Raw daily bars for ALL EQ/BE symbols (not just current members, so later
        membership corrections and merger detection need no re-download)."""
        missing: list[date] = []
        for year in range(start.year, end.year + 1):
            a, b = max(start, date(year, 1, 1)), min(end, date(year, 12, 31))
            df, miss = nse.download_bhavcopy(self.fetch("bhavcopy"), self.n, a, b)
            missing += miss
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

    # ------------------------------------------------------- special sessions
    def special_sessions(
        self, raw: ParquetStore, minute_store: ParquetStore | None
    ) -> pl.DataFrame:
        """Draft -> manual/special_sessions_draft.csv; reviewed exclude rows ->
        special_sessions.csv (header-only until something is confirmed)."""
        from orb.refdata import special_sessions as ss

        cpath = self.root / MANUAL_DIR / "special_session_candidates.csv"
        cands = {}
        if cpath.exists():
            c = pl.read_csv(cpath, try_parse_dates=True)
            cands = dict(zip(c["date"].to_list(), c["label"].to_list(), strict=True))
        bhav = ss.bhav_flags(ss.market_turnover(raw))
        minute = None
        if minute_store is not None:
            syms = minute_store.symbols("minute")
            minute = ss.minute_flags(minute_store, syms) if syms else None
        dpath = self.root / MANUAL_DIR / "special_sessions_draft.csv"
        new = ss.draft(bhav, minute, cands)
        if dpath.exists():  # keep the user's review decisions for dates already drafted
            old = pl.read_csv(dpath, try_parse_dates=True)
            keep = old.filter(pl.col("needs_review").cast(pl.String).str.to_lowercase() == "false")
            new = pl.concat(
                [
                    keep.select(new.columns).cast(new.schema),
                    new.join(keep.select("date"), on="date", how="anti"),
                ]
            ).sort("date")
        self._write(new, f"{MANUAL_DIR}/special_sessions_draft.csv")
        confirmed = new.filter(~pl.col("needs_review") & (pl.col("proposed_action") == "exclude"))
        self._write(confirmed.select("date", "session_type"), self.cfg.reference.special_sessions)
        return new

    # ------------------------------------------------------- expiries/results
    def expiries(self, start: date, end: date) -> pl.DataFrame:
        df = nse.download_expiries(self.fetch("fo_bhavcopy"), self.n, start, end)
        self._write(df, self.cfg.reference.expiries)
        return df

    def results_dates(self, start: date, end: date) -> pl.DataFrame:
        df = nse.download_results_dates(
            self.fetch("board_meetings"),
            self.n,
            start,
            end,
            fresh=self.fetch("board_meetings", refresh=True),
            today=date.today(),
        )
        self._write(df, self.cfg.reference.results_dates)
        return df

    # -------------------------------------------------------------------- ban
    def ban_list(self, start: date, end: date) -> pl.DataFrame:
        ban = nse.download_ban(self.fetch("ban"), self.n, start, end)
        self._write(ban, self.cfg.reference.ban_list)
        return ban

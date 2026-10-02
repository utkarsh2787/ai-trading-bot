"""Reference-data parsers, tested on real NSE / niftyindices.com files (fixtures)."""

import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from orb.refdata import bhavcopy, nifty200, nse
from orb.refdata.http import CachedFetcher

FIX = Path(__file__).parent / "fixtures" / "nse"


# ------------------------------------------------------------------- ban list


def test_parse_ban_list_real_files():
    day, syms = nse.parse_ban((FIX / "fo_secban_01082024.csv").read_text())
    assert day == date(2024, 8, 1) and syms == ["GRANULES", "INDIACEM"]
    day, syms = nse.parse_ban((FIX / "fo_secban_02012023.csv").read_text())
    assert day == date(2023, 1, 2) and syms == []


class DictFetcher:
    def __init__(self, files: dict[str, bytes | None]):
        self.files = files
        self.calls: list[str] = []

    def get(self, url):
        self.calls.append(url)
        return self.files.get(url)


def test_download_ban_skips_holidays_and_html_404(cfg):
    n = cfg.data.nse
    f = DictFetcher(
        {
            nse.ban_url(n, date(2024, 8, 1)): (FIX / "fo_secban_01082024.csv").read_bytes(),
            nse.ban_url(n, date(2024, 8, 2)): b"<!DOCTYPE html><html>404</html>",
        }
    )
    df = nse.download_ban(f, n, date(2024, 8, 1), date(2024, 8, 5))
    assert df.to_dicts() == [
        {"date": date(2024, 8, 1), "symbol": "GRANULES"},
        {"date": date(2024, 8, 1), "symbol": "INDIACEM"},
    ]
    assert len(f.calls) == 3  # Thu, Fri, Mon (weekend skipped)


# ------------------------------------------------------------ corporate actions


@pytest.mark.parametrize(
    "subject, kind, factor",
    [
        (
            "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share",
            "split",
            0.2,
        ),
        (
            "Face Value Split (Sub-Division) - From Re 1/- Per Share To Re 0.50/- Per Share",
            "split",
            0.5,
        ),
        ("Bonus 4:1", "bonus", 0.2),
        ("Bonus 1:1", "bonus", 0.5),
        ("Bonus 3:4", "bonus", 4 / 7),
        ("Rights 1:6 @ Premium Rs 100.98/-", "rights", None),
        ("Demerger", "demerger", None),
        ("Scheme of Amalgamation", "merger", None),
        ("Interim Dividend - Rs 5 Per Share", "dividend", None),
        ("Annual General Meeting", "other", None),
    ],
)
def test_classify_action(subject, kind, factor):
    k, f, _ = nse.classify_action(subject)
    assert k == kind
    assert f == (pytest.approx(factor) if factor is not None else None)


def test_parse_real_corporate_actions(cfg):
    recs = json.loads((FIX / "corporate_actions_oct2021.json").read_text())
    ca = nse.parse_corporate_actions(recs, cfg.data.nse.series)
    rows = {(r["symbol"], r["ex_date"], r["action_type"]): r["price_factor"] for r in ca.to_dicts()}
    assert rows[("IRCTC", date(2021, 10, 28), "split")] == pytest.approx(0.2)
    assert rows[("SRF", date(2021, 10, 13), "bonus")] == pytest.approx(0.2)
    assert rows[("GPIL", date(2021, 10, 26), "split")] == pytest.approx(0.5)
    assert rows[("GPIL", date(2021, 10, 26), "bonus")] == pytest.approx(0.5)
    assert not ca.filter(pl.col("symbol") == "645GS2029").height  # GS series dropped


def test_rights_factor_from_terp():
    ca = pl.DataFrame(
        {
            "symbol": ["X"],
            "ex_date": [date(2024, 1, 10)],
            "action_type": ["rights"],
            "price_factor": [None],
            "subject": [""],
            "face_value": [10.0],
            "rights_new": [1],
            "rights_held": [4],
            "rights_premium": [40.0],
        },
        schema_overrides={"price_factor": pl.Float64},
    )
    daily = pl.DataFrame({"symbol": ["X"], "date": [date(2024, 1, 9)], "close": [100.0]})
    out = nse.fill_rights_factors(ca, daily)
    # TERP = (4*100 + 1*50) / 5 = 90 -> factor 0.9
    assert out["price_factor"][0] == pytest.approx(0.9)


# --------------------------------------------------------------- symbol changes


def test_parse_symbol_changes_real_file():
    sm = nse.parse_symbol_changes((FIX / "symbolchange.csv").read_text())
    pairs = {(r["old_symbol"], r["new_symbol"]): r["effective_date"] for r in sm.to_dicts()}
    assert pairs[("LTI", "LTIM")] == date(2022, 12, 5)
    assert pairs[("LTIM", "LTM")] == date(2026, 2, 27)
    assert pairs[("ZOMATO", "ETERNAL")] == date(2025, 4, 9)
    assert pairs[("RDAXEDG", "NDAXEDG")] == date(2019, 10, 30)  # name with commas/spaces
    assert set(sm["change_type"]) == {"rename"}


# -------------------------------------------------------------------- bhavcopy


def test_bhavcopy_legacy_and_udiff_formats(cfg):
    old = bhavcopy.parse((FIX / "cm27OCT2021bhav.csv").read_bytes(), ["EQ", "BE"])
    irctc = old.filter(pl.col("symbol") == "IRCTC").row(0, named=True)
    assert irctc == {
        "symbol": "IRCTC",
        "date": date(2021, 10, 27),
        "open": 4250.5,
        "high": 4334.8,
        "low": 4033.75,
        "close": 4130.15,
        "volume": 5358793,
    }
    new = bhavcopy.parse(
        (FIX / "BhavCopy_NSE_CM_0_0_0_20250801_F_0000.csv").read_bytes(), ["EQ", "BE"]
    )
    assert new.to_dicts() == [
        {
            "symbol": "RELIANCE",
            "date": date(2025, 8, 1),
            "open": 1386.9,
            "high": 1405.9,
            "low": 1384.3,
            "close": 1393.7,
            "volume": 10321171,
        }
    ]  # GB (gold bond) series dropped


def test_bhavcopy_url_choice(cfg):
    n = cfg.data.nse
    assert bhavcopy.urls_for(n.archives_base, date(2021, 10, 27), n.bhavcopy_udiff_from)[0] == (
        "https://nsearchives.nseindia.com/content/historical/EQUITIES/2021/OCT/cm27OCT2021bhav.csv.zip"
    )
    assert bhavcopy.urls_for(n.archives_base, date(2025, 8, 1), n.bhavcopy_udiff_from)[0] == (
        "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20250801_F_0000.csv.zip"
    )


# ------------------------------------------------------------- nifty 200 history


def test_press_list_parse_and_filter(cfg):
    html = (FIX / "press_release_list.html").read_text()
    pr = nifty200.parse_press_list(html, cfg.data.nse.niftyindices_base)
    assert pr.height == 3
    assert pr["url"].to_list()[0].startswith("https://www.niftyindices.com/Press_Release/")
    eq = nifty200.equity_releases(pr, date(2017, 1, 1))
    assert "Changes in Nifty Fixed Income indices" not in " ".join(eq["title"])
    assert eq.height == 2


def test_parse_semi_annual_review_2025():
    r = nifty200.parse_release((FIX / "prs_21022025_excerpt.txt").read_text())
    assert r.method == "section" and r.effective_date == date(2025, 3, 28)
    assert r.removes == [
        "BALKRISIND",
        "DELHIVERY",
        "FACT",
        "IDBI",
        "IOB",
        "JSWINFRA",
        "MRPL",
        "NLCINDIA",
        "POONAWALLA",
        "SUNDARMFIN",
        "TATACHEM",
    ]
    assert r.adds == [
        "BAJAJHFL",
        "GLENMARK",
        "HYUNDAI",
        "MOTILALOFS",
        "NATIONALUM",
        "NTPCGREEN",
        "OLAELEC",
        "PREMIERENE",
        "SWIGGY",
        "VMM",
        "WAAREEENER",
    ]
    assert not r.needs_review


def test_parse_review_2018_with_page_break():
    r = nifty200.parse_release((FIX / "prs_21022018_excerpt.txt").read_text())
    assert r.effective_date == date(2018, 4, 2)
    assert len(r.removes) == 11 and len(r.adds) == 11
    assert "SBILIFE" in r.adds and "GODREJAGRO" in r.adds  # rows after the page break
    assert "RCOM" in r.removes


def test_parse_single_company_exclusion():
    r = nifty200.parse_release((FIX / "prs_05092023.txt").read_text())
    assert r.method == "single_exclusion"
    assert r.removes == ["JIOFIN"] and r.adds == []
    assert r.effective_date == date(2023, 9, 7)


def test_unparseable_mention_needs_review():
    r = nifty200.parse_release("Some table we cannot read mentioning Nifty 200 constituents.")
    assert r.mentions_index and r.needs_review
    assert not nifty200.parse_release("Nifty200 Momentum 30 rebalance").mentions_index


def test_reconstruct_membership_backwards_with_rename():
    changes = pl.DataFrame(
        {
            "effective_date": [date(2023, 3, 31), date(2023, 3, 31)],
            "symbol": ["C", "B"],
            "change": ["add", "remove"],
            "source": ["x", "x"],
        }
    )
    renames = pl.DataFrame(
        {
            "old_symbol": ["OLDA"],
            "new_symbol": ["A"],
            "effective_date": [date(2022, 6, 1)],
            "change_type": ["rename"],
        }
    )
    rec = nifty200.reconstruct(["A", "C"], date(2024, 1, 1), changes, renames, date(2022, 1, 1))
    m = {(r["symbol"], r["valid_from"], r["valid_to"]) for r in rec.membership.to_dicts()}
    assert m == {
        ("A", date(2022, 6, 1), None),
        ("OLDA", date(2022, 1, 1), date(2022, 5, 31)),
        ("C", date(2023, 3, 31), None),
        ("B", date(2022, 1, 1), date(2023, 3, 30)),
    }
    assert rec.inconsistencies == []
    assert rec.size_violations(2).height == 0


def test_reconstruct_flags_inconsistency_and_size():
    changes = pl.DataFrame(
        {"effective_date": [date(2023, 3, 31)], "symbol": ["Z"], "change": ["add"], "source": ["x"]}
    )
    empty_renames = pl.DataFrame(
        schema={
            "old_symbol": pl.String,
            "new_symbol": pl.String,
            "effective_date": pl.Date,
            "change_type": pl.String,
        }
    )
    rec = nifty200.reconstruct(
        ["A", "B"], date(2024, 1, 1), changes, empty_renames, date(2022, 1, 1)
    )
    assert rec.inconsistencies and "add Z" in rec.inconsistencies[0]


def test_manual_overrides():
    auto = pl.DataFrame(
        {
            "effective_date": [date(2023, 1, 1)] * 2,
            "symbol": ["A", "B"],
            "change": ["add", "add"],
            "source": ["pdf", "pdf"],
        }
    )
    manual = pl.DataFrame(
        {
            "effective_date": [date(2023, 1, 1), date(2023, 1, 1)],
            "symbol": ["A", "B"],
            "change": ["remove", "ignore"],
            "source": ["manual", "manual"],
        }
    )
    out = nifty200.merge_manual(auto, manual)
    assert out.to_dicts() == [
        {"effective_date": date(2023, 1, 1), "symbol": "A", "change": "remove", "source": "manual"}
    ]


# ---------------------------------------------------------------------- caching


def test_cached_fetcher_resumes_and_caches_404(tmp_path):
    inner = DictFetcher({"https://x/a.csv": b"data"})
    f = CachedFetcher(inner, tmp_path)
    assert f.get("https://x/a.csv") == b"data"
    assert f.get("https://x/missing.csv") is None
    again = CachedFetcher(DictFetcher({}), tmp_path)  # fresh process, offline
    assert again.get("https://x/a.csv") == b"data"
    assert again.get("https://x/missing.csv") is None
    assert again.inner.calls == []


def test_old_heading_with_index_suffix():
    text = (
        "These changes shall become effective from June 29, 2018.\n"
        "6) NIFTY 200 Index\n\nThe following companies are being excluded:\n"
        "Sr. No. Company Name Symbol\n1 Arvind Ltd. ARVIND\n2 Tata Communications Ltd. TATACOMM\n"
        "The following companies are being included:\nSr. No. Company Name Symbol\n"
        "1 Future Retail Ltd. FRETAIL\n2 Quess Corp Ltd. QUESS\n\n"
        "7) NIFTY LargeMidcap 250 Index\nThe following companies are being excluded:\n"
        "1 Something Ltd. OTHER\n"
    )
    r = nifty200.parse_release(text)
    assert r.removes == ["ARVIND", "TATACOMM"] and r.adds == ["FRETAIL", "QUESS"]
    assert r.effective_date == date(2018, 6, 29)


def test_spinoff_releases_are_non_events():
    jio = nifty200.parse_release((FIX / "prs_05092023.txt").read_text())
    assert jio.spinoff
    assert nifty200.classify_release("Exclusion of Jio Financial Services Limited", jio) == "ignore"
    adj = nifty200.ParsedRelease(effective_date=date(2025, 10, 14), mentions_index=True)
    assert nifty200.classify_release("Corporate Adjustment for Tata Motors Ltd.", adj) == "ignore"
    assert (
        nifty200.classify_release(
            "Corporate Adjustment for Vedanta Ltd. and Replacement in Nifty Indices", adj
        )
        == "review"
    )
    assert nifty200.classify_release("NSE Indices launches Nifty200 Value 30", adj) == "ignore"
    semi = nifty200.parse_release((FIX / "prs_21022025_excerpt.txt").read_text())
    assert nifty200.classify_release("Replacements in indices", semi) == "changes"


def test_wrapped_table_row_is_parsed():
    text = (
        "These changes shall become effective from June 26, 2020.\n12) NIFTY 200\n"
        "The following companies are being included:\nSr. No. Company Name Symbol\n"
        "5 Gujarat Gas Ltd. GUJGASLTD\n6 \nIndian Railway Catering And Tourism \n"
        "Corporation Ltd. IRCTC\n7 NIIT Technologies Ltd. NIITTECH\n13) NIFTY Auto\n"
    )
    assert nifty200.parse_release(text).adds == ["GUJGASLTD", "IRCTC", "NIITTECH"]


# ----------------------------------------------------------- expiries / results


def test_fo_expiries_both_formats_and_classification():
    old = nse.parse_fo_expiries((FIX / "fo27OCT2021bhav.csv").read_bytes())
    new = nse.parse_fo_expiries((FIX / "BhavCopy_NSE_FO_0_0_0_20250801_F_0000.csv").read_bytes())
    e = nse.classify_expiries(pl.concat([old, new]))
    got = {(r["date"], r["expiry_type"]): r["underlyings"] for r in e.to_dicts()}
    assert got[(date(2021, 10, 28), "index_monthly")] == "BANKNIFTY;NIFTY"
    assert got[(date(2021, 10, 28), "stock_monthly")] == "RELIANCE"
    assert got[(date(2021, 11, 3), "index_weekly")] == "BANKNIFTY;NIFTY"
    assert got[(date(2021, 11, 25), "index_monthly")] == "BANKNIFTY;NIFTY"
    # long-dated option with no futures yet is still a monthly expiry
    assert got[(date(2022, 6, 30), "index_monthly")] == "BANKNIFTY;NIFTY"
    # 2025: Nifty weeklies on Thursdays, monthly on the last Thursday
    assert got[(date(2025, 8, 21), "index_weekly")] == "NIFTY"
    assert got[(date(2025, 8, 28), "index_monthly")] == "BANKNIFTY;NIFTY"
    assert (date(2025, 8, 28), "index_weekly") not in got


def test_download_expiries_samples_weekly(cfg):
    n = cfg.data.nse
    blob = (FIX / "fo27OCT2021bhav.csv").read_bytes()
    import io as _io
    import zipfile

    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fo27OCT2021bhav.csv", blob)
    f = DictFetcher({nse.fo_urls(n, date(2021, 10, 27))[0]: buf.getvalue()})
    df = nse.download_expiries(f, n, date(2021, 10, 25), date(2021, 12, 31))
    assert date(2021, 11, 3) in df["date"].to_list()
    assert len(f.calls) <= 2 * 5 * 10  # <= 5 weekdays x 2 formats per sampled week


def test_results_meetings_real_payload():
    recs = json.loads((FIX / "board_meetings_oct2024.json").read_text())
    df = nse.parse_results_meetings(recs)
    rows = {(r["symbol"], r["date"]) for r in df.to_dicts()}
    assert ("INFY", date(2024, 10, 17)) in rows and ("HDFCBANK", date(2024, 10, 19)) in rows
    assert ("AGSTRA", date(2024, 10, 31)) in rows  # intimation whose text says results
    assert not any(s == "IIFL" for s, _ in rows)  # fund raising only


def test_results_day_is_meeting_date_or_next_trading_day():
    from orb.regimes import results_days

    cal = [date(2024, 10, d) for d in (17, 18, 21, 22)]  # 19-20 is a weekend
    res = pl.DataFrame(
        {"symbol": ["INFY", "HDFCBANK"], "date": [date(2024, 10, 17), date(2024, 10, 19)]}
    )
    tagged = {(r["symbol"], r["date"]) for r in results_days(res, cal).to_dicts()}
    assert tagged == {
        ("INFY", date(2024, 10, 17)),
        ("INFY", date(2024, 10, 18)),
        ("HDFCBANK", date(2024, 10, 21)),
    }  # Saturday meeting -> Monday

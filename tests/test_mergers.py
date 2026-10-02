from datetime import date

from orb.refdata.mergers import find_candidates

HDFC_RELEASE = (
    "=== page 1 ===\nPRESS RELEASE\nReplacement in indices\n"
    "replacement of stock in various indices as listed hereunder on account of scheme of "
    "amalgamation of\nHousing Development Finance Corporation Ltd. with HDFC Bank Ltd. These "
    "changes shall become\neffective from July 13, 2023 (close of July 12, 2023).\n"
    "1) Nifty 50\nThe following company is being excluded:\nSr. No. Company Name Symbol\n"
    "1 Housing Development Finance Corporation Ltd. HDFC\n"
)
TABLES = (
    "=== page 1 ===\n1 HDFC Bank Ltd. HDFCBANK\n2 Mindtree Ltd. MINDTREE\n"
    "3 Larsen & Toubro Infotech Ltd. LTI\n"
)


def test_amalgamation_mapped_to_symbols_with_source_page():
    texts = {"ind_prs04072023.pdf": HDFC_RELEASE, "other.pdf": TABLES}
    last = {"HDFC": date(2023, 7, 12), "HDFCBANK": date(2026, 9, 30)}
    c = find_candidates(texts, last, renamed=set(), last_bhav_date=date(2026, 9, 30))
    r = c.row(0, named=True)
    assert (r["old_symbol"], r["new_symbol"], r["effective_date"]) == (
        "HDFC",
        "HDFCBANK",
        date(2023, 7, 13),  # day after the last trade (2023-07-12)
    )
    assert r["index_release_date"] == date(2023, 7, 13)
    assert r["old_last_traded"] == date(2023, 7, 12) and r["needs_review"]
    assert r["source"] == "ind_prs04072023.pdf p.1"


def test_still_trading_or_renamed_targets_dropped():
    texts = {"a.pdf": HDFC_RELEASE, "b.pdf": TABLES}
    trading = {"HDFC": date(2026, 9, 30), "HDFCBANK": date(2026, 9, 30)}
    assert find_candidates(texts, trading, set(), date(2026, 9, 30)).height == 0
    assert (
        find_candidates(texts, {"HDFC": date(2023, 7, 12)}, {"HDFC"}, date(2026, 9, 30)).height == 0
    )

from datetime import date

import pytest

from orb.data.reference import (
    ReferenceError,
    load_membership,
    load_reference,
    load_table,
    members_on,
)


def _w(path, text):
    path.write_text(text)
    return path


def test_snapshot_membership_to_intervals(tmp_path):
    # sparse reconstitution snapshots: A always; B dropped at 2023-03-31; C added then
    p = _w(
        tmp_path / "m.csv",
        "date,symbol\n2022-09-30,A\n2022-09-30,B\n2023-03-31,A\n2023-03-31,C\n"
        "2023-09-29,A\n2023-09-29,C\n2023-09-29,B\n",
    )
    iv = load_membership(p)
    rows = {(r["symbol"], r["valid_from"], r["valid_to"]) for r in iv.to_dicts()}
    assert rows == {
        ("A", date(2022, 9, 30), None),
        ("B", date(2022, 9, 30), date(2023, 3, 30)),
        ("B", date(2023, 9, 29), None),
        ("C", date(2023, 3, 31), None),
    }
    assert members_on(iv, date(2023, 3, 30)) == ["A", "B"]
    assert members_on(iv, date(2023, 3, 31)) == ["A", "C"]
    assert members_on(iv, date(2022, 9, 29)) == []
    assert members_on(iv, date(2030, 1, 1)) == ["A", "B", "C"]


def test_daily_snapshots_merge_into_one_interval(tmp_path):
    lines = ["date,symbol"] + [f"2024-01-0{d},A" for d in range(1, 6)]
    iv = load_membership(_w(tmp_path / "m.csv", "\n".join(lines) + "\n"))
    assert iv.height == 1 and iv["valid_to"][0] is None


def test_change_log_membership_and_overlap_rejected(tmp_path):
    p = _w(
        tmp_path / "m.csv", "symbol,valid_from,valid_to\nA,2020-01-01,2021-01-01\nA,2022-01-01,\n"
    )
    iv = load_membership(p)
    assert members_on(iv, date(2021, 6, 1)) == []
    assert members_on(iv, date(2023, 1, 1)) == ["A"]
    bad = _w(
        tmp_path / "b.csv", "symbol,valid_from,valid_to\nA,2020-01-01,2021-01-01\nA,2020-06-01,\n"
    )
    with pytest.raises(ReferenceError, match="overlapping"):
        load_membership(bad)


def test_corporate_actions_validation(tmp_path):
    ok = _w(
        tmp_path / "ca.csv",
        "symbol,ex_date,action_type,price_factor\nA,2024-01-05,SPLIT,0.2\nA,2024-02-05,dividend,\n",
    )
    df = load_table("corporate_actions", ok, required=True)
    assert df["action_type"].to_list() == ["split", "dividend"]
    for body, msg in [
        ("A,2024-01-05,split,\n", "price_factor required"),
        ("A,2024-01-05,spinoff,0.5\n", "unknown action_type"),
        ("A,2024-01-05,bonus,-1\n", "> 0"),
    ]:
        bad = _w(tmp_path / "bad.csv", "symbol,ex_date,action_type,price_factor\n" + body)
        with pytest.raises(ReferenceError, match=msg):
            load_table("corporate_actions", bad, required=True)


def test_required_missing_vs_optional_missing(tmp_path):
    with pytest.raises(ReferenceError, match="required"):
        load_table("ban_list", tmp_path / "nope.csv", required=True)
    assert load_table("expiries", tmp_path / "nope.csv", required=False).height == 0


def test_bad_date_rejected(tmp_path):
    p = _w(tmp_path / "ban.csv", "date,symbol\n2024-13-01,A\n")
    with pytest.raises(ReferenceError, match=r"ban_list \(.*ban.csv\): cannot parse"):
        load_table("ban_list", p, required=True)


def test_exclusions(tmp_path, cfg):
    root = tmp_path
    _w(root / "nifty200_membership.csv", "date,symbol\n2024-01-01,A\n2024-01-01,B\n")
    _w(root / "fo_ban.csv", "date,symbol\n2024-01-03,A\n2024-01-03,A\n")
    _w(
        root / "corporate_actions.csv",
        "symbol,ex_date,action_type,price_factor\nB,2024-01-04,bonus,0.5\nB,2024-01-05,dividend,\n",
    )
    _w(root / "special_sessions.csv", "date,session_type\n2024-11-01,muhurat\n")
    ref = load_reference(cfg.reference.model_copy(update={"root": str(root)}))
    ex = ref.stock_day_exclusions(cfg.reference.excluding_action_types)
    assert ex.to_dicts() == [
        {"symbol": "A", "date": date(2024, 1, 3), "reason": "FNO_BAN"},
        {"symbol": "B", "date": date(2024, 1, 4), "reason": "CORPORATE_ACTION_BONUS"},
    ]
    assert ref.day_exclusions()["reason"].to_list() == ["SPECIAL_SESSION_MUHURAT"]
    assert ref.all_symbols() == ["A", "B"]

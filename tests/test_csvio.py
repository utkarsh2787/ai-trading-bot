import shutil

import polars as pl
import pytest
import yaml

from orb import csvio
from tests.conftest import ROOT


def test_write_quotes_commas_and_roundtrips(tmp_path):
    df = pl.DataFrame(
        {"date": ["2026-02-01"], "source": ["user list; session found, turnover 93%"]}
    )
    p = csvio.write_csv(df, tmp_path / "x.csv")
    assert '"user list; session found, turnover 93%"' in p.read_text()
    assert csvio.ragged_rows(p) == []
    assert csvio.read_csv(p).equals(df)
    assert not (tmp_path / "x.csv.tmp").exists()


def test_read_error_names_the_file(tmp_path):
    bad = tmp_path / "special_session_candidates.csv"
    bad.write_text("date,label,source\n2026-02-01,Budget,a, b\n")
    with pytest.raises(csvio.CsvReadError, match="special_session_candidates.csv"):
        csvio.read_csv(bad)
    with pytest.raises(csvio.CsvReadError, match="https://x/fo.csv"):
        csvio.read_csv(b"a,b\n1,2,3\n", name="https://x/fo.csv")


def test_ragged_rows_detected_but_quoted_commas_are_fine(tmp_path):
    (tmp_path / "manual").mkdir()
    (tmp_path / "_cache").mkdir()
    (tmp_path / "manual" / "ok.csv").write_text('date,note\n2024-01-01,"a, b"\n')
    (tmp_path / "manual" / "bad.csv").write_text(
        "date,label,source\n2024-01-01,x,y\n2026-02-01,Budget,a, b\n"
    )
    (tmp_path / "_cache" / "skip.csv").write_text("a,b\n1,2,3\n")  # cache is skipped
    issues = csvio.check_tree(tmp_path)
    assert issues.to_dicts() == [
        {"file": str(tmp_path / "manual" / "bad.csv"), "line": 3, "fields": 4, "expected": 3}
    ]


def test_reference_loader_error_names_path(tmp_path):
    from orb.data.reference import ReferenceError, load_table

    p = tmp_path / "fo_ban.csv"
    p.write_text("date,symbol\n2024-13-01,A\n")
    with pytest.raises(ReferenceError, match="fo_ban.csv"):
        load_table("ban_list", p, required=True)


def test_dq_refuses_ragged_reference_csv(tmp_path):
    from orb.cli import main

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    for n in ("default.yaml", "tick_sizes.yaml", "costs.yaml"):
        shutil.copy(ROOT / "config" / n, cfg_dir / n)
    raw = yaml.safe_load((cfg_dir / "default.yaml").read_text())
    raw["data"]["root"] = str(tmp_path / "data")
    raw["reference"]["root"] = str(tmp_path / "ref")
    (cfg_dir / "default.yaml").write_text(yaml.safe_dump(raw))
    (tmp_path / "ref" / "manual").mkdir(parents=True)
    (tmp_path / "ref" / "manual" / "special_session_candidates.csv").write_text(
        "date,label,source\n2026-02-01,Sunday session (Budget),list; found, turnover 93%\n"
    )
    with pytest.raises(SystemExit, match=r"special_session_candidates.csv:2: 4 fields"):
        main(["--config", str(cfg_dir / "default.yaml"), "dq"])
    assert (tmp_path / "data" / "_dq" / "ref_csv_issues.csv").exists()

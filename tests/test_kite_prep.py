"""orb login, download universe, --plan and coverage (no network)."""

import json
import os
import stat
from datetime import date, datetime

import polars as pl
import pytest

from orb.data import download_plan, kite_auth
from orb.data.download import Manifest
from orb.data.store import ParquetStore
from orb.synthetic import minute_session

IST = kite_auth.IST


class FakeKite:
    def __init__(self, api_key):
        self.api_key = api_key

    def login_url(self):
        return f"https://kite.zerodha.com/connect/login?v=3&api_key={self.api_key}"

    def generate_session(self, request_token, api_secret):
        assert request_token == "abc123XYZ789" and api_secret == "s3cret"
        return {"access_token": "tok_ABCDEF", "user_id": "AB1234"}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("KITE_API_KEY", "key1234")
    monkeypatch.setenv("KITE_API_SECRET", "s3cret")
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)


def test_dotenv_does_not_override(tmp_path, monkeypatch):
    monkeypatch.setenv("KITE_API_KEY", "from_env")
    monkeypatch.delenv("KITE_API_SECRET", raising=False)
    (tmp_path / ".env").write_text(
        '# comment\nKITE_API_KEY=from_file\nexport KITE_API_SECRET="x y"\n'
    )
    assert kite_auth.load_dotenv(tmp_path / ".env") == ["KITE_API_SECRET"]
    assert os.environ["KITE_API_KEY"] == "from_env" and os.environ["KITE_API_SECRET"] == "x y"


def test_extract_request_token():
    url = "https://127.0.0.1/?action=login&type=login&status=success&request_token=abc123XYZ789"
    assert kite_auth.extract_request_token(url) == "abc123XYZ789"
    assert kite_auth.extract_request_token(" abc123XYZ789 ") == "abc123XYZ789"
    with pytest.raises(kite_auth.KiteAuthError):
        kite_auth.extract_request_token("https://127.0.0.1/?status=cancelled")


def test_login_saves_private_session_and_token_expires(env, tmp_path):
    assert "api_key=key1234" in kite_auth.login_url(FakeKite)
    login = datetime(2026, 10, 5, 9, 0, tzinfo=IST)
    meta = kite_auth.create_session("abc123XYZ789", tmp_path, FakeKite, now=login)
    assert "access_token" not in meta and meta["user_id"] == "AB1234"
    p = kite_auth.session_path(tmp_path)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert json.loads(p.read_text())["access_token"] == "tok_ABCDEF"
    assert kite_auth.access_token(tmp_path, now=datetime(2026, 10, 5, 23, 0, tzinfo=IST)) == (
        "tok_ABCDEF"
    )
    with pytest.raises(kite_auth.KiteAuthError, match="expired"):
        kite_auth.access_token(tmp_path, now=datetime(2026, 10, 6, 6, 1, tzinfo=IST))


def test_missing_secret_and_session(tmp_path, monkeypatch):
    monkeypatch.setenv("KITE_API_KEY", "k")
    monkeypatch.delenv("KITE_API_SECRET", raising=False)
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)
    with pytest.raises(kite_auth.KiteAuthError, match="KITE_API_SECRET"):
        kite_auth.create_session("abc123XYZ789", tmp_path, FakeKite)
    with pytest.raises(kite_auth.KiteAuthError, match="orb login"):
        kite_auth.access_token(tmp_path)


def _ref(tmp_path, cfg):
    root = tmp_path / "ref"
    (root / "manual").mkdir(parents=True)
    (root / "_cache" / "nifty200").mkdir(parents=True)
    (root / "nifty200_membership.csv").write_text(
        "symbol,valid_from,valid_to\nAAA,2018-01-01,\nBBB,2018-01-01,2020-01-01\n"
    )
    (root / "nifty200_changes_parsed.csv").write_text(
        "effective_date,symbol,change,source\n2020-01-02,CCC,add,x\n"
    )
    (root / "manual" / "nifty200_changes_manual.csv").write_text(
        "effective_date,symbol,change,source,needs_review,note\n2021-09-30,DDD,add,ocr,true,\n"
    )
    (root / "_cache" / "nifty200" / "abc_ind_nifty200list.csv").write_text(
        "Company Name,Industry,Symbol,Series,ISIN Code\nE Ltd,X,EEE,EQ,INE\n"
    )
    return cfg.model_copy(
        update={"reference": cfg.reference.model_copy(update={"root": str(root)})}
    )


def test_download_list_is_union_including_pending_review(tmp_path, cfg):
    c = _ref(tmp_path, cfg)
    syms, counts = download_plan.download_symbols(c)
    assert syms == ["AAA", "BBB", "CCC", "DDD", "EEE", "NIFTY 200", "NIFTY 50", "INDIA VIX"]
    assert counts["manual_changes"] == 1 and counts["stocks"] == 5  # DDD is needs_review


def test_plan_counts_and_resume(tmp_path, cfg):
    syms = ["AAA", "BBB"]
    end = date(2026, 9, 30)
    p = download_plan.plan(cfg, syms, end, None, "kite")
    n_min = len(list(download_plan.date_chunks(cfg.data.minute_history_start, end, 60)))
    assert p.minute_requests == 2 * n_min and p.daily_requests == 2 * 2
    assert p.requests == 2 * n_min + 4 + 2
    m = Manifest(tmp_path / "m.jsonl")
    a, b = next(iter(download_plan.date_chunks(cfg.data.minute_history_start, end, 60)))
    m.mark("kite", "minute", "AAA", a, b, 10)
    assert download_plan.plan(cfg, syms, end, m, "kite").already_done == 1
    assert "req/s limit" in p.text(3)


def test_coverage_95_percent_date(tmp_path):
    store = ParquetStore(tmp_path)
    syms = [f"S{i:02d}" for i in range(20)]
    for i, s in enumerate(syms):  # S00 from 2018-01-01, ..., S18 from 2018-01-19; S19 none
        if i < 19:
            store.write_minute(minute_session(s, date(2018, 1, 1 + i)))
    cov, line = download_plan.coverage(store, syms)
    assert cov.filter(pl.col("symbol") == "S19")["first_1min"][0] is None
    assert line.startswith("coverage: 95% of 20 symbols have 1-min data from 2018-01-19")
    _, line2 = download_plan.coverage(store, syms + ["X1", "X2"])
    assert "never reached" in line2


def test_cli_plan_makes_no_snapshot_and_no_kite_call(tmp_path, cfg, capsys, monkeypatch):
    import shutil

    import yaml

    from orb.cli import main
    from tests.conftest import ROOT

    c = _ref(tmp_path, cfg)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    for n in ("default.yaml", "tick_sizes.yaml", "costs.yaml"):
        shutil.copy(ROOT / "config" / n, cfg_dir / n)
    raw = yaml.safe_load((cfg_dir / "default.yaml").read_text())
    raw["data"]["root"] = str(tmp_path / "data")
    raw["reference"]["root"] = c.reference.root
    (cfg_dir / "default.yaml").write_text(yaml.safe_dump(raw))
    monkeypatch.setattr("orb.cli.make_provider", lambda cfg: pytest.fail("Kite called"))
    main(["--config", str(cfg_dir / "default.yaml"), "download", "--plan"])
    out = capsys.readouterr().out
    assert "symbols:          8" in out and "est. runtime" in out
    assert not (tmp_path / "data" / "vendor").exists()

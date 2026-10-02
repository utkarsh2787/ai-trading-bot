import os
import subprocess
from datetime import date

from orb.data.store import ParquetStore
from orb.repro import data_version, git_state
from orb.synthetic import daily_bars


def test_data_version_tracks_content_not_cache(tmp_path):
    store = ParquetStore(tmp_path / "data")
    store.write_daily(daily_bars("A", date(2024, 1, 1), 10))
    cache = tmp_path / "cache.json"
    v1 = data_version(tmp_path / "data", cache)
    assert data_version(tmp_path / "data", cache) == v1  # cached path
    cache.unlink()
    assert data_version(tmp_path / "data", cache) == v1  # cold path
    store.write_daily(daily_bars("A", date(2024, 1, 1), 11))
    assert data_version(tmp_path / "data", cache) != v1


def test_git_state(tmp_path):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }

    def run(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, env=env, capture_output=True)

    run("init", "-q")
    (tmp_path / "f.txt").write_text("a")
    run("add", "f.txt")
    run("commit", "-qm", "x")
    s = git_state(tmp_path)
    assert len(s["commit"]) == 40 and s["dirty"] is False
    (tmp_path / "f.txt").write_text("b")
    assert git_state(tmp_path)["dirty"] is True

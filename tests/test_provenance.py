"""Code provenance: clean tree, first-run pin, changelog gate, diff since the pin."""

import os
import subprocess
from pathlib import Path

import pytest

from orb import provenance as pv

ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, env=ENV, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path, files: dict[str, str], msg: str) -> str:
    for f, body in files.items():
        p = repo / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", msg)
    return git(repo, "rev-parse", "HEAD")


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    commit(path, {"README.md": "x\n", "src/orb/signals.py": "v1\n"}, "init")
    return path


def test_dirty_tree_refused_for_in_sample_and_oos(tmp_path):
    repo = make_repo(tmp_path / "r")
    (repo / "src/orb/signals.py").write_text("edited\n")
    for oos in (False, True):
        with pytest.raises(SystemExit, match="uncommitted changes"):
            pv.preflight(repo, tmp_path / "data", oos=oos)
    git(repo, "checkout", "--", ".")
    (repo / "scratch.py").write_text("untracked\n")
    with pytest.raises(SystemExit, match="scratch.py"):
        pv.preflight(repo, tmp_path / "data", oos=False)


def test_first_in_sample_run_pins_and_oos_needs_a_pin(tmp_path):
    repo = make_repo(tmp_path / "r")
    data = tmp_path / "data"
    with pytest.raises(SystemExit, match="no pinned in-sample run"):
        pv.preflight(repo, data, oos=True)
    p = pv.preflight(repo, data, oos=False)
    assert p["will_pin"] and p["pinned_commit"] is None
    pin = pv.save_pin(data, p["commit"], "run1")
    assert pv.load_pin(data)["commit"] == p["commit"] == pin["commit"]
    with pytest.raises(SystemExit, match="already exists"):
        pv.save_pin(data, p["commit"], "run2")
    p2 = pv.preflight(repo, data, oos=False)
    assert not p2["will_pin"] and p2["pinned_commit"] == pin["commit"]


def test_unlogged_relevant_change_refused_logged_one_allowed(tmp_path):
    repo = make_repo(tmp_path / "r")
    data = tmp_path / "data"
    pinned = pv.preflight(repo, data, oos=False)["commit"]
    pv.save_pin(data, pinned, "run1")
    commit(repo, {"README.md": "docs only\n"}, "docs")  # irrelevant: allowed
    assert pv.preflight(repo, data, oos=False)["pinned_commit"] == pinned
    bug = commit(repo, {"src/orb/sim/trade.py": "fix\n"}, "fix stop fill")
    with pytest.raises(SystemExit, match=r"(?s)not logged.*fix stop fill.*src/orb/sim/trade.py"):
        pv.preflight(repo, data, oos=False)
    commit(
        repo,
        {"docs/CHANGELOG_RESEARCH.md": f"## 2026-10-10\n- Commit: `{bug[:12]}`\n"},
        "log the fix",
    )
    p = pv.preflight(repo, data, oos=True)  # OOS records the diff since the pin
    d = p["diff_since_pin"]
    assert d["from"] == pinned and len(d["commits"]) == 3
    assert d["relevant_files"] == ["src/orb/sim/trade.py"]
    assert "trade.py" in d["stat"]

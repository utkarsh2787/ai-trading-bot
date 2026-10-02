"""Code provenance for backtest runs (docs/PREREGISTRATION.md, amendment of 2026-10-02).

* every ``orb backtest`` refuses to run on a dirty working tree;
* the FIRST successful in-sample run pins its commit in
  ``data/_runs/insample_pin.json`` and in its ``meta.json``;
* after that, any commit touching a result-relevant module (``RELEVANT_PATHS``)
  must be logged in ``docs/CHANGELOG_RESEARCH.md`` (its hash, 7+ characters,
  must appear there), otherwise the run is refused;
* every run after the pin, the OOS run in particular, records the diff summary
  between the pinned commit and its own commit; ``report.md`` prints it.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

# signal, scoring, sizing, fills, costs, engine (and the inputs they are built from)
RELEVANT_PATHS = [
    "src/orb/signals.py",
    "src/orb/features.py",
    "src/orb/context.py",
    "src/orb/scan.py",
    "src/orb/scoring/",
    "src/orb/sim/",
    "src/orb/portfolio.py",
    "src/orb/engine.py",
    "src/orb/invariants.py",
    "src/orb/data/adjust.py",
    "config/",
]
CHANGELOG = "docs/CHANGELOG_RESEARCH.md"


class ProvenanceError(SystemExit):
    pass


def _git(repo: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise ProvenanceError(f"backtest refused: git failed in {repo}: {e}") from None


def head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def dirty_files(repo: Path) -> list[str]:
    out = _git(repo, "status", "--porcelain", "--untracked-files=normal")
    return [ln[3:] for ln in out.splitlines() if ln.strip()]


def pin_path(data_root: str | Path) -> Path:
    return Path(data_root) / "_runs" / "insample_pin.json"


def load_pin(data_root: str | Path) -> dict | None:
    p = pin_path(data_root)
    return json.loads(p.read_text()) if p.exists() else None


def save_pin(data_root: str | Path, commit: str, run_id: str) -> dict:
    p = pin_path(data_root)
    if p.exists():
        raise ProvenanceError(f"in-sample pin already exists: {p}")
    pin = {
        "commit": commit,
        "run_id": run_id,
        "pinned_at": datetime.now().isoformat(timespec="seconds"),
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(pin, indent=1))
    return pin


def relevant_commits(repo: Path, since: str) -> list[dict]:
    """Commits after ``since`` (exclusive) touching RELEVANT_PATHS."""
    out = _git(repo, "log", "--format=%H%x09%s", f"{since}..HEAD", "--", *RELEVANT_PATHS)
    rows = []
    for ln in out.splitlines():
        h, _, subject = ln.partition("\t")
        files = _git(repo, "show", "--name-only", "--format=", h, "--", *RELEVANT_PATHS)
        rows.append({"commit": h, "subject": subject, "files": files.splitlines()})
    return rows


def unlogged(commits: list[dict], changelog_text: str) -> list[dict]:
    return [c for c in commits if c["commit"][:7] not in changelog_text]


def diff_summary(repo: Path, since: str, until: str = "HEAD") -> dict:
    return {
        "from": since,
        "to": _git(repo, "rev-parse", until),
        "commits": _git(repo, "log", "--oneline", f"{since}..{until}").splitlines(),
        "stat": _git(repo, "diff", "--stat", since, until),
        "relevant_files": _git(
            repo, "diff", "--name-only", since, until, "--", *RELEVANT_PATHS
        ).splitlines(),
    }


def preflight(repo: str | Path, data_root: str | Path, oos: bool) -> dict:
    """Checks before a backtest; returns provenance fields for meta.json."""
    repo = Path(repo)
    dirty = dirty_files(repo)
    if dirty:
        raise ProvenanceError(
            "backtest refused: uncommitted changes (commit or stash them first):\n  "
            + "\n  ".join(dirty[:20])
            + ("\n  ..." if len(dirty) > 20 else "")
        )
    commit = head(repo)
    pin = load_pin(data_root)
    if pin is None:
        if oos:
            raise ProvenanceError("OOS refused: no pinned in-sample run yet")
        return {"commit": commit, "pinned_commit": None, "will_pin": True}
    changelog = repo / CHANGELOG
    text = changelog.read_text() if changelog.exists() else ""
    missing = unlogged(relevant_commits(repo, pin["commit"]), text)
    if missing:
        raise ProvenanceError(
            f"backtest refused: commits since the pinned in-sample commit {pin['commit'][:12]} "
            f"touch result-relevant code but are not logged in {CHANGELOG}:\n  "
            + "\n  ".join(
                f"{c['commit'][:12]} {c['subject']} ({', '.join(c['files'])})" for c in missing
            )
        )
    return {
        "commit": commit,
        "pinned_commit": pin["commit"],
        "pin_run_id": pin["run_id"],
        "will_pin": False,
        "diff_since_pin": diff_summary(repo, pin["commit"]),
    }

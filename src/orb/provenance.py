"""Code provenance for backtest runs (docs/PREREGISTRATION.md, amendment of 2026-10-02).

* every ``orb backtest`` refuses to run on a dirty working tree;
* the FIRST successful in-sample run pins its commit in
  ``data/_runs/insample_pin.json`` and in its ``meta.json``;
* after that, any commit touching a result-relevant module (``RELEVANT_PATHS``)
  must be logged in ``docs/CHANGELOG_RESEARCH.md`` (its hash, 7+ characters,
  must appear there), otherwise the run is refused;
* every run after the pin, the OOS run in particular, records the diff summary
  between the pinned commit and its own commit; ``report.md`` prints it.

Per strategy (``STRATEGIES``): each has its own pin, result-relevant paths and
changelog section (``# V1`` / ``# V2`` in ``CHANGELOG_RESEARCH.md``; a hash only
counts as logged inside its strategy's section). Every function defaults to V1,
whose pin and records are unchanged.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Strategy:
    key: str
    section: str  # changelog section header, e.g. "V1" for "# V1"
    pin_file: str  # relative to the data root
    relevant_paths: tuple[str, ...]
    prereg_doc: str


STRATEGIES = {
    "v1": Strategy(
        "v1", "V1", "_runs/insample_pin.json", tuple(RELEVANT_PATHS), "docs/PREREGISTRATION.md"
    ),
    "v2": Strategy(
        "v2",
        "V2",
        "_runs/v2/insample_pin.json",
        (
            "src/orb/v2/",
            "src/orb/refdata/fno.py",
            "src/orb/context.py",
            "src/orb/features.py",
            "src/orb/scan.py",
            "src/orb/sim/costs.py",
            "src/orb/sim/ticks.py",
            "src/orb/data/adjust.py",
            "config/v2.yaml",
            "config/tick_sizes.yaml",
            "config/costs.yaml",
        ),
        "docs/PREREGISTRATION_V2.md",
    ),
}


def changelog_section(text: str, section: str) -> str:
    """The text under ``# <section>`` up to the next top-level ``# `` header. A
    changelog without strategy sections (the pre-V2 layout) counts whole for V1."""
    m = re.search(rf"^# {re.escape(section)}\s*$", text, flags=re.M)
    if m is None:
        return text if section == "V1" and not re.search(r"^# V\d", text, flags=re.M) else ""
    rest = text[m.end() :]
    nxt = re.search(r"^# ", rest, flags=re.M)
    return rest[: nxt.start()] if nxt else rest


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


def pin_path(data_root: str | Path, strategy: str = "v1") -> Path:
    return Path(data_root) / STRATEGIES[strategy].pin_file


def load_pin(data_root: str | Path, strategy: str = "v1") -> dict | None:
    p = pin_path(data_root, strategy)
    return json.loads(p.read_text()) if p.exists() else None


def save_pin(data_root: str | Path, commit: str, run_id: str, strategy: str = "v1") -> dict:
    p = pin_path(data_root, strategy)
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


def relevant_commits(repo: Path, since: str, paths: tuple[str, ...] | list[str] = ()) -> list[dict]:
    """Commits after ``since`` (exclusive) touching ``paths`` (default: V1's)."""
    paths = list(paths or RELEVANT_PATHS)
    out = _git(repo, "log", "--format=%H%x09%s", f"{since}..HEAD", "--", *paths)
    rows = []
    for ln in out.splitlines():
        h, _, subject = ln.partition("\t")
        files = _git(repo, "show", "--name-only", "--format=", h, "--", *paths)
        rows.append({"commit": h, "subject": subject, "files": files.splitlines()})
    return rows


def unlogged(commits: list[dict], changelog_text: str) -> list[dict]:
    return [c for c in commits if c["commit"][:7] not in changelog_text]


def diff_summary(
    repo: Path, since: str, until: str = "HEAD", paths: tuple[str, ...] | list[str] = ()
) -> dict:
    paths = list(paths or RELEVANT_PATHS)
    return {
        "from": since,
        "to": _git(repo, "rev-parse", until),
        "commits": _git(repo, "log", "--oneline", f"{since}..{until}").splitlines(),
        "stat": _git(repo, "diff", "--stat", since, until),
        "relevant_files": _git(
            repo, "diff", "--name-only", since, until, "--", *paths
        ).splitlines(),
    }


def preflight(repo: str | Path, data_root: str | Path, oos: bool, strategy: str = "v1") -> dict:
    """Checks before a backtest; returns provenance fields for meta.json."""
    st = STRATEGIES[strategy]
    repo = Path(repo)
    dirty = dirty_files(repo)
    if dirty:
        raise ProvenanceError(
            "backtest refused: uncommitted changes (commit or stash them first):\n  "
            + "\n  ".join(dirty[:20])
            + ("\n  ..." if len(dirty) > 20 else "")
        )
    commit = head(repo)
    pin = load_pin(data_root, strategy)
    if pin is None:
        if oos:
            raise ProvenanceError("OOS refused: no pinned in-sample run yet")
        return {"commit": commit, "pinned_commit": None, "will_pin": True}
    changelog = repo / CHANGELOG
    text = changelog.read_text() if changelog.exists() else ""
    text = changelog_section(text, st.section)
    missing = unlogged(relevant_commits(repo, pin["commit"], st.relevant_paths), text)
    if missing:
        raise ProvenanceError(
            f"backtest refused: commits since the pinned in-sample commit {pin['commit'][:12]} "
            f"touch result-relevant code but are not logged under '# {st.section}' in "
            f"{CHANGELOG}:\n  "
            + "\n  ".join(
                f"{c['commit'][:12]} {c['subject']} ({', '.join(c['files'])})" for c in missing
            )
        )
    return {
        "commit": commit,
        "pinned_commit": pin["commit"],
        "pin_run_id": pin["run_id"],
        "will_pin": False,
        "diff_since_pin": diff_summary(repo, pin["commit"], paths=st.relevant_paths),
    }

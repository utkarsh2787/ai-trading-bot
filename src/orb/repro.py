"""Reproducibility metadata: config hash, git commit, data version."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from orb.config import Config


def git_state(repo: str | Path = ".") -> dict[str, object]:
    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()

    try:
        commit = run("rev-parse", "HEAD")
        dirty = bool(run("status", "--porcelain", "--untracked-files=no"))
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {"commit": None, "dirty": None}
    return {"commit": commit, "dirty": dirty}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def data_version(
    root: str | Path, cache_file: str | Path | None = None, exclude_dirs: tuple[str, ...] = ()
) -> str:
    """sha256 over (relative path, content hash) of every Parquet/CSV under root.

    Per-file hashes are cached keyed by (size, mtime_ns) so repeat calls are cheap;
    the result depends only on content, never on the cache.
    """
    root = Path(root)
    cache_path = Path(cache_file) if cache_file else root / "_manifest" / "file_hashes.json"
    cache: dict[str, list] = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    entries = []
    files = sorted(
        p
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix in (".parquet", ".csv")
        and not set(p.relative_to(root).parts[:-1]) & set(exclude_dirs)
    )
    for p in files:
        rel = p.relative_to(root).as_posix()
        st = p.stat()
        key = [st.st_size, st.st_mtime_ns]
        hit = cache.get(rel)
        digest = hit[2] if hit and hit[:2] == key else _sha256(p)
        cache[rel] = [*key, digest]
        entries.append(f"{rel}:{digest}")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({k: cache[k] for k in sorted(cache)}, indent=0))
    return hashlib.sha256("\n".join(entries).encode()).hexdigest()


def data_versions(cfg: Config) -> dict[str, object]:
    """Vendor snapshot (frozen, immutable) + raw store + reference files."""
    from orb.data.pipeline import read_build_record

    root = Path(cfg.data.root)
    build = read_build_record(root / "raw") or {}
    raw_v = data_version(root / "raw", root / "_manifest" / "raw_hashes.json")
    ref_v = data_version(
        cfg.reference.root, root / "_manifest" / "ref_hashes.json", exclude_dirs=("_cache",)
    )
    parts = [build.get("snapshot_hash") or "no-snapshot", raw_v, ref_v]
    return {
        "vendor_snapshot_id": build.get("snapshot_id"),
        "vendor_snapshot_hash": build.get("snapshot_hash"),
        "raw_hash": raw_v,
        "reference_hash": ref_v,
        "data_version": hashlib.sha256("|".join(parts).encode()).hexdigest(),
    }


def run_metadata(cfg: Config, repo: str | Path = ".") -> dict[str, object]:
    return {"config_hash": cfg.hash(), "git": git_state(repo), **data_versions(cfg)}

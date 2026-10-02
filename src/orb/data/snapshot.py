"""Immutable vendor snapshots.

Kite can revise past candles, so every download goes into its own snapshot:

    data/vendor/<provider>/snapshots/<id>/   minute/, daily/, downloads.jsonl

A snapshot is written (and resumed) until ``freeze``: then every file's sha256
goes into ``SNAPSHOT.json``, files are made read-only, and further downloads
into it are refused. ``build-raw`` only reads frozen snapshots and records the
snapshot id + content hash, so the data version of every run pins the exact
vendor bytes it was built from.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime
from pathlib import Path

from orb.data.store import ParquetStore

SNAP_FILE = "SNAPSHOT.json"


class SnapshotError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class Snapshot:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.id = self.path.name

    @property
    def store(self) -> ParquetStore:
        return ParquetStore(self.path)

    @property
    def manifest_path(self) -> Path:
        return self.path / "downloads.jsonl"

    @property
    def frozen(self) -> bool:
        return (self.path / SNAP_FILE).exists()

    def meta(self) -> dict:
        if not self.frozen:
            raise SnapshotError(f"snapshot {self.id} is not frozen")
        return json.loads((self.path / SNAP_FILE).read_text())

    @property
    def content_hash(self) -> str:
        return self.meta()["content_hash"]

    def _files(self) -> list[Path]:
        return sorted(
            p
            for p in self.path.rglob("*")
            if p.is_file() and p.name != SNAP_FILE and not p.name.endswith(".tmp")
        )

    def _hashes(self) -> dict[str, str]:
        return {p.relative_to(self.path).as_posix(): _sha256(p) for p in self._files()}

    def require_writable(self) -> None:
        if self.frozen:
            raise SnapshotError(f"snapshot {self.id} is frozen; start a new one")

    def freeze(self, provider: str) -> dict:
        self.require_writable()
        files = self._hashes()
        if not files:
            raise SnapshotError(f"snapshot {self.id} is empty")
        content = hashlib.sha256(
            "\n".join(f"{k}:{v}" for k, v in sorted(files.items())).encode()
        ).hexdigest()
        meta = {
            "id": self.id,
            "provider": provider,
            "frozen_at": datetime.now().isoformat(timespec="seconds"),
            "n_files": len(files),
            "content_hash": content,
            "files": files,
        }
        (self.path / SNAP_FILE).write_text(json.dumps(meta, indent=1, sort_keys=True))
        ro = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
        for p in [*self._files(), self.path / SNAP_FILE]:
            os.chmod(p, ro)
        return meta

    def verify(self) -> list[str]:
        """Paths whose content no longer matches the frozen manifest."""
        expected = self.meta()["files"]
        actual = self._hashes()
        return sorted(k for k in set(expected) | set(actual) if expected.get(k) != actual.get(k))


def snapshots_dir(root: str | Path, provider: str) -> Path:
    return Path(root) / "vendor" / provider / "snapshots"


def list_snapshots(root: str | Path, provider: str) -> list[Snapshot]:
    d = snapshots_dir(root, provider)
    return (
        sorted((Snapshot(p) for p in d.iterdir() if p.is_dir()), key=lambda s: s.id)
        if d.exists()
        else []
    )


def latest(root: str | Path, provider: str, frozen: bool) -> Snapshot | None:
    snaps = [s for s in list_snapshots(root, provider) if s.frozen == frozen]
    return snaps[-1] if snaps else None


def for_download(root: str | Path, provider: str, snap_id: str | None = None) -> Snapshot:
    """The named snapshot, else the latest unfrozen one, else a new one."""
    if snap_id:
        s = Snapshot(snapshots_dir(root, provider) / snap_id)
    else:
        s = latest(root, provider, frozen=False) or Snapshot(
            snapshots_dir(root, provider) / datetime.now().strftime("%Y%m%dT%H%M%S")
        )
    s.require_writable()
    s.path.mkdir(parents=True, exist_ok=True)
    return s


def for_build(root: str | Path, provider: str, snap_id: str | None = None) -> Snapshot:
    s = (
        Snapshot(snapshots_dir(root, provider) / snap_id)
        if snap_id
        else latest(root, provider, frozen=True)
    )
    if s is None or not s.frozen:
        raise SnapshotError("build-raw needs a frozen snapshot: run `orb snapshot freeze`")
    bad = s.verify()
    if bad:
        raise SnapshotError(f"snapshot {s.id} was modified after freezing: {bad[:5]}")
    return s

import os
from datetime import date

import pytest

from orb.data import snapshot
from orb.synthetic import daily_bars


def test_snapshot_lifecycle(tmp_path):
    s = snapshot.for_download(tmp_path, "kite")
    s.store.write_daily(daily_bars("A", date(2024, 1, 1), 5))
    assert snapshot.for_download(tmp_path, "kite").id == s.id  # resumes the open one
    with pytest.raises(snapshot.SnapshotError, match="frozen snapshot"):
        snapshot.for_build(tmp_path, "kite")
    meta = s.freeze("kite")
    assert meta["n_files"] == 1 and len(meta["content_hash"]) == 64
    assert snapshot.for_build(tmp_path, "kite").id == s.id
    with pytest.raises(snapshot.SnapshotError, match="frozen"):
        snapshot.for_download(tmp_path, "kite", s.id)
    f = s.store.daily_path("A")
    assert not os.access(f, os.W_OK)  # read-only after freezing


def test_tampering_detected(tmp_path):
    s = snapshot.for_download(tmp_path, "kite", "20260101T000000")
    s.store.write_daily(daily_bars("A", date(2024, 1, 1), 5))
    s.freeze("kite")
    f = s.store.daily_path("A")
    os.chmod(f, 0o644)
    f.write_bytes(f.read_bytes() + b"x")  # a "revised" candle file
    with pytest.raises(snapshot.SnapshotError, match="modified"):
        snapshot.for_build(tmp_path, "kite", s.id)


def test_new_download_after_freeze_starts_new_snapshot(tmp_path):
    s = snapshot.for_download(tmp_path, "kite", "20260101T000000")
    s.store.write_daily(daily_bars("A", date(2024, 1, 1), 5))
    s.freeze("kite")
    s2 = snapshot.for_download(tmp_path, "kite")
    assert s2.id != s.id and not s2.frozen

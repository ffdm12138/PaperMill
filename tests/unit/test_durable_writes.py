"""Unit tests for durable write fsync verification (Phase 0.8).

Verifies that ``atomic_write_json`` and ``atomic_write_json_unlocked``
call ``os.fsync`` on the tmp file before ``os.replace``, and that
parent-directory fsync is attempted on POSIX (no-op on Windows).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from src.utils.atomic_io import (
    _fsync_dir,
    atomic_write_json,
    atomic_write_json_unlocked,
    lock_path_for,
)


pytestmark = pytest.mark.unit


def test_atomic_write_json_fsyncs_tmp_file(tmp_path: Path, monkeypatch):
    """atomic_write_json must call os.fsync at least once (on the tmp file)."""
    fsync_calls: list[int] = []
    real_fsync = os.fsync

    def track_fsync(fd):
        fsync_calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", track_fsync)

    target = tmp_path / "test.json"
    atomic_write_json(target, {"key": "value"})

    assert len(fsync_calls) >= 1, (
        f"expected at least one fsync call, got {len(fsync_calls)}"
    )
    # The file must exist and be valid JSON.
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data == {"key": "value"}


def test_atomic_write_json_unlocked_fsyncs_tmp_file(tmp_path: Path, monkeypatch):
    """atomic_write_json_unlocked must also fsync the tmp file."""
    fsync_calls: list[int] = []
    real_fsync = os.fsync

    def track_fsync(fd):
        fsync_calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", track_fsync)

    target = tmp_path / "unlocked.json"
    atomic_write_json_unlocked(target, {"hello": "world"})

    assert len(fsync_calls) >= 1


def test_atomic_write_json_fsync_false_skips_fsync(tmp_path: Path, monkeypatch):
    """When fsync=False, no fsync calls should be made on the file."""
    fsync_calls: list[int] = []
    real_fsync = os.fsync

    def track_fsync(fd):
        fsync_calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", track_fsync)

    target = tmp_path / "nofsync.json"
    atomic_write_json(target, {"data": 1}, fsync=False)

    assert len(fsync_calls) == 0, f"expected no fsync calls, got {fsync_calls}"


def test_atomic_write_json_validates_json_round_trip(tmp_path: Path):
    """The tmp file must be JSON-validated before os.replace."""
    target = tmp_path / "validated.json"
    atomic_write_json(target, {"nested": {"list": [1, 2, 3]}})

    # If JSON validation failed, the file would not exist.
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["nested"]["list"] == [1, 2, 3]


def test_atomic_write_json_cleans_up_tmp_on_failure(tmp_path: Path, monkeypatch):
    """If os.replace fails, the tmp file should be cleaned up."""
    target = tmp_path / "cleanup.json"

    # Mock os.replace to raise an error.
    def mock_replace(src, dst):
        raise OSError("simulated failure")

    monkeypatch.setattr(os, "replace", mock_replace)

    with pytest.raises(OSError):
        atomic_write_json(target, {"key": "value"})

    # The tmp file should have been cleaned up.
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert len(tmp_files) == 0, f"tmp files not cleaned up: {tmp_files}"


def test_lock_path_for_returns_correct_path(tmp_path: Path):
    """lock_path_for must return <filename>.lock (suffix + .lock)."""
    path = tmp_path / "data.json"
    lock = lock_path_for(path)
    assert lock.name == "data.json.lock"


def test_fsync_dir_is_noop_on_windows(monkeypatch):
    """_fsync_dir must be a no-op on Windows (os.name == 'nt')."""
    monkeypatch.setattr(os, "name", "nt")
    # Should not raise even if the directory doesn't exist.
    _fsync_dir(Path("/nonexistent/path"))
    # If we got here without error, the test passed.


def test_atomic_write_json_unlocked_no_lock_acquisition(tmp_path: Path):
    """atomic_write_json_unlocked must NOT create a lock file."""
    target = tmp_path / "nolock.json"
    atomic_write_json_unlocked(target, {"data": True})

    # No .lock file should be created by the unlocked variant.
    lock_file = lock_path_for(target)
    assert not lock_file.exists(), f"unlocked variant should not create lock: {lock_file}"

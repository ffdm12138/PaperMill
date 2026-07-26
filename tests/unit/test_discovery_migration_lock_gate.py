"""Migration maintenance lock unit tests and production writer gate tests.

The production discovery writers (``discover_papers.py`` and
``discover_papers_concurrent.py``) must refuse to start while a discovery
migration holds the maintenance lock, and must ignore stale locks left by
dead processes.  All lock paths are monkeypatched into ``tmp_path``; no
real ``data/`` directory is touched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from filelock import FileLock

import scripts.discover_papers as discover_single
import scripts.discover_papers_concurrent as discover_concurrent
import src.discovery.workspace as workspace_mod
from src.discovery.maintenance_gate import (
    LOCK_INFO_SUFFIX,
    MigrationMaintenanceLock,
    MigrationMaintenanceLockError,
    migration_maintenance_block_reason,
    read_maintenance_lock_info,
)
from src.utils.process import is_pid_alive as _is_pid_alive


@pytest.fixture
def lock_path(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "migrations" / ".migration.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(workspace_mod, "MIGRATION_LOCK_PATH", path)
    return path


def _sidecar(lock_path: Path) -> Path:
    return lock_path.with_name(lock_path.name + LOCK_INFO_SUFFIX)


def _write_sidecar(lock_path: Path, pid: int, *, purpose: str = "apply") -> Path:
    sidecar = _sidecar(lock_path)
    sidecar.write_text(
        json.dumps({
            "pid": pid,
            "purpose": purpose,
            "migration_id": "v4-test",
            "acquired_at": "2026-01-01T00:00:00+00:00",
            "command": "simulated",
        }),
        encoding="utf-8",
    )
    return sidecar


def _dead_pid() -> int:
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    proc.wait(timeout=30)
    assert proc.pid is not None
    assert not _is_pid_alive(proc.pid)
    return proc.pid


class _LiveProcess:
    """Context manager: a live child process that exits when stdin closes."""

    def __enter__(self) -> subprocess.Popen:
        self._proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        return self._proc

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        assert self._proc.stdin is not None
        self._proc.stdin.close()
        self._proc.wait(timeout=30)
        return False


class TestMaintenanceLock:
    def test_acquire_writes_owner_info_and_release_removes_it(self, lock_path):
        with MigrationMaintenanceLock(
            "apply", migration_id="v4-x", lock_path=lock_path
        ) as lock:
            info = read_maintenance_lock_info(lock_path)
            assert info is not None
            assert info["pid"] == os.getpid()
            assert info["purpose"] == "apply"
            assert info["migration_id"] == "v4-x"
            assert info["acquired_at"]
            assert lock.lock_path == lock_path
        assert read_maintenance_lock_info(lock_path) is None
        assert not _sidecar(lock_path).exists()

    def test_acquire_fails_closed_when_mutex_held_elsewhere(self, lock_path):
        held = FileLock(str(lock_path), timeout=0)
        errors: dict[str, Exception] = {}
        with held:
            def _try():
                try:
                    MigrationMaintenanceLock("cutover", lock_path=lock_path).acquire()
                except MigrationMaintenanceLockError as exc:
                    errors["exc"] = exc

            worker = threading.Thread(target=_try)
            worker.start()
            worker.join(timeout=60)
        assert not worker.is_alive()
        assert "exc" in errors
        assert "held by another" in str(errors["exc"])

    def test_stale_sidecar_dead_pid_is_taken_over(self, lock_path):
        _write_sidecar(lock_path, _dead_pid())
        with MigrationMaintenanceLock("resume", lock_path=lock_path):
            info = read_maintenance_lock_info(lock_path)
            assert info is not None
            assert info["pid"] == os.getpid()
            assert info["purpose"] == "resume"
        assert read_maintenance_lock_info(lock_path) is None

    def test_live_other_pid_sidecar_fails_closed(self, lock_path):
        with _LiveProcess() as proc:
            _write_sidecar(lock_path, proc.pid, purpose="apply")
            with pytest.raises(MigrationMaintenanceLockError, match="live pid"):
                MigrationMaintenanceLock("apply", lock_path=lock_path).acquire()
        # The failed acquisition left no sidecar of its own behind.
        info = read_maintenance_lock_info(lock_path)
        assert info is not None
        assert info["pid"] == proc.pid

    def test_block_reason_is_none_without_lock(self, lock_path):
        assert migration_maintenance_block_reason() is None

    def test_block_reason_ignores_stale_sidecar(self, lock_path):
        _write_sidecar(lock_path, _dead_pid())
        assert migration_maintenance_block_reason() is None

    def test_block_reason_reports_live_owner(self, lock_path):
        with _LiveProcess() as proc:
            _write_sidecar(lock_path, proc.pid)
            reason = migration_maintenance_block_reason()
            assert reason is not None
            assert "discovery migration in progress" in reason
            assert str(proc.pid) in reason

    def test_block_reason_probes_mutex_without_sidecar(self, lock_path):
        held = FileLock(str(lock_path), timeout=0)
        reasons: dict[str, str | None] = {}
        with held:
            worker = threading.Thread(
                target=lambda: reasons.setdefault(
                    "reason", migration_maintenance_block_reason()
                )
            )
            worker.start()
            worker.join(timeout=60)
        assert not worker.is_alive()
        assert reasons["reason"] is not None
        assert "discovery migration in progress" in reasons["reason"]


class TestWriterMigrationGate:
    """Both production discovery writers fail closed during migration."""

    _CONCURRENT_ARGS = ["--keyword-zh", "甲", "--keyword-zh", "乙", "--keyword-zh", "丙"]

    def test_concurrent_writer_blocked_while_lock_held(self, lock_path, capsys):
        held = FileLock(str(lock_path), timeout=0)
        results: dict[str, int] = {}
        with held:
            worker = threading.Thread(
                target=lambda: results.setdefault(
                    "rc", discover_concurrent.main_internal(self._CONCURRENT_ARGS)
                )
            )
            worker.start()
            worker.join(timeout=60)
        assert not worker.is_alive()
        assert results["rc"] == 1
        assert "migration in progress" in capsys.readouterr().err

    def test_concurrent_writer_blocked_by_live_owner_sidecar(
        self, lock_path, capsys
    ):
        with _LiveProcess() as proc:
            _write_sidecar(lock_path, proc.pid)
            rc = discover_concurrent.main_internal(self._CONCURRENT_ARGS)
        assert rc == 1
        err = capsys.readouterr().err
        assert "migration in progress" in err
        assert str(proc.pid) in err

    def test_concurrent_writer_ignores_stale_lock(
        self, lock_path, capsys, tmp_path, monkeypatch
    ):
        _write_sidecar(lock_path, _dead_pid())
        monkeypatch.setattr(
            workspace_mod, "ACTIVE_GENERATION_PATH", tmp_path / "missing.json"
        )
        rc = discover_concurrent.main_internal(self._CONCURRENT_ARGS)
        assert rc == 1
        err = capsys.readouterr().err
        assert "migration in progress" not in err
        assert "no active discovery workspace" in err

    def test_concurrent_writer_workspace_root_not_exempt(
        self, lock_path, capsys, tmp_path
    ):
        """--workspace-root never bypasses the maintenance gate."""
        workspace_root = tmp_path / "ws"
        (workspace_root / "keyword_notebooks").mkdir(parents=True)
        held = FileLock(str(lock_path), timeout=0)
        results: dict[str, int] = {}
        with held:
            worker = threading.Thread(
                target=lambda: results.setdefault(
                    "rc", discover_concurrent.main_internal(
                        self._CONCURRENT_ARGS
                        + ["--workspace-root", str(workspace_root)]
                    )
                )
            )
            worker.start()
            worker.join(timeout=60)
        assert not worker.is_alive()
        assert results["rc"] == 1
        assert "migration in progress" in capsys.readouterr().err

    def test_writer_allowed_when_self_owns_lock(
        self, lock_path, capsys, tmp_path, monkeypatch
    ):
        """The in-process migration smoke passes: this process owns the lock."""
        workspace_root = tmp_path / "ws"
        (workspace_root / "keyword_notebooks").mkdir(parents=True)
        monkeypatch.setattr(
            workspace_mod, "ACTIVE_GENERATION_PATH", tmp_path / "missing.json"
        )
        with MigrationMaintenanceLock("apply", lock_path=lock_path):
            rc = discover_concurrent.main_internal(
                self._CONCURRENT_ARGS + ["--workspace-root", str(workspace_root)]
            )
        # The gate passes; the run fails later on the empty notebook dir.
        assert rc == 1
        assert "migration in progress" not in capsys.readouterr().err

    def test_single_writer_blocked_while_lock_held(self, lock_path, capsys):
        held = FileLock(str(lock_path), timeout=0)
        results: dict[str, int] = {}
        with held:
            worker = threading.Thread(
                target=lambda: results.setdefault(
                    "rc", discover_single.main(["--keyword-zh", "测试"])
                )
            )
            worker.start()
            worker.join(timeout=60)
        assert not worker.is_alive()
        assert results["rc"] == 1
        assert "migration in progress" in capsys.readouterr().err

    def test_single_writer_ignores_stale_lock(
        self, lock_path, capsys, tmp_path, monkeypatch
    ):
        _write_sidecar(lock_path, _dead_pid())
        monkeypatch.setattr(
            workspace_mod, "ACTIVE_GENERATION_PATH", tmp_path / "missing.json"
        )
        rc = discover_single.main(["--keyword-zh", "测试"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "migration in progress" not in err
        assert "workspace resolution failed" in err

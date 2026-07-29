"""Discovery writer-lease / maintenance-lock mutual exclusion probes.

Phase 5 read/write maintenance mutual exclusion, exercised deterministically
against tmp lock paths (every gate class accepts ``lock_path=``; no real
``data/`` directory is touched).  Thread scenarios synchronize with
``threading.Event`` — no fixed sleeps.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
from filelock import FileLock

from src.discovery.maintenance_gate import (
    LOCK_INFO_SUFFIX,
    DiscoveryMaintenanceLock,
    DiscoveryMaintenanceLockError,
    DiscoveryWriterLease,
    active_writer_leases,
    discovery_maintenance_block_reason,
)
from src.utils.process import is_pid_alive

pytestmark = pytest.mark.unit


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    path = tmp_path / "migrations" / ".maintenance.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _foreign_live_pid() -> int:
    """A definitely-alive pid that is never this process.

    On Windows pid 4 (System) qualifies; the generic path takes the first
    live pid from ``psutil.pids()`` that is not ours.
    """
    import psutil

    own = os.getpid()
    candidates = [4, *sorted(psutil.pids())]
    for pid in candidates:
        if pid != own and is_pid_alive(pid):
            return pid
    pytest.skip("no live foreign pid available on this host")
    raise AssertionError("unreachable")


def _write_maintenance_sidecar(lock_path: Path, pid: int, *, purpose: str = "repair") -> Path:
    sidecar = lock_path.with_name(lock_path.name + LOCK_INFO_SUFFIX)
    sidecar.write_text(
        json.dumps(
            {
                "pid": pid,
                "start_time": None,
                "nonce": None,
                "purpose": purpose,
                "operation_id": "probe-15",
                "acquired_at": "2026-01-01T00:00:00+00:00",
                "command": "simulated",
            }
        ),
        encoding="utf-8",
    )
    return sidecar


class TestWriterLeaseBlocksMaintenance:
    def test_held_writer_lease_fails_maintenance_acquire(self, lock_path: Path):
        with DiscoveryWriterLease("discover-papers", lock_path=lock_path):
            held = active_writer_leases(lock_path)
            assert len(held) == 1
            assert held[0]["purpose"] == "discover-papers"
            with pytest.raises(DiscoveryMaintenanceLockError):
                DiscoveryMaintenanceLock("repair", lock_path=lock_path).acquire()
        # After the lease is released, maintenance proceeds.
        with DiscoveryMaintenanceLock("repair", lock_path=lock_path):
            pass


class TestForeignMaintenanceBlocksWriter:
    def test_live_foreign_sidecar_fails_lease_and_rolls_back(self, lock_path: Path):
        _write_maintenance_sidecar(lock_path, _foreign_live_pid())
        with pytest.raises(DiscoveryMaintenanceLockError):
            DiscoveryWriterLease("discover-papers", lock_path=lock_path).acquire()
        # The failed acquisition rolled back: no lease artifacts remain.
        leases_dir = lock_path.parent / "writer_leases"
        assert not list(leases_dir.glob("*.lock"))
        assert not list(leases_dir.glob("*.json"))


class TestStaleWriterLeaseRecovery:
    def test_stale_lease_artifacts_are_cleaned_and_maintenance_proceeds(
        self, lock_path: Path
    ):
        leases_dir = lock_path.parent / "writer_leases"
        leases_dir.mkdir(parents=True)
        nonce = "deadbeefcafebabe"
        stale_lock = leases_dir / f"{nonce}.lock"
        stale_info = leases_dir / f"{nonce}.json"
        # Artifacts left behind by a writer that exited without release:
        # the lock file exists but nothing holds it.
        stale_lock.write_text("", encoding="utf-8")
        stale_info.write_text(
            json.dumps({"pid": 999999, "nonce": nonce, "purpose": "ghost"}),
            encoding="utf-8",
        )

        assert active_writer_leases(lock_path) == []
        assert not stale_lock.exists()
        assert not stale_info.exists()

        with DiscoveryMaintenanceLock("repair", lock_path=lock_path):
            pass


class TestMaintenanceHeldBlocksSecondMaintenance:
    def test_second_acquire_fails_closed_while_mutex_held(self, lock_path: Path):
        """A plain (non-singleton) FileLock simulates the foreign holder.

        ``DiscoveryMaintenanceLock`` uses a singleton FileLock, so a second
        instance in this process would be re-entrant; the plain lock held by
        this fixture forces the real OS-level conflict instead.
        """
        held = FileLock(str(lock_path), timeout=0)
        started = threading.Event()
        done = threading.Event()
        outcome: dict[str, BaseException] = {}
        with held:

            def _worker() -> None:
                started.set()
                try:
                    DiscoveryMaintenanceLock("second", lock_path=lock_path).acquire()
                except DiscoveryMaintenanceLockError as exc:
                    outcome["error"] = exc
                finally:
                    done.set()

            worker = threading.Thread(target=_worker)
            worker.start()
            assert started.wait(timeout=30)
            assert done.wait(timeout=30)
            worker.join(timeout=30)
        assert not worker.is_alive()
        assert isinstance(outcome.get("error"), DiscoveryMaintenanceLockError)


class TestFailClosedOSError:
    def test_lock_path_as_directory_returns_block_reason(self, lock_path: Path):
        # An un-openable lock path (a directory) must fail closed: a
        # non-None reason string, never an exception and never None.
        lock_path.mkdir()
        reason = discovery_maintenance_block_reason(lock_path)
        assert isinstance(reason, str)
        assert reason


class TestThreadOrdering:
    def test_maintenance_attempt_during_lease_raises_then_succeeds_after_release(
        self, lock_path: Path
    ):
        lease = DiscoveryWriterLease("discover-papers", lock_path=lock_path).acquire()
        attempt_started = threading.Event()
        attempt_done = threading.Event()
        outcome: dict[str, BaseException] = {}

        def _worker() -> None:
            attempt_started.set()
            try:
                DiscoveryMaintenanceLock("repair", lock_path=lock_path).acquire()
            except DiscoveryMaintenanceLockError as exc:
                outcome["error"] = exc
            finally:
                attempt_done.set()

        worker = threading.Thread(target=_worker)
        worker.start()
        # The attempt happens strictly while the lease is held and fails
        # closed without waiting (the gate is non-blocking).
        assert attempt_started.wait(timeout=30)
        assert attempt_done.wait(timeout=30)
        worker.join(timeout=30)
        assert not worker.is_alive()
        assert isinstance(outcome.get("error"), DiscoveryMaintenanceLockError)

        lease.release()
        with DiscoveryMaintenanceLock("repair", lock_path=lock_path):
            pass

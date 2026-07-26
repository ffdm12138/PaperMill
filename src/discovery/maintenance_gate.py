"""Shared discovery maintenance gate.

One global lock guards every mutating discovery-v4 migration command
(``--apply``, ``--resume``, ``--cutover``, ``--rollback``, ``--abort``,
``--clean-legacy``, ``--finalize``).  The mutex is a non-blocking
``filelock.FileLock`` on ``<migrations>/.migration.lock`` — the same file
``src.discovery.workspace.commit_workspace`` already uses — and a JSON
sidecar ``<migrations>/.migration.lock.info.json`` records owner
diagnostics (pid, purpose, migration id, timestamp, command) so operators
and the production discovery writers can tell who holds the lock and
whether the holder is still alive.

Stale-lock rule: a sidecar whose owner pid is dead never blocks — the OS
has already released the mutex, and the next command takes over (this is
how ``--resume`` recovers a crashed ``--apply``).  A live owner from a
different process fails every mutating command closed, and the production
discovery writers refuse to start while the lock is held
(:func:`assert_discovery_write_allowed`).

Writer gate: every production discovery writer calls
:func:`assert_discovery_write_allowed` unconditionally — there is no CLI
flag that skips it.  The only exemption is ownership: the process holding
the lock (matched by pid against the sidecar, plus the per-process mutex
semantics of the OS) is allowed through, which is how the in-process
migration smoke run executes a real batch during a maintenance window.
An external process cannot forge ownership: it has a different pid, and
the sidecar/mutex pair cannot be claimed while the live owner holds them.

This module is shared runtime infrastructure owned by production discovery.
It doubles as the fail-closed operator lock for maintenance windows; the
finalized v3→v4 migration used it through this same interface.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout as FileLockTimeout

from src.mineru_lock import _is_pid_alive
from src.utils.atomic_io import atomic_replace_bytes_unlocked

LOCK_INFO_SUFFIX = ".info.json"


class MigrationMaintenanceLockError(RuntimeError):
    """The migration maintenance lock is held by another live process."""


def _default_lock_path() -> Path:
    # Resolved lazily: tests reload src.discovery.workspace with patched
    # settings paths, so the module attribute must be read at call time.
    from src.discovery import workspace as workspace_mod

    return workspace_mod.MIGRATION_LOCK_PATH


def _info_path(lock_path: Path) -> Path:
    return lock_path.with_name(lock_path.name + LOCK_INFO_SUFFIX)


def read_maintenance_lock_info(lock_path: Path | None = None) -> dict[str, Any] | None:
    """Read the owner sidecar without acquiring anything.

    Returns ``None`` when the sidecar is absent or unreadable; an unreadable
    sidecar is treated like a stale one (the mutex itself still guards every
    mutation).
    """
    effective = Path(lock_path) if lock_path else _default_lock_path()
    try:
        data = json.loads(_info_path(effective).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _owner_is_live_other_process(info: dict[str, Any]) -> bool:
    owner_pid = info.get("pid")
    return (
        isinstance(owner_pid, int)
        and owner_pid != os.getpid()
        and _is_pid_alive(owner_pid)
    )


def _write_info_atomic(path: Path, payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    atomic_replace_bytes_unlocked(path, raw)


class MigrationMaintenanceLock:
    """Non-blocking global migration maintenance lock with owner sidecar.

    Acquisition fails closed (never waits) when another live process holds
    the lock.  A sidecar left by a dead process is taken over: the OS has
    already released the mutex, so acquisition proceeds and the sidecar is
    rewritten with the new owner.
    """

    def __init__(
        self,
        purpose: str,
        *,
        migration_id: str | None = None,
        lock_path: Path | None = None,
    ) -> None:
        if not purpose:
            raise ValueError("maintenance lock purpose must be non-empty")
        self._purpose = purpose
        self._migration_id = migration_id
        self._lock_path = Path(lock_path) if lock_path else _default_lock_path()
        self._lock: FileLock | None = None

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    @property
    def info_path(self) -> Path:
        return _info_path(self._lock_path)

    def acquire(self) -> "MigrationMaintenanceLock":
        existing = read_maintenance_lock_info(self._lock_path)
        if existing and _owner_is_live_other_process(existing):
            raise MigrationMaintenanceLockError(
                f"migration maintenance lock is held by live pid "
                f"{existing.get('pid')} (purpose={existing.get('purpose')!r}, "
                f"migration_id={existing.get('migration_id')!r}, "
                f"since {existing.get('acquired_at')}): {self._lock_path}"
            )
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        # is_singleton=True makes same-thread re-acquisition re-entrant, so
        # --cutover can hold this lock while commit_workspace acquires its
        # own FileLock on the very same file.  A different process (or a
        # plain non-singleton FileLock) still fails to acquire while held.
        lock = FileLock(str(self._lock_path), timeout=0, is_singleton=True)
        try:
            lock.acquire()
        except FileLockTimeout as exc:
            raise MigrationMaintenanceLockError(
                "migration maintenance lock mutex is held by another "
                f"process: {self._lock_path}"
            ) from exc
        self._lock = lock
        _write_info_atomic(self.info_path, {
            "pid": os.getpid(),
            "purpose": self._purpose,
            "migration_id": self._migration_id,
            "acquired_at": datetime.now(timezone.utc).isoformat(),
            "command": " ".join(sys.argv),
        })
        return self

    def release(self) -> None:
        if self._lock is None:
            return
        try:
            info = read_maintenance_lock_info(self._lock_path)
            if info is not None and info.get("pid") == os.getpid():
                try:
                    self.info_path.unlink()
                except OSError:
                    pass
        finally:
            self._lock.release()
            self._lock = None

    def __enter__(self) -> "MigrationMaintenanceLock":
        return self.acquire()

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> bool:
        self.release()
        return False


def migration_maintenance_block_reason(lock_path: Path | None = None) -> str | None:
    """Return why a discovery writer must refuse to start, or ``None``.

    A live migration maintenance window blocks production discovery runs.
    A lock held by the calling process itself (the in-process migration
    smoke run) and stale locks (dead owner pid) never block.
    """
    effective = Path(lock_path) if lock_path else _default_lock_path()
    info = read_maintenance_lock_info(effective)
    if info is not None:
        if _owner_is_live_other_process(info):
            return (
                f"discovery migration in progress: maintenance lock held by "
                f"pid {info.get('pid')} (purpose={info.get('purpose')!r}, "
                f"migration_id={info.get('migration_id')!r}, "
                f"since {info.get('acquired_at')})"
            )
        if info.get("pid") == os.getpid():
            # Self-owned sidecar: this process holds the lock (the in-process
            # migration smoke).  The sidecar is written under the mutex, so a
            # foreign holder can never claim our pid; do not probe the mutex —
            # on Windows a second FileLock handle in the same process would
            # report a false conflict.
            return None
        # Stale sidecar: the mutex probe below is authoritative.
    if not effective.parent.is_dir():
        # No migrations directory means no migration ever ran here.
        return None
    # Probe the mutex itself: covers a holder that never wrote (or has not
    # yet written) its sidecar.  Same-thread holders (the in-process smoke
    # run) re-acquire without blocking.
    probe = FileLock(str(effective), timeout=0)
    try:
        with probe:
            pass
    except FileLockTimeout:
        return (
            "discovery migration in progress: maintenance lock "
            f"{effective} is held by another process"
        )
    except OSError:
        return None
    return None


def assert_discovery_write_allowed(lock_path: Path | None = None) -> None:
    """Fail closed when a migration maintenance window is active.

    Every production discovery writer calls this unconditionally at
    startup.  There is deliberately no opt-out parameter: the only way
    through is ownership (the calling process itself holds the lock, e.g.
    the in-process migration smoke) or a stale/absent lock.
    """
    reason = migration_maintenance_block_reason(lock_path)
    if reason is not None:
        raise MigrationMaintenanceLockError(reason)

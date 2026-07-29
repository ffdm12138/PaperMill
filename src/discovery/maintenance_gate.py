"""Shared discovery maintenance gate — reader/writer mutual exclusion.

Lock model
----------
- **Exclusive maintenance lock** (``.maintenance.lock`` under the discovery
  migrations directory): fresh-install bootstrap, keyword notebook
  mutations, relevance-profile apply/resume/abort, and workspace repair.
  Acquisition is non-blocking and only succeeds while ZERO writer leases
  are held.
- **Shared writer lease** (``writer_leases/<nonce>.lock`` + JSON sidecar):
  every production discovery batch holds one for its entire lifetime.
  Many writers may coexist; any single live lease blocks maintenance.

Ordering rule (both sides, no resolve-before-lock):

1. Writer: create the lease, THEN assert no maintenance window; on a
   conflict the lease is released and the writer fails closed.
2. Maintenance: acquire the exclusive mutex, THEN scan writer leases; any
   held lease releases the mutex and fails the command closed.

Because a writer re-checks maintenance AFTER publishing its lease and
maintenance re-scans leases AFTER taking the mutex, neither side can slip
through the other's window.

Sidecar identity: every sidecar binds ``pid`` + process ``start_time`` +
a random ``nonce``, so PID reuse never authenticates a stale owner.  The
OS-level lock probe is authoritative for "held" state; sidecars provide
diagnostics and deterministic stale cleanup.  Lock probes that raise
``OSError`` and unreadable sidecars are fail-closed everywhere.

The global mutex is the same file ``src.discovery.workspace.commit_workspace``
uses, so workspace commits stay serialized with maintenance commands.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout as FileLockTimeout

from src.utils.process import is_pid_alive
from src.utils.atomic_io import atomic_replace_bytes_unlocked

LOCK_INFO_SUFFIX = ".info.json"
WRITER_LEASES_DIRNAME = "writer_leases"


class DiscoveryMaintenanceLockError(RuntimeError):
    """The discovery maintenance lock is held by another live process."""


def _default_lock_path() -> Path:
    # Resolved lazily: tests reload src.discovery.workspace with patched
    # settings paths, so the module attribute must be read at call time.
    from src.discovery import workspace as workspace_mod

    return workspace_mod.DISCOVERY_MAINTENANCE_LOCK_PATH


def _info_path(lock_path: Path) -> Path:
    return lock_path.with_name(lock_path.name + LOCK_INFO_SUFFIX)


def _writer_leases_dir(lock_path: Path) -> Path:
    return lock_path.parent / WRITER_LEASES_DIRNAME


def _process_start_time(pid: int) -> float | None:
    """Process creation time for PID-reuse-safe identity, or ``None``."""
    try:
        import psutil

        return float(psutil.Process(pid).create_time())
    except Exception:
        return None


def _sidecar_payload(purpose: str, operation_id: str | None, nonce: str | None) -> dict[str, Any]:
    pid = os.getpid()
    return {
        "pid": pid,
        "start_time": _process_start_time(pid),
        "nonce": nonce,
        "purpose": purpose,
        "operation_id": operation_id,
        "acquired_at": datetime.now(timezone.utc).isoformat(),
        "command": " ".join(sys.argv),
    }


def read_maintenance_lock_info(lock_path: Path | None = None) -> dict[str, Any] | None:
    """Read the owner sidecar without acquiring anything.

    Returns ``None`` when the sidecar is absent or unreadable.  Callers
    that must fail closed check ``_info_path(...).exists()`` first to
    distinguish "absent" from "unreadable".
    """
    effective = Path(lock_path) if lock_path else _default_lock_path()
    try:
        data = json.loads(_info_path(effective).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _owner_is_live_other_process(info: dict[str, Any]) -> bool:
    owner_pid = info.get("pid")
    if not isinstance(owner_pid, int) or owner_pid == os.getpid():
        return False
    if not is_pid_alive(owner_pid):
        return False
    # PID-reuse guard: when both sides record a start time, a mismatch
    # means the recorded owner is gone and the pid was recycled.
    recorded = info.get("start_time")
    if isinstance(recorded, (int, float)):
        current = _process_start_time(owner_pid)
        if current is not None and abs(current - float(recorded)) > 1e-6:
            return False
    return True


def _write_info_atomic(path: Path, payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    atomic_replace_bytes_unlocked(path, raw)


def _probe_held(lock_file: Path, *, owner: str) -> bool:
    """Authoritative held-check for ``lock_file``.  Fail closed on OSError."""
    probe = FileLock(str(lock_file), timeout=0)
    try:
        with probe:
            return False
    except FileLockTimeout:
        return True
    except OSError as exc:
        raise DiscoveryMaintenanceLockError(
            f"{owner} lock probe failed (fail closed): {lock_file}: {exc}"
        ) from exc


# ── Shared writer lease ───────────────────────────────────────────────────


class DiscoveryWriterLease:
    """Shared lease held by a production discovery writer for its whole run.

    While any lease is held, no exclusive maintenance command can start.
    Acquisition order: publish the lease first, then assert the
    maintenance gate — a maintenance window that started in between is
    detected and the lease is rolled back.
    """

    def __init__(
        self,
        purpose: str,
        *,
        operation_id: str | None = None,
        lock_path: Path | None = None,
    ) -> None:
        if not purpose:
            raise ValueError("writer lease purpose must be non-empty")
        self._purpose = purpose
        self._operation_id = operation_id
        self._lock_path = Path(lock_path) if lock_path else _default_lock_path()
        self._nonce = uuid.uuid4().hex
        self._lease_lock: FileLock | None = None

    @property
    def nonce(self) -> str:
        return self._nonce

    @property
    def lease_lock_path(self) -> Path:
        return _writer_leases_dir(self._lock_path) / f"{self._nonce}.lock"

    @property
    def lease_info_path(self) -> Path:
        return _writer_leases_dir(self._lock_path) / f"{self._nonce}.json"

    def acquire(self) -> "DiscoveryWriterLease":
        leases_dir = _writer_leases_dir(self._lock_path)
        leases_dir.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(self.lease_lock_path), timeout=0)
        try:
            lock.acquire()
        except (FileLockTimeout, OSError) as exc:
            raise DiscoveryMaintenanceLockError(
                f"cannot acquire writer lease (fail closed): "
                f"{self.lease_lock_path}: {exc}"
            ) from exc
        self._lease_lock = lock
        _write_info_atomic(
            self.lease_info_path,
            _sidecar_payload(self._purpose, self._operation_id, self._nonce),
        )
        try:
            # Post-publication gate: a maintenance window that started
            # while we were publishing is caught here.
            assert_discovery_write_allowed(self._lock_path)
        except Exception:
            self.release()
            raise
        return self

    def release(self) -> None:
        if self._lease_lock is None:
            return
        try:
            info = None
            try:
                info = json.loads(
                    self.lease_info_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                info = None
            if isinstance(info, dict) and info.get("nonce") == self._nonce:
                try:
                    self.lease_info_path.unlink()
                except OSError:
                    pass
        finally:
            self._lease_lock.release()
            self._lease_lock = None
            try:
                self.lease_lock_path.unlink()
            except OSError:
                pass

    def __enter__(self) -> "DiscoveryWriterLease":
        return self.acquire()

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> bool:
        self.release()
        return False


def active_writer_leases(lock_path: Path | None = None) -> list[dict[str, Any]]:
    """Return diagnostic dicts for every currently HELD writer lease.

    Stale leases (lock file free) are deterministically cleaned up.
    Unreadable sidecars on held leases are fail-closed: they are reported
    as a held lease with ``sidecar_error`` set.
    """
    effective = Path(lock_path) if lock_path else _default_lock_path()
    leases_dir = _writer_leases_dir(effective)
    if not leases_dir.is_dir():
        return []
    held: list[dict[str, Any]] = []
    for lock_file in sorted(leases_dir.glob("*.lock")):
        if _probe_held(lock_file, owner="writer lease"):
            sidecar = lock_file.with_suffix(".json")
            info: dict[str, Any] = {"lease_lock": str(lock_file)}
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    info.update(data)
                else:
                    info["sidecar_error"] = "not a JSON object"
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                info["sidecar_error"] = str(exc)
            held.append(info)
        else:
            # Stale lease: the OS already released the lock (writer exited).
            for artifact in (lock_file, lock_file.with_suffix(".json")):
                try:
                    artifact.unlink()
                except OSError:
                    pass
    return held


# ── Exclusive maintenance lock ────────────────────────────────────────────


class DiscoveryMaintenanceLock:
    """Non-blocking global discovery maintenance lock with owner sidecar.

    Acquisition fails closed (never waits) when another live process holds
    the lock OR any writer lease is active.  A sidecar left by a dead
    process is taken over: the OS has already released the mutex, so
    acquisition proceeds and the sidecar is rewritten with the new owner.
    """

    def __init__(
        self,
        purpose: str,
        *,
        operation_id: str | None = None,
        lock_path: Path | None = None,
    ) -> None:
        if not purpose:
            raise ValueError("maintenance lock purpose must be non-empty")
        self._purpose = purpose
        self._operation_id = operation_id
        self._lock_path = Path(lock_path) if lock_path else _default_lock_path()
        self._lock: FileLock | None = None

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    @property
    def info_path(self) -> Path:
        return _info_path(self._lock_path)

    def acquire(self) -> "DiscoveryMaintenanceLock":
        if self.info_path.exists():
            existing = read_maintenance_lock_info(self._lock_path)
            if existing is None:
                raise DiscoveryMaintenanceLockError(
                    f"discovery maintenance lock sidecar is unreadable "
                    f"(fail closed): {self.info_path}"
                )
            if _owner_is_live_other_process(existing):
                raise DiscoveryMaintenanceLockError(
                    f"discovery maintenance lock is held by live pid "
                    f"{existing.get('pid')} (purpose={existing.get('purpose')!r}, "
                    f"operation_id={existing.get('operation_id')!r}, "
                    f"since {existing.get('acquired_at')}): {self._lock_path}"
                )
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        # is_singleton=True makes same-thread re-acquisition re-entrant, so
        # a maintenance command can hold this lock while commit_workspace
        # acquires its own FileLock on the very same file.  A different
        # process (or a plain non-singleton FileLock) still fails to acquire
        # while held.
        lock = FileLock(str(self._lock_path), timeout=0, is_singleton=True)
        try:
            lock.acquire()
        except FileLockTimeout as exc:
            raise DiscoveryMaintenanceLockError(
                "discovery maintenance lock mutex is held by another "
                f"process: {self._lock_path}"
            ) from exc
        except OSError as exc:
            raise DiscoveryMaintenanceLockError(
                f"discovery maintenance lock mutex probe failed "
                f"(fail closed): {self._lock_path}: {exc}"
            ) from exc
        try:
            # Exclusive means zero active writers: check AFTER taking the
            # mutex so a writer that published first is always visible.
            held = active_writer_leases(self._lock_path)
            if held:
                raise DiscoveryMaintenanceLockError(
                    f"discovery writer lease(s) active ({len(held)}): "
                    + "; ".join(
                        f"pid {h.get('pid')} purpose={h.get('purpose')!r} "
                        f"nonce={h.get('nonce')}"
                        + (
                            f" [sidecar unreadable: {h['sidecar_error']}]"
                            if h.get("sidecar_error")
                            else ""
                        )
                        for h in held
                    )
                )
        except Exception:
            lock.release()
            raise
        self._lock = lock
        _write_info_atomic(
            self.info_path,
            _sidecar_payload(self._purpose, self._operation_id, nonce=None),
        )
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

    def __enter__(self) -> "DiscoveryMaintenanceLock":
        return self.acquire()

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> bool:
        self.release()
        return False


def discovery_maintenance_block_reason(lock_path: Path | None = None) -> str | None:
    """Return why a discovery writer must refuse to start, or ``None``.

    A live discovery maintenance window blocks production discovery runs.
    A lock held by the calling process itself and stale locks (dead owner
    pid) never block.  An unreadable sidecar or a failed lock probe blocks
    (fail closed).
    """
    effective = Path(lock_path) if lock_path else _default_lock_path()
    if _info_path(effective).exists():
        info = read_maintenance_lock_info(effective)
        if info is None:
            return (
                f"discovery maintenance lock sidecar is unreadable "
                f"(fail closed): {_info_path(effective)}"
            )
        if _owner_is_live_other_process(info):
            return (
                f"discovery maintenance window in progress: lock held by "
                f"pid {info.get('pid')} (purpose={info.get('purpose')!r}, "
                f"operation_id={info.get('operation_id')!r}, "
                f"since {info.get('acquired_at')})"
            )
        if info.get("pid") == os.getpid():
            # Self-owned sidecar: this process holds the lock.  The sidecar
            # is written under the mutex, so a foreign holder can never
            # claim our pid; do not probe the mutex — on Windows a second
            # FileLock handle in the same process would report a false
            # conflict.
            return None
        # Stale sidecar: the mutex probe below is authoritative.
    if not effective.parent.is_dir():
        # No migrations directory means no maintenance window ever ran here.
        return None
    # Probe the mutex itself: covers a holder that never wrote (or has not
    # yet written) its sidecar.  Same-thread holders re-acquire without
    # blocking.
    probe = FileLock(str(effective), timeout=0)
    try:
        with probe:
            pass
    except FileLockTimeout:
        return (
            "discovery maintenance window in progress: lock "
            f"{effective} is held by another process"
        )
    except OSError as exc:
        return (
            f"discovery maintenance lock probe failed (fail closed): "
            f"{effective}: {exc}"
        )
    return None


def assert_discovery_write_allowed(lock_path: Path | None = None) -> None:
    """Fail closed when a discovery maintenance window is active.

    Every production discovery writer enters through
    :class:`DiscoveryWriterLease`, which calls this after publishing its
    lease.  There is deliberately no opt-out parameter: the only way
    through is ownership (the calling process itself holds the lock) or a
    stale/absent lock.
    """
    reason = discovery_maintenance_block_reason(lock_path)
    if reason is not None:
        raise DiscoveryMaintenanceLockError(reason)

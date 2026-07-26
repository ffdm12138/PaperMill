"""Unified test-runtime workspace manager.

Every test group (fast, full, stress, area) gets its own isolated workspace
under the system temporary directory.  The workspace is tracked by a machine-
readable marker so stale workspaces can be identified and cleaned up safely.

Usage::

    with TestRuntimeWorkspace(group="catalog") as ws:
        env = ws.child_env()
        subprocess.run(
            [sys.executable, "-m", "pytest", ...],
            env=env,
            shell=False,
        )

The context manager guarantees cleanup on normal exit, test failure, unhandled
exception, or KeyboardInterrupt.  Stale workspaces whose owner PID is dead are
safe to delete via ``cleanup_test_caches.py``.

Contract
--------
* All path values in ``child_env()`` use the native OS separator and are never
  interpolated into shell strings, ``PYTEST_ADDOPTS``, or ``addopts``.
* ``shell=False`` is mandatory for every subprocess that receives this env.
* Workspaces must live under the system temporary directory — never in the
  repo, never on a drive root.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

_MARKER_SCHEMA_VERSION = "1.0"
_MARKER_OWNER = "mineru-test-runner"
_MAX_CLEANUP_RETRIES = 5
_CLEANUP_RETRY_DELAYS = [0.1, 0.2, 0.5, 1.0, 2.0]
_WORKSPACE_NAME_RE = re.compile(r"^mineru_.+_[0-9a-f]{8}$")
_MARKER_IDENTITY_KEYS = (
    "schema_version", "owner", "group", "pid", "created_at", "repo_root",
)


# ---------------------------------------------------------------------------
# Workspace state types
# ---------------------------------------------------------------------------


class WorkspaceStatus(str, Enum):
    """Classification of a workspace directory on disk."""
    ACTIVE = "active"           # valid marker, matching repo, PID alive
    STALE = "stale"             # valid marker, matching repo, PID dead
    UNRECOGNIZED = "unrecognized"  # no marker or not mineru-owned
    FOREIGN = "foreign"         # mineru-owned but different repo
    INVALID = "invalid"         # unsupported schema, bad pid, broken marker


@dataclass(frozen=True)
class WorkspaceInspection:
    """Result of inspecting a workspace directory."""
    path: Path
    status: WorkspaceStatus
    safe_to_delete: bool
    reason: str
    marker: dict | None = None


def inspect_workspace(path: Path, *, repo_root: Optional[Path] = None) -> WorkspaceInspection:
    """Classify a workspace directory without mutating it.

    Only ``WorkspaceStatus.STALE`` has ``safe_to_delete=True``.
    """
    root_error = _workspace_root_safety(
        path,
        repo_root=repo_root,
        require_managed_name=False,
        require_system_temp=False,
    )
    if root_error is not None:
        return WorkspaceInspection(path, WorkspaceStatus.INVALID, False, root_error)

    marker = _read_marker(path)
    if marker is None:
        return WorkspaceInspection(path, WorkspaceStatus.UNRECOGNIZED, False,
                                   "no valid marker — not a recognised workspace")

    if marker.get("owner") != _MARKER_OWNER:
        return WorkspaceInspection(path, WorkspaceStatus.UNRECOGNIZED, False,
                                   f"owner is {marker.get('owner')!r}, not {_MARKER_OWNER!r}",
                                   marker)

    if marker.get("schema_version") != _MARKER_SCHEMA_VERSION:
        return WorkspaceInspection(path, WorkspaceStatus.INVALID, False,
                                   f"unknown marker schema {marker.get('schema_version')!r}",
                                   marker)

    pid = marker.get("pid")
    if pid is None:
        return WorkspaceInspection(path, WorkspaceStatus.INVALID, False,
                                   "marker missing pid", marker)
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return WorkspaceInspection(path, WorkspaceStatus.INVALID, False,
                                   f"pid is not a positive integer: {pid!r}", marker)

    for key in ("group", "created_at"):
        if not isinstance(marker.get(key), str) or not marker[key].strip():
            return WorkspaceInspection(path, WorkspaceStatus.INVALID, False,
                                       f"marker missing {key}", marker)

    if repo_root is not None:
        marker_repo = marker.get("repo_root", "")
        if not isinstance(marker_repo, str) or not marker_repo.strip():
            return WorkspaceInspection(path, WorkspaceStatus.INVALID, False,
                                       "marker missing repo_root", marker)
        try:
            if Path(marker_repo).resolve() != repo_root.resolve():
                return WorkspaceInspection(path, WorkspaceStatus.FOREIGN, False,
                                           f"belongs to different repo {marker_repo!r}",
                                           marker)
        except Exception:
            return WorkspaceInspection(path, WorkspaceStatus.FOREIGN, False,
                                       f"unresolvable repo_root {marker_repo!r}",
                                       marker)

    if _pid_alive(pid):
        return WorkspaceInspection(path, WorkspaceStatus.ACTIVE, False,
                                   f"owner pid {pid} is alive", marker)

    return WorkspaceInspection(path, WorkspaceStatus.STALE, True,
                               f"owner pid {pid} is dead — safe to delete", marker)


# ---------------------------------------------------------------------------
# Cleanup result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkspaceCleanupResult:
    """Outcome of a workspace cleanup attempt."""
    success: bool
    path: Path
    attempts: int
    error: str | None = None


def _system_temp_dir() -> Path:
    """Return the real system temporary directory (resolved)."""
    return Path(tempfile.gettempdir()).resolve()


def _timestamp_iso() -> str:
    """ISO-8601 timestamp in local timezone with seconds precision."""
    import datetime
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _marker_path(workspace_root: Path) -> Path:
    return workspace_root / ".mineru-test-workspace.json"


def _read_marker(workspace_root: Path) -> Optional[dict]:
    mp = _marker_path(workspace_root)
    if not mp.is_file():
        return None
    try:
        return json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _update_marker_cleanup_failed(
    root: Path,
    marker_snapshot: dict,
    attempts: int,
    error: str,
) -> bool:
    """Restore the complete marker and add cleanup-failure diagnostics.

    ``shutil.rmtree`` can remove the marker before a later Windows permission
    error stops the tree deletion.  The caller therefore supplies the marker
    captured *before* the first mutation instead of re-reading a possibly
    half-deleted workspace.
    """
    if _workspace_root_safety(root, repo_root=Path(str(marker_snapshot.get("repo_root", "")))) is not None:
        return False
    mp = _marker_path(root)
    existing = dict(marker_snapshot)
    existing.update({
        "cleanup_status": "failed",
        "cleanup_attempts": attempts,
        "cleanup_last_error": error,
        "cleanup_failed_at": _timestamp_iso(),
    })
    tmp = mp.with_suffix(mp.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(mp)
        return True
    except OSError:
        return False


def is_workspace_candidate_name(name: str) -> bool:
    """Return whether *name* has the shape produced by this workspace manager."""
    return bool(_WORKSPACE_NAME_RE.fullmatch(name))


def _path_is_reparse(path: Path, *, st: os.stat_result | None = None) -> bool:
    """Detect symlinks and Windows reparse points without following them."""
    try:
        info = st or path.lstat()
    except OSError:
        return True
    if stat.S_ISLNK(info.st_mode):
        return True
    attrs = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attrs & reparse_flag)


def _workspace_root_safety(
    root: Path,
    *,
    repo_root: Optional[Path],
    require_managed_name: bool = True,
    require_system_temp: bool = True,
) -> str | None:
    """Validate a workspace root without reading or following its marker path."""
    if require_managed_name and not is_workspace_candidate_name(root.name):
        return f"name does not match managed workspace pattern: {root.name!r}"
    try:
        info = root.lstat()
    except OSError as exc:
        return f"cannot lstat workspace root: {exc}"
    if not stat.S_ISDIR(info.st_mode):
        return "workspace root is not a directory"
    if _path_is_reparse(root, st=info):
        return "workspace root is a symlink, junction, or reparse point"
    try:
        resolved = root.resolve()
        system_tmp = _system_temp_dir()
    except OSError as exc:
        return f"cannot resolve workspace root: {exc}"
    if require_system_temp and (resolved == system_tmp or system_tmp not in resolved.parents):
        return f"workspace root is outside system temporary directory: {resolved}"
    if repo_root is not None:
        try:
            resolved_repo = repo_root.resolve()
            if resolved == resolved_repo or resolved_repo in resolved.parents:
                return "workspace root is inside repository"
        except OSError as exc:
            return f"cannot resolve repository root: {exc}"
    return None


def _marker_identity_matches(actual: dict, expected: dict) -> bool:
    return all(actual.get(key) == expected.get(key) for key in _MARKER_IDENTITY_KEYS)


def _validate_verified_workspace(
    root: Path,
    marker_snapshot: dict,
    *,
    repo_root: Path,
) -> str | None:
    """Validate root containment and the cached marker identity before mutation."""
    root_error = _workspace_root_safety(root, repo_root=repo_root)
    if root_error is not None:
        return root_error
    if marker_snapshot.get("schema_version") != _MARKER_SCHEMA_VERSION:
        return "cached marker has unsupported schema"
    if marker_snapshot.get("owner") != _MARKER_OWNER:
        return "cached marker has wrong owner"
    pid = marker_snapshot.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return "cached marker has invalid pid"
    for key in ("group", "created_at"):
        value = marker_snapshot.get(key)
        if not isinstance(value, str) or not value.strip():
            return f"cached marker is missing {key}"
    try:
        if Path(str(marker_snapshot.get("repo_root", ""))).resolve() != repo_root.resolve():
            return "cached marker belongs to a different repository"
    except OSError as exc:
        return f"cannot validate cached marker repository: {exc}"
    current = _read_marker(root)
    if current is None:
        return "workspace marker is missing or unreadable"
    if not _marker_identity_matches(current, marker_snapshot):
        return "workspace marker identity changed"
    return None


def _plain_path_within_workspace(root: Path, candidate: Path) -> bool:
    """Return True only for a lexical descendant with no reparse component."""
    root_abs = Path(os.path.abspath(root))
    candidate_abs = Path(os.path.abspath(candidate))
    try:
        relative = candidate_abs.relative_to(root_abs)
    except ValueError:
        return False
    current = root_abs
    for part in (None, *relative.parts):
        if part is not None:
            current = current / part
        try:
            info = current.lstat()
        except OSError:
            return False
        if _path_is_reparse(current, st=info):
            return False
    return True


def _windows_readonly_retry_handler(root: Path):
    """Build a bounded ``rmtree`` callback for plain paths inside *root*.

    The callback performs one chmod-and-retry per path.  It is installed only
    on Windows and refuses every symlink, junction, reparse point, or path
    outside the already verified workspace.
    """
    retried: set[str] = set()

    def _handle(func, path: str, exc_info) -> None:
        error = exc_info[1]
        candidate = Path(path)
        key = os.path.normcase(os.path.abspath(candidate))
        if not isinstance(error, PermissionError):
            raise error
        if key in retried or not _plain_path_within_workspace(root, candidate):
            raise error
        retried.add(key)
        os.chmod(candidate, stat.S_IWRITE | stat.S_IREAD)
        func(path)

    return _handle


def remove_verified_workspace_tree(
    root: Path,
    *,
    marker_snapshot: dict,
    repo_root: Path,
    max_retries: int = _MAX_CLEANUP_RETRIES,
) -> WorkspaceCleanupResult:
    """Remove a marker-verified workspace and preserve identity on failure."""
    validation_error = _validate_verified_workspace(
        root, marker_snapshot, repo_root=repo_root,
    )
    if validation_error is not None:
        return WorkspaceCleanupResult(False, root, 0, validation_error)

    last_error: str | None = None
    attempts = max(1, min(int(max_retries), _MAX_CLEANUP_RETRIES))
    completed_attempts = 0
    for attempt in range(attempts):
        completed_attempts = attempt + 1
        try:
            handler = _windows_readonly_retry_handler(root) if os.name == "nt" else None
            shutil.rmtree(root, ignore_errors=False, onerror=handler)
            if not root.exists():
                return WorkspaceCleanupResult(True, root, attempt + 1)
            last_error = "rmtree reported success but directory still exists"
        except OSError as exc:
            last_error = str(exc)

        if root.exists():
            _update_marker_cleanup_failed(root, marker_snapshot, attempt + 1, last_error or "unknown")
        if attempt < attempts - 1:
            validation_error = _validate_verified_workspace(
                root, marker_snapshot, repo_root=repo_root,
            )
            if validation_error is not None:
                last_error = validation_error
                break
            time.sleep(_CLEANUP_RETRY_DELAYS[attempt])

    if root.exists():
        _update_marker_cleanup_failed(
            root, marker_snapshot, completed_attempts, last_error or "unknown",
        )
    return WorkspaceCleanupResult(False, root, completed_attempts, last_error)


def _pid_alive(pid: int) -> bool:
    """Best-effort check: is *pid* an existing process?"""
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            return exit_code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False


def is_stale_workspace(root: Path, *, repo_root: Optional[Path] = None) -> Optional[str]:
    """Backward-compatible wrapper — prefer :func:`inspect_workspace`.

    Returns ``None`` for ``STALE`` (safe to delete), or a reason string
    for any other status (not safe).
    """
    ins = inspect_workspace(root, repo_root=repo_root)
    if ins.status == WorkspaceStatus.STALE:
        return None
    return ins.reason


class TestRuntimeWorkspace:
    """Isolated workspace for a single test group invocation.

    Creates a uniquely-named directory under the system temp directory
    with the structure::

        <temp>/mineru_<group>_<random>/
            cache/           # PYTHONPYCACHEPREFIX
            pytest/          # pytest --basetemp
            temp/            # TMP / TEMP / TMPDIR
            home/            # HOME / USERPROFILE (only when needed)
            logs/            # test output logs
            .mineru-test-workspace.json

    Parameters
    ----------
    group: str
        Logical test group name (e.g. ``"catalog"``, ``"ingest"``).
    repo_root: Path, optional
        Absolute path to the repository root.  Written into the marker so
        the cleaner can verify workspace ownership.  Defaults to the parent
        of the ``scripts/`` directory containing this module.
    set_home: bool
        When True, ``child_env()`` sets ``HOME`` to ``<root>/home``.
        Default False — only enable when tests require an isolated home.
    """

    __test__ = False  # Not a pytest test class

    def __init__(self, *, group: str,
                 repo_root: Optional[Path] = None,
                 set_home: bool = False):
        self._group = group
        self._repo_root = repo_root or Path(__file__).resolve().parent.parent
        self._set_home = set_home
        self._root: Optional[Path] = None
        self._pid: int = os.getpid()
        self._created_at: str = _timestamp_iso()

    # -- paths ---------------------------------------------------------------

    @property
    def root(self) -> Path:
        if self._root is None:
            raise RuntimeError("Workspace not entered")
        return self._root

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    @property
    def pytest_dir(self) -> Path:
        return self.root / "pytest"

    @property
    def temp_dir(self) -> Path:
        return self.root / "temp"

    @property
    def home_dir(self) -> Path:
        return self.root / "home"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    # -- env -----------------------------------------------------------------

    def child_env(self, *, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        """Return an ``os.environ`` copy suitable for a test subprocess.

        **Never** put any of the values from this dict into a shell command
        string, ``PYTEST_ADDOPTS``, or ``addopts`` ini value.  Use them only
        via the ``env`` parameter of ``subprocess.Popen`` / ``subprocess.run``
        with ``shell=False``.
        """
        if self._root is None:
            raise RuntimeError("Workspace not entered")
        env = os.environ.copy()
        env.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        # Prevent bytecode writes into the source tree.  PYTHONDONTWRITEBYTECODE
        # is the primary guard; PYTHONPYCACHEPREFIX provides an isolated target
        # for subprocesses that genuinely need caching.
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPYCACHEPREFIX"] = str(self.cache_dir)
        # Temporary directory overrides.
        env["TMP"] = str(self.temp_dir)
        env["TEMP"] = str(self.temp_dir)
        env["TMPDIR"] = str(self.temp_dir)
        # Isolated home (opt-in).
        if self._set_home:
            env["HOME"] = str(self.home_dir)
            env["USERPROFILE"] = str(self.home_dir)
        if extra:
            env.update(extra)
        return env

    # -- context manager -----------------------------------------------------

    def __enter__(self) -> "TestRuntimeWorkspace":
        system_tmp = _system_temp_dir()
        suffix = secrets.token_hex(4)
        self._root = system_tmp / f"mineru_{self._group}_{suffix}"
        self._root.mkdir(parents=True, exist_ok=False)
        # Create subdirectories
        for d in (self.cache_dir, self.pytest_dir, self.temp_dir,
                  self.home_dir, self.logs_dir):
            d.mkdir(exist_ok=True)
        # Write marker
        marker = {
            "schema_version": _MARKER_SCHEMA_VERSION,
            "owner": _MARKER_OWNER,
            "group": self._group,
            "pid": self._pid,
            "created_at": self._created_at,
            "repo_root": str(self._repo_root.resolve()),
        }
        _marker_path(self._root).write_text(
            json.dumps(marker, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        result = self.cleanup()
        if not result.success and exc_type is None:
            raise RuntimeError(
                f"Workspace cleanup failed: {result.path}\n"
                f"  attempts: {result.attempts}\n"
                f"  error: {result.error}"
            )

    # -- cleanup -------------------------------------------------------------

    def cleanup(self) -> WorkspaceCleanupResult:
        """Remove the workspace directory tree.

        Retries on Windows where anti-virus or pending I/O can briefly hold
        file handles after a subprocess exits.

        Returns a structured result.  On failure the marker is updated with
        cleanup status and ``_root`` is preserved so the path can be reported.
        """
        if self._root is None:
            return WorkspaceCleanupResult(True, Path("."), 0)
        root = self._root
        if not root.exists():
            return WorkspaceCleanupResult(True, root, 0)
        marker_snapshot = _read_marker(root) or {
            "schema_version": _MARKER_SCHEMA_VERSION,
            "owner": _MARKER_OWNER,
            "group": self._group,
            "pid": self._pid,
            "created_at": self._created_at,
            "repo_root": str(self._repo_root.resolve()),
        }
        return remove_verified_workspace_tree(
            root,
            marker_snapshot=marker_snapshot,
            repo_root=self._repo_root,
        )


# -- module-level helpers for acceptance pre/post checks ---------------------

def count_root_pollution(temp_dir: Optional[Path] = None,
                         legacy_flattened: bool = True) -> int:
    """Count directories on the system-drive root that match the legacy
    flattened-cache naming pattern.

    Parameters
    ----------
    temp_dir: Path, optional
        The system temp directory (used to derive the flattened prefix).
        Defaults to ``tempfile.gettempdir()``.
    legacy_flattened: bool
        When True, scan for the legacy flattened-root pollution pattern.
    """
    if not legacy_flattened:
        return 0
    if temp_dir is None:
        temp_dir = _system_temp_dir()
    flattened_prefix = _flatten_path(temp_dir)
    try:
        drive_root = Path(temp_dir.anchor) if temp_dir.anchor else Path("/")
    except Exception:
        return 0
    if not drive_root.exists():
        return 0
    count = 0
    pattern = f"{flattened_prefix}mineru_"
    cache_suffix = "cache"
    try:
        for entry in drive_root.iterdir():
            if not entry.is_dir():
                continue
            name = entry.name
            if name.startswith(pattern) and name.endswith(cache_suffix):
                count += 1
    except PermissionError:
        pass
    return count


def _flatten_path(path: Path) -> str:
    """Return the path with all separators and the anchor prefix removed.

    ``C:\\Users\\Admin\\AppData\\Local\\Temp`` → ``UsersAdminAppDataLocalTemp``
    """
    resolved = str(path.resolve())
    # Remove drive letter / UNC anchor
    if resolved.startswith("\\\\"):
        # UNC path: keep server/share but remove leading backslashes
        normalized = resolved.replace("\\", "/")
        parts = [p for p in normalized.split("/") if p]
        return "".join(parts)
    # Drive-letter path: e.g. C:\Users\... → strip "C:" and join parts
    if len(resolved) >= 2 and resolved[1] == ":":
        resolved = resolved[2:]
    parts = [p for p in resolved.replace("\\", "/").split("/") if p]
    return "".join(parts)


# Top-level directories pruned from bytecode-pollution scans.  Only the
# TOP-LEVEL entries are skipped (a nested ``src/data/`` is still scanned),
# matching the historical rglob-then-filter semantics.  ``.git`` was never
# reportable (no .pyc/__pycache__ live there) but rglob used to descend its
# entire object store; pruning it is a pure walk-cost saving.
_SCAN_SKIP_TOPS = ("data", "output", "reports", "write", ".git")


def scan_repo_bytecode(repo_root: Optional[Path] = None) -> tuple[list[str], list[str]]:
    """Single pruned walk for bytecode pollution.

    Returns ``(pycache_dirs, pyc_files)`` as repo-relative POSIX paths.
    Unlike a naive ``rglob`` (which visits every entry under ``data/`` and
    ``output/`` before filtering), runtime directories are pruned at walk
    time, so the scan touches only source trees.  ``__pycache__`` directories
    are still descended so the ``.pyc`` files inside them are listed
    individually, matching the historical output.
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent
    root_str = str(repo_root)
    pycache: list[str] = []
    pyc: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root_str):
        if dirpath == root_str:
            dirnames[:] = [d for d in dirnames if d not in _SCAN_SKIP_TOPS]
        rel_dir = os.path.relpath(dirpath, root_str).replace("\\", "/")
        for d in dirnames:
            if d == "__pycache__":
                pycache.append(d if rel_dir == "." else f"{rel_dir}/{d}")
        for f in filenames:
            if f.endswith(".pyc"):
                pyc.append(f if rel_dir == "." else f"{rel_dir}/{f}")
    return pycache, pyc


def count_repo_pycache(repo_root: Optional[Path] = None) -> int:
    """Count ``__pycache__`` directories inside the repository."""
    return len(scan_repo_bytecode(repo_root)[0])


def count_repo_pyc(repo_root: Optional[Path] = None) -> int:
    """Count ``.pyc`` files inside the repository (excluding runtime dirs)."""
    return len(scan_repo_bytecode(repo_root)[1])

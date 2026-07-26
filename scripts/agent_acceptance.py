#!/usr/bin/env python
"""Agent acceptance — single command to run tests, pack, and verify.

Usage:
    python scripts/agent_acceptance.py                        # default fast acceptance
    python scripts/agent_acceptance.py --full                 # full pytest suite
    python scripts/agent_acceptance.py --full-groups          # full pytest in groups (diagnostic)
    python scripts/agent_acceptance.py --no-pack              # skip pack (debug only)
    python scripts/agent_acceptance.py --snapshot-mode        # validate unpacked snapshot (no git)
    python scripts/agent_acceptance.py --process              # real process tests only
    python scripts/agent_acceptance.py --stress               # race/stress tests only
    python scripts/agent_acceptance.py --area packaging       # focused area (packaging|ingest|discovery|security)
    python scripts/agent_acceptance.py --profile source       # source-only snapshot (git-tracked only)

Flags:
    --full                    Run the full pytest suite (excluding stress, external).
    --full-groups             Run full pytest in diagnostic groups (max 10 files each).
    --full-timeout-seconds N  Per-invocation timeout for --full / fast pytest (default 900).
    --group-timeout-seconds N Per-group timeout for --full-groups (default 300).
    --stop-on-first-failure   With fast groups or --full-groups, halt on first failing
                              group (forces sequential execution).
    --jobs N                  Parallelism: fast-gate group concurrency and xdist worker
                              count for --full. 0 = auto (default: min(12, cpus - 2)).
    --no-parallel             Force the legacy sequential code paths everywhere.
    --process                 Run only real process and cross-process tests.
    --stress                  Run only high-iteration race/stress tests.
    --area NAME               Run a focused area without relying on Git diff.
    --no-pack                 Skip pack_repo and snapshot verification (debug only).
    --profile {audit,source}  Source selection profile for the snapshot (default: audit).
    --snapshot-mode           Validate an unpacked audit snapshot. Skips git-dependent
                              checks when ``.git`` is absent; the pack step defaults to
                              audit profile (filesystem scan fallback).

All pytest subprocesses run with PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 so
third-party plugins cannot change behavior or hang the process; parallel runs
load pytest-xdist explicitly via ``-p xdist`` appended after the prefix.

Exit 0 on success, non-zero on failure.
"""
from __future__ import annotations

import argparse
from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import dataclass
import fnmatch
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

# Prevent the acceptance process AND all child subprocesses from writing
# .pyc / __pycache__ into the repository during project-module imports.
# sys.dont_write_bytecode covers the current process; PYTHONDONTWRITEBYTECODE
# in the environment covers child processes (pack_repo, compileall, etc.).
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.test_runtime_workspace import (
    TestRuntimeWorkspace,
    WorkspaceStatus,
    _system_temp_dir,
    inspect_workspace,
    scan_repo_bytecode,
)


# Layered fast acceptance tests — directories only, no individual files.
FAST_ACCEPTANCE_TESTS = [
    "tests/contract",
    "tests/security",
    "tests/hygiene",
    "tests/unit/test_repository_hygiene_policy.py",
    "tests/unit/test_pack_repo_rules.py",
    "tests/unit/test_v3_metadata_catalog_contracts.py",
    "tests/unit/test_reference_generation.py",
    "tests/unit/test_discovery_models.py",
    "tests/unit/test_keyword_notebook.py",
    "tests/unit/test_discovery_dual_lane_scheduler.py",
    "tests/unit/test_discovery_global_coordinator.py",
    "tests/unit/discovery",
    "tests/unit/test_catalog_folders.py",
    "tests/unit/test_catalog_registry_lifecycle.py",
    "tests/unit/test_formal_registry_errors.py",
    "tests/integration/test_frozen_v32_transaction_pipeline.py",
    "tests/integration/test_writing_metadata_catalog_roles.py",
    "tests/integration/test_rollback_cli.py",
    "tests/integration/test_server_security.py",
    "tests/integration/test_catalog_folder_classification_lifecycle.py",
    "tests/integration/test_catalog_doctor_fail_closed.py",
    "tests/integration/test_writer_catalog_safety_gate.py",
    "tests/integration/test_catalog_apply_recovery.py",
    "tests/integration/test_validate_v2_library.py",
]
# Logical groups for fast acceptance — each group runs as its own pytest
# invocation with a per-group timeout so a single hung test file doesn't
# silently consume the entire fast-gate timeout budget.
FAST_GROUPS: list[tuple[str, list[str]]] = [
    ("contract", ["tests/contract"]),
    ("security", ["tests/security"]),
    ("hygiene", ["tests/hygiene"]),
    ("catalog", [
        "tests/unit/test_catalog_folders.py",
        "tests/unit/test_catalog_registry_lifecycle.py",
        "tests/unit/test_formal_registry_errors.py",
        "tests/integration/test_catalog_folder_classification_lifecycle.py",
        "tests/integration/test_catalog_doctor_fail_closed.py",
        "tests/integration/test_catalog_apply_recovery.py",
    ]),
    ("discovery", [
        "tests/unit/test_discovery_models.py",
        "tests/unit/test_keyword_notebook.py",
        "tests/unit/test_discovery_dual_lane_scheduler.py",
        "tests/unit/test_discovery_global_coordinator.py",
        "tests/unit/discovery",
    ]),
    ("ingest", [
        "tests/unit/test_v3_metadata_catalog_contracts.py",
        "tests/unit/test_reference_generation.py",
        "tests/integration/test_frozen_v32_transaction_pipeline.py",
        "tests/integration/test_writing_metadata_catalog_roles.py",
        "tests/integration/test_validate_v2_library.py",
        "tests/integration/test_writer_catalog_safety_gate.py",
    ]),
    ("rollback-cli", ["tests/integration/test_rollback_cli.py"]),
    ("app-server", [
        "tests/integration/test_server_security.py",
    ]),
    ("packaging", [
        "tests/unit/test_repository_hygiene_policy.py",
        "tests/unit/test_pack_repo_rules.py",
    ]),
]
FAST_MARKERS = "not process and not slow and not stress and not external"
FULL_MARKERS = "not stress and not external"
# Full-gate split: the parallel chunk runs everything safe under pytest-xdist;
# the sequential residue keeps load-sensitive process/slow/performance tests
# deterministic.  The union of the two expressions selects exactly the same
# tests as FULL_MARKERS and their intersection is empty (asserted by
# tests/unit/test_acceptance_parallel.py).
FULL_PARALLEL_MARKERS = "not process and not slow and not stress and not external"
FULL_RESIDUE_MARKERS = "(process or slow) and not stress and not external"
PYTEST_PREFIX = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider"]


def _effective_jobs(jobs: int, *, no_parallel: bool) -> int:
    """Resolve the parallelism level from CLI flags.

    ``jobs == 0`` means auto: ``min(12, max(2, cpu_count - 2))``.  Capped at
    12 because pytest workers are I/O + process heavy; beyond that the wall
    clock is dominated by the slowest group/test, not core count.
    """
    if no_parallel:
        return 1
    if jobs < 0:
        raise SystemExit("--jobs must be >= 0")
    if jobs == 0:
        cpu = os.cpu_count() or 4
        return min(12, max(2, cpu - 2))
    return jobs


def step(name: str) -> None:
    print(f"\n{'='*60}", flush=True)
    print(f"  {name}", flush=True)
    print(f"{'='*60}", flush=True)


def run_command_with_timeout(
    command: list[str],
    *,
    timeout_seconds: int | None = None,
    cwd: Path | str = ROOT,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> int:
    """Run a command with a process-tree-safe timeout.

    This is the single runner used by every step of agent acceptance
    (compileall, pytest fast/full/full-groups, pack_repo). It starts the child
    in its own process group/session so a timeout can kill the WHOLE tree —
    pytest plus any subprocesses spawned by tests (audit scripts, conversion
    helpers). ``subprocess.run(timeout=...)`` only terminates the direct child,
    which on Windows leaves orphaned grandchildren holding inherited stdout
    handles; that can make a step appear to hang even after pytest prints its
    final result, and the timeout never visibly fires.

    Output is redirected to a temporary file, never a pipe, so descendants
    cannot keep an unread pipe endpoint open after the direct child exits.
    The file is replayed after completion and tailed in timeout diagnostics.
    """
    print(f"  $ {' '.join(command)}", flush=True)
    if timeout_seconds is not None:
        print(f"  timeout: {timeout_seconds}s", flush=True)
    log = tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace")
    popen_kwargs: dict = {
        "cwd": str(cwd), "env": env or os.environ.copy(),
        "stdout": log, "stderr": subprocess.STDOUT,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(command, **popen_kwargs)
    except FileNotFoundError as exc:
        log.close()
        print(f"\n[FAIL] could not start {command[0]}: {exc}", flush=True)
        if check:
            sys.exit(127)
        return 127

    timed_out = False
    interrupted: BaseException | None = None
    known_descendants: set[int] = set()
    try:
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        while proc.poll() is None:
            known_descendants.update(_descendant_pids(proc.pid))
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.05)
    except BaseException as exc:
        # KeyboardInterrupt, SystemExit, etc. — must still clean up.
        timed_out = True
        interrupted = exc
    finally:
        # Kill the process tree on timeout OR interrupt.  Using ``finally``
        # ensures cleanup runs even for BaseException (SIGTERM/KeyboardInterrupt)
        # which would otherwise bypass an ``except TimeoutExpired`` clause.
        if timed_out or proc.poll() is None:
            _kill_process_tree(proc)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                print(f"  [WARN] pid {proc.pid} did not exit after kill", flush=True)

    residual = sorted(pid for pid in known_descendants if _pid_state(pid) == "alive")
    if residual:
        _terminate_pids(residual)
    log.flush()
    log.seek(0)
    output = log.read()
    log.close()
    if output:
        try:
            print(output, end="" if output.endswith("\n") else "\n", flush=True)
        except UnicodeEncodeError:
            safe = output.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
                sys.stdout.encoding or "utf-8", errors="replace"
            )
            print(safe, end="" if safe.endswith("\n") else "\n", flush=True)
    if interrupted is not None:
        raise interrupted

    if timed_out:
        tail = output[-8000:]
        print(f"\n[TIMEOUT] command exceeded {timeout_seconds}s", flush=True)
        print(f"  pid: {proc.pid}", flush=True)
        print(f"  residual processes: {residual or 'none'}", flush=True)
        if tail:
            print("  last output:\n" + tail, flush=True)
        if check:
            sys.exit(124)
        return 124
    rc = proc.returncode
    if check and rc != 0:
        print(f"\n[FAIL] {command[0]} exited {rc}", flush=True)
        sys.exit(rc)
    return rc


def _descendant_pids(pid: int) -> set[int]:
    try:
        import psutil
        return {child.pid for child in psutil.Process(pid).children(recursive=True)}
    except Exception:
        return set()


def _terminate_pids(pids: list[int]) -> None:
    try:
        import psutil
        processes = [psutil.Process(pid) for pid in pids if psutil.pid_exists(pid)]
        for process in processes:
            process.terminate()
        _, alive = psutil.wait_procs(processes, timeout=3)
        for process in alive:
            process.kill()
        psutil.wait_procs(alive, timeout=3)
    except Exception:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass


def run(cmd: list[str], *, check: bool = True, env: dict[str, str] | None = None,
        timeout: int | None = None) -> int:
    """Backward-compatible wrapper around :func:`run_command_with_timeout`."""
    return run_command_with_timeout(cmd, timeout_seconds=timeout, env=env, check=check)


def run_command_captured(
    command: list[str],
    *,
    timeout_seconds: int | None = None,
    cwd: Path | str = ROOT,
    env: dict[str, str] | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[int, str]:
    """Like :func:`run_command_with_timeout` but silent: capture and return output.

    Used by the concurrent fast-gate scheduler so worker threads never write
    to the shared stdout; the orchestrator replays each group's buffered
    output in declaration order.  Returns ``(returncode, output)`` where the
    return code is 124 on timeout and 130 when ``cancel_event`` was set.
    Never calls ``sys.exit``.  The child runs in its own process group and
    the whole tree is killed on timeout/cancel, mirroring the sequential path.
    """
    log = tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace")
    popen_kwargs: dict = {
        "cwd": str(cwd), "env": env or os.environ.copy(),
        "stdout": log, "stderr": subprocess.STDOUT,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(command, **popen_kwargs)
    except FileNotFoundError as exc:
        log.close()
        return 127, f"[FAIL] could not start {command[0]}: {exc}\n"

    timed_out = False
    cancelled = False
    known_descendants: set[int] = set()
    try:
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        while proc.poll() is None:
            known_descendants.update(_descendant_pids(proc.pid))
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.05)
    finally:
        if timed_out or cancelled or proc.poll() is None:
            _kill_process_tree(proc)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass

    residual = sorted(pid for pid in known_descendants if _pid_state(pid) == "alive")
    if residual:
        _terminate_pids(residual)
    log.flush()
    log.seek(0)
    output = log.read()
    log.close()
    if cancelled:
        return 130, output + "\n[CANCELLED] group stopped by scheduler\n"
    if timed_out:
        return 124, output + f"\n[TIMEOUT] command exceeded {timeout_seconds}s\n"
    return proc.returncode, output


def _run_groups_concurrently(
    groups: list[tuple[str, list[str]]],
    run_one,
    *,
    jobs: int,
    on_result=None,
) -> list[tuple[str, int, str]]:
    """Run ``(name, paths)`` groups through ``run_one`` with bounded concurrency.

    ``run_one(name, paths, cancel_event) -> (rc, output)`` executes one group.
    Results are delivered strictly in declaration order — a group's result is
    reported only after every earlier group's result, so replayed output never
    interleaves even though execution overlaps.  A worker that raises is
    recorded as rc 1 with the exception text (workspace-cleanup failures fail
    the gate).  On KeyboardInterrupt the cancel event is set so running
    children are killed, queued groups are cancelled, and the interrupt is
    re-raised.
    """
    cancel = threading.Event()
    results: list[tuple[str, int, str]] = []
    pool = ThreadPoolExecutor(max_workers=max(1, jobs))
    futures = [pool.submit(run_one, name, paths, cancel) for name, paths in groups]
    try:
        for (name, _paths), future in zip(groups, futures):
            try:
                rc, output = future.result()
            except CancelledError:
                rc, output = 130, "[CANCELLED] group did not start\n"
            except Exception as exc:
                rc, output = 1, f"[FAIL] group runner raised: {exc!r}\n"
            results.append((name, rc, output))
            if on_result is not None:
                on_result(name, rc, output)
    except BaseException:
        cancel.set()
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return results


def verify_git_hygiene(root_path: Path | None = None) -> list[str]:
    """Check git tracked files for forbidden runtime assets.

    This enforces Git hygiene: real paper assets, runtime data, PDFs, metadata,
    catalogs, and conversion output must NOT be git-tracked.
    It checks ``git ls-files --cached``, not the zip file.

    Returns a list of error messages (empty = clean).
    """
    root = root_path or ROOT
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached"],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        if result.returncode != 0:
            return [f"git ls-files failed: {result.stderr.strip()}"]
        bad: list[str] = []
        for f in result.stdout.splitlines():
            if is_forbidden_git_member(f):
                bad.append(f"forbidden repository member in git index: {f}")
        return bad
    except FileNotFoundError:
        return ["git not available, cannot check git hygiene"]
    except Exception as e:
        return [f"git hygiene check failed: {e}"]


def verify_root_hygiene(root_path: Path | None = None) -> list[str]:
    """Reject root-level debug leftovers and unowned helper scripts."""
    root = root_path or ROOT
    denied_patterns = (
        "*.tmp",
        "*.bak",
        "*.orig",
        "*.rej",
        "loopback_check*.txt",
        "debug*.txt",
        "debug*.log",
        "trace*.log",
    )
    allowed_root_tools = {
        "start.bat",
        "start_fast_api_mode.bat",
    }
    bad: list[str] = []
    for path in sorted(p for p in root.iterdir() if p.is_file()):
        name = path.name
        lower = name.lower()
        if any(fnmatch.fnmatch(lower, pattern) for pattern in denied_patterns):
            bad.append(f"forbidden root temporary/debug file: {name}")
        if lower.endswith((".py", ".bat", ".cmd", ".ps1")) and name not in allowed_root_tools:
            bad.append(f"root tool must be moved under scripts/: {name}")
    return bad


from scripts.pack_repo import (
    LIGHTWEIGHT_ALLOWED_SUFFIXES,
    DENIED_NAMES as PACK_DENIED_NAMES,
    DENIED_PATH_PARTS as PACK_DENIED_PARTS,
    HEAVY_OR_BINARY_DENIED_SUFFIXES,
    SINGLE_FILE_MAX_BYTES,
    ZIP_MAX_BYTES,
    _verify_snapshot_self_check,
)
from src.utils.repository_hygiene import is_forbidden_git_member, is_forbidden_snapshot_member


# Snapshot verification delegates runtime classification to the shared policy.
# files (catalog, metadata, markdown, .paper.number markers, etc.).
# verify_snapshot() enforces this as a final gate: a file that is not
# heavy/binary, not a secret, and not a tombstone but lives under one of these
# dirs must still have a lightweight suffix — an unknown suffix like ``.exe``
# must be rejected here rather than slip through on pack rules alone.
def verify_snapshot(root_path: Path | None = None, *,
                    include_self_check: bool = True) -> list[str]:
    """Check mineru_snapshot.zip for runtime-zero snapshot compliance.

    The zip may contain source and synthetic test fixtures, but must NOT contain:
    - HEAVY_OR_BINARY_DENIED_SUFFIXES (PDFs, images, binaries, etc.)
    - DENIED_PATH_PARTS (cache, venv, output, images, logs, etc.)
    - DENIED_NAMES (secrets)
    - Files under forbidden prefixes (tmp, discovery, data/locks, etc.)
    - Tombstone files or backup artifacts

    ``include_self_check=False`` skips the packer's own full-member re-hash
    self check.  Acceptance step 5 uses that: step 4's pack_repo run already
    executed the identical self check on the freshly packed zip, so running
    it a second time in the same acceptance run is pure duplication.
    Standalone callers keep the default (True).
    """
    root = root_path or ROOT
    zip_path = root / "mineru_snapshot.zip"
    if not zip_path.exists():
        return ["mineru_snapshot.zip not found"]

    ALLOWED = {
        # .gitkeep files are always allowed
        "data/papers/.gitkeep",
        "data/paper_raw/.gitkeep",
        "data/raw/.gitkeep",
        "data/raw_all/.gitkeep",
        "data/tmp/.gitkeep",
        "data/logs/.gitkeep",
        "data/jobs/.gitkeep",
        "data/transactions/.gitkeep",
        "data/import_work/.gitkeep",
        "data/discovery/doi_candidates/.gitkeep",
        "data/discovery/pdf_fetch_logs/.gitkeep",
        "data/discovery/pending_pages/.gitkeep",
        "data/discovery/locks/.gitkeep",
        "data/discovery/exports/.gitkeep",
        "data/discovery/queries/.gitkeep",
        "data/discovery/queries/keywords.example.txt",
        "write/jobs/.gitkeep",
        "reports/.gitkeep",
    }

    FORBIDDEN_PREFIXES = [
        ".reasonix/",
        "data/tmp/",
        "data/logs/",
        "data/jobs/",
        "data/transactions/",
        "data/papers/",
        "data/paper_raw/",
        "data/raw/",
        "data/raw_all/",
        "data/import_work/",
        "data/discovery/doi_candidates/",
        "data/discovery/pdf_fetch_logs/",
        "data/discovery/fetch_logs/",
        "data/discovery/keyword_notebooks/",
        "data/discovery/pending_pages/",
        "data/discovery/locks/",
        "data/discovery/exports/",
        "data/discovery/queries/",
        "write/jobs/",
        "output/",
        "reports/",
    ]

    bad: list[str] = []
    manifest = None
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if name == "snapshot_manifest.json":
                try: manifest = json.loads(zf.read(name).decode("utf-8"))
                except Exception as exc: bad.append(f"invalid snapshot manifest: {exc}")
                continue
            if is_forbidden_snapshot_member(name):
                bad.append(f"forbidden prefix (runtime-zero): {name}")
                continue
            if name in ALLOWED:
                continue
            parts = Path(name).parts

            # Denied path components (cache, venv, output, images, etc.)
            for denied_part in PACK_DENIED_PARTS:
                if denied_part in parts:
                    bad.append(f"denied path component '{denied_part}': {name}")
                    break
            else:
                # Prefix check
                for prefix in FORBIDDEN_PREFIXES:
                    if name.startswith(prefix):
                        bad.append(f"forbidden prefix: {name}")
                        break
                else:
                    ext = Path(name).suffix.lower()
                    # Heavy/binary denied suffixes
                    if ext in HEAVY_OR_BINARY_DENIED_SUFFIXES:
                        bad.append(f"heavy/binary denied suffix {ext}: {name}")
                        continue
                    # Secrets / denied names
                    if Path(name).name in PACK_DENIED_NAMES:
                        bad.append(f"denied file name: {name}")
                        continue
                    # Tombstone files
                    if name.endswith("._deleted") or name.endswith(".py._deleted"):
                        bad.append(f"tombstone file: {name}")
                        continue
    if manifest is None:
        bad.append("snapshot_manifest.json missing or unreadable")
    elif manifest.get("runtime_files_included") != 0:
        bad.append("snapshot manifest runtime_files_included is not zero")

    if include_self_check:
        bad.extend(_verify_snapshot_self_check(zip_path))
    return list(dict.fromkeys(bad))


def _chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _pid_state(pid: int) -> str:
    """Return the state of a PID: ``"alive"``, ``"zombie"``, or ``"dead"``.

    POSIX: reads ``/proc/<pid>/stat`` — the state field ``"Z"`` means the
    process has exited but its entry persists until the parent calls
    ``wait()``.  A zombie is **not** alive (it holds no resources except a
    PID slot) and must not be treated as a running descendant.

    Windows: uses ``OpenProcess`` + ``GetExitCodeProcess`` with the
    ``STILL_ACTIVE`` (259) sentinel.  Windows does not have POSIX zombies;
    an exited process simply fails to open.

    On POSIX without ``/proc`` (e.g. macOS), falls back to ``os.kill(pid, 0)``.
    ``PermissionError`` means the process exists but is owned by another user
    — it is **alive**, not dead.
    """
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
        )
        if not handle:
            return "dead"
        try:
            exit_code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            return "alive" if exit_code.value == STILL_ACTIVE else "dead"
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        stat_path = Path(f"/proc/{pid}/stat")
        if stat_path.exists():
            try:
                stat = stat_path.read_text()
                # /proc/<pid>/stat: pid (comm) state ...
                # state is the first char after the last ')'
                state = stat.rsplit(")", 1)[1].strip()[0]
                if state == "Z":
                    return "zombie"
                return "alive"
            except (OSError, IndexError):
                return "dead"
        # No /proc (e.g. macOS) — fall back to signal probe.
        try:
            os.kill(pid, 0)
            return "alive"
        except ProcessLookupError:
            return "dead"
        except PermissionError:
            # Process exists but we can't signal it — still alive.
            return "alive"


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill a process and all its descendants.

    POSIX: the child is the leader of its own session (``start_new_session=True``),
    so ``os.killpg`` kills the whole group. Windows: ``taskkill /T /F`` kills the
    process tree (the child was started with ``CREATE_NEW_PROCESS_GROUP``).
    Falls back to ``proc.kill()`` if the platform-specific call fails.
    """
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=15)
            return
        except Exception:
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def _run_fast_groups(*, stop_on_first_failure: bool = False,
                     group_timeout: int | None = None,
                     jobs: int = 1) -> int:
    """Run fast acceptance tests in diagnostic groups.

    Each group is a separate pytest invocation with its own isolated
    workspace and timeout so a single hung test file never consumes the full
    fast-gate budget.  With ``jobs > 1`` the groups run as concurrent
    subprocesses (one thread per group, each with its own
    ``TestRuntimeWorkspace``, ``--basetemp``, environment, and process tree);
    buffered output is replayed in declaration order.
    ``--stop-on-first-failure`` forces sequential execution for deterministic
    halt semantics.
    """
    flattened = [path for _, paths in FAST_GROUPS for path in paths]
    expected = set(FAST_ACCEPTANCE_TESTS)
    if set(flattened) != expected or len(flattened) != len(expected):
        raise RuntimeError(
            "FAST_GROUPS does not cover exactly FAST_ACCEPTANCE_TESTS. "
            f"Missing: {expected - set(flattened)}, "
            f"Extra: {set(flattened) - expected}"
        )

    if jobs > 1 and not stop_on_first_failure:
        return _run_fast_groups_parallel(group_timeout=group_timeout, jobs=jobs)

    all_ok = True
    for gname, gpaths in FAST_GROUPS:
        print(f"\n{'─'*60}", flush=True)
        print(f"  fast group: {gname}", flush=True)
        print(f"  paths: {len(gpaths)}", flush=True)
        for p in gpaths:
            print(f"    - {p}", flush=True)
        cmd = PYTEST_PREFIX + ["-q", "-m", FAST_MARKERS] + gpaths \
              + ["--durations=20"]
        cmd_display = ' '.join(cmd)
        print(f"  cmd: {cmd_display}", flush=True)
        if group_timeout:
            print(f"  timeout: {group_timeout}s", flush=True)
        print(f"{'─'*60}", flush=True)

        try:
            with TestRuntimeWorkspace(group=f"fast_{gname}") as ws:
                full_cmd = cmd + ["--basetemp", str(ws.pytest_dir)]
                group_env = ws.child_env()
                rc = run_command_with_timeout(
                    full_cmd,
                    timeout_seconds=group_timeout,
                    env=group_env,
                    check=False,
                )
        except KeyboardInterrupt:
            print(f"\n[FAIL] fast group {gname} interrupted", flush=True)
            return 1

        if rc == 124:
            print(f"\n[FAIL] fast group {gname} timed out after {group_timeout}s", flush=True)
            print(f"  last file: {gpaths[-1]}", flush=True)
            print(f"  reproduce: {cmd_display}", flush=True)
            all_ok = False
            if stop_on_first_failure:
                print("\n  [STOP] --stop-on-first-failure set, halting", flush=True)
                return 1
            continue

        if rc != 0:
            print(f"\n[FAIL] fast group {gname} exited {rc}", flush=True)
            print(f"  reproduce: {cmd_display}", flush=True)
            all_ok = False
            if stop_on_first_failure:
                print("\n  [STOP] --stop-on-first-failure set, halting", flush=True)
                return 1

    if all_ok:
        print("\n  [OK] all fast groups passed", flush=True)
        return 0

    print("\n  [WARN] some fast groups failed", flush=True)
    return 1


def _run_fast_groups_parallel(*, group_timeout: int | None, jobs: int) -> int:
    """Concurrent fast gate: every group keeps its own workspace and timeout."""
    worker_count = min(len(FAST_GROUPS), jobs)
    print(f"  parallel fast gate: {len(FAST_GROUPS)} groups, {worker_count} workers",
          flush=True)

    def _group_cmd(gpaths: list[str]) -> list[str]:
        return PYTEST_PREFIX + ["-q", "-m", FAST_MARKERS] + gpaths + ["--durations=20"]

    def _run_one(gname: str, gpaths: list[str],
                 cancel: threading.Event) -> tuple[int, str]:
        with TestRuntimeWorkspace(group=f"fast_{gname}") as ws:
            return run_command_captured(
                _group_cmd(gpaths) + ["--basetemp", str(ws.pytest_dir)],
                timeout_seconds=group_timeout,
                env=ws.child_env(),
                cancel_event=cancel,
            )

    failures: list[str] = []

    def _report(gname: str, rc: int, output: str) -> None:
        gpaths = dict(FAST_GROUPS)[gname]
        cmd_display = " ".join(_group_cmd(gpaths))
        print(f"\n{'─'*60}", flush=True)
        print(f"  fast group: {gname}", flush=True)
        print(f"  cmd: {cmd_display}", flush=True)
        if group_timeout:
            print(f"  timeout: {group_timeout}s", flush=True)
        print(f"{'─'*60}", flush=True)
        if output:
            try:
                print(output, end="" if output.endswith("\n") else "\n", flush=True)
            except UnicodeEncodeError:
                encoding = sys.stdout.encoding or "utf-8"
                safe = output.encode(encoding, errors="replace").decode(
                    encoding, errors="replace")
                print(safe, end="" if safe.endswith("\n") else "\n", flush=True)
        if rc == 124:
            print(f"\n[FAIL] fast group {gname} timed out after {group_timeout}s",
                  flush=True)
            print(f"  reproduce: {cmd_display}", flush=True)
            failures.append(gname)
        elif rc != 0:
            print(f"\n[FAIL] fast group {gname} exited {rc}", flush=True)
            print(f"  reproduce: {cmd_display}", flush=True)
            failures.append(gname)

    try:
        _run_groups_concurrently(
            list(FAST_GROUPS), _run_one, jobs=worker_count, on_result=_report,
        )
    except KeyboardInterrupt:
        print("\n[FAIL] fast groups interrupted", flush=True)
        return 1

    if not failures:
        print(f"\n  [OK] all fast groups passed ({worker_count} workers)", flush=True)
        return 0
    print(f"\n  [WARN] fast groups failed: {', '.join(failures)}", flush=True)
    return 1


def _run_full_groups(*, stop_on_first_failure: bool = False,
                     group_timeout: int | None = None) -> int:
    """Run pytest in diagnostic groups, max 10 files each.

    Prints group name, file count, and full pytest command before each group,
    streams output live, and prints return code + command on failure.

    ``group_timeout`` (seconds) kills a group that hangs so the agent gets a
    clear error instead of waiting forever.
    """
    all_test_files = [
        p.relative_to(ROOT).as_posix()
        for p in sorted((ROOT / "tests").rglob("test_*.py"))
    ]
    buckets: dict[str, list[str]] = {}
    for path in all_test_files:
        parts = Path(path).parts
        layer = parts[1] if len(parts) > 2 else "root"
        buckets.setdefault(layer, []).append(path)
    groups: list[tuple[str, list[str]]] = []
    preferred = ["contract", "security", "hygiene", "unit", "integration", "e2e", "slow", "legacy", "root"]
    for layer in preferred + sorted(set(buckets) - set(preferred)):
        for idx, chunk in enumerate(_chunked(buckets.get(layer, []), 10), 1):
            groups.append((f"{layer}_{idx:02d}", chunk))
    flattened = [path for _, paths in groups for path in paths]
    if set(flattened) != set(all_test_files) or len(flattened) != len(set(flattened)):
        raise RuntimeError("full-groups test discovery omitted or duplicated files")

    all_ok = True
    for gname, gpaths in groups:
        print(f"\n{'─'*60}", flush=True)
        print(f"  group: {gname}", flush=True)
        print(f"  files: {len(gpaths)}", flush=True)
        for p in gpaths:
            print(f"    - {p}", flush=True)
        cmd = PYTEST_PREFIX + ["-q", "-m", FULL_MARKERS] + gpaths \
              + ["--durations=30", "--durations-min=0.5"]
        cmd_display = ' '.join(cmd)
        print(f"  cmd: {cmd_display}", flush=True)
        if group_timeout:
            print(f"  timeout: {group_timeout}s", flush=True)
        print(f"{'─'*60}", flush=True)

        try:
            with TestRuntimeWorkspace(group=f"full_{gname}") as ws:
                full_cmd = cmd + ["--basetemp", str(ws.pytest_dir)]
                group_env = ws.child_env()
                rc = run_command_with_timeout(
                    full_cmd,
                    timeout_seconds=group_timeout,
                    env=group_env,
                    check=False,
                )
        except KeyboardInterrupt:
            print(f"\n[FAIL] group {gname} interrupted", flush=True)
            return 1

        if rc == 124:
            print(f"\n[FAIL] group {gname} timed out after {group_timeout}s", flush=True)
            print(f"  cmd: {cmd_display}", flush=True)
            print(f"  reproduce: {cmd_display}", flush=True)
            all_ok = False
            if stop_on_first_failure:
                print("\n  [STOP] --stop-on-first-failure set, halting", flush=True)
                return 1
            continue

        if rc != 0:
            print(f"\n[FAIL] group {gname} exited {rc}", flush=True)
            print(f"  cmd: {cmd_display}", flush=True)
            all_ok = False
            if stop_on_first_failure:
                print("\n  [STOP] --stop-on-first-failure set, halting", flush=True)
                return 1

    if all_ok:
        print("\n  [OK] all groups passed", flush=True)
        return 0

    print("\n  [WARN] some groups failed", flush=True)
    return 1

# Module-level counter set by _check_python_syntax
_syntax_file_count: int = 0


def _check_python_syntax() -> list[str]:
    """Read-only syntax check for every .py file under scripts/, src/, tests/.

    Uses ``compile(source, filename, 'exec')`` which **never** writes a .pyc
    file or ``__pycache__`` directory.  Returns a list of error messages
    (empty = clean).
    """
    global _syntax_file_count
    errors: list[str] = []
    count = 0
    for top in ("scripts", "src", "tests"):
        top_path = ROOT / top
        if not top_path.is_dir():
            continue
        for py_file in sorted(top_path.rglob("*.py")):
            # Skip runtime dirs that happen to contain .py files
            rel = py_file.relative_to(ROOT)
            if rel.parts and rel.parts[0] in ("data", "output", "reports", "write"):
                continue
            # Skip __pycache__ (leftover from previous runs)
            if "__pycache__" in rel.parts:
                continue
            try:
                source = py_file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                try:
                    source = py_file.read_text(encoding="utf-8", errors="ignore")
                except Exception as exc:
                    errors.append(f"{rel}: cannot read: {exc}")
                    continue
            try:
                compile(source, str(py_file), "exec")
            except SyntaxError as exc:
                errors.append(f"{rel}:{exc.lineno}: {exc.msg}")
            count += 1
    _syntax_file_count = count
    return errors


@dataclass(frozen=True)
class PollutionSnapshot:
    """Immutable snapshot of cache pollution on disk.

    All paths are relative to the repository root (for repo items) or
    absolute (for drive-root items).
    """
    repo_pycache: frozenset[str]   # relative paths like "scripts/__pycache__"
    repo_pyc: frozenset[str]       # relative paths like "scripts/foo.pyc"
    root_pollution: frozenset[str] # absolute paths on drive root
    workspace_issues: frozenset[str] = frozenset()  # invalid/stale temp workspaces

    @property
    def is_clean(self) -> bool:
        return not (
            self.repo_pycache or self.repo_pyc or self.root_pollution
            or self.workspace_issues
        )


def _collect_temp_workspace_issues(temp_dir: Path | None = None) -> frozenset[str]:
    """Report stale or untrusted ``mineru_*`` temp entries without mutation."""
    root = temp_dir or _system_temp_dir()
    issues: list[str] = []
    try:
        entries = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        return frozenset({f"{root} [scan_error] {exc}"})
    for entry in entries:
        if not entry.name.startswith("mineru_") or entry.name == "mineru_cleanup_reports":
            continue
        inspection = inspect_workspace(entry, repo_root=ROOT)
        if inspection.status in {
            WorkspaceStatus.STALE,
            WorkspaceStatus.INVALID,
            WorkspaceStatus.UNRECOGNIZED,
        }:
            issues.append(
                f"{entry} [{inspection.status.value}] {inspection.reason}"
            )
    return frozenset(issues)


def _collect_pollution_snapshot() -> PollutionSnapshot:
    """Return a :class:`PollutionSnapshot` of the current on-disk state.

    Read-only — never mutates, never deletes.
    """
    pycache, pyc = scan_repo_bytecode(ROOT)
    root_pol: list[str] = []
    try:
        from scripts.test_runtime_workspace import _system_temp_dir, _flatten_path
        temp_dir = _system_temp_dir()
        flattened_prefix = _flatten_path(temp_dir)
        drive_root = Path(temp_dir.anchor) if temp_dir.anchor else None
        if drive_root is not None and drive_root.exists():
            pattern = f"{flattened_prefix}mineru_"
            cache_suffix = "cache"
            for entry in drive_root.iterdir():
                if entry.is_dir() and entry.name.startswith(pattern) and entry.name.endswith(cache_suffix):
                    root_pol.append(str(entry))
    except Exception:
        pass
    return PollutionSnapshot(
        repo_pycache=frozenset(pycache),
        repo_pyc=frozenset(pyc),
        root_pollution=frozenset(root_pol),
        workspace_issues=_collect_temp_workspace_issues(),
    )


# Pre-flight snapshot (populated in main before any test step runs).
_pollution_before: PollutionSnapshot | None = None


def _pollution_pre_check() -> list[str]:
    """Check for pre-existing cache pollution before running tests.

    Fails closed on ANY existing pollution.  The repository must be clean
    before acceptance begins.  Returns a list of error messages (empty = clean).
    """
    global _pollution_before
    snap = _collect_pollution_snapshot()
    _pollution_before = snap

    errors: list[str] = []

    if snap.root_pollution:
        errors.append(
            f"{len(snap.root_pollution)} legacy flattened cache directories on "
            f"drive root.  Run: python scripts/cleanup_test_caches.py "
            f"--legacy-flattened-root  (add --apply to delete)\n"
            f"    Paths: {', '.join(sorted(snap.root_pollution)[:20])}"
        )

    if snap.workspace_issues:
        paths = "    " + "\n    ".join(sorted(snap.workspace_issues)[:20])
        errors.append(
            f"{len(snap.workspace_issues)} stale or untrusted mineru test "
            "workspace entries under system temp. Run the cleanup command "
            "without --apply to classify them; only valid stale markers are "
            "eligible for automatic deletion. Invalid/unrecognized entries "
            f"require manual ownership review:\n{paths}"
        )

    if snap.repo_pycache:
        paths = "    " + "\n    ".join(sorted(snap.repo_pycache)[:30])
        errors.append(
            f"{len(snap.repo_pycache)} __pycache__ dir(s) in repo — "
            f"remove before acceptance:\n{paths}"
        )

    if snap.repo_pyc:
        paths = "    " + "\n    ".join(sorted(snap.repo_pyc)[:30])
        errors.append(
            f"{len(snap.repo_pyc)} .pyc file(s) in repo — "
            f"remove before acceptance:\n{paths}"
        )

    return errors


def _pollution_post_check(*, label: str = "post") -> list[str]:
    """Check for cache pollution created DURING this acceptance run.

    Uses path-set diffs against the pre-flight snapshot so only items
    created since pre-flight are reported.  Returns a list of error
    messages (empty = clean).
    """
    errors: list[str] = []
    now = _collect_pollution_snapshot()
    before = _pollution_before
    if before is None:
        errors.append(f"[{label}] no pre-flight snapshot — cannot diff")
        return errors

    new_pycache = now.repo_pycache - before.repo_pycache
    new_pyc = now.repo_pyc - before.repo_pyc
    new_root = now.root_pollution - before.root_pollution
    new_workspace_issues = now.workspace_issues - before.workspace_issues

    if new_root:
        errors.append(
            f"[{label}] {len(new_root)} new legacy flattened cache "
            f"directories: {', '.join(sorted(new_root)[:10])}"
        )

    if new_workspace_issues:
        errors.append(
            f"[{label}] {len(new_workspace_issues)} new stale or untrusted "
            "mineru temp workspace entries: "
            f"{', '.join(sorted(new_workspace_issues)[:10])}"
        )

    if new_pycache:
        errors.append(
            f"[{label}] {len(new_pycache)} __pycache__ dir(s) created: "
            f"{', '.join(sorted(new_pycache)[:15])}"
        )

    if new_pyc:
        errors.append(
            f"[{label}] {len(new_pyc)} .pyc file(s) created: "
            f"{', '.join(sorted(new_pyc)[:15])}"
        )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent acceptance")
    parser.add_argument("--full", action="store_true",
                        help="Run full pytest suite instead of fast acceptance")
    parser.add_argument("--process", action="store_true",
                        help="Run only real process and cross-process tests")
    parser.add_argument("--stress", action="store_true",
                        help="Run only high-iteration race/stress tests")
    parser.add_argument("--area", choices=["packaging", "ingest", "discovery", "security"],
                        help="Run a focused area without relying on Git diff")
    parser.add_argument("--full-groups", action="store_true",
                        help="Run full pytest in groups with per-group diagnostics")
    parser.add_argument("--stop-on-first-failure", action="store_true",
                        help="With fast groups or --full-groups, halt on the first "
                             "failing group (forces sequential execution)")
    parser.add_argument("--jobs", type=int, default=0,
                        help="Parallelism: fast-gate group concurrency and xdist "
                             "worker count for --full. 0 = auto (min(12, cpus-2))")
    parser.add_argument("--no-parallel", action="store_true",
                        help="Force the legacy sequential code paths everywhere")
    parser.add_argument("--group-timeout-seconds", type=int, default=300,
                        help="Per-group timeout in seconds for --full-groups (default 300)")
    parser.add_argument("--full-timeout-seconds", type=int, default=900,
                        help="Per-invocation timeout for --full / fast pytest subprocesses "
                             "in seconds (default 900; the sequential process/slow residue "
                             "measures ~6-10 min under load)")
    parser.add_argument("--no-pack", action="store_true",
                        help="Skip pack_repo and snapshot verification (debug only; not valid final acceptance)")
    parser.add_argument("--profile", type=str, default="audit", choices=["audit", "source"],
                        help='Source selection profile; every profile is strictly runtime-zero.')
    parser.add_argument("--snapshot-mode", action="store_true",
                        help="Validate an unpacked audit snapshot without requiring .git metadata.")
    args = parser.parse_args()
    selected_modes = sum(bool(x) for x in (args.full, args.process, args.stress, args.area, args.full_groups))
    if selected_modes > 1:
        parser.error("choose only one of --full, --process, --stress, --area, --full-groups")

    # 0. Pre-flight pollution check (fail closed on any pollution).
    step("0/6 pre-flight pollution check")
    if not sys.dont_write_bytecode:
        print("\n[FAIL] sys.dont_write_bytecode is not set — acceptance "
              "process would pollute the repository", flush=True)
        return 1
    pre_errors = _pollution_pre_check()
    if pre_errors:
        print("\n[FAIL] Pre-existing cache pollution detected:", flush=True)
        for e in pre_errors:
            print(f"  - {e}", flush=True)
        return 1
    print("  [OK] pre-flight clean", flush=True)

    # 1. Syntax gate — verify every .py file compiles without writing bytecode.
    # Uses compile(source, filename, 'exec') which never touches disk.
    step("1/6 syntax gate")
    syntax_errors = _check_python_syntax()
    if syntax_errors:
        print("\n[FAIL] Python syntax errors detected:", flush=True)
        for e in syntax_errors[:20]:
            print(f"  - {e}", flush=True)
        if len(syntax_errors) > 20:
            print(f"  ... {len(syntax_errors) - 20} more", flush=True)
        return 1
    print(f"  [OK] {_syntax_file_count} files passed", flush=True)

    # 2. Test suite
    if args.process:
        step("2/6 pytest --process")
        with TestRuntimeWorkspace(group="process") as ws:
            run(PYTEST_PREFIX + ["-q", "-m",
                 "process and not stress and not external", "--durations=20",
                 "--basetemp", str(ws.pytest_dir)],
                env=ws.child_env(), timeout=args.full_timeout_seconds)
    elif args.stress:
        step("2/6 pytest --stress")
        seed = os.environ.get("MINERU_STRESS_SEED", "20260711")
        print(f"  stress seed: {seed}", flush=True)
        with TestRuntimeWorkspace(group="stress") as ws:
            env = ws.child_env(extra={"MINERU_STRESS_SEED": seed})
            run(PYTEST_PREFIX + ["-q", "-m", "stress",
                 "--durations=20", "--basetemp", str(ws.pytest_dir)],
                env=env, timeout=args.full_timeout_seconds)
    elif args.area:
        step(f"2/6 pytest --area {args.area}")
        area_tests = {
            "packaging": ["tests/unit/test_pack_repo_rules.py", "tests/hygiene/test_snapshot_hygiene.py", "tests/unit/test_repository_hygiene_policy.py"],
            "ingest": ["tests/contract/test_ingest_locking_contract.py", "tests/integration/test_frozen_v32_transaction_pipeline.py"],
            "discovery": ["tests/unit/discovery", "tests/contract/test_discovery_reconciliation.py"],
            "security": ["tests/security", "tests/hygiene/test_snapshot_hygiene.py"],
        }
        with TestRuntimeWorkspace(group=f"area_{args.area}") as ws:
            run(PYTEST_PREFIX + ["-q", "-m", FAST_MARKERS] + area_tests[args.area]
                + ["--basetemp", str(ws.pytest_dir)],
                env=ws.child_env(), timeout=args.full_timeout_seconds)
    elif args.full_groups:
        step("2/6 pytest --full-groups")
        rc = _run_full_groups(stop_on_first_failure=args.stop_on_first_failure,
                              group_timeout=args.group_timeout_seconds)
        if rc != 0:
            return rc
    elif args.full:
        jobs = _effective_jobs(args.jobs, no_parallel=args.no_parallel)
        xdist_available = importlib.util.find_spec("xdist") is not None
        if jobs > 1 and not xdist_available:
            print("\n  [WARN] pytest-xdist not installed — falling back to the "
                  "sequential full gate", flush=True)
        if jobs > 1 and xdist_available:
            step(f"2/6 pytest --full (parallel chunk, -n {jobs})")
            with TestRuntimeWorkspace(group="full_parallel") as ws:
                run(PYTEST_PREFIX + ["-p", "xdist", "-n", str(jobs),
                     "-q", "-m", FULL_PARALLEL_MARKERS,
                     "--durations=30", "--durations-min=0.5",
                     "--basetemp", str(ws.pytest_dir)],
                    env=ws.child_env(), timeout=args.full_timeout_seconds)
            step("2/6 pytest --full (sequential residue: process/slow)")
            with TestRuntimeWorkspace(group="full_residue") as ws:
                run(PYTEST_PREFIX + ["-q", "-m", FULL_RESIDUE_MARKERS,
                     "--durations=30", "--durations-min=0.5",
                     "--basetemp", str(ws.pytest_dir)],
                    env=ws.child_env(), timeout=args.full_timeout_seconds)
        else:
            step("2/6 pytest --full")
            with TestRuntimeWorkspace(group="full") as ws:
                run(PYTEST_PREFIX + ["-q", "-m", FULL_MARKERS,
                     "--durations=30", "--durations-min=0.5",
                     "--basetemp", str(ws.pytest_dir)],
                    env=ws.child_env(), timeout=args.full_timeout_seconds)
    else:
        step("2/6 pytest (fast acceptance — groups)")
        rc = _run_fast_groups(stop_on_first_failure=args.stop_on_first_failure,
                              group_timeout=args.group_timeout_seconds,
                              jobs=_effective_jobs(args.jobs,
                                                   no_parallel=args.no_parallel))
        if rc != 0:
            return rc

    if args.no_pack:
        post_errors = _pollution_post_check(label="post-tests")
        if post_errors:
            print("\n[FAIL] Cache pollution detected after test run:")
            for error in post_errors:
                print(f"  - {error}")
            return 1
        print("\n  [OK] tests passed (--no-pack, skipping pack)")
        return 0

    # 3. Git/root hygiene check
    step("3/6 hygiene")
    bad_hygiene = []
    # Auto-detect snapshot mode when .git is absent (e.g. unpacked ZIP)
    snapshot_mode = args.snapshot_mode or not (ROOT / ".git").exists()
    if snapshot_mode:
        print("  [OK] snapshot mode: .git absent, skipping git index hygiene")
    else:
        bad_hygiene.extend(verify_git_hygiene())
    bad_hygiene.extend(verify_root_hygiene())
    if bad_hygiene:
        print("\n[FAIL] Hygiene check failed:")
        for b in bad_hygiene:
            print(f"  - {b}")
        return 1
    print("  [OK] hygiene clean")

    # 4. Pack repo
    step("4/6 pack_repo")
    run([sys.executable, "scripts/pack_repo.py", "--profile", args.profile])

    # 5. Verify snapshot (structural checks only — step 4's pack_repo already
    # ran the full self check on this exact zip immediately before install).
    step("5/6 verify snapshot")
    bad = verify_snapshot(include_self_check=False)
    if bad:
        print("\n[FAIL] Snapshot contains forbidden content:")
        for b in bad:
            print(f"  - {b}")
        return 1

    # 6. Post-test pollution check
    step("6/6 post-test pollution check")
    post_errors = _pollution_post_check()
    if post_errors:
        print("\n[FAIL] Cache pollution detected after test run:")
        for e in post_errors:
            print(f"  - {e}")
        return 1
    print("  [OK] no pollution detected")

    print("\n" + "=" * 60)
    if args.full:
        print("  [OK] full pytest passed")
    if args.full_groups:
        print("  [OK] full-groups pytest passed")
    print("  [OK] agent acceptance passed")
    print(f"  [OK] Packed: mineru_snapshot.zip")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

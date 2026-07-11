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
    --full-timeout-seconds N  Timeout for --full / fast pytest (default 600).
    --group-timeout-seconds N Per-group timeout for --full-groups (default 300).
    --stop-on-first-failure   With --full-groups, halt on first failing group.
    --process                 Run only real process and cross-process tests.
    --stress                  Run only high-iteration race/stress tests.
    --area NAME               Run a focused area without relying on Git diff.
    --no-pack                 Skip pack_repo and snapshot verification (debug only).
    --profile {audit,source}  Source selection profile for the snapshot (default: audit).
    --snapshot-mode           Validate an unpacked audit snapshot. Skips git-dependent
                              checks when ``.git`` is absent; the pack step defaults to
                              audit profile (filesystem scan fallback).

All pytest subprocesses run with PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 so
third-party plugins cannot change behavior or hang the process.

Exit 0 on success, non-zero on failure.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _pytest_env() -> dict[str, str]:
    """Isolated environment for pytest subprocesses.

    Disables entry-point plugin auto-loading so third-party plugins installed
    in the environment cannot change warning/asyncio/coverage/tracing behavior
    or cause the pytest process to hang after tests pass. conftest.py files,
    ``-p`` options, and ``PYTEST_PLUGINS`` are unaffected.
    """
    env = os.environ.copy()
    env.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    return env

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
    "tests/unit/discovery",
    "tests/integration/test_frozen_v32_transaction_pipeline.py",
    "tests/integration/test_writing_metadata_catalog_roles.py",
]
FAST_MARKERS = "not process and not slow and not stress and not external"
FULL_MARKERS = "not stress and not external"


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
        print(output, end="" if output.endswith("\n") else "\n", flush=True)
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
        "app.py",
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
from src.services.repository_hygiene import is_forbidden_git_member, is_forbidden_snapshot_member


# Snapshot verification delegates runtime classification to the shared policy.
# files (catalog, metadata, markdown, .paper.number markers, etc.).
# verify_snapshot() enforces this as a final gate: a file that is not
# heavy/binary, not a secret, and not a tombstone but lives under one of these
# dirs must still have a lightweight suffix — an unknown suffix like ``.exe``
# must be rejected here rather than slip through on pack rules alone.
def verify_snapshot(root_path: Path | None = None) -> list[str]:
    """Check mineru_snapshot.zip for runtime-zero snapshot compliance.

    The zip may contain source and synthetic test fixtures, but must NOT contain:
    - HEAVY_OR_BINARY_DENIED_SUFFIXES (PDFs, images, binaries, etc.)
    - DENIED_PATH_PARTS (cache, venv, output, images, logs, etc.)
    - DENIED_NAMES (secrets)
    - Files under forbidden prefixes (tmp, discovery, data/locks, etc.)
    - Tombstone files or backup artifacts
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
        group_tmp = tempfile.TemporaryDirectory(prefix=f"mineru_{gname}_")
        temp_root = Path(group_tmp.name)
        cmd = [sys.executable, "-m", "pytest", "-q", "-m", FULL_MARKERS] + gpaths \
              + ["--durations=30", "--durations-min=0.5", "--basetemp", str(temp_root / "pytest")]
        print(f"  cmd: {' '.join(cmd)}", flush=True)
        if group_timeout:
            print(f"  timeout: {group_timeout}s", flush=True)
        print(f"{'─'*60}", flush=True)

        # Use the same process-tree-safe runner as fast/full mode so a timeout
        # kills the whole pytest process tree (pytest + any subprocesses
        # spawned by tests). check=False so a failing group does not sys.exit
        # the whole acceptance run; we report and continue to the next group.
        try:
            group_env = _pytest_env()
            group_env.update({
                "TMP": str(temp_root),
                "TEMP": str(temp_root),
                "PYTEST_ADDOPTS": f"-o cache_dir={temp_root / 'cache'}",
            })
            rc = run_command_with_timeout(
                cmd,
                timeout_seconds=group_timeout,
                env=group_env,
                check=False,
            )
        except KeyboardInterrupt:
            print(f"\n[FAIL] group {gname} interrupted", flush=True)
            return 1
        finally:
            group_tmp.cleanup()

        if rc == 124:
            print(f"\n[FAIL] group {gname} timed out after {group_timeout}s", flush=True)
            print(f"  cmd: {' '.join(cmd)}", flush=True)
            print(f"  reproduce: {' '.join(cmd)}", flush=True)
            all_ok = False
            if stop_on_first_failure:
                print("\n  [STOP] --stop-on-first-failure set, halting", flush=True)
                return 1
            continue

        if rc != 0:
            print(f"\n[FAIL] group {gname} exited {rc}", flush=True)
            print(f"  cmd: {' '.join(cmd)}", flush=True)
            all_ok = False
            if stop_on_first_failure:
                print("\n  [STOP] --stop-on-first-failure set, halting", flush=True)
                return 1

    if all_ok:
        print("\n  [OK] all groups passed", flush=True)
        return 0

    print("\n  [WARN] some groups failed", flush=True)
    return 1

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
                        help="With --full-groups, halt on the first failing group")
    parser.add_argument("--group-timeout-seconds", type=int, default=300,
                        help="Per-group timeout in seconds for --full-groups (default 300)")
    parser.add_argument("--full-timeout-seconds", type=int, default=600,
                        help="Timeout for --full / fast pytest subprocess in seconds (default 600)")
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

    # 1. Compile check
    step("1/5 compileall")
    run([sys.executable, "-m", "compileall", "-q", "scripts", "src", "tests"])

    # 2. Test suite
    if args.process:
        step("2/5 pytest --process")
        run([sys.executable, "-m", "pytest", "-q", "-m", "process and not stress and not external", "--durations=20"],
            env=_pytest_env(), timeout=args.full_timeout_seconds)
    elif args.stress:
        step("2/5 pytest --stress")
        seed = os.environ.get("MINERU_STRESS_SEED", "20260711")
        print(f"  stress seed: {seed}", flush=True)
        env = _pytest_env(); env["MINERU_STRESS_SEED"] = seed
        run([sys.executable, "-m", "pytest", "-q", "-m", "stress", "--durations=20"],
            env=env, timeout=args.full_timeout_seconds)
    elif args.area:
        step(f"2/5 pytest --area {args.area}")
        area_tests = {
            "packaging": ["tests/unit/test_pack_repo_rules.py", "tests/hygiene/test_snapshot_hygiene.py", "tests/unit/test_repository_hygiene_policy.py"],
            "ingest": ["tests/contract/test_ingest_locking_contract.py", "tests/integration/test_frozen_v32_transaction_pipeline.py"],
            "discovery": ["tests/unit/discovery", "tests/contract/test_discovery_reconciliation.py"],
            "security": ["tests/security", "tests/hygiene/test_snapshot_hygiene.py"],
        }
        run([sys.executable, "-m", "pytest", "-q", "-m", FAST_MARKERS] + area_tests[args.area],
            env=_pytest_env(), timeout=args.full_timeout_seconds)
    elif args.full_groups:
        step("2/5 pytest --full-groups")
        rc = _run_full_groups(stop_on_first_failure=args.stop_on_first_failure,
                              group_timeout=args.group_timeout_seconds)
        if rc != 0:
            return rc
    elif args.full:
        step("2/5 pytest --full")
        run([sys.executable, "-m", "pytest", "-q", "-m", FULL_MARKERS,
             "--durations=30", "--durations-min=0.5"],
            env=_pytest_env(), timeout=args.full_timeout_seconds)
    else:
        step("2/5 pytest (fast acceptance)")
        run([sys.executable, "-m", "pytest", "-q", "-m", FAST_MARKERS] + FAST_ACCEPTANCE_TESTS,
            env=_pytest_env(), timeout=args.full_timeout_seconds)

    if args.no_pack:
        print("\n  [OK] tests passed (--no-pack, skipping pack)")
        return 0

    # 3. Git/root hygiene check
    step("3/5 hygiene")
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
    step("4/5 pack_repo")
    run([sys.executable, "scripts/pack_repo.py", "--profile", args.profile])

    # 5. Verify snapshot
    step("5/5 verify snapshot")
    bad = verify_snapshot()
    if bad:
        print("\n[FAIL] Snapshot contains forbidden content:")
        for b in bad:
            print(f"  - {b}")
        return 1

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

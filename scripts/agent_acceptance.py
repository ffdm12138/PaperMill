#!/usr/bin/env python
"""Agent acceptance — single command to run tests, pack, and verify.

Usage:
    python scripts/agent_acceptance.py               # default fast acceptance
    python scripts/agent_acceptance.py --full        # full pytest suite
    python scripts/agent_acceptance.py --full-groups # full pytest in groups (diagnostic)
    python scripts/agent_acceptance.py --no-pack     # skip pack (debug only)

The default fast mode runs the layered test directories:
contract + hygiene + unit + integration + e2e.

All pytest subprocesses run with PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 so
third-party plugins cannot change behavior or hang the process.

Exit 0 on success, non-zero on failure.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
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
    "tests/hygiene",
    "tests/unit",
    "tests/integration",
    "tests/e2e",
]


def step(name: str) -> None:
    print(f"\n{'='*60}", flush=True)
    print(f"  {name}", flush=True)
    print(f"{'='*60}", flush=True)


def run(cmd: list[str], *, check: bool = True, env: dict[str, str] | None = None,
        timeout: int | None = None) -> int:
    print(f"  $ {' '.join(cmd)}", flush=True)
    try:
        result = subprocess.run(cmd, cwd=str(ROOT), env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"\n[FAIL] {cmd[0]} timed out after {timeout}s", flush=True)
        print(f"  cmd: {' '.join(cmd)}", flush=True)
        sys.exit(124)
    if check and result.returncode != 0:
        print(f"\n[FAIL] {cmd[0]} exited {result.returncode}", flush=True)
        sys.exit(result.returncode)
    return result.returncode


def verify_git_hygiene(root_path: Path | None = None) -> list[str]:
    """Check git tracked files for forbidden runtime assets.

    This enforces Git hygiene: real paper assets, runtime data, PDFs, metadata,
    catalogs, and conversion output must NOT be git-tracked.
    It checks ``git ls-files --cached``, not the zip file.

    Returns a list of error messages (empty = clean).
    """
    root = root_path or ROOT
    FORBIDDEN_PREFIXES = [
        "data/papers/",
        "data/paper_raw/",
        "data/raw/",
        "data/raw_all/",
        "data/tmp/",
        "data/logs/",
        "data/jobs/",
        "data/transactions/",
        "data/import_work/",
        "data/discovery/doi_candidates/",
        "data/discovery/pdf_fetch_logs/",
        "data/discovery/fetch_logs/",
        "data/discovery/queries/",
        "write/jobs/",
        "output/",
        "reports/",
        ".reasonix/",
    ]
    ALLOWED_TRACKED = {
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
        "data/discovery/queries/.gitkeep",
        "write/jobs/.gitkeep",
        "reports/.gitkeep",
    }

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached"],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        if result.returncode != 0:
            return [f"git ls-files failed: {result.stderr.strip()}"]
        bad: list[str] = []
        for f in result.stdout.splitlines():
            f = f.replace("\\", "/")
            if f in ALLOWED_TRACKED:
                continue
            for prefix in FORBIDDEN_PREFIXES:
                if f.startswith(prefix):
                    bad.append(f"runtime asset in git index: {f}")
                    break
        return bad
    except FileNotFoundError:
        return ["git not available, cannot check git hygiene"]
    except Exception as e:
        return [f"git hygiene check failed: {e}"]


from scripts.pack_repo import (
    LIGHTWEIGHT_ALLOWED_SUFFIXES,
    DENIED_NAMES as PACK_DENIED_NAMES,
    DENIED_PATH_PARTS as PACK_DENIED_PARTS,
    HEAVY_OR_BINARY_DENIED_SUFFIXES,
    SINGLE_FILE_MAX_BYTES,
    ZIP_MAX_BYTES,
)


# Runtime sample directories whose contents must be lightweight text/structure
# files (catalog, metadata, markdown, .paper.number markers, etc.).
# verify_snapshot() enforces this as a final gate: a file that is not
# heavy/binary, not a secret, and not a tombstone but lives under one of these
# dirs must still have a lightweight suffix — an unknown suffix like ``.exe``
# must be rejected here rather than slip through on pack rules alone.
RUNTIME_SAMPLE_PREFIXES = (
    "data/papers/",
    "data/paper_raw/",
    "data/raw/",
    "data/raw_all/",
)


def _snapshot_lightweight_suffix_match(name: str) -> bool:
    """True if the zip entry name has a lightweight allow-listed suffix.

    Mirrors ``pack_repo._lightweight_suffix_match`` so the verifier and the
    packer agree on what counts as lightweight. Handles the dotfile / multi-dot
    quirks (``.gitkeep``, ``.paper.number``) that ``Path.suffix`` mishandles.
    """
    path = Path(name)
    if path.name == ".gitkeep" or name.endswith(".gitkeep"):
        return True
    if name.endswith(".paper.number"):
        return True
    return path.suffix.lower() in LIGHTWEIGHT_ALLOWED_SUFFIXES


def verify_snapshot(root_path: Path | None = None) -> list[str]:
    """Check mineru_snapshot.zip for lightweight snapshot compliance.

    The zip is a lightweight audit snapshot: it may contain lightweight
    text/structure files (catalog, metadata, markdown, configs, source_records)
    from git-ignored runtime directories, but must NOT contain:
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
        "data/discovery/queries/.gitkeep",
        "write/jobs/.gitkeep",
        "reports/.gitkeep",
    }

    FORBIDDEN_PREFIXES = [
        ".reasonix/",
        "data/tmp/",
        "data/logs/",
        "data/jobs/",
        "data/transactions/",
        "data/import_work/",
        "data/discovery/doi_candidates/",
        "data/discovery/pdf_fetch_logs/",
        "data/discovery/fetch_logs/",
        "data/discovery/queries/",
        "write/jobs/",
        "output/",
        "reports/",
    ]

    bad: list[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
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
                    # Runtime sample directories: final lightweight gate.
                    # A file that passed heavy/secret/tombstone checks but lives
                    # under data/papers|paper_raw|raw|raw_all must still have a
                    # lightweight suffix; an unknown suffix like .exe is rejected.
                    if name.startswith(RUNTIME_SAMPLE_PREFIXES) and not _snapshot_lightweight_suffix_match(name):
                        bad.append(f"non-lightweight runtime sample: {name}")
                        continue

    return bad


def _chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


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
    import subprocess as _sp

    layered_dirs = [
        "tests/contract", "tests/hygiene", "tests/unit",
        "tests/integration", "tests/e2e",
    ]

    groups: list[tuple[str, list[str]]] = [
        ("layered", list(layered_dirs)),
    ]

    covered: set[str] = set()
    for d in layered_dirs:
        for p in (ROOT / d).rglob("test_*.py"):
            covered.add(str(p.relative_to(ROOT)).replace("\\", "/"))

    # Legacy tests
    legacy_dir = ROOT / "tests" / "legacy"
    if legacy_dir.is_dir():
        legacy_files = [
            str(p.relative_to(ROOT)).replace("\\", "/")
            for p in sorted(legacy_dir.glob("test_*.py"))
        ]
        if legacy_files:
            groups.append(("legacy", legacy_files))
            covered.update(legacy_files)

    # Slow tests
    slow_dir = ROOT / "tests" / "slow"
    if slow_dir.is_dir():
        slow_files = [
            str(p.relative_to(ROOT)).replace("\\", "/")
            for p in sorted(slow_dir.glob("test_*.py"))
        ]
        if slow_files:
            groups.append(("slow", slow_files))
            covered.update(slow_files)

    remaining = [
        str(p.relative_to(ROOT)).replace("\\", "/")
        for p in sorted((ROOT / "tests").glob("test_*.py"))
        if str(p.relative_to(ROOT)).replace("\\", "/") not in covered
    ]

    for idx, chunk in enumerate(_chunked(remaining, 10), 1):
        groups.append((f"remaining_{idx:02d}", chunk))

    all_ok = True
    for gname, gpaths in groups:
        print(f"\n{'─'*60}", flush=True)
        print(f"  group: {gname}", flush=True)
        print(f"  files: {len(gpaths)}", flush=True)
        for p in gpaths:
            print(f"    - {p}", flush=True)
        cmd = [sys.executable, "-m", "pytest", "-q"] + gpaths \
              + ["--durations=30", "--durations-min=0.5"]
        print(f"  cmd: {' '.join(cmd)}", flush=True)
        if group_timeout:
            print(f"  timeout: {group_timeout}s", flush=True)
        print(f"{'─'*60}", flush=True)

        # Run pytest in its own process group/session so a timeout can kill
        # the whole process tree (pytest + any subprocesses spawned by tests).
        # subprocess.run(timeout=...) only terminates the direct child; on
        # Windows that leaves orphaned grandchildren (e.g. audit-script
        # subprocesses) holding stdout, which makes the gate appear to hang
        # and the timeout never visibly fires.
        popen_kwargs: dict = {"cwd": str(ROOT), "env": _pytest_env()}
        if os.name == "nt":
            popen_kwargs["creationflags"] = _sp.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        try:
            proc = _sp.Popen(cmd, **popen_kwargs)
        except Exception as exc:
            print(f"\n[FAIL] group {gname}: could not start pytest: {exc}", flush=True)
            all_ok = False
            if stop_on_first_failure:
                return 1
            continue

        timed_out = False
        try:
            # Inherit stdout/stderr (no capture) so pytest output streams live.
            proc.wait(timeout=group_timeout)
        except _sp.TimeoutExpired:
            timed_out = True
            print(f"\n[TIMEOUT] group {gname} exceeded {group_timeout}s — "
                  f"killing process tree (pid {proc.pid})", flush=True)
            _kill_process_tree(proc)
            try:
                proc.wait(timeout=15)
            except _sp.TimeoutExpired:
                print(f"  [WARN] pid {proc.pid} did not exit after kill", flush=True)
        except KeyboardInterrupt:
            print(f"\n[FAIL] group {gname} interrupted", flush=True)
            _kill_process_tree(proc)
            return 1

        if timed_out:
            print(f"\n[FAIL] group {gname} timed out after {group_timeout}s", flush=True)
            print(f"  cmd: {' '.join(cmd)}", flush=True)
            print(f"  timeout: {group_timeout}s", flush=True)
            print(f"  reproduce: {' '.join(cmd)}", flush=True)
            all_ok = False
            if stop_on_first_failure:
                print("\n  [STOP] --stop-on-first-failure set, halting", flush=True)
                return 1
            continue

        if proc.returncode != 0:
            print(f"\n[FAIL] group {gname} exited {proc.returncode}", flush=True)
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
                        help='Packaging profile: "audit" (default) includes git-ignored runtime '
                             'samples; "source" only includes git-tracked source + .gitkeep.')
    args = parser.parse_args()

    # 1. Compile check
    step("1/5 compileall")
    run([sys.executable, "-m", "compileall", "-q", "scripts", "src", "tests"])

    # 2. Test suite
    if args.full_groups:
        step("2/5 pytest --full-groups")
        rc = _run_full_groups(stop_on_first_failure=args.stop_on_first_failure,
                              group_timeout=args.group_timeout_seconds)
        if rc != 0:
            return rc
    elif args.full:
        step("2/5 pytest --full")
        run([sys.executable, "-m", "pytest", "-q", "--durations=30", "--durations-min=0.5"],
            env=_pytest_env(), timeout=args.full_timeout_seconds)
    else:
        step("2/5 pytest (fast acceptance)")
        run([sys.executable, "-m", "pytest", "-q"] + FAST_ACCEPTANCE_TESTS,
            env=_pytest_env(), timeout=args.full_timeout_seconds)

    if args.no_pack:
        print("\n  [OK] tests passed (--no-pack, skipping pack)")
        return 0

    # 3. Git hygiene check
    step("3/5 git hygiene")
    bad_hygiene = verify_git_hygiene()
    if bad_hygiene:
        print("\n[FAIL] Git hygiene — runtime assets found in git index:")
        for b in bad_hygiene:
            print(f"  - {b}")
        return 1
    print("  [OK] git hygiene clean")

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

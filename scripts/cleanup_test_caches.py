#!/usr/bin/env python
"""Safe cleanup of stale test-runtime workspaces and legacy flattened caches.

**Default behaviour is dry-run — nothing is deleted.**  Add ``--apply`` to
perform actual removal.

Usage::

    python scripts/cleanup_test_caches.py                           # dry-run new-style stale workspaces
    python scripts/cleanup_test_caches.py --apply                   # delete stale workspaces
    python scripts/cleanup_test_caches.py --legacy-flattened-root   # dry-run legacy C:\\ pollution
    python scripts/cleanup_test_caches.py --legacy-flattened-root --apply  # delete legacy pollution

Safety guarantees
-----------------
* Never follows symlinks, junctions, or reparse points.
* Re-checks every candidate immediately before deletion.
* Only deletes directories with a valid ``.mineru-test-workspace.json`` marker
  whose owner PID is dead (new-style) or legacy flattened-root caches whose
  content is exclusively ``.pyc`` / ``__pycache__`` artefacts (legacy).
* Reports all decisions to a JSON file under the system temporary directory.
* System directories, repo directories, and non-mineru directories are
  NEVER candidates.
* Invalid or unrecognized ``mineru_*`` entries are reported but never deleted.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# The cleanup command must not create the very repo pollution that acceptance
# checks.  Set this before importing any repository-local module.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.test_runtime_workspace import (
    _flatten_path,
    _system_temp_dir,
    inspect_workspace,
    remove_verified_workspace_tree,
    WorkspaceStatus,
)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _report_path() -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_dir = _system_temp_dir() / "mineru_cleanup_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    return report_dir / f"test_cache_cleanup_{ts}.json"


def _write_report(entries: list[dict], report_path: Path) -> None:
    scanned = len(entries)
    matched = sum(1 for e in entries if e.get("verdict") == "matched")
    deleted = sum(1 for e in entries if e.get("deleted"))
    refused = sum(1 for e in entries if e.get("verdict") == "refused")
    failed = sum(1 for e in entries if e.get("verdict") == "error")
    bytes_reclaimed = sum(e.get("bytes_reclaimed", 0) for e in entries if e.get("deleted"))

    report = {
        "schema_version": "1.0",
        "cleanup_type": "test_cache",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": {
            "scanned": scanned,
            "matched": matched,
            "deleted": deleted,
            "refused": refused,
            "failed": failed,
            "bytes_reclaimed": bytes_reclaimed,
        },
        "entries": entries,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\nReport: {report_path}", flush=True)
    print(f"  scanned={scanned} matched={matched} deleted={deleted} "
          f"refused={refused} failed={failed} "
          f"bytes_reclaimed={bytes_reclaimed:,}", flush=True)


# ---------------------------------------------------------------------------
# Directory size
# ---------------------------------------------------------------------------

def _dir_size_and_count(root: Path) -> tuple[int, int]:
    """Return (total_bytes, file_count) for all files under *root*."""
    total = 0
    count = 0
    for p in root.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
                count += 1
            except OSError:
                pass
    return total, count


# ---------------------------------------------------------------------------
# Safety checks
# ---------------------------------------------------------------------------

def _is_safe_to_delete(path: Path, *, allow_drive_root: bool = False) -> Optional[str]:
    """Return None if *path* is safe to delete, or a reason string.

    When *allow_drive_root* is True the boundary check skips the drive root
    itself — used for legacy flattened caches that by definition live on
    a drive root.

    Checks are performed in order: lstat → symlink/junction/reparse →
    resolve → boundary.  This ensures reparse points are detected BEFORE
    resolve follows them.
    """
    # 1. lstat first — do NOT follow symlinks / junctions.
    try:
        st = path.lstat()
    except OSError as e:
        return f"cannot lstat: {e}"

    # 2. Not a directory.
    if not path.is_dir():
        return "not a directory"

    # 3. Symlink / junction / reparse point — reject BEFORE resolve.
    if os.name == "nt":
        try:
            import stat as _st_mod
            FILE_ATTRIBUTE_REPARSE_POINT = getattr(_st_mod, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
            if st.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT:
                return "reparse point (junction / symlink)"
        except Exception:
            pass
    if path.is_symlink() or (os.name == "nt" and _is_junction(path)):
        return "symlink or junction"

    # 4. Now resolve — safe because we know it's not a reparse point.
    try:
        resolved = path.resolve()
    except OSError as e:
        return f"cannot resolve: {e}"

    # 5. Boundary checks on the resolved path.
    system_tmp = _system_temp_dir()
    try:
        drive_root = Path(resolved.anchor) if resolved.anchor else None
        is_under_temp = system_tmp in resolved.parents or resolved == system_tmp
        is_legacy_root = (
            allow_drive_root
            and drive_root is not None
            and resolved.parent == drive_root
        )
        if not is_under_temp and not is_legacy_root:
            dangerous = _dangerous_roots()
            for d in dangerous:
                try:
                    if resolved == d or d in resolved.parents:
                        return f"under protected path {d}"
                except Exception:
                    pass
    except Exception:
        pass

    # 6. Must NOT be the repo root or inside the repo.
    try:
        if resolved == ROOT.resolve() or ROOT.resolve() in resolved.parents:
            return "inside repository"
    except Exception:
        pass

    return None


def _is_junction(path: Path) -> bool:
    """Windows-only: return True if *path* is an NTFS junction."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs == 0xFFFFFFFF:
            return False
        FILE_ATTRIBUTE_REPARSE_POINT = 0x400
        return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT) and path.is_dir()
    except Exception:
        return False


def _dangerous_roots() -> set[Path]:
    """Return a set of paths that must NEVER be deleted."""
    dangerous = set()
    for env_name in ("SystemRoot", "WINDIR", "ProgramFiles", "ProgramFiles(x86)",
                     "ProgramData", "USERPROFILE", "HOMEDRIVE"):
        val = os.environ.get(env_name)
        if val:
            try:
                dangerous.add(Path(val).resolve())
            except Exception:
                pass
    # Add common protected locations
    for p in (r"C:\Windows", r"C:\Program Files", r"C:\Program Files (x86)",
              r"C:\ProgramData", r"C:\Users"):
        try:
            pp = Path(p)
            if pp.exists():
                dangerous.add(pp.resolve())
        except Exception:
            pass
    return dangerous


# ---------------------------------------------------------------------------
# New-style workspace cleanup
# ---------------------------------------------------------------------------

def _scan_stale_workspaces(temp_dir: Optional[Path] = None) -> list[dict]:
    """Find stale new-style workspace directories under *temp_dir*."""
    if temp_dir is None:
        temp_dir = _system_temp_dir()
    entries: list[dict] = []
    try:
        candidates = sorted(
            [e for e in temp_dir.iterdir()
             if e.name.startswith("mineru_")
             and e.name != "mineru_cleanup_reports"],
            key=lambda p: p.name,
        )
    except PermissionError:
        return entries

    for candidate in candidates:
        entry = {
            "path": str(candidate),
            "name": candidate.name,
            "type": "new_workspace",
            "verdict": "unknown",
            "reason": "",
            "deleted": False,
            "bytes_reclaimed": 0,
        }
        ins = inspect_workspace(candidate, repo_root=ROOT)
        if ins.status != WorkspaceStatus.STALE:
            entry["verdict"] = "refused"
            entry["reason"] = f"[{ins.status.value}] {ins.reason}"
            entries.append(entry)
            continue
        # Verify path safety
        safety = _is_safe_to_delete(candidate)
        if safety is not None:
            entry["verdict"] = "refused"
            entry["reason"] = f"safety: {safety}"
            entries.append(entry)
            continue
        size, file_count = _dir_size_and_count(candidate)
        marker = ins.marker or {}
        entry["verdict"] = "matched"
        entry["reason"] = f"stale workspace (pid {marker.get('pid')} dead), {file_count} files, {size:,} bytes"
        entry["size_bytes"] = size
        entry["file_count"] = file_count
        entry["pid"] = marker.get("pid")
        entry["group"] = marker.get("group", "unknown")
        entries.append(entry)

    return entries


# ---------------------------------------------------------------------------
# Legacy flattened-root cache cleanup
# ---------------------------------------------------------------------------

def _scan_legacy_flattened() -> list[dict]:
    """Find legacy flattened-cache directories on the system drive root.

    These have names like ``UsersAdminAppDataLocalTempmineru_fast_catalog_xxxxxxxxcache``
    and are the result of ``shlex.split(posix=True)`` stripping backslashes
    from Windows paths that were embedded in ``PYTEST_ADDOPTS``.
    """
    entries: list[dict] = []
    temp_dir = _system_temp_dir()
    flattened_prefix = _flatten_path(temp_dir)

    # Determine drive root to scan
    try:
        drive_root = Path(temp_dir.anchor)
    except Exception:
        return entries
    if not drive_root.exists():
        return entries

    pattern = f"{flattened_prefix}mineru_"
    cache_suffix = "cache"

    try:
        candidates = sorted(
            [e for e in drive_root.iterdir()
             if e.is_dir() and e.name.startswith(pattern) and e.name.endswith(cache_suffix)],
            key=lambda p: p.name,
        )
    except PermissionError:
        return entries

    for candidate in candidates:
        entry = {
            "path": str(candidate),
            "name": candidate.name,
            "type": "legacy_flattened",
            "verdict": "unknown",
            "reason": "",
            "deleted": False,
            "bytes_reclaimed": 0,
        }
        # Safety check (legacy caches live on drive root by definition)
        safety = _is_safe_to_delete(candidate, allow_drive_root=True)
        if safety is not None:
            entry["verdict"] = "refused"
            entry["reason"] = f"safety: {safety}"
            entries.append(entry)
            continue
        # Content check: should contain only __pycache__ and .pyc files
        content_ok, content_reason = _check_cache_only_content(candidate)
        if not content_ok:
            entry["verdict"] = "refused"
            entry["reason"] = f"content: {content_reason}"
            entries.append(entry)
            continue
        size, file_count = _dir_size_and_count(candidate)
        entry["verdict"] = "matched"
        entry["reason"] = f"legacy flattened cache, {file_count} files, {size:,} bytes"
        entry["size_bytes"] = size
        entry["file_count"] = file_count
        entries.append(entry)

    return entries


def _check_cache_only_content(path: Path) -> tuple[bool, str]:
    """Return (True, "") if *path* is a legitimate pytest cache directory.

    A valid pytest cache MUST contain ``CACHEDIR.TAG`` with the standard
    pytest cache signature.  Content is limited to pytest-root files
    (``.gitignore``, ``README.md``), ``v/cache/`` standard keys, and
    optional ``__pycache__`` with ``.pyc`` files.

    Symlinks, junctions, and reparse points inside the directory cause
    immediate rejection.
    """
    # 1. Must contain CACHEDIR.TAG with valid pytest cache signature.
    tag_path = path / "CACHEDIR.TAG"
    if not tag_path.is_file():
        return False, "missing CACHEDIR.TAG"
    try:
        tag_content = tag_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, f"cannot read CACHEDIR.TAG: {exc}"
    if "Signature: 8a477f597d28d172789f06886806bc55" not in tag_content:
        return False, "CACHEDIR.TAG signature does not match pytest cache"

    _PYTEST_ROOT = {".gitignore", "CACHEDIR.TAG", "README.md",
                    ".mineru-test-workspace.json", ".gitkeep"}
    _PYTEST_CACHE_KEYS = {"lastfailed", "nodeids", "stepwise", "durations"}

    for p in path.rglob("*"):
        # 2. Reject symlinks / junctions / reparse points inside the candidate.
        if p.is_symlink():
            return False, f"symlink inside: {p.relative_to(path)}"
        if os.name == "nt":
            if _is_junction(p):
                return False, f"junction inside: {p.relative_to(path)}"
            try:
                import stat as _st_mod
                RP = getattr(_st_mod, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
                if p.stat().st_file_attributes & RP:
                    return False, f"reparse point inside: {p.relative_to(path)}"
            except Exception:
                pass

        if not p.is_file():
            continue

        # 3. Allow .pyc files anywhere.
        if p.suffix == ".pyc":
            continue

        rel = p.relative_to(path)

        # 4. Allow known pytest root files.
        if len(rel.parts) == 1 and p.name in _PYTEST_ROOT:
            continue

        # 5. Allow known pytest cache keys under v/cache/.
        if len(rel.parts) >= 3 and rel.parts[0] == "v" and rel.parts[1] == "cache" and p.name in _PYTEST_CACHE_KEYS:
            continue

        # 6. Reject anything else.
        return False, f"non-pytest-cache file: {rel}"

    return True, ""


# ---------------------------------------------------------------------------
# Delete entry
# ---------------------------------------------------------------------------

def _delete_entry(entry: dict) -> None:
    """Delete a directory entry with pre-delete re-verification."""
    path = Path(entry["path"])
    is_legacy = entry.get("type") == "legacy_flattened"
    inspection = None
    if not is_legacy:
        # Classify before any filesystem mutation or generic path handling so
        # untrusted marker state is reported accurately and never deleted.
        inspection = inspect_workspace(path, repo_root=ROOT)
        if inspection.status != WorkspaceStatus.STALE or inspection.marker is None:
            entry["verdict"] = "refused"
            entry["reason"] = (
                "pre-delete workspace re-check failed: "
                f"[{inspection.status.value}] {inspection.reason}"
            )
            return
    # Re-verify safety immediately before deletion
    safety = _is_safe_to_delete(path, allow_drive_root=is_legacy)
    if safety is not None:
        entry["verdict"] = "refused"
        entry["reason"] = f"pre-delete safety re-check failed: {safety}"
        return
    # For legacy entries, re-check content.
    if is_legacy:
        ok, reason = _check_cache_only_content(path)
        if not ok:
            entry["verdict"] = "refused"
            entry["reason"] = f"pre-delete content re-check failed: {reason}"
            return
    else:
        # Marker state is mutable (the owner PID could restart or the marker
        # could be replaced) so re-check it immediately before the helper's
        # identity verification and first mutation.
        assert inspection is not None and inspection.marker is not None
        result = remove_verified_workspace_tree(
            path,
            marker_snapshot=inspection.marker,
            repo_root=ROOT,
        )
        if result.success:
            entry["deleted"] = True
            entry["bytes_reclaimed"] = entry.get("size_bytes", 0)
            return
        entry["verdict"] = "error"
        entry["reason"] = (
            f"deletion failed after {result.attempts} attempt(s): {result.error}"
        )
        return

    size_before = entry.get("size_bytes", 0)
    try:
        shutil.rmtree(path, ignore_errors=False)
        entry["deleted"] = True
        entry["bytes_reclaimed"] = size_before
    except OSError as e:
        entry["verdict"] = "error"
        entry["reason"] = f"deletion failed: {e}"
        # Retry with ignore_errors
        try:
            shutil.rmtree(path, ignore_errors=True)
            if not path.exists():
                entry["deleted"] = True
                entry["bytes_reclaimed"] = size_before
                entry["verdict"] = "matched"
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safe cleanup of stale test-runtime workspaces and legacy caches",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually delete.  Without this flag the script is read-only (dry-run).",
    )
    parser.add_argument(
        "--legacy-flattened-root", action="store_true",
        help="Scan the system-drive root for legacy flattened cache directories "
             "(e.g. UsersAdminAppDataLocalTempmineru_*cache).",
    )
    parser.add_argument(
        "--temp-dir", type=str, default=None,
        help="Override the system temporary directory for scanning.",
    )
    args = parser.parse_args()

    dry_run = not args.apply
    if dry_run:
        print("=" * 60, flush=True)
        print("  DRY RUN — nothing will be deleted", flush=True)
        print("  Add --apply to perform actual removal", flush=True)
        print("=" * 60, flush=True)

    temp_dir = Path(args.temp_dir) if args.temp_dir else _system_temp_dir()
    print(f"System temp dir: {temp_dir}", flush=True)
    print(f"Repo root:       {ROOT}", flush=True)

    entries: list[dict] = []

    if args.legacy_flattened_root:
        print("\nScanning drive root for legacy flattened caches...", flush=True)
        legacy_entries = _scan_legacy_flattened()
        entries.extend(legacy_entries)
        matched = sum(1 for e in legacy_entries if e["verdict"] == "matched")
        refused = sum(1 for e in legacy_entries if e["verdict"] == "refused")
        print(f"  Legacy: {len(legacy_entries)} found "
              f"({matched} matched, {refused} refused)", flush=True)
    else:
        print("\nScanning for stale new-style workspaces...", flush=True)
        ws_entries = _scan_stale_workspaces(temp_dir)
        entries.extend(ws_entries)
        matched = sum(1 for e in ws_entries if e["verdict"] == "matched")
        refused = sum(1 for e in ws_entries if e["verdict"] == "refused")
        print(f"  Workspaces: {len(ws_entries)} found "
              f"({matched} matched, {refused} refused)", flush=True)

    # Print details
    for entry in entries:
        verdict = entry["verdict"].upper()
        marker = {"matched": "[DELETE]", "refused": "[KEEP]  ", "error": "[ERROR] "}.get(
            entry["verdict"], "[????]  ")
        print(f"\n  {marker} {entry['name']}", flush=True)
        print(f"          {entry['reason']}", flush=True)
        if "size_bytes" in entry:
            print(f"          {entry['size_bytes']:,} bytes, "
                  f"{entry.get('file_count', 0)} files", flush=True)

    # Apply deletions
    if args.apply:
        to_delete = [e for e in entries if e["verdict"] == "matched"]
        if not to_delete:
            print("\nNothing to delete.", flush=True)
        else:
            print(f"\nDeleting {len(to_delete)} directories...", flush=True)
            for entry in to_delete:
                print(f"  Deleting: {entry['name']}...", flush=True)
                _delete_entry(entry)
                if entry.get("deleted"):
                    print(f"    [OK] deleted ({entry.get('bytes_reclaimed', 0):,} bytes)", flush=True)
                else:
                    print(f"    [FAIL] {entry.get('reason', 'unknown')}", flush=True)

    # Write report
    report_path = _report_path()
    _write_report(entries, report_path)

    # Exit status
    failed = sum(1 for e in entries if e.get("verdict") == "error")
    if failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

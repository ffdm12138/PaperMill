#!/usr/bin/env python
"""Read-only, complete diagnostics for discovery reset state.

The audit never repairs state and never follows a symlink while inspecting the
audited tree.  Every component emits findings independently; readiness is
derived only after all component checks have run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

sys.dont_write_bytecode = True
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SCHEMA_VERSION = "2.0"
DEFAULT_DATA = PROJECT_ROOT / "data"


@dataclass(frozen=True)
class ResetAuditFinding:
    code: str
    severity: Literal["block", "repair", "reset", "info"]
    component: str
    path: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "component": self.component,
            "path": self.path,
            "detail": self.detail,
        }


def _finding(
    code: str, severity: Literal["block", "repair", "reset", "info"],
    component: str, path: Path | str, detail: str,
) -> ResetAuditFinding:
    return ResetAuditFinding(code, severity, component, str(path), detail)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_safe_report_path(
    requested_path: str | Path,
    *,
    audited_root: Path,
) -> Path:
    """Resolve an external report path without allowing audited-tree writes."""
    audited = Path(audited_root).absolute()
    report = Path(os.path.abspath(os.fspath(Path(requested_path).expanduser())))
    if os.path.lexists(str(audited)) and _is_reparse(audited):
        raise ValueError(f"audited root is a symlink/reparse point: {audited}")

    try:
        report.relative_to(audited)
    except ValueError:
        pass
    else:
        raise ValueError(f"report path must be outside audited root: {report}")

    current = Path(report.anchor)
    for part in report.parts[1:]:
        current = current / part
        if not os.path.lexists(str(current)):
            continue
        if _is_reparse(current):
            raise ValueError(f"report path contains symlink/reparse component: {current}")
    if os.path.lexists(str(report)) and not report.is_file():
        raise ValueError(f"report target is not a regular file: {report}")

    audited_real = audited.resolve(strict=False)
    parent_real = report.parent.resolve(strict=False)
    try:
        parent_real.relative_to(audited_real)
    except ValueError:
        return report
    raise ValueError(f"report parent resolves inside audited root: {report.parent}")


def _write_report_atomically(
    report_path: Path, payload: bytes, *, audited_root: Path,
) -> None:
    report_path = resolve_safe_report_path(report_path, audited_root=audited_root)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path = resolve_safe_report_path(report_path, audited_root=audited_root)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=report_path.parent, prefix=f".{report_path.name}.",
            suffix=".tmp", delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, report_path)
        try:
            directory_fd = os.open(str(report_path.parent), os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary_path is not None and os.path.lexists(str(temporary_path)):
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return True
    if os.path.islink(path):
        return True
    attrs = getattr(info, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _path_has_reparse_component(root: Path, path: Path) -> bool:
    """Check lexical path components without resolving/following them."""
    root_abs = Path(root).absolute()
    path_abs = Path(path).absolute()
    try:
        relative = path_abs.relative_to(root_abs)
    except ValueError:
        return True
    current = root_abs
    if os.path.lexists(str(current)) and _is_reparse(current):
        return True
    for part in relative.parts:
        current = current / part
        if not os.path.lexists(str(current)):
            return False
        if _is_reparse(current):
            return True
    return False


def _safe_sha256(path: Path) -> str | None:
    """Stream a regular file; never read a symlink target."""
    try:
        info = path.lstat()
        if _is_reparse(path) or not stat.S_ISREG(info.st_mode):
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, ValueError):
        return None


def _load_json(path: Path) -> tuple[Any | None, str | None]:
    try:
        if _is_reparse(path):
            return None, "symlink_or_reparse_point"
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _iter_tree(
    root: Path, *, findings: list[ResetAuditFinding] | None = None,
    component: str = "audit",
):
    """Yield lexical paths without descending through links/reparse points."""
    if not root.exists() or _is_reparse(root):
        return
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda item: item.name, reverse=True)
        except OSError as exc:
            if findings is not None:
                findings.append(_finding(
                    "tree_unreadable", "repair", component, current,
                    f"directory enumeration failed: {type(exc).__name__}: {exc}",
                ))
            continue
        for path in entries:
            try:
                info = path.lstat()
            except OSError as exc:
                if findings is not None:
                    findings.append(_finding(
                        "tree_member_unreadable", "repair", component, path,
                        f"lstat failed: {type(exc).__name__}: {exc}",
                    ))
                yield path
                continue
            yield path
            if stat.S_ISDIR(info.st_mode) and not _is_reparse(path):
                if "__pycache__" not in path.parts:
                    stack.append(path)


def _snapshot_paths(
    data_root: Path, *, findings: list[ResetAuditFinding] | None = None,
) -> dict[str, Path]:
    """Return lexical data-root-relative paths for zero-write comparison."""
    root = Path(data_root)
    result: dict[str, Path] = {}
    for path in _iter_tree(root, findings=findings, component="zero_write") or ():
        relative = path.relative_to(root).as_posix()
        result[relative] = path
    return result


def _snapshot_facts(
    data_root: Path, *, findings: list[ResetAuditFinding] | None = None,
) -> dict[str, dict[str, Any]]:
    facts: dict[str, dict[str, Any]] = {}
    for relative, path in _snapshot_paths(data_root, findings=findings).items():
        try:
            info = path.lstat()
        except OSError:
            facts[relative] = {
                "relative_path": relative, "type": "unreadable", "size": -1,
                "streaming_sha256": "", "symlink_target": "",
            }
            continue
        if _is_reparse(path):
            try:
                target = os.readlink(path)
            except OSError:
                target = "<unreadable>"
            facts[relative] = {
                "relative_path": relative, "type": "symlink", "size": 0,
                "streaming_sha256": "", "symlink_target": target,
            }
        elif stat.S_ISDIR(info.st_mode):
            facts[relative] = {
                "relative_path": relative, "type": "directory", "size": 0,
                "streaming_sha256": "", "symlink_target": "",
            }
        elif stat.S_ISREG(info.st_mode):
            facts[relative] = {
                "relative_path": relative, "type": "file", "size": info.st_size,
                "streaming_sha256": _safe_sha256(path) or "", "symlink_target": "",
            }
        else:
            facts[relative] = {
                "relative_path": relative, "type": "other", "size": info.st_size,
                "streaming_sha256": "", "symlink_target": "",
            }
    return facts


@dataclass(frozen=True)
class LockProbe:
    path: Path
    classification: Literal["active", "stale", "unverifiable"]
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "classification": self.classification,
            "detail": self.detail,
        }


def probe_existing_file_lock(path: Path) -> LockProbe:
    """Probe an existing lock without creating, truncating, or writing it."""
    path = Path(path)
    try:
        info = path.lstat()
        if _is_reparse(path) or not stat.S_ISREG(info.st_mode):
            return LockProbe(path, "unverifiable", "lock is not a regular file")
    except OSError as exc:
        return LockProbe(path, "unverifiable", f"lstat failed: {exc}")
    if os.name == "nt":
        fd: int | None = None
        try:
            # Do not call the high-level lock helper here: its public acquire
            # path is allowed to create the lock file.  The reset audit must never mutate the
            # object it is measuring, including its mtime.
            import msvcrt
            fd = os.open(str(path), os.O_RDWR)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return LockProbe(path, "stale", "lock was obtainable")
        except OSError as exc:
            if getattr(exc, "errno", None) in {13, 36}:
                return LockProbe(path, "active", "lock is held")
            return LockProbe(path, "unverifiable", f"lock probe failed: {exc}")
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
    try:
        import fcntl
        with path.open("r+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return LockProbe(path, "active", "lock is held")
            finally:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        return LockProbe(path, "stale", "lock was obtainable")
    except OSError as exc:
        return LockProbe(path, "unverifiable", f"lock probe failed: {exc}")


def _classify_lock(lock_path: Path) -> dict[str, Any]:
    return probe_existing_file_lock(lock_path).to_dict()


def _audit_paper_raw(data_root: Path) -> dict[str, Any]:
    root = data_root / "paper_raw"
    findings: list[ResetAuditFinding] = []
    if not root.exists():
        return {
            "exists": False, "digital_workspaces": [], "digital_count": 0,
            "empty_directories": [], "gitkeep_only": [], "non_digital_members": [],
            "orphan_files": [], "raw_write_lock": None, "findings": [],
        }
    if _path_has_reparse_component(data_root, root):
        finding = _finding(
            "paper_raw_root_symlink", "repair", "paper_raw", root,
            "paper_raw root is a symlink/reparse point",
        )
        return {
            "exists": True, "digital_workspaces": [], "digital_count": 0,
            "empty_directories": [], "gitkeep_only": [],
            "non_digital_members": [], "orphan_files": [],
            "symlinks": [str(root)], "raw_write_lock": None,
            "findings": [finding.to_dict()],
        }
    digital: list[str] = []
    empty: list[str] = []
    gitkeep_only: list[str] = []
    non_digital: list[str] = []
    orphan_files: list[str] = []
    symlinks: list[str] = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if _is_reparse(entry):
            symlinks.append(entry.name)
            findings.append(_finding(
                "paper_raw_symlink", "repair", "paper_raw", entry,
                "paper_raw audit refuses to follow symlink/reparse point",
            ))
            continue
        if entry.is_dir():
            if entry.name.isdigit() and len(entry.name) == 16:
                try:
                    contents = list(entry.iterdir())
                except OSError as exc:
                    findings.append(_finding("paper_raw_unreadable", "repair", "paper_raw", entry, str(exc)))
                    continue
                has_gitkeep = len(contents) == 1 and contents[0].name == ".gitkeep"
                if not contents:
                    empty.append(entry.name)
                elif has_gitkeep:
                    gitkeep_only.append(entry.name)
                else:
                    digital.append(entry.name)
            else:
                non_digital.append(entry.name)
        else:
            # The repository root marker is intentional.  The writer lock is
            # audited by the lock component below, so it is not an orphan
            # merely because it is a file in paper_raw.
            if entry.name == ".gitkeep" or entry.name == ".paper_raw_write.lock":
                continue
            orphan_files.append(entry.name)
    raw_lock_path = root / ".paper_raw_write.lock"
    raw_lock = _classify_lock(raw_lock_path) if raw_lock_path.is_file() and not _is_reparse(raw_lock_path) else None
    return {
        "exists": True,
        "digital_workspaces": sorted(digital), "digital_count": len(digital),
        "empty_directories": sorted(empty), "gitkeep_only": sorted(gitkeep_only),
        "non_digital_members": sorted(non_digital), "orphan_files": sorted(orphan_files),
        "symlinks": sorted(symlinks), "raw_write_lock": raw_lock,
        "findings": [item.to_dict() for item in findings],
    }


def _audit_ledger(data_root: Path) -> dict[str, Any]:
    path = data_root / "catalog" / "paper_number_ledger.json"
    findings: list[ResetAuditFinding] = []
    if _path_has_reparse_component(data_root, path) or not path.is_file() or _is_reparse(path):
        return {
            "readable": False, "path": str(path), "error": "ledger missing or unsafe",
            "items": {}, "findings": [_finding("ledger_unreadable", "repair", "ledger", path, "ledger missing or symlinked").to_dict()],
        }
    raw, error = _load_json(path)
    if error or not isinstance(raw, dict):
        finding = _finding("ledger_unreadable", "repair", "ledger", path, error or "ledger root is not an object")
        return {"readable": False, "path": str(path), "error": error or "not valid JSON", "items": {}, "findings": [finding.to_dict()]}
    try:
        from src.discovery.workspace_registry import validate_ledger_view
        _validated, issues = validate_ledger_view(raw)
        for issue in issues:
            findings.append(_finding("ledger_invalid", "repair", "ledger", path, str(issue)))
    except Exception as exc:
        findings.append(_finding("ledger_validator_failed", "repair", "ledger", path, str(exc)))
    from src.library.paper_number_state import (
        ALL_LEDGER_STATES, LEDGER_ALLOCATING, LEDGER_ACTIVE,
        LEDGER_METADATA_STAGED, LEDGER_RESERVED,
    )

    items = raw.get("items")
    if not isinstance(items, dict):
        items = {}
    by_state: dict[str, int] = {}
    raw_refs: set[str] = set()
    formal_refs: set[str] = set()
    reserved: list[str] = []
    names: dict[str, str] = {}
    for paper_number, entry in items.items():
        number = str(paper_number)
        if not isinstance(entry, dict):
            findings.append(_finding("ledger_invalid_item", "repair", "ledger", path, f"{number}: item is not an object"))
            continue
        state = str(entry.get("state") or "")
        by_state[state] = by_state.get(state, 0) + 1
        if state in {LEDGER_ALLOCATING, LEDGER_RESERVED, LEDGER_METADATA_STAGED}:
            raw_refs.add(number)
        if state == LEDGER_ACTIVE:
            formal_refs.add(number)
            paper_name = str(entry.get("paper_name") or "")
            folder_path = str(entry.get("folder_path") or "")
            if not paper_name or not folder_path:
                findings.append(_finding("ledger_active_path_mismatch", "repair", "ledger", path, f"{number}: active item lacks paper_name/folder_path"))
            if paper_name in names and names[paper_name] != number:
                findings.append(_finding("ledger_duplicate_identity", "repair", "ledger", path, f"paper_name {paper_name!r} belongs to {names[paper_name]} and {number}"))
            names[paper_name] = number
        if state == LEDGER_RESERVED:
            reserved.append(number)
        if state not in ALL_LEDGER_STATES:
            findings.append(_finding("ledger_invalid_state", "repair", "ledger", path, f"{number}: unknown state {state!r}"))
    from src.path_utils import resolve_stored_path

    project_root = data_root.parent
    raw_dangling: list[str] = []
    formal_dangling: list[str] = []
    for number, entry in items.items():
        if not isinstance(entry, dict):
            continue
        state = str(entry.get("state") or "")
        stored = str(entry.get("folder_path") or "")
        if state not in {LEDGER_ALLOCATING, LEDGER_RESERVED, LEDGER_METADATA_STAGED, LEDGER_ACTIVE}:
            continue
        try:
            folder = resolve_stored_path(stored, project_root=project_root)
            expected_root = data_root / ("papers" if state == LEDGER_ACTIVE else "paper_raw")
            if (
                not stored
                or _is_reparse(folder)
                or folder.resolve(strict=False).parent != expected_root.resolve(strict=False)
            ):
                findings.append(_finding(
                    "ledger_folder_unsafe", "repair", "ledger", path,
                    f"{number}: folder_path is outside the isolated {state} root",
                ))
                (formal_dangling if state == LEDGER_ACTIVE else raw_dangling).append(str(number))
            elif not folder.is_dir():
                (formal_dangling if state == LEDGER_ACTIVE else raw_dangling).append(str(number))
                findings.append(_finding(
                    "ledger_folder_missing", "repair", "ledger", path,
                    f"{number}: workspace folder is missing",
                ))
        except (OSError, ValueError) as exc:
            findings.append(_finding(
                "ledger_folder_unreadable", "repair", "ledger", path,
                f"{number}: {exc}",
            ))
            (formal_dangling if state == LEDGER_ACTIVE else raw_dangling).append(str(number))
    raw_dangling = sorted(set(raw_dangling))
    formal_dangling = sorted(set(formal_dangling))
    for number in raw_dangling:
        findings.append(_finding("ledger_raw_dangling", "repair", "ledger", path, f"raw workspace missing for {number}"))
    for number in formal_dangling:
        findings.append(_finding("ledger_formal_dangling", "repair", "ledger", path, f"papers root missing for {number}"))
    return {
        "readable": True, "path": str(path),
        "schema_version": str(raw.get("schema_version") or ""),
        "total_items": len(items), "max_number": str(raw.get("max_number") or ""),
        "by_state": by_state, "active_count": by_state.get("active", 0),
        "reserved_count": by_state.get("reserved", 0),
        "metadata_staged_count": by_state.get("metadata_staged", 0),
        "raw_dangling_refs": raw_dangling, "formal_dangling_refs": formal_dangling,
        "duplicate_paper_numbers": [], "reserved_but_not_metadata_staged": sorted(reserved),
        "items": dict(items), "findings": [item.to_dict() for item in findings],
    }


def _contained_formal_path(stored: str, papers_dir: Path, project_root: Path) -> Path | None:
    if not stored:
        return None
    from src.path_utils import resolve_stored_path
    try:
        candidate = resolve_stored_path(stored, project_root=project_root)
        lexical = candidate.absolute()
        lexical.relative_to(papers_dir.absolute())
        if _path_has_reparse_component(project_root, candidate) or _is_reparse(candidate):
            return None
        candidate.resolve(strict=False).relative_to(papers_dir.resolve(strict=False))
        return candidate
    except (OSError, ValueError):
        return None


def _audit_formal(
    data_root: Path, expected: int, ledger_items: dict[str, Any],
    *, project_root: Path | None = None,
) -> dict[str, Any]:
    papers_dir = data_root / "papers"
    project_root = project_root or data_root.parent
    findings: list[ResetAuditFinding] = []
    if _path_has_reparse_component(data_root, papers_dir):
        finding = _finding(
            "formal_root_symlink", "repair", "formal", papers_dir,
            "papers root is a symlink/reparse point",
        )
        return {
            "exists": papers_dir.exists(), "papers": [], "expected": expected,
            "health": "INVALID", "violations": [finding.detail],
            "findings": [finding.to_dict()],
        }
    try:
        from src.library.validation import validate_formal_paper
        from src.discovery.formal_publication import validate_publication_state
    except Exception as exc:
        finding = _finding("formal_validator_unavailable", "repair", "formal", papers_dir, str(exc))
        return {"exists": papers_dir.exists(), "papers": [], "expected": expected, "health": "INVALID", "violations": [str(exc)], "findings": [finding.to_dict()]}
    active: dict[str, dict[str, str]] = {
        str(number): {
            "paper_name": str(entry.get("paper_name") or ""),
            "folder_path": str(entry.get("folder_path") or ""),
        }
        for number, entry in ledger_items.items()
        if isinstance(entry, dict) and entry.get("state") == "active"
    }
    violations: list[str] = []
    papers: list[dict[str, Any]] = []
    validated_count = 0
    if not papers_dir.exists() and not (expected == 0 and not active):
        violations.append("papers directory missing")
        findings.append(_finding("formal_root_missing", "repair", "formal", papers_dir, "papers directory is missing"))
    for paper_number, info in sorted(active.items()):
        folder = _contained_formal_path(info["folder_path"], papers_dir, project_root)
        if folder is None and info["paper_name"] and not info["folder_path"]:
            fallback = papers_dir / info["paper_name"]
            if fallback.is_dir() and not _is_reparse(fallback):
                folder = fallback
        if folder is None or not folder.is_dir():
            detail = f"active ledger item {paper_number} ({info['paper_name']!r}): formal folder missing or outside isolated root"
            violations.append(detail)
            findings.append(_finding("formal_folder_missing", "repair", "formal", papers_dir, detail))
            papers.append({"paper_number": paper_number, "paper_name": info["paper_name"], "folder_exists": False, "valid": False})
            continue
        try:
            value = validate_formal_paper(folder, expected_paper_name=info["paper_name"])
            validated_count += 1
            papers.append({
                "paper_number": value["paper_number"], "paper_name": value["paper_name"],
                "folder_exists": True, "valid": True,
                "doi": str(value.get("metadata", {}).get("identifiers", {}).get("doi", "") if isinstance(value.get("metadata"), dict) else ""),
            })
        except Exception as exc:
            detail = f"active {paper_number} ({info['paper_name']!r}): {exc}"
            violations.append(detail)
            findings.append(_finding("formal_validation_failed", "repair", "formal", folder, detail))
            papers.append({"paper_number": paper_number, "paper_name": info["paper_name"], "folder_exists": True, "valid": False, "error": str(exc)})
    safe_ledger_items = dict(ledger_items)
    unsafe_active = [number for number, info in active.items() if _contained_formal_path(info["folder_path"], papers_dir, project_root) is None]
    if unsafe_active:
        violations.append(f"publication validation skipped for unsafe active paths: {unsafe_active}")
    else:
        try:
            publication = validate_publication_state(
                papers_dir=str(papers_dir), ledger_items=safe_ledger_items,
                project_root=project_root,
            )
            if not publication.valid:
                violations.append(f"publication_state: {publication.issues or 'invalid'}")
                findings.append(_finding("publication_state_invalid", "repair", "formal", papers_dir, str(publication.issues or "invalid")))
        except Exception as exc:
            violations.append(f"publication_state exception: {exc}")
            findings.append(_finding("publication_state_unreadable", "repair", "formal", papers_dir, str(exc)))
    if papers_dir.exists():
        formal_dirs = [path for path in papers_dir.iterdir() if path.is_dir() and not path.name.startswith(".") and not _is_reparse(path)]
        if expected == 0 and not active and formal_dirs:
            violations.append(f"untracked formal directories: {[path.name for path in formal_dirs]}")
            findings.append(_finding("formal_untracked", "repair", "formal", papers_dir, str([path.name for path in formal_dirs])))
    active_count = len(active)
    if expected == 0 and active_count == 0 and validated_count == 0 and not violations:
        health = "HEALTHY"
    elif active_count != expected:
        health = "DEGRADED" if papers_dir.exists() else "INVALID"
    elif validated_count != active_count or violations:
        health = "DEGRADED"
    else:
        health = "HEALTHY"
    return {
        "exists": papers_dir.exists(), "ledger_active_count": active_count,
        "validated_count": validated_count, "expected": expected,
        "health": health, "violations": violations, "papers": papers,
        "findings": [item.to_dict() for item in findings],
    }


def _audit_notebooks(data_root: Path) -> dict[str, Any]:
    root = data_root / "discovery" / "keyword_notebooks"
    findings: list[ResetAuditFinding] = []
    notebooks: list[dict[str, Any]] = []
    cursors: list[dict[str, Any]] = []
    profile_hashes: dict[str, str] = {}
    if not root.exists():
        finding = _finding(
            "notebook_root_missing", "repair", "notebook", root,
            "no executable discovery notebook configuration is present",
        )
        return {
            "exists": False, "notebooks": [], "cursors": [],
            "profile_hashes": {}, "findings": [finding.to_dict()],
        }
    if _path_has_reparse_component(data_root, root):
        finding = _finding(
            "notebook_root_symlink", "repair", "notebook", root,
            "keyword_notebooks root is a symlink/reparse point",
        )
        return {
            "exists": True, "notebooks": [], "cursors": [],
            "profile_hashes": {}, "findings": [finding.to_dict()],
        }
    from src.discovery.backfill_state import describe_nonpristine_unbound_backfill, is_strictly_pristine_unbound_backfill
    from src.discovery.contracts.notebook import validate_discovery_readiness, validate_notebook
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        finding = _finding(
            "notebook_root_unreadable", "repair", "notebook", root,
            f"directory enumeration failed: {type(exc).__name__}: {exc}",
        )
        return {
            "exists": True, "notebooks": [], "cursors": [],
            "profile_hashes": {}, "findings": [finding.to_dict()],
        }
    for path in entries:
        if path.name.endswith(".json.lock"):
            continue
        if path.suffix.casefold() != ".json":
            findings.append(_finding(
                "notebook_unknown_member", "repair", "notebook", path,
                "unknown notebook directory member",
            ))
            continue
        if _is_reparse(path):
            findings.append(_finding("notebook_symlink", "repair", "notebook", path, "notebook symlink is not read"))
            continue
        raw, error = _load_json(path)
        if error or not isinstance(raw, dict):
            findings.append(_finding("notebook_unreadable", "repair", "notebook", path, error or "notebook root is not an object"))
            continue
        try:
            notebook = validate_notebook(raw)
        except Exception as exc:
            findings.append(_finding("notebook_invalid", "repair", "notebook", path, str(exc)))
            continue
        keyword = str(notebook["keyword_zh"])
        keyword_id = str(notebook["keyword_id"])
        if notebook.get("enabled") is False:
            findings.append(_finding("disabled_notebook", "info", "notebook", path, f"{keyword} is disabled"))
            notebooks.append({"keyword_zh": keyword, "keyword_id": keyword_id, "enabled": False, "ready": False})
            continue
        readiness = validate_discovery_readiness(notebook)
        for error_text in readiness.errors:
            findings.append(_finding("notebook_not_ready", "repair", "notebook", path, error_text))
        profile = notebook.get("relevance_profile")
        if isinstance(profile, dict) and profile.get("profile_hash"):
            profile_hashes[keyword_id] = str(profile["profile_hash"])
        notebooks.append({
            "keyword_zh": keyword, "keyword_id": keyword_id, "enabled": True,
            "ready": readiness.ready, "readiness_errors": list(readiness.errors),
        })
        queries = notebook.get("search_queries", {})
        for query_id, query in queries.items():
            if not isinstance(query, dict) or not query.get("active", True):
                continue
            providers = query.get("providers")
            if not isinstance(providers, dict):
                findings.append(_finding("notebook_provider_state_invalid", "repair", "notebook", path, f"search_queries.{query_id}.providers is invalid"))
                continue
            for provider in ("openalex", "crossref"):
                provider_state = providers.get(provider)
                if not isinstance(provider_state, dict):
                    findings.append(_finding("notebook_provider_state_invalid", "repair", "notebook", path, f"{query_id}/{provider} is invalid"))
                    continue
                backfill = provider_state.get("backfill")
                if not isinstance(backfill, dict):
                    findings.append(_finding("notebook_backfill_invalid", "repair", "notebook", path, f"{query_id}/{provider}/backfill is invalid"))
                else:
                    pristine = is_strictly_pristine_unbound_backfill(backfill)
                    reasons = list(describe_nonpristine_unbound_backfill(backfill)) if not pristine else []
                    if not pristine:
                        findings.append(_finding("notebook_backfill_nonpristine", "reset", "notebook", path, f"{query_id}/{provider}: {reasons}"))
                    cursors.append({
                        "keyword_zh": keyword, "keyword_id": keyword_id,
                        "query_id": query_id, "provider": provider, "lane": "backfill",
                        "cursor": str(backfill.get("cursor") or ""),
                        "generation": int(backfill.get("generation") or 1),
                        "exhausted": bool(backfill.get("exhausted")),
                        "pages_succeeded": int(backfill.get("pages_succeeded") or 0),
                        "request_signature": str(backfill.get("request_signature") or ""),
                        "is_strictly_pristine_unbound": pristine,
                        "nonpristine_reasons": reasons,
                    })
                refresh = provider_state.get("refresh")
                if not isinstance(refresh, dict):
                    findings.append(_finding("notebook_refresh_invalid", "repair", "notebook", path, f"{query_id}/{provider}/refresh is invalid"))
    if not any(item.get("enabled") and item.get("ready") for item in notebooks):
        findings.append(_finding(
            "no_enabled_notebook_configuration", "repair", "notebook", root,
            "no ready enabled discovery notebook is configured",
        ))
    return {
        "exists": True, "notebooks": notebooks, "cursors": cursors,
        "profile_hashes": profile_hashes,
        "findings": [item.to_dict() for item in findings],
    }


def _audit_page_journals(data_root: Path, profile_hashes: dict[str, str]) -> dict[str, Any]:
    pending = data_root / "discovery" / "pending_pages"
    findings: list[ResetAuditFinding] = []
    if not pending.exists():
        return {"exists": False, "total_pages": 0, "pages": 0, "total_candidates": 0, "findings": []}
    if _is_reparse(pending):
        finding = _finding(
            "page_journal_root_symlink", "repair", "page_journal", pending,
            "pending_pages root is a symlink/reparse point",
        )
        return {
            "exists": True, "total_pages": 0, "pages": 0,
            "total_candidates": 0, "findings": [finding.to_dict()],
        }
    from src.discovery.contracts.page_journal import (
        classify_candidate_lifecycle,
        validate_journal_drain_index,
    )
    from src.discovery.stores.journal_drain_index import JournalDrainIndex
    from src.discovery.stores.page_journal_store import PageJournalStoreV4 as PageJournalStore
    store = PageJournalStore(pending)
    total_pages = 0
    total_candidates = 0
    by_state: dict[str, int] = {}
    by_relevance: dict[str, int] = {}
    by_candidate_status: dict[str, int] = {}
    paths: list[Path] = []
    unsafe_tree = False
    for path in sorted(
        _iter_tree(pending, findings=findings, component="page_journal") or (),
        key=lambda item: item.as_posix(),
    ):
        if _is_reparse(path):
            unsafe_tree = True
            findings.append(_finding("page_journal_symlink", "repair", "page_journal", path, "journal symlink is not read"))
            continue
        if path.is_dir():
            continue
        if path.suffix.casefold() == ".lock":
            continue
        if path.suffix.casefold() != ".json":
            findings.append(_finding(
                "page_journal_unknown_member", "repair", "page_journal", path,
                "unknown pending-pages member",
            ))
            continue
        total_pages += 1
        raw, error = _load_json(path)
        if error or not isinstance(raw, dict):
            findings.append(_finding("page_journal_unreadable", "repair", "page_journal", path, error or "root is not an object"))
            continue
        candidates = raw.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    findings.append(_finding("page_candidate_invalid", "repair", "page_journal", path, "candidate is not an object"))
                    continue
                total_candidates += 1
                status = str(candidate.get("status") or "unknown")
                by_candidate_status[status] = by_candidate_status.get(status, 0) + 1
                lifecycle = classify_candidate_lifecycle(status)
                if lifecycle.value == "invalid":
                    findings.append(_finding("unknown_candidate_lifecycle", "repair", "page_journal", path, f"unknown candidate status {status!r}"))
                relevance = candidate.get("relevance")
                if isinstance(relevance, dict):
                    state = str(relevance.get("state") or "profile_unbound")
                    by_relevance[state] = by_relevance.get(state, 0) + 1
        try:
            page = store.read(path)
            paths.append(path)
            state = str(page.get("state") or "unknown")
            by_state[state] = by_state.get(state, 0) + 1
        except Exception as exc:
            findings.append(_finding("page_journal_invalid", "repair", "page_journal", path, f"{type(exc).__name__}: {exc}"))
    index_violations: list[str] = []
    if paths and not unsafe_tree:
        try:
            index = JournalDrainIndex.build(store, active_profile_hashes=profile_hashes)
            index_violations = list(validate_journal_drain_index(index))
        except Exception as exc:
            index_violations = [f"{type(exc).__name__}: {exc}"]
    for detail in index_violations:
        findings.append(_finding("journal_drain_index_invalid", "repair", "page_journal", pending, detail))
    from src.discovery.contracts.page_journal import NONTERMINAL_CANDIDATE_STATES
    unfinished_statuses = NONTERMINAL_CANDIDATE_STATES
    unfinished = sum(count for status, count in by_candidate_status.items() if status in unfinished_statuses)
    return {
        "exists": True, "total_pages": total_pages, "pages": total_pages,
        "total_candidates": total_candidates,
        "corrupt_page_count": sum(1 for item in findings if item.code == "page_journal_unreadable"),
        "journal_violations": index_violations,
        "journal_drain_index_healthy": not findings,
        "by_page_state": by_state, "by_relevance_state": by_relevance,
        "by_candidate_status": by_candidate_status, "unfinished_candidates": unfinished,
        "findings": [item.to_dict() for item in findings],
    }


def _lock_kind(path: Path, data_root: Path) -> str | None:
    rel = path.relative_to(data_root).parts
    lower = tuple(part.casefold() for part in rel)
    name = path.name.casefold()
    if lower[:3] == ("transactions", "locks", "relevance_profiles.lock"):
        return "relevance"
    if lower[:2] == ("transactions", "locks"):
        return "transaction"
    if lower[:2] == ("transactions", "relevance_profiles"):
        return "relevance"
    if lower and lower[0] == "catalog" and name == "paper_number_ledger.json.lock":
        return "ledger"
    if lower and lower[0] == "paper_raw" and name == ".paper_raw_write.lock":
        return "paper_raw"
    if len(lower) >= 3 and lower[0:2] == ("discovery", "keyword_notebooks") and name.endswith(".json.lock"):
        return "keyword_notebook"
    if len(lower) >= 3 and lower[0:3] == ("discovery", "locks", "doi"):
        return "doi"
    if len(lower) >= 3 and lower[0:2] == ("discovery", "locks"):
        if name == "page_journal.lock":
            return "page_journal"
        if name.endswith(".backfill.lock"):
            return "backfill"
        if "resolution" in lower or "workspace" in lower or "import" in lower:
            return "discovery_aux"
    return None


def _audit_locks(data_root: Path) -> dict[str, Any]:
    findings: list[ResetAuditFinding] = []
    all_locks: list[dict[str, Any]] = []
    categorized: dict[str, list[dict[str, Any]]] = {}
    unknown: list[dict[str, Any]] = []
    for path in sorted(
        (item for item in (_iter_tree(data_root, findings=findings, component="locks") or ()) if item.suffix.casefold() == ".lock"),
        key=lambda item: item.as_posix(),
    ):
        if _is_reparse(path):
            finding = _finding("lock_symlink", "repair", "locks", path, "lock symlink is not probed")
            findings.append(finding)
            continue
        info = _classify_lock(path)
        kind = _lock_kind(path, data_root)
        info["kind"] = kind or "unknown"
        all_locks.append(info)
        if kind is None:
            unknown.append(info)
            findings.append(_finding("unknown_lock", "repair", "locks", path, "lock path is not part of the known lock contract"))
        else:
            categorized.setdefault(kind, []).append(info)
    active: list[str] = []
    stale: list[str] = []
    unverifiable: list[str] = []
    for info in all_locks:
        label = f"{info['kind']}:{Path(info['path']).name}"
        if info["classification"] == "active":
            active.append(label)
            if info["kind"] != "unknown":
                findings.append(_finding("active_lock", "block", "locks", info["path"], label))
        elif info["classification"] == "stale":
            stale.append(label)
        else:
            unverifiable.append(label)
            findings.append(_finding("lock_unverifiable", "repair", "locks", info["path"], label))
    applying: list[str] = []
    committed: list[str] = []
    relevance_dir = data_root / "transactions" / "relevance_profiles"
    if relevance_dir.exists() and _is_reparse(relevance_dir):
        findings.append(_finding(
            "relevance_transaction_symlink", "repair", "transactions",
            relevance_dir, "relevance transaction root is a symlink/reparse point",
        ))
    elif relevance_dir.exists():
        for path in sorted(
            (item for item in (_iter_tree(relevance_dir, findings=findings, component="transactions") or ()) if item.suffix.casefold() == ".json"),
            key=lambda item: item.as_posix(),
        ):
            if path.name.endswith((".commit.json", ".manifest.json")):
                continue
            raw, error = _load_json(path)
            if error or not isinstance(raw, dict):
                findings.append(_finding("relevance_transaction_corrupt", "repair", "transactions", path, error or "not an object"))
                continue
            try:
                from src.discovery.relevance_profiles import validate_relevance_profile_transaction_journal
                validate_relevance_profile_transaction_journal(raw, journal_path=path)
            except Exception as exc:
                findings.append(_finding(
                    "relevance_transaction_invalid", "repair", "transactions", path,
                    f"{type(exc).__name__}: {exc}",
                ))
            state = str(raw.get("state") or "")
            if state == "applying":
                applying.append(path.name)
                findings.append(_finding("active_relevance_transaction", "block", "transactions", path, "state=applying"))
            elif state in {"committed", "aborted", "complete"}:
                committed.append(path.name)
            else:
                findings.append(_finding("unknown_relevance_transaction_state", "repair", "transactions", path, state))
    commit_active, commit_history = _audit_transaction_dir(
        data_root / "transactions" / "commit",
        {"prepared", "staging_complete", "final_installed", "ledger_active", "category_reconcile_requested", "source_deleted"},
        "commit", findings, data_root=data_root,
    )
    rollback_active, rollback_history = _audit_transaction_dir(
        data_root / "transactions" / "rollback",
        {"prepared", "formal_quarantined", "raw_installed", "ledger_reserved", "category_links_removed", "quarantine_removed"},
        "rollback", findings, data_root=data_root,
    )
    return {
        "all_locks": all_locks,
        "transaction_locks": categorized.get("transaction", []),
        "doi_lease_locks": categorized.get("doi", []),
        "backfill_locks": categorized.get("backfill", []),
        "keyword_notebook_locks": categorized.get("keyword_notebook", []),
        "resolution_locks": categorized.get("discovery_aux", []),
        "ledger_locks": categorized.get("ledger", []),
        "paper_raw_locks": categorized.get("paper_raw", []),
        "relevance_profile_lock": (categorized.get("relevance", []) or [None])[0],
        "page_journal_lock": (categorized.get("page_journal", []) or [None])[0],
        "unknown_locks": unknown,
        "applying_relevance_transactions": applying,
        "committed_relevance_transactions": committed,
        "commit_journals": commit_active,
        "commit_history": commit_history,
        "rollback_journals": rollback_active,
        "rollback_history": rollback_history,
        "active_locks": active, "stale_locks": stale,
        "unverifiable_locks": unverifiable,
        "findings": [item.to_dict() for item in findings],
    }


def _audit_transaction_dir(
    directory: Path, active_states: set[str], kind: str,
    findings: list[ResetAuditFinding],
    *, data_root: Path | None = None,
) -> tuple[list[str], list[str]]:
    active: list[str] = []
    history: list[str] = []
    if not directory.exists():
        return active, history
    if _is_reparse(directory):
        findings.append(_finding(
            f"{kind}_transaction_root_symlink", "repair", "transactions",
            directory, "transaction root is a symlink/reparse point",
        ))
        return active, history
    transaction_root = (
        Path(data_root) / "transactions" if data_root is not None else directory.parent
    )
    raw_root = transaction_root.parent / "paper_raw"
    papers_root = transaction_root.parent / "papers"
    for path in sorted(
        (item for item in (_iter_tree(directory, findings=findings, component="transactions") or ()) if item.suffix.casefold() == ".json"),
        key=lambda item: item.as_posix(),
    ):
        raw, error = _load_json(path)
        if error or not isinstance(raw, dict):
            findings.append(_finding(f"{kind}_transaction_corrupt", "repair", "transactions", path, error or "not an object"))
            continue
        state = str(raw.get("phase") or raw.get("state") or "")
        validated = None
        try:
            if data_root is not None and kind == "commit":
                from src.services.transaction_paths import validate_commit_journal
                validated = validate_commit_journal(
                    raw, journal_path=path, paper_raw_root=raw_root,
                    papers_root=papers_root, transaction_root=transaction_root,
                )
            elif data_root is not None and kind == "rollback":
                from src.services.transaction_paths import validate_rollback_journal
                validated = validate_rollback_journal(
                    raw, journal_path=path, paper_raw_root=raw_root,
                    papers_root=papers_root, transaction_root=transaction_root,
                )
        except Exception as exc:
            findings.append(_finding(
                f"{kind}_transaction_invalid", "repair", "transactions", path,
                f"{type(exc).__name__}: {exc}",
            ))
            # A malformed journal is repair-required, never an active blocker.
            if path.parent.name == "completed":
                history.append(path.name)
            continue
        if isinstance(validated, dict):
            state = str(validated.get("phase") or state)
        if state in active_states:
            active.append(path.name)
            findings.append(_finding(f"active_{kind}_transaction", "block", "transactions", path, state))
        elif state in {"complete", "completed", "committed"} or path.parent.name == "completed":
            history.append(path.name)
        else:
            findings.append(_finding(f"unknown_{kind}_transaction_state", "repair", "transactions", path, state))
    return active, history


def _audit_registry(data_root: Path, *, project_root: Path) -> dict[str, Any]:
    issues: list[str] = []
    repair: list[str] = []
    unsettled: list[str] = []
    for root_name in ("paper_raw", "papers", "catalog"):
        candidate = data_root / root_name
        if _path_has_reparse_component(data_root, candidate):
            issues.append(f"{root_name}_root_symlink_or_reparse")
    if issues:
        return {
            "registry_available": False, "repair_backlog_numbers": [],
            "repair_backlog_count": 0, "unsettled_numbers": [],
            "unsettled_count": 0, "issues": issues,
            "findings": [
                _finding("registry_root_unsafe", "repair", "registry", data_root, issue).to_dict()
                for issue in issues
            ],
        }
    try:
        from src.discovery.workspace_registry import build_workspace_registry
        from src.library.paper_number_ledger import PaperNumberLedger
        result = build_workspace_registry(
            paper_raw_dir=data_root / "paper_raw",
            papers_dir=data_root / "papers",
            ledger=PaperNumberLedger(data_root / "catalog" / "paper_number_ledger.json"),
            project_root=project_root,
        )
        if result.registry is not None:
            repair = sorted(result.registry.repair_backlog_numbers)
            unsettled = sorted(result.unsettled_numbers)
        issues.extend(str(issue) for issue in result.issues)
    except Exception as exc:
        issues.append(f"build_registry failed: {type(exc).__name__}: {exc}")
    return {
        "registry_available": not any("failed" in issue for issue in issues),
        "repair_backlog_numbers": repair, "repair_backlog_count": len(repair),
        "unsettled_numbers": unsettled, "unsettled_count": len(unsettled),
        "issues": issues,
        "findings": [
            _finding("registry_issue", "repair", "registry", data_root, issue).to_dict()
            for issue in issues
        ],
    }


def _component_findings(*components: Mapping[str, Any]) -> list[ResetAuditFinding]:
    result: list[ResetAuditFinding] = []
    for component in components:
        for value in component.get("findings", []) or []:
            if isinstance(value, Mapping):
                try:
                    result.append(ResetAuditFinding(
                        str(value["code"]), str(value["severity"]),
                        str(value["component"]), str(value["path"]), str(value["detail"]),
                    ))
                except (KeyError, TypeError):
                    result.append(_finding("malformed_finding", "repair", "audit", "", str(value)))
    return result


def audit_reset_state(*, data_root: Path | None = None, expected_formal_count: int = 4) -> dict[str, Any]:
    root = Path(data_root or DEFAULT_DATA).absolute()
    snapshot_findings: list[ResetAuditFinding] = []
    before = _snapshot_facts(root, findings=snapshot_findings)
    project_root = root.parent
    notebooks = _audit_notebooks(root)
    ledger = _audit_ledger(root)
    paper_raw = _audit_paper_raw(root)
    formal = _audit_formal(
        root, expected_formal_count,
        ledger.get("items") if isinstance(ledger.get("items"), dict) else {},
        project_root=project_root,
    )
    journals = _audit_page_journals(root, notebooks.get("profile_hashes", {}))
    cursors = {"exists": notebooks.get("exists", False), "notebooks": notebooks.get("notebooks", []), "cursors": notebooks.get("cursors", [])}
    locks = _audit_locks(root)
    registry = _audit_registry(root, project_root=project_root)
    after = _snapshot_facts(root, findings=snapshot_findings)
    changes: list[dict[str, Any]] = []
    for relative in sorted(set(before) | set(after)):
        if relative not in before:
            changes.append({"path": relative, "change_type": "added", "details": ["file_created"]})
        elif relative not in after:
            changes.append({"path": relative, "change_type": "deleted", "details": ["file_removed"]})
        elif before[relative] != after[relative]:
            details = []
            for key in ("type", "size", "streaming_sha256", "symlink_target"):
                if before[relative].get(key) != after[relative].get(key):
                    details.append("symlink_target_changed" if key == "symlink_target" else f"{key}_changed")
            changes.append({"path": relative, "change_type": "modified", "details": details})

    findings = snapshot_findings + _component_findings(
        notebooks, ledger, paper_raw, formal, journals, locks, registry
    )
    if changes:
        findings.append(_finding("zero_write_proof_failed", "repair", "zero_write", root, f"{len(changes)} paths changed during read-only audit"))
    if not ledger.get("readable", False):
        findings.append(_finding("ledger_unreadable", "repair", "ledger", ledger.get("path", root), ledger.get("error", "ledger unreadable")))
    if formal.get("health") not in {"HEALTHY"}:
        findings.append(_finding("formal_library_unhealthy", "repair", "formal", root / "papers", str(formal.get("violations", []))))
    raw = paper_raw
    if raw.get("digital_workspaces") or raw.get("empty_directories") or raw.get("gitkeep_only") or raw.get("orphan_files") or raw.get("non_digital_members"):
        findings.append(_finding("paper_raw_not_empty", "reset", "paper_raw", root / "paper_raw", "paper_raw contains workspace or orphan state"))
    for label, values in (("repair_backlog", registry.get("repair_backlog_numbers", [])), ("unsettled_workspace", registry.get("unsettled_numbers", []))):
        if values:
            findings.append(_finding(label, "repair", "registry", root, str(values)))
    if ledger.get("reserved_but_not_metadata_staged"):
        findings.append(_finding("reserved_ledger_state", "repair", "ledger", root / "catalog", str(ledger["reserved_but_not_metadata_staged"])))
    if journals.get("by_relevance_state", {}).get("profile_unbound", 0):
        findings.append(_finding("profile_unbound_candidates", "reset", "page_journal", root / "discovery" / "pending_pages", str(journals["by_relevance_state"]["profile_unbound"])))
    if journals.get("unfinished_candidates", 0):
        findings.append(_finding("unfinished_candidates", "reset", "page_journal", root / "discovery" / "pending_pages", str(journals["unfinished_candidates"])))
    if journals.get("by_relevance_state", {}).get("verification_deferred", 0):
        findings.append(_finding("verification_deferred_candidates", "reset", "page_journal", root / "discovery" / "pending_pages", str(journals["by_relevance_state"]["verification_deferred"])))
    if expected_formal_count != ledger.get("active_count", 0) and not (expected_formal_count == 0 and ledger.get("active_count", 0) == 0):
        findings.append(_finding("formal_count_mismatch", "repair", "formal", root / "papers", f"active={ledger.get('active_count', 0)} expected={expected_formal_count}"))
    raw_lock = paper_raw.get("raw_write_lock")
    if raw_lock and raw_lock.get("classification") == "active":
        findings.append(_finding("active_paper_raw_lock", "block", "locks", raw_lock.get("path", ""), "paper_raw writer lock is active"))
    elif raw_lock and raw_lock.get("classification") == "unverifiable":
        findings.append(_finding("paper_raw_lock_unverifiable", "repair", "locks", raw_lock.get("path", ""), "paper_raw writer lock cannot be probed"))
    for cursor in notebooks.get("cursors", []):
        if cursor.get("lane") == "backfill" and not cursor.get("is_strictly_pristine_unbound", True):
            # The notebook component already reports the precise finding; the
            # aggregate check is intentionally independent and never short-circuits.
            pass
    severity_rank = {"info": 0, "reset": 1, "repair": 2, "block": 3}
    highest = max((severity_rank[item.severity] for item in findings), default=0)
    readiness = {0: "READY", 1: "RESET_REQUIRED", 2: "REPAIR_REQUIRED", 3: "BLOCKED_BY_ACTIVE_TRANSACTION"}[highest]
    reasons = [f"{item.code}: {item.detail}" for item in findings if item.severity != "info"]
    zero_write = not changes
    return {
        "schema_version": SCHEMA_VERSION,
        "audited_at": _now(),
        "data_root": str(root),
        "formal_library_health": formal.get("health", "INVALID"),
        "fresh_discovery_readiness": readiness,
        "readiness_reasons": reasons,
        "expected_formal_count": expected_formal_count,
        "findings": [item.to_dict() for item in findings],
        "zero_write_evidence": {
            "files_before": len(before), "files_after": len(after),
            "files_added": sum(1 for item in changes if item["change_type"] == "added"),
            "files_deleted": sum(1 for item in changes if item["change_type"] == "deleted"),
            "files_modified": sum(1 for item in changes if item["change_type"] == "modified"),
            "files_changed": len(changes),
            "changed_paths": [item["path"] for item in changes[:50]],
            "change_details": changes[:50], "zero_write": zero_write,
        },
        "formal": formal, "paper_raw": paper_raw, "ledger": ledger,
        "page_journals": journals, "cursors": cursors,
        "locks_and_transactions": locks, "registry": registry,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only discovery reset-state audit")
    parser.add_argument("--expected-formal-count", type=int, default=4)
    parser.add_argument("--data-root", type=str, default=None)
    parser.add_argument("--json-report", type=str, default=None)
    args = parser.parse_args()
    audited_root = Path(args.data_root).absolute() if args.data_root else DEFAULT_DATA.absolute()
    report_path: Path | None = None
    if args.json_report:
        try:
            report_path = resolve_safe_report_path(
                args.json_report, audited_root=audited_root,
            )
        except ValueError as exc:
            print(f"ERROR: unsafe --json-report path: {exc}", file=sys.stderr)
            return 2
    report = audit_reset_state(
        data_root=audited_root, expected_formal_count=args.expected_formal_count,
    )
    json_text = json.dumps(report, ensure_ascii=False, indent=2)
    if report_path is not None:
        _write_report_atomically(
            report_path, json_text.encode("utf-8"), audited_root=audited_root,
        )
        print(f"Report written to {report_path}")
    else:
        print(json_text)
    if report["fresh_discovery_readiness"] == "BLOCKED_BY_ACTIVE_TRANSACTION":
        return 3
    if report["fresh_discovery_readiness"] in {"RESET_REQUIRED", "REPAIR_REQUIRED"}:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

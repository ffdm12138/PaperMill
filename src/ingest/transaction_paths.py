"""Unified transaction path validation — commit, rollback, and migration.

Every transaction journal path is checked against these rules before any
destructive operation (rmtree, copytree, os.replace):

    1. transaction_id is a canonical UUID (no legacy non-UUID format).
    2. paper_number matches ``^[0-9]{16}$``.
    3. paper_name contains no path separators, ``..``, drive prefixes, or NUL.
    4. Every resolved path stays inside its expected root (containment).
    5. Symlink chains are rejected — no symlink on any parent component
       between root and candidate.
    6. Expected basename matches at the final path segment.

All three transaction domains (commit, rollback, legacy migration) MUST
use this module instead of ad-hoc path checks.
"""

from __future__ import annotations

import re
import uuid as _uuid
from pathlib import Path
from typing import Any, Mapping

from src.naming import validate_paper_name as _validate_paper_name

# ── Exceptions ─────────────────────────────────────────────────────────

class TransactionPathError(RuntimeError):
    """Base for all transaction-path validation failures."""


class TransactionIdentityError(TransactionPathError):
    """Invalid or unsafe transaction identifier (ID, number, or name)."""


class TransactionContainmentError(TransactionPathError):
    """Path resolves outside its expected root directory."""


class TransactionSymlinkError(TransactionPathError):
    """Symlink chain detected — destructive operation refused."""


# ── Pattern constants ──────────────────────────────────────────────────

_PAPER_NUMBER_RE = re.compile(r"^\d{16}$")


# ── Identity validators ────────────────────────────────────────────────


def validate_transaction_id(value: str) -> str:
    """Return canonical (lowercase-hex) UUID string or raise.

    Rejects empty strings, embedded ``/``, ``\\``, ``..``, and any
    non-UUID format.  There is no legacy non-UUID format to support.
    """
    if not isinstance(value, str) or not value:
        raise TransactionIdentityError(
            "transaction_id must be a non-empty string"
        )
    # Guard against path escape before UUID parsing
    _assert_no_path_escape(value)
    try:
        uid = _uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise TransactionIdentityError(
            f"transaction_id is not a valid UUID: {value!r}"
        ) from exc
    canonical = str(uid)
    # Double-check the canonical UUID round-trip is also safe
    _assert_no_path_escape(canonical)
    return canonical


def validate_paper_number(value: str) -> str:
    """Return validated 16-digit paper-number string or raise."""
    if not isinstance(value, str) or not value:
        raise TransactionIdentityError("paper_number must be a non-empty string")
    if not _PAPER_NUMBER_RE.match(value):
        raise TransactionIdentityError(
            f"paper_number must be exactly 16 digits: {value!r}"
        )
    return value


def validate_paper_name(value: str) -> str:
    """Return validated paper_name or raise.

    Reuses ``src.naming.validate_paper_name`` for the character-level
    rules, then adds extra guards for ``..``, separators, drive prefixes
    and NUL beyond what the base validator enforces.
    """
    if not isinstance(value, str) or not value:
        raise TransactionIdentityError("paper_name must be a non-empty string")
    _assert_no_path_escape(value)
    # Reuse the project's existing paper_name character checks
    _validate_paper_name(value)
    return value


def _assert_no_path_escape(value: str) -> None:
    """Reject characters that could break journal filename safety."""
    if not value:
        raise TransactionIdentityError("value must not be empty")
    if "/" in value or "\\" in value:
        raise TransactionIdentityError(
            f"value contains path separator: {value!r}"
        )
    if ".." in value:
        raise TransactionIdentityError(
            f"value contains '..' path escape: {value!r}"
        )
    # Reject Windows drive prefix (e.g. "C:")
    if len(value) >= 2 and value[1] == ":" and value[0].isascii():
        raise TransactionIdentityError(
            f"value contains drive prefix: {value!r}"
        )
    # Reject NUL
    if "\0" in value:
        raise TransactionIdentityError("value contains NUL character")


# ── Path containment and symlink helpers ────────────────────────────────


def resolve_existing_or_future_path(path: Path) -> Path:
    """Resolve *path* to an absolute canonical form.

    - If *path* exists, returns ``path.resolve(strict=False)``.
    - If *path* does not exist, resolves the nearest existing parent
      and re-appends the non-existing tail, so the returned Path is
      always absolute and has no relative components.
    """
    if path.exists():
        return path.resolve(strict=False)
    # Walk up until we find an existing parent
    resolved_parts: list[str] = []
    current = path
    while current and not current.exists():
        resolved_parts.append(current.name)
        current = current.parent
    if not current or not current.exists():
        # Nothing at all exists — use the absolute form of the input
        return path.absolute().resolve()
    base = current.resolve(strict=False)
    for part in reversed(resolved_parts):
        base = base / part
    return base


def assert_resolved_child(root: Path, candidate: Path, *, field: str) -> Path:
    """Assert *candidate* (resolved) is a child of *root* (resolved).

    Returns the resolved candidate path.
    """
    root_resolved = root.resolve(strict=False)
    cand_resolved = resolve_existing_or_future_path(candidate)
    try:
        cand_resolved.relative_to(root_resolved)
    except ValueError:
        raise TransactionContainmentError(
            f"'{field}': {str(cand_resolved)!r} is not under "
            f"expected root {str(root_resolved)!r}"
        )
    return cand_resolved


def assert_not_root(root: Path, candidate: Path, *, field: str) -> None:
    """Assert *candidate* is not the same as *root*."""
    root_resolved = root.resolve(strict=False)
    cand_resolved = resolve_existing_or_future_path(candidate)
    if cand_resolved == root_resolved:
        raise TransactionContainmentError(
            f"'{field}' resolves to the root directory itself: "
            f"{str(cand_resolved)!r}"
        )


def _symlink_in_chain(root: Path, leaf: Path) -> Path | None:
    """Walk from *root* toward *leaf*; return the first symlink found, or None.

    Checks the ORIGINAL (unresolved) path components so symlinks are detected
    even if they would resolve to a different location inside the root.
    Non-existing path segments (future paths) are skipped.
    """
    root_abs = root.absolute()
    leaf_abs = leaf.absolute()
    try:
        leaf_abs.relative_to(root_abs)
    except ValueError:
        return None  # Not under root

    relative = leaf_abs.relative_to(root_abs)
    parts = relative.parts
    if not parts:
        return None  # same as root

    current = root_abs
    for part in parts:
        current = current / part
        try:
            if current.is_symlink():
                return current
        except OSError:
            pass  # might not exist yet for a future path
    return None


def assert_no_symlink_chain(root: Path, candidate: Path, *, field: str) -> None:
    """Reject if any component between *root* and *candidate* is a symlink.

    Checks the existing directory tree from root toward candidate.
    If candidate does not exist, checks the nearest existing ancestor chain.
    """
    symlink = _symlink_in_chain(root, candidate)
    if symlink is not None:
        root_resolved = root.resolve(strict=False)
        raise TransactionSymlinkError(
            f"'{field}': symlink detected at {str(symlink)!r} "
            f"in path {str(candidate)!r} under root "
            f"{str(root_resolved)!r}"
        )


def assert_expected_name(candidate: Path, expected: str, *, field: str) -> None:
    """Assert the final path component of *candidate* equals *expected*."""
    name = candidate.name
    if name != expected:
        raise TransactionIdentityError(
            f"'{field}': expected basename {expected!r}, got {name!r}"
        )


def assert_exact_path(candidate: Path, expected: Path, *, field: str) -> None:
    """Assert *candidate* resolves to the *exact* canonical *expected* path.

    Unlike ``check_destructive_path`` (which only verifies containment
    and basename), this enforces that the journal's stored path is the
    one and only canonical path for this transaction — no alias,
    different subdirectory, or unrelated workspace within the same root
    is accepted.
    """
    cand_resolved = resolve_existing_or_future_path(candidate)
    exp_resolved = resolve_existing_or_future_path(expected)
    if cand_resolved != exp_resolved:
        raise TransactionIdentityError(
            f"'{field}': expected {str(exp_resolved)!r}, "
            f"got {str(cand_resolved)!r}"
        )


# ── Multi-check convenience ─────────────────────────────────────────────


def check_destructive_path(
    root: Path,
    candidate: Path,
    *,
    field: str,
    expected_name: str | None = None,
    not_equal_to: Path | None = None,
) -> Path:
    """Run all containment, symlink, and optional name checks.

    When *not_equal_to* is given, also asserts that *candidate*
    does not resolve to the same path.

    Returns the resolved candidate path.
    """
    resolved = assert_resolved_child(root, candidate, field=field)
    assert_not_root(root, candidate, field=field)
    assert_no_symlink_chain(root, candidate, field=field)
    if expected_name is not None:
        assert_expected_name(candidate, expected_name, field=field)
    if not_equal_to is not None:
        other_resolved = resolve_existing_or_future_path(not_equal_to)
        if resolved == other_resolved:
            raise TransactionContainmentError(
                f"'{field}': {str(candidate)!r} resolves to the same path "
                f"as {str(not_equal_to)!r}"
            )
    return resolved


# ── Commit-path template values ─────────────────────────────────────────


def commit_journal_name(transaction_id: str) -> str:
    """Return the expected journal filename for a commit transaction."""
    return f"{transaction_id}.json"


def rollback_journal_name(transaction_id: str) -> str:
    """Return the expected journal filename for a rollback transaction."""
    return f"{transaction_id}.json"


def commit_source_workspace_path(paper_raw_root: Path, paper_number: str) -> Path:
    """Return the expected paper_raw source workspace."""
    return (paper_raw_root / paper_number).resolve()


def commit_staging_path(papers_root: Path, paper_name: str, transaction_id: str) -> Path:
    """Return the expected commit staging path."""
    return (papers_root / f".{paper_name}.staging_{transaction_id}").resolve()


def commit_final_path(papers_root: Path, paper_name: str) -> Path:
    """Return the expected commit final (formal) path."""
    return (papers_root / paper_name).resolve()


def rollback_staging_path(paper_raw_root: Path, paper_number: str, transaction_id: str) -> Path:
    """Return the expected rollback raw staging path."""
    return (paper_raw_root / f".rollback_{paper_number}_{transaction_id}").resolve()


def rollback_quarantine_path(papers_root: Path, paper_name: str, transaction_id: str) -> Path:
    """Return the expected rollback formal quarantine path."""
    return (papers_root / f".{paper_name}.rollback_quarantine_{transaction_id}").resolve()


# ── Commit journal validation ───────────────────────────────────────────


def validate_commit_journal(
    journal: Mapping[str, Any],
    *,
    journal_path: Path,
    paper_raw_root: Path,
    papers_root: Path,
    transaction_root: Path,
) -> dict[str, Any]:
    """Validate every path and identity in a commit journal.

    Returns a validated copy with all paths as resolved ``Path`` objects.
    Raises ``TransactionPathError`` (or subclass) on any violation.
    """
    raw = dict(journal)

    # --- Identity fields ---
    transaction_id = validate_transaction_id(str(raw.get("transaction_id") or ""))
    paper_number = validate_paper_number(str(raw.get("paper_number") or ""))
    paper_name = validate_paper_name(str(raw.get("paper_name") or ""))

    # --- Journal filename consistency ---
    expected_name = commit_journal_name(transaction_id)
    assert_expected_name(journal_path, expected_name, field="journal_filename")
    # Journal must be in the active or completed directory
    journal_parent = journal_path.parent.resolve(strict=False)
    active_dir = (transaction_root / "commit").resolve(strict=False)
    completed_dir = (transaction_root / "commit/completed").resolve(strict=False)
    try:
        journal_parent.relative_to(active_dir)
    except ValueError:
        try:
            journal_parent.relative_to(completed_dir)
        except ValueError:
            raise TransactionContainmentError(
                f"journal path {str(journal_path)!r} is not under "
                f"transaction commit root {str(active_dir)!r}"
            )

    # --- Phase consistency ---
    phase = str(raw.get("phase") or "")
    if phase == "complete":
        try:
            journal_parent.relative_to(completed_dir)
        except ValueError:
            raise TransactionPathError(
                f"complete journal not in completed directory: {str(journal_path)!r}"
            )
        # Also confirm it's not in the active dir (completed is subdir of active)
        try:
            journal_parent.relative_to(active_dir)
            # It IS under active_dir (since completed under commit) — that's OK
            # for containment, but we also verify the journal_path itself is
            # directly in completed_dir
            if journal_parent != completed_dir:
                raise TransactionPathError(
                    f"complete journal not directly in completed subdirectory: "
                    f"{str(journal_path)!r}"
                )
        except ValueError:
            # Not under active_dir at all — also an error
            raise TransactionPathError(
                f"complete journal {str(journal_path)!r} is not under "
                f"commit root at all"
            )
    else:
        # Non-complete: must be in active dir AND NOT in completed dir
        try:
            journal_parent.relative_to(active_dir)
        except ValueError:
            raise TransactionPathError(
                f"non-complete journal not in active directory: {str(journal_path)!r}"
            )
        # Ensure it's NOT in the completed subdirectory
        try:
            journal_parent.relative_to(completed_dir)
            raise TransactionPathError(
                f"non-complete journal found in completed subdirectory: "
                f"{str(journal_path)!r}"
            )
        except ValueError:
            pass  # Not in completed — correct

    # --- Source workspace ---
    source_raw = raw.get("source_workspace")
    source_path = Path(str(source_raw)) if source_raw else None
    if source_path is None:
        raise TransactionPathError("commit journal missing source_workspace")
    check_destructive_path(
        paper_raw_root, source_path,
        field="source_workspace",
        expected_name=paper_number,
    )
    # Enforce exact canonical path (not just containment + basename)
    expected_source = commit_source_workspace_path(paper_raw_root, paper_number)
    assert_exact_path(source_path, expected_source, field="source_workspace")

    # --- Staging ---
    staging_raw = raw.get("staging_path")
    staging_path = Path(str(staging_raw)) if staging_raw else None
    if staging_path is None:
        raise TransactionPathError("commit journal missing staging_path")
    check_destructive_path(
        papers_root, staging_path,
        field="staging_path",
        expected_name=f".{paper_name}.staging_{transaction_id}",
        not_equal_to=source_path,
    )
    # Enforce exact canonical path
    expected_staging = commit_staging_path(papers_root, paper_name, transaction_id)
    assert_exact_path(staging_path, expected_staging, field="staging_path")

    # --- Final ---
    final_raw = raw.get("final_path")
    final_path = Path(str(final_raw)) if final_raw else None
    if final_path is None:
        raise TransactionPathError("commit journal missing final_path")
    check_destructive_path(
        papers_root, final_path,
        field="final_path",
        expected_name=paper_name,
        not_equal_to=staging_path,
    )
    # Enforce exact canonical path
    expected_final = commit_final_path(papers_root, paper_name)
    assert_exact_path(final_path, expected_final, field="final_path")
    if source_path.resolve(strict=False) == final_path.resolve(strict=False):
        raise TransactionContainmentError("commit source aliases final path")

    return {
        "transaction_id": transaction_id,
        "paper_number": paper_number,
        "paper_name": paper_name,
        "phase": phase,
        "source_workspace": source_path,
        "staging_path": staging_path,
        "final_path": final_path,
    }


# ── Rollback journal validation ─────────────────────────────────────────


def validate_rollback_journal(
    journal: Mapping[str, Any],
    *,
    journal_path: Path,
    paper_raw_root: Path,
    papers_root: Path,
    transaction_root: Path,
) -> dict[str, Any]:
    """Validate every path and identity in a rollback journal.

    Returns a validated copy with all paths as resolved ``Path`` objects.
    """
    raw = dict(journal)

    # --- Identity fields ---
    transaction_id = validate_transaction_id(str(raw.get("transaction_id") or ""))
    paper_number = validate_paper_number(str(raw.get("paper_number") or ""))
    paper_name = validate_paper_name(str(raw.get("paper_name") or ""))

    # --- Journal filename ---
    expected_name = rollback_journal_name(transaction_id)
    assert_expected_name(journal_path, expected_name, field="journal_filename")

    # --- Journal containment ---
    journal_parent = journal_path.parent.resolve(strict=False)
    transaction_resolved = transaction_root.resolve(strict=False)
    try:
        journal_parent.relative_to(transaction_resolved)
    except ValueError:
        raise TransactionContainmentError(
            f"rollback journal {str(journal_path)!r} is not under "
            f"transaction root {str(transaction_resolved)!r}"
        )

    # --- Phase ---
    phase = str(raw.get("phase") or "")

    # --- Formal path ---
    formal_raw = raw.get("formal_path")
    formal_path = Path(str(formal_raw)) if formal_raw else None
    if formal_path is None:
        raise TransactionPathError("rollback journal missing formal_path")
    check_destructive_path(
        papers_root, formal_path,
        field="formal_path",
        expected_name=paper_name,
    )
    # Enforce exact canonical path
    expected_formal = commit_final_path(papers_root, paper_name)
    assert_exact_path(formal_path, expected_formal, field="formal_path")

    # --- Raw path ---
    raw_raw = raw.get("raw_path")
    raw_path = Path(str(raw_raw)) if raw_raw else None
    if raw_path is None:
        raise TransactionPathError("rollback journal missing raw_path")
    check_destructive_path(
        paper_raw_root, raw_path,
        field="raw_path",
        expected_name=paper_number,
    )
    # Enforce exact canonical path
    expected_raw = commit_source_workspace_path(paper_raw_root, paper_number)
    assert_exact_path(raw_path, expected_raw, field="raw_path")

    # --- Staging path (rollback staging in paper_raw) ---
    staging_raw = raw.get("staging_path")
    staging_path = Path(str(staging_raw)) if staging_raw else None
    if staging_path is None:
        raise TransactionPathError("rollback journal missing staging_path")
    check_destructive_path(
        paper_raw_root, staging_path,
        field="staging_path",
    )
    # Enforce exact canonical path
    expected_staging = rollback_staging_path(paper_raw_root, paper_number, transaction_id)
    assert_exact_path(staging_path, expected_staging, field="staging_path")

    # --- Formal quarantine path ---
    quarantine_raw = raw.get("formal_quarantine")
    quarantine_path = Path(str(quarantine_raw)) if quarantine_raw else None
    if quarantine_path is None:
        raise TransactionPathError("rollback journal missing formal_quarantine")
    check_destructive_path(
        papers_root, quarantine_path,
        field="formal_quarantine",
    )
    # Enforce exact canonical path
    expected_quarantine = rollback_quarantine_path(papers_root, paper_name, transaction_id)
    assert_exact_path(quarantine_path, expected_quarantine, field="formal_quarantine")

    return {
        "transaction_id": transaction_id,
        "paper_number": paper_number,
        "paper_name": paper_name,
        "phase": phase,
        "formal_path": formal_path,
        "raw_path": raw_path,
        "staging_path": staging_path,
        "formal_quarantine": quarantine_path,
    }


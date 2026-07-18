"""Neutral runtime-root and lock-path resolution for relevance profiles.

Every consumer — Discovery, plan, apply, resume, abort, and migration —
must use the same resolved paths.  The plan and transaction journal
persist all four values; apply/resume verify them byte-for-byte before
any mutation.

This module has no high-level dependencies (no coordinator, no relevance
transaction, no notebook migration, no page journal logic).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from config.settings import DISCOVERY_KEYWORD_NOTEBOOK_DIR, TRANSACTION_ROOT


@dataclass(frozen=True)
class RelevanceRuntimePaths:
    """Single authoritative runtime-root and lock-path resolution."""

    notebook_root: Path
    journal_root: Path
    transaction_root: Path
    lock_path: Path

    @classmethod
    def resolve_default(
        cls,
        *,
        notebook_root: Path,
        journal_root: Path,
        transaction_root: Path | None = None,
    ) -> "RelevanceRuntimePaths":
        """Resolve the default transaction root exactly once for a caller."""
        notebook_root = Path(notebook_root)
        journal_root = Path(journal_root)
        if transaction_root is None:
            # Production keeps relevance transactions beside the other
            # repository transactions.  Isolated callers (tests and local
            # sandboxes) keep the historical sibling layout so the resolver
            # remains self-contained and never makes the coordinator rebuild
            # this policy.
            if notebook_root.resolve() == Path(DISCOVERY_KEYWORD_NOTEBOOK_DIR).resolve():
                transaction_root = Path(TRANSACTION_ROOT) / "relevance_profiles"
            else:
                transaction_root = notebook_root.parent / "transactions" / "relevance_profiles"
        return cls.resolve(
            notebook_root=notebook_root,
            journal_root=journal_root,
            transaction_root=Path(transaction_root),
        )

    @classmethod
    def resolve(
        cls,
        *,
        notebook_root: Path,
        journal_root: Path,
        transaction_root: Path,
    ) -> "RelevanceRuntimePaths":
        notebook_root = Path(notebook_root)
        journal_root = Path(journal_root)
        transaction_root = Path(transaction_root)
        lock_path = transaction_root.parent / "locks" / "relevance_profiles.lock"
        return cls(
            notebook_root=notebook_root.resolve(),
            journal_root=journal_root.resolve(),
            transaction_root=transaction_root.resolve(),
            lock_path=lock_path.resolve(),
        )

    @classmethod
    def from_plan(cls, plan: Mapping[str, Any]) -> "RelevanceRuntimePaths":
        """Load and fully validate the four persisted resolved paths."""
        required = {
            "resolved_notebook_root", "resolved_journal_root",
            "resolved_transaction_root", "resolved_lock_path",
        }
        retired = {"notebook_dir", "pending_pages_dir", "transaction_root", "lock_path"}
        present_retired = sorted(retired & set(plan))
        if present_retired:
            raise ValueError(
                f"relevance runtime plan contains retired paths: {present_retired}"
            )
        missing = required - set(plan)
        if missing:
            raise ValueError(f"relevance runtime plan is missing paths: {sorted(missing)}")
        values = {key: plan[key] for key in required}
        if any(not isinstance(value, str) or not value for value in values.values()):
            raise ValueError("relevance runtime plan paths must be non-empty strings")
        paths = {
            key: Path(value)
            for key, value in values.items()
        }
        if any(not path.is_absolute() for path in paths.values()):
            raise ValueError("relevance runtime plan paths must be absolute")
        resolved = cls.resolve(
            notebook_root=paths["resolved_notebook_root"],
            journal_root=paths["resolved_journal_root"],
            transaction_root=paths["resolved_transaction_root"],
        )
        expected = {
            "resolved_notebook_root": resolved.notebook_root,
            "resolved_journal_root": resolved.journal_root,
            "resolved_transaction_root": resolved.transaction_root,
            "resolved_lock_path": resolved.lock_path,
        }
        for key, expected_path in expected.items():
            if paths[key] != expected_path:
                raise ValueError(f"relevance runtime plan path drift: {key}")
        journal_path = plan.get("transaction_journal_path")
        if journal_path is not None:
            journal = Path(str(journal_path))
            transaction_id = str(plan.get("transaction_id") or "")
            if (
                not journal.is_absolute()
                or journal.parent != resolved.transaction_root
                or not transaction_id
                or journal.name != f"{transaction_id}.json"
            ):
                raise ValueError("relevance transaction journal is outside transaction root")
        return resolved

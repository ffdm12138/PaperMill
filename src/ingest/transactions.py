"""External durable commit journals and transaction locks."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from src.file_fingerprint import compute_sha256
from src.ingest.locking import acquire_locks, transaction_requests
from src.services.transaction_paths import (
    TransactionIdentityError,
    TransactionPathError,
    validate_commit_journal,
    validate_transaction_id,
)
from src.utils.atomic_io import atomic_write_json

PHASES = (
    "prepared",
    "staging_complete",
    "final_installed",
    "ledger_active",
    "category_reconcile_requested",
    "source_deleted",
    "complete",
)
ACTIVE_PHASES = set(PHASES[:-1])
NEXT = {left: right for left, right in zip(PHASES, PHASES[1:])}

ROLLBACK_ACTIVE_PHASES = {
    "prepared", "formal_quarantined", "raw_installed", "ledger_reserved",
    "category_links_removed", "quarantine_removed",
}


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def find_active_transaction_for_paper(
    transaction_root: Path,
    paper_number: str,
    *,
    paper_raw_root: Path,
    papers_root: Path,
) -> tuple[str, Path, dict] | None:
    """Return the sole active commit/rollback journal, failing on ambiguity."""
    root = Path(transaction_root)
    store = CommitJournalStore(root, paper_raw_root=paper_raw_root, papers_root=papers_root)
    matches: list[tuple[str, Path, dict]] = [
        ("commit", store.path_for(data), data) for data in store.find_active(paper_number)
    ]
    rollback_root = root / "rollback"
    if rollback_root.exists():
        from src.services.transaction_paths import validate_rollback_journal
        for path in sorted(rollback_root.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise RuntimeError(f"transaction_repair_required: corrupt rollback journal {path}") from exc
            validated = validate_rollback_journal(
                data,
                journal_path=path,
                paper_raw_root=paper_raw_root,
                papers_root=papers_root,
                transaction_root=root,
            )
            if validated["phase"] not in ROLLBACK_ACTIVE_PHASES:
                if validated["phase"] != "completed":
                    raise RuntimeError(f"transaction_repair_required: invalid rollback phase {validated['phase']!r}")
                continue
            if validated["paper_number"] == paper_number:
                matches.append(("rollback", path, data))
    if len(matches) > 1:
        raise RuntimeError(f"ambiguous_active_transaction: {paper_number} has {len(matches)} journals")
    return matches[0] if matches else None


class CommitJournalStore:
    """Persistent journal store for commit transactions.

    When *paper_raw_root* and *papers_root* are provided, *load_all()*
    validates every journal through ``validate_commit_journal`` before
    returning, rejecting malformed or unsafe entries.
    """

    def __init__(
        self,
        root: Path,
        *,
        paper_raw_root: Path | None = None,
        papers_root: Path | None = None,
    ):
        self.root = Path(root)
        self.active = self.root / "commit"
        self.completed = self.active / "completed"
        self.locks = self.root / "locks"
        self._paper_raw_root = paper_raw_root
        self._papers_root = papers_root

    def lock_requests(self, numbers: list[str]):
        self.locks.mkdir(parents=True, exist_ok=True)
        return transaction_requests(self.locks, numbers)

    def _journal_paths(self) -> list[Path]:
        result: list[Path] = []
        if self.active.exists():
            result.extend(sorted(self.active.glob("*.json")))
        if self.completed.exists():
            result.extend(sorted(self.completed.glob("*.json")))
        return result

    def _validate_or_raise(self, path: Path, data: dict) -> dict:
        """Run ``validate_commit_journal`` when validation roots are set."""
        if self._paper_raw_root is not None and self._papers_root is not None:
            validate_commit_journal(
                data,
                journal_path=path,
                paper_raw_root=self._paper_raw_root,
                papers_root=self._papers_root,
                transaction_root=self.root,
            )
            return data
        # Without validation roots, return minimal validated fields
        transaction = validate_transaction_id(str(data.get("transaction_id") or ""))
        data["transaction_id"] = transaction
        return data

    def load_all(self) -> list[tuple[Path, dict]]:
        """Load journals, validating paths when configured.

        Raises on any corrupt, malformed, or unsafe journal.  A single
        invalid journal prevents all further processing (fail closed).
        """
        seen: dict[str, Path] = {}
        out: list[tuple[Path, dict]] = []
        for path in self._journal_paths():
            root_abs = self.root.absolute()
            path_abs = path.absolute()
            try:
                relative = path_abs.relative_to(root_abs)
            except ValueError as exc:
                raise TransactionPathError(f"journal path escapes transaction root: {path}") from exc
            current = root_abs
            if current.is_symlink():
                raise TransactionPathError(f"transaction root is a symlink: {self.root}")
            for part in relative.parts:
                current /= part
                if current.is_symlink():
                    raise TransactionPathError(f"journal path contains a symlink: {current}")
            try:
                path.resolve(strict=True).relative_to(self.root.resolve(strict=True))
            except (OSError, ValueError) as exc:
                raise TransactionPathError(f"journal resolved path escapes transaction root: {path}") from exc
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise RuntimeError(
                    f"corrupt commit journal {path}: {exc}"
                ) from exc
            transaction = str(data.get("transaction_id") or "")
            if not transaction or data.get("phase") not in PHASES:
                raise RuntimeError(f"invalid commit journal: {path}")
            if path.parent == self.completed and data.get("phase") != "complete":
                raise RuntimeError(
                    f"completed directory contains non-complete journal: {path}"
                )
            if transaction in seen:
                first = seen[transaction]
                if compute_sha256(first) != compute_sha256(path):
                    raise RuntimeError(
                        f"contradictory duplicate journal: {transaction}"
                    )
                raise RuntimeError(
                    f"duplicate journal exists in active and completed storage: "
                    f"{transaction}"
                )
            seen[transaction] = path
            # Validate journal before accepting
            validated = self._validate_or_raise(path, data)
            out.append((path, validated))
        return out

    def find_active(self, number: str) -> list[dict]:
        return [
            data
            for _, data in self.load_all()
            if data.get("paper_number") == number
               and data.get("phase") in ACTIVE_PHASES
        ]

    def create(self, **kwargs) -> dict:
        """Atomically reject conflicts and create one active journal."""
        paper_number = str(kwargs.get("paper_number") or "")
        with ordered_transaction_locks(self, [paper_number]):
            if self.find_active(paper_number):
                raise RuntimeError(
                    f"active_journal_conflict: unfinished commit transaction exists for {paper_number}"
                )
            return self._create_unlocked(**kwargs)

    def _create_unlocked(
        self,
        *,
        paper_number: str,
        paper_name: str,
        source: Path,
        staging: Path,
        final: Path,
        formalization: Path,
        metadata_freeze: Path | None = None,
        catalog_freeze: Path | None = None,
        transaction_id: str | None = None,
    ) -> dict:
        """Create a journal while the caller holds its transaction lock.

        If *transaction_id* is given it is used as-is (and the caller is
        responsible for ensuring it appears in *staging*'s basename).
        Otherwise a new UUID is generated.
        """
        self.active.mkdir(parents=True, exist_ok=True)
        transaction = transaction_id or str(uuid.uuid4())

        def read_json(path: Path | None):
            return (
                json.loads(path.read_text(encoding="utf-8"))
                if path and path.is_file()
                else None
            )

        source_records = {}
        source_records_dir = source / "source_records"
        if source_records_dir.is_dir():
            source_records = {
                p.relative_to(source).as_posix(): json.loads(
                    p.read_text(encoding="utf-8")
                )
                for p in sorted(source_records_dir.rglob("*.json"))
            }

        data = {
            "schema_version": "1.0",
            "transaction_id": transaction,
            "paper_number": paper_number,
            "paper_name": paper_name,
            "source_workspace": str(source.resolve()),
            "staging_path": str(staging.resolve()),
            "final_path": str(final.resolve()),
            "source_metadata_sha256": compute_sha256(
                source / f"{paper_number}.metadata.json"
            ),
            "source_catalog_sha256": compute_sha256(
                source / f"{paper_number}.catalog.json"
            ),
            "formalization_sha256": compute_sha256(formalization),
            "metadata_freeze_sha256": (
                compute_sha256(metadata_freeze) if metadata_freeze else ""
            ),
            "catalog_freeze_sha256": (
                compute_sha256(catalog_freeze) if catalog_freeze else ""
            ),
            "audit_receipts": {
                "metadata_match": read_json(
                    source / f"{paper_number}.metadata_match.json"
                ),
                "metadata_freeze": read_json(metadata_freeze),
                "conversion_manifest": read_json(
                    source / f"{paper_number}.conversion.json"
                ),
                "catalog_task": read_json(
                    source / f"{paper_number}.catalog_task.json"
                ),
                "catalog_freeze": read_json(catalog_freeze),
                "formalization": read_json(formalization),
            },
            "source_record_copies": source_records,
            "phase": "prepared",
            "attempt": 1,
            "created_at": _now(),
            "updated_at": _now(),
            "last_error": None,
        }
        path = self.active / f"{transaction}.json"
        atomic_write_json(path, data, indent=2)
        return data

    def path_for(self, journal: dict) -> Path:
        name = f"{journal['transaction_id']}.json"
        active = self.active / name
        completed = self.completed / name
        if active.exists() and completed.exists():
            self.load_all()
        return active if active.exists() else completed

    def update(self, journal: dict, phase: str, *, last_error: str | None = None, allow_same: bool = False) -> dict:
        """Advance a journal to the next *phase*.

        The ``transaction_id`` is validated before it is used to construct
        any filesystem path, ensuring a malicious journal payload cannot
        cause path escape.
        """
        if phase not in PHASES:
            raise ValueError(f"unknown journal phase: {phase}")
        current = str(journal.get("phase") or "")
        if phase != current and NEXT.get(current) != phase:
            raise ValueError(f"illegal journal transition: {current} -> {phase}")
        if phase == current and not allow_same:
            raise ValueError(f"journal already at phase {phase}")
        journal = dict(journal)
        journal.update(
            {"phase": phase, "updated_at": _now(), "last_error": last_error}
        )

        # Validate and construct canonical path — never trust journal dict path
        tx_id = validate_transaction_id(str(journal.get("transaction_id") or ""))
        self.active.mkdir(parents=True, exist_ok=True)
        path = self.active / f"{tx_id}.json"
        # Verify containment of target path
        try:
            path.resolve(strict=False).relative_to(self.active.resolve(strict=False))
        except ValueError:
            raise TransactionPathError(
                f"journal path {path} escapes active directory {self.active}"
            )
        atomic_write_json(path, journal, indent=2)

        if phase == "complete":
            self.completed.mkdir(parents=True, exist_ok=True)
            os.replace(path, self.completed / path.name)
        return journal

    def archive_complete(self, journal: dict) -> Path:
        if journal.get("phase") != "complete":
            raise ValueError("only complete journals can be archived")
        tx_id = validate_transaction_id(
            str(journal.get("transaction_id") or "")
        )
        source = self.active / f"{tx_id}.json"
        target = self.completed / source.name
        if source.exists():
            self.completed.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
        return target


def ordered_transaction_locks(store: CommitJournalStore, numbers: list[str]):
    return acquire_locks(*store.lock_requests(numbers))

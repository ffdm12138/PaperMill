"""Recoverable formal-paper rollback coordinated by a public entry point."""
from __future__ import annotations

import json
import os
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from filelock import Timeout as FileLockTimeout

from src.catalog.freeze import assert_catalog_frozen
from src.discovery.formal_publication import (
    publish_formal_publication_state_unlocked,
    publication_state_path,
)
from src.ingest.locking import (
    LEDGER_RANK,
    INDEX_PUBLISH_RANK,
    LEDGER_RANK,
    PAPER_RAW_GLOBAL_RANK,
    PAPERS_INSTALL_RANK,
    WORKSPACE_RANK,
    LockRequest,
    acquire_locks,
)
from src.ingest.status import initial_status, initialize_status
from src.ingest.transactions import (
    CommitJournalStore,
    find_active_transaction_for_paper,
)
from src.ingest.workspace import PaperRawWorkspace, validate_workspace_contents
from src.library.paper_number_ledger import PaperNumberLedger
from src.library.validation import validate_formal_paper
from src.metadata.freeze import assert_metadata_frozen
from src.services.transaction_paths import (
    check_destructive_path,
    rollback_quarantine_path,
    rollback_staging_path,
    validate_rollback_journal,
)
from src.utils.atomic_io import atomic_write_json


ROLLBACK_PHASES = (
    "prepared",
    "formal_quarantined",
    "raw_installed",
    "ledger_reserved",
    "category_links_removed",
    "quarantine_removed",
    "completed",
)
_NEXT_PHASE = {left: right for left, right in zip(ROLLBACK_PHASES, ROLLBACK_PHASES[1:])}
_COPY_SUFFIXES = (
    "metadata.json", "catalog.json", "md", "pdf", "metadata_match.json",
    "metadata_freeze.json", "catalog_task.json", "catalog_freeze.json",
    "conversion.json",
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _journal_path(transaction_root: Path, transaction_id: str) -> Path:
    return transaction_root / "rollback" / f"{transaction_id}.json"


@contextmanager
def _transaction_lock(store: CommitJournalStore, number: str, timeout: float):
    try:
        with acquire_locks(*store.lock_requests([number]), timeout=timeout):
            yield
    except FileLockTimeout as exc:
        raise RuntimeError(f"transaction_lock_timeout: rollback lock for {number}") from exc


def _write_journal(path: Path, journal: dict, phase: str) -> dict:
    current = str(journal.get("phase") or "")
    if _NEXT_PHASE.get(current) != phase:
        raise ValueError(f"illegal rollback journal transition: {current} -> {phase}")
    value = dict(journal)
    value.update({"phase": phase, "updated_at": _now()})
    atomic_write_json(path, value, indent=2)
    if phase == "completed":
        completed = path.parent / "completed"
        completed.mkdir(parents=True, exist_ok=True)
        os.replace(path, completed / path.name)
    return value


def _fault(callback, phase: str) -> None:
    if callback is not None:
        callback(phase)


def _validate_quarantine_state(formal: Path, quarantine: Path) -> None:
    if formal.exists() and quarantine.exists():
        raise RuntimeError("transaction_repair_required: formal and quarantine both exist")
    if not formal.exists() and not quarantine.exists():
        raise RuntimeError("transaction_repair_required: formal and quarantine both missing")


def _stage_raw(
    quarantine: Path,
    staging: Path,
    info: dict,
    *,
    papers_dir: Path,
    paper_raw_root: Path,
) -> None:
    """Build a numeric raw workspace from the quarantined formal closure."""
    number = info["paper_number"]
    paper_name = info["paper_name"]
    check_destructive_path(paper_raw_root, staging, field="staging_path")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for suffix in _COPY_SUFFIXES:
        shutil.copyfile(quarantine / f"{paper_name}.{suffix}", staging / f"{number}.{suffix}")
    shutil.copytree(quarantine / "images", staging / "images")
    shutil.copytree(quarantine / "source_records", staging / "source_records")
    atomic_write_json(staging / f"{number}.paper.number", {
        "schema_version": "1.0",
        "paper_number": number,
        "folder_name": number,
        "state": "reserved",
        "planned_paper_name": paper_name,
    }, indent=2)
    status = initial_status(number)
    status.update({
        "metadata": {"state": "frozen", "revision": int(info["manifest"].get("metadata_revision") or 1)},
        "pdf": {"state": "attached"},
        "conversion": {"state": "complete"},
        "catalog": {"state": "frozen"},
        "formalization": {"state": "stale"},
        "commit": {"state": "pending"},
        "rollback": {"from_paper_name": paper_name, "restored_at": _now()},
    })
    initialize_status(staging, number, status)
    validate_workspace_contents(
        staging,
        number,
        layout="raw",
        require_canonical_directory_name=False,
        papers_dir=papers_dir,
        paper_raw_root=paper_raw_root,
    )


def _raw_is_valid(target: Path, number: str, *, papers_dir: Path, paper_raw_root: Path) -> None:
    validate_workspace_contents(
        target, number, papers_dir=papers_dir, paper_raw_root=paper_raw_root
    )
    assert_metadata_frozen(target, number)
    assert_catalog_frozen(
        target, number, papers_dir=papers_dir, paper_raw_root=paper_raw_root
    )


def _assert_metadata_staged_marker(target: Path, number: str, paper_name: str) -> None:
    markers = sorted(target.glob("*.paper.number"))
    expected = target / f"{number}.paper.number"
    if markers != [expected]:
        raise RuntimeError(
            "transaction_repair_required: rollback marker is not canonical"
        )
    try:
        marker = json.loads(expected.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            "transaction_repair_required: rollback marker is unreadable"
        ) from exc
    expected_fields = {
        "schema_version": "1.0",
        "paper_number": number,
        "folder_name": number,
        "state": "metadata_staged",
        "planned_paper_name": paper_name,
    }
    if not isinstance(marker, dict) or any(
        marker.get(key) != value for key, value in expected_fields.items()
    ):
        raise RuntimeError(
            "transaction_repair_required: rollback marker identity mismatch"
        )


def resume_rollback(
    journal: dict,
    *,
    journal_path: Path,
    papers_dir: Path,
    paper_raw_root: Path,
    transaction_root: Path,
    ledger: PaperNumberLedger,
    catalog_root: Path,
    fault_injector=None,
) -> dict:
    """Resume a validated rollback journal using durable state as evidence."""
    validated = validate_rollback_journal(
        journal,
        journal_path=journal_path,
        paper_raw_root=paper_raw_root,
        papers_root=papers_dir,
        transaction_root=transaction_root,
    )
    number = validated["paper_number"]
    paper_name = validated["paper_name"]
    formal = validated["formal_path"]
    target = validated["raw_path"]
    staging = validated["staging_path"]
    quarantine = validated["formal_quarantine"]

    while journal["phase"] != "completed":
        phase = journal["phase"]
        if phase == "prepared":
            _validate_quarantine_state(formal, quarantine)
            if formal.exists():
                info = validate_formal_paper(formal)
                if info["paper_number"] != number or info["paper_name"] != paper_name:
                    raise RuntimeError("transaction_repair_required: formal identity mismatch")
                item = (ledger.load().get("items") or {}).get(number) or {}
                if item.get("state") != "active":
                    raise RuntimeError("transaction_repair_required: prepared rollback ledger is not active")
                with acquire_locks(
                    LockRequest.path_lock(PAPERS_INSTALL_RANK, papers_dir / ".papers_install.lock")
                ):
                    _validate_quarantine_state(formal, quarantine)
                    check_destructive_path(papers_dir, quarantine, field="formal_quarantine")
                    os.replace(formal, quarantine)
            journal = _write_journal(journal_path, journal, "formal_quarantined")
            _fault(fault_injector, "formal_quarantined")

        elif phase == "formal_quarantined":
            _validate_quarantine_state(formal, quarantine)
            if formal.exists():
                raise RuntimeError("transaction_repair_required: quarantine phase still has formal paper")
            info = validate_formal_paper(quarantine, expected_paper_name=paper_name)
            if info["paper_number"] != number or info["paper_name"] != paper_name:
                raise RuntimeError("transaction_repair_required: quarantine identity mismatch")
            with acquire_locks(
                LockRequest.path_lock(PAPER_RAW_GLOBAL_RANK, paper_raw_root / ".paper_raw_write.lock"),
                LockRequest.paper_lock(WORKSPACE_RANK, paper_raw_root / f".{number}.lock", number),
            ):
                if target.exists():
                    _raw_is_valid(target, number, papers_dir=papers_dir, paper_raw_root=paper_raw_root)
                else:
                    _stage_raw(quarantine, staging, info, papers_dir=papers_dir, paper_raw_root=paper_raw_root)
                    check_destructive_path(paper_raw_root, target, field="raw_path", expected_name=number)
                    os.replace(staging, target)
                _raw_is_valid(target, number, papers_dir=papers_dir, paper_raw_root=paper_raw_root)
            journal = _write_journal(journal_path, journal, "raw_installed")
            _fault(fault_injector, "raw_installed")

        elif phase == "raw_installed":
            _raw_is_valid(target, number, papers_dir=papers_dir, paper_raw_root=paper_raw_root)
            with acquire_locks(
                LockRequest.path_lock(LEDGER_RANK, ledger._lock_path),
                LockRequest.path_lock(PAPERS_INSTALL_RANK, papers_dir / ".papers_install.lock"),
                LockRequest.path_lock(
                    INDEX_PUBLISH_RANK,
                    Path(publication_state_path(papers_dir).as_posix() + ".lock"),
                ),
            ):
                item = (ledger.load().get("items") or {}).get(number) or {}
                state = item.get("state")
                if state == "active":
                    ledger.rollback_active_to_metadata_staged_locked(number, target, planned_paper_name=paper_name)
                elif state == "metadata_staged":
                    if item.get("planned_paper_name") != paper_name or Path(str(item.get("folder_path") or "")).name != number:
                        raise RuntimeError("transaction_repair_required: rollback ledger identity mismatch")
                    ledger.write_marker(
                        target,
                        number,
                        state="metadata_staged",
                        planned_paper_name=paper_name,
                    )
                elif state == "reserved":
                    raise RuntimeError(
                        "transaction_repair_required: reserved ledger state is not valid rollback evidence"
                    )
                else:
                    raise RuntimeError(f"transaction_repair_required: rollback ledger state {state!r}")
                _assert_metadata_staged_marker(target, number, paper_name)
                publish_formal_publication_state_unlocked(
                    papers_dir=papers_dir, ledger_items=ledger.load().get("items") or {},
                )
            journal = _write_journal(journal_path, journal, "ledger_reserved")
            _fault(fault_injector, "ledger_reserved")

        elif phase == "ledger_reserved":
            item = (ledger.load().get("items") or {}).get(number) or {}
            if item.get("state") not in {"reserved", "metadata_staged"}:
                raise RuntimeError("transaction_repair_required: ledger reservation evidence missing")
            from src.catalog_folders.formal_registry import FormalPaperRegistry
            from src.catalog_folders.reconcile import reconcile_catalog_folders
            reconcile_catalog_folders(
                root=catalog_root,
                formal_registry=FormalPaperRegistry(papers_dir=papers_dir, ledger=ledger),
                apply=True,
                allow_empty_categories=True,
            )
            assignment = Path(catalog_root) / ".state" / "assignments" / f"{number}.json"
            assignment.unlink(missing_ok=True)
            journal = _write_journal(journal_path, journal, "category_links_removed")
            _fault(fault_injector, "category_links_removed")

        elif phase == "category_links_removed":
            _raw_is_valid(target, number, papers_dir=papers_dir, paper_raw_root=paper_raw_root)
            if quarantine.exists():
                check_destructive_path(papers_dir, quarantine, field="formal_quarantine")
                shutil.rmtree(quarantine)
            if staging.exists():
                check_destructive_path(paper_raw_root, staging, field="staging_path")
                shutil.rmtree(staging)
            journal = _write_journal(journal_path, journal, "quarantine_removed")
            _fault(fault_injector, "quarantine_removed")

        elif phase == "quarantine_removed":
            _fault(fault_injector, "before_completed")
            if formal.exists() or quarantine.exists() or staging.exists():
                raise RuntimeError("transaction_repair_required: rollback cleanup incomplete")
            _raw_is_valid(target, number, papers_dir=papers_dir, paper_raw_root=paper_raw_root)
            journal = _write_journal(journal_path, journal, "completed")
        else:
            raise RuntimeError(f"transaction_repair_required: unsupported rollback phase {phase!r}")
    return journal


def _resolve_paper_number_from_paper_name(
    *,
    paper_name: str,
    papers_dir: Path,
    paper_raw_root: Path,
    transaction_root: Path,
    ledger: PaperNumberLedger,
) -> str:
    """Resolve a paper_name to a unique paper_number for rollback.

    Resolution order:

    1. **Active rollback journals** — search ``transaction_root/rollback/``
       for journals that contain this paper_name and are still active (not
       completed).  If exactly one matches, return its ``paper_number`` so
       rollback resumes from where it stopped.

    2. **Active ledger entries** — search ledger items where
       ``state == "active"`` and ``entry.paper_name == paper_name``.  Requires
       exactly one match.

    Raises ``ValueError`` with a descriptive code for each failure mode:
    ``paper_name_not_found``, ``ambiguous_paper_name``,
    ``repair_required``.
    """

    # ---- 1. Active rollback journals ----
    rollback_root = transaction_root / "rollback"
    journal_matches: list[dict] = []
    if rollback_root.exists():
        for path in sorted(rollback_root.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            try:
                validated = validate_rollback_journal(
                    data,
                    journal_path=path,
                    paper_raw_root=paper_raw_root,
                    papers_root=papers_dir,
                    transaction_root=transaction_root,
                )
            except Exception:
                continue
            if validated["paper_name"] != paper_name:
                continue
            # Active = any phase except "completed"
            if validated["phase"] not in ROLLBACK_PHASES[:-1]:
                continue
            journal_matches.append(data)

    if len(journal_matches) > 1:
        raise ValueError(
            f"ambiguous_paper_name: {len(journal_matches)} active rollback "
            f"journals found for paper_name={paper_name!r}"
        )
    if len(journal_matches) == 1:
        pn = str(journal_matches[0].get("paper_number") or "").strip()
        if not pn:
            raise RuntimeError(
                f"transaction_repair_required: rollback journal for "
                f"paper_name={paper_name!r} missing paper_number"
            )
        return pn

    # ---- 2. Active ledger entries ----
    items = (ledger.load().get("items") or {})
    active_matches: list[tuple[str, dict]] = []
    for number, item in items.items():
        if not isinstance(item, dict):
            continue
        if (item.get("state") or "") != "active":
            continue
        if str(item.get("paper_name") or "") == paper_name:
            active_matches.append((number, item))

    if len(active_matches) > 1:
        raise ValueError(
            f"ambiguous_paper_name: {len(active_matches)} active ledger "
            f"entries for paper_name={paper_name!r}"
        )
    if not active_matches:
        raise ValueError(
            f"paper_name_not_found: no active ledger entry or rollback "
            f"journal for paper_name={paper_name!r}"
        )
    return active_matches[0][0]


def resolve_paper_number_by_paper_name(
    *,
    paper_name: str,
    papers_dir: Path,
    paper_raw_root: Path,
    transaction_root: Path,
    ledger: PaperNumberLedger,
) -> str:
    """Resolve paper_name → paper_number with journal-first lookup.

    Resolution order:

    1. **Active rollback journals** — returns ``paper_number`` from a
       matching incomplete journal so rollback resumes from crash point.

    2. **Active ledger entries** — `state == "active"` and
       `paper_name == paper_name`.  Requires exactly one match.

    Raises ``ValueError`` for ``paper_name_not_found``,
    ``ambiguous_paper_name``, or ``repair_required``.

    The *paper_name* string is validated via
    :func:`~src.services.transaction_paths.validate_paper_name` before
    resolution.
    """
    from src.services.transaction_paths import validate_paper_name as _validate_pid

    pid = _validate_pid(str(paper_name))
    return _resolve_paper_number_from_paper_name(
        paper_name=pid,
        papers_dir=papers_dir,
        paper_raw_root=paper_raw_root,
        transaction_root=transaction_root,
        ledger=ledger,
    )


def rollback_formal_papers(
    *,
    papers_dir: Path,
    paper_raw_root: Path,
    transaction_root: Path,
    ledger_path: Path,
    catalog_root: Path,
    paper_number: str | None = None,
    paper_name: str | None = None,
    interactive: bool = False,
    fault_injector=None,
    lock_timeout: float = 30.0,
) -> str:
    """Create or automatically resume the sole rollback for one paper."""
    del interactive
    papers_dir = Path(papers_dir)
    paper_raw_root = Path(paper_raw_root)
    transaction_root = Path(transaction_root)
    ledger = PaperNumberLedger(ledger_path)
    store = CommitJournalStore(
        transaction_root, paper_raw_root=paper_raw_root, papers_root=papers_dir
    )
    if paper_number:
        number = str(paper_number)
    elif paper_name:
        number = resolve_paper_number_by_paper_name(
            paper_name=str(paper_name),
            papers_dir=papers_dir,
            paper_raw_root=paper_raw_root,
            transaction_root=transaction_root,
            ledger=ledger,
        )
    else:
        raise ValueError("either paper_number or paper_name is required")

    with _transaction_lock(store, number, lock_timeout):
        active = find_active_transaction_for_paper(
            transaction_root,
            number,
            paper_raw_root=paper_raw_root,
            papers_root=papers_dir,
        )
        if active is not None:
            kind, journal_path, journal = active
            if kind == "commit":
                raise RuntimeError(f"active_commit_transaction: rollback refused for {number}")
        else:
            item = (ledger.load().get("items") or {}).get(number) or {}
            if item.get("state") != "active":
                raise ValueError(f"paper_number {number} is not active (state={item.get('state') or ''})")
            resolved_paper_name = str(item.get("paper_name") or "")
            formal = papers_dir / (item.get("folder_name") or number)
            if not resolved_paper_name or not formal.is_dir():
                raise RuntimeError("transaction_repair_required: active formal identity missing")
            info = validate_formal_paper(formal)
            if info["paper_number"] != number or info["paper_name"] != resolved_paper_name:
                raise RuntimeError("transaction_repair_required: initial formal identity mismatch")
            tx_id = str(uuid.uuid4())
            journal = {
                "schema_version": "1.0",
                "transaction_id": tx_id,
                "paper_number": number,
                "paper_name": resolved_paper_name,
                "formal_path": str(formal.resolve()),
                "raw_path": str((paper_raw_root / number).resolve()),
                "staging_path": str(rollback_staging_path(paper_raw_root, number, tx_id)),
                "formal_quarantine": str(rollback_quarantine_path(papers_dir, resolved_paper_name, tx_id)),
                "phase": "prepared",
                "created_at": _now(),
                "updated_at": _now(),
            }
            journal_path = _journal_path(transaction_root, tx_id)
            journal_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(journal_path, journal, indent=2)
            _fault(fault_injector, "prepared")
        result = resume_rollback(
            journal,
            journal_path=journal_path,
            papers_dir=papers_dir,
            paper_raw_root=paper_raw_root,
            transaction_root=transaction_root,
            ledger=ledger,
            catalog_root=catalog_root,
            fault_injector=fault_injector,
        )
        return result["paper_number"]

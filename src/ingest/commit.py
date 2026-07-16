"""Recoverable transaction commit from numeric paper_raw to formal papers."""
from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

from src.file_fingerprint import compute_sha256
from src.ingest.formalization import assert_formalization_current
from src.ingest.duplicate_inspection import inspect_ingest_duplicates
from src.ingest.locking import (
    INDEX_PUBLISH_RANK,
    LEDGER_RANK,
    PAPER_RAW_GLOBAL_RANK,
    PAPERS_INSTALL_RANK,
    WORKSPACE_RANK,
    LockRequest,
    acquire_locks,
)
from src.discovery.formal_publication import (
    publish_formal_publication_state_unlocked,
    publication_state_path,
    validate_publication_state,
)
from src.ingest.transactions import (
    CommitJournalStore,
    find_active_transaction_for_paper,
    ordered_transaction_locks,
)
from src.ingest.workspace import PaperRawWorkspace
from src.library.validation import validate_formal_paper
from src.library.paper_number_ledger import PaperNumberLedger
from src.services.transaction_paths import (
    check_destructive_path,
    validate_commit_journal,
    commit_staging_path,
    commit_final_path,
    commit_source_workspace_path,
)
from src.utils.atomic_io import atomic_write_json


def _fault(callback, phase):
    if callback:
        callback(phase)


def _assert_journal_inputs(
    journal: dict,
    workspace: PaperRawWorkspace,
) -> None:
    expected = {
        "source_metadata_sha256": compute_sha256(workspace.metadata),
        "source_catalog_sha256": compute_sha256(workspace.catalog),
        "formalization_sha256": compute_sha256(workspace.formalization),
        "metadata_freeze_sha256": compute_sha256(workspace.metadata_freeze),
        "catalog_freeze_sha256": compute_sha256(workspace.catalog_freeze),
    }
    for key, value in expected.items():
        if journal.get(key) != value:
            raise RuntimeError(
                f"transaction input changed; abandon and re-formalize: {key}"
            )


# ── Path re-validation helpers (destructive-operation guards) ──────────


def _revalidate_commit_targets(
    journal: dict,
    *,
    paper_raw_root: Path,
    papers_root: Path,
    transaction_root: Path,
) -> dict:
    """Re-validate all paths from a loaded journal before destructive ops.

    This is intentionally called again just before ``rmtree`` / ``copytree``
    / ``os.replace`` so that a TOCTOU race between ``load_all()`` and the
    destructive operation cannot escape the configured roots.
    """
    return validate_commit_journal(
        journal,
        journal_path=transaction_root / "commit" / f"{journal['transaction_id']}.json",
        paper_raw_root=paper_raw_root,
        papers_root=papers_root,
        transaction_root=transaction_root,
    )


def _revalidate_before_rmtree(
    root: Path,
    target: Path,
    *,
    field: str,
    expected_name: str | None = None,
) -> None:
    """Re-validate a path about to be removed via ``shutil.rmtree``."""
    check_destructive_path(
        root, target,
        field=field,
        expected_name=expected_name,
    )


def _revalidate_staging_is_safe_to_clean(
    staging: Path,
    *,
    papers_root: Path,
    paper_name: str,
    transaction_id: str,
) -> None:
    """Assert *staging* can be safely removed and recreated.

    Checks:
    - Under ``papers_root``.
    - Not a symlink.
    - Named exactly ``.<paper_name>.staging_<transaction_id>``.
    - Is not the papers_root itself.
    - Is not the final formal path.
    """
    check_destructive_path(
        papers_root, staging,
        field="staging_path",
        expected_name=f".{paper_name}.staging_{transaction_id}",
    )


# ── Staging preparation ────────────────────────────────────────────────


def _prepare_staging(
    workspace: PaperRawWorkspace,
    plan: dict,
    staging: Path,
    *,
    papers_root: Path,
    paper_name: str,
    transaction_id: str,
) -> None:
    """Prepare the hidden staging directory with formalized assets.

    Safe-removes any existing staging after re-validating, then
    copies all assets.
    """
    # Re-validate before any destructive operation
    _revalidate_staging_is_safe_to_clean(
        staging,
        papers_root=papers_root,
        paper_name=paper_name,
        transaction_id=transaction_id,
    )

    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=False)

    copies: list[tuple[Path, Path]] = [
        (workspace.metadata, staging / f"{paper_name}.metadata.json"),
        (workspace.catalog, staging / f"{paper_name}.catalog.json"),
        (workspace.markdown, staging / f"{paper_name}.md"),
        (workspace.pdf, staging / f"{paper_name}.pdf"),
        (workspace.metadata_match, staging / f"{paper_name}.metadata_match.json"),
        (workspace.metadata_freeze, staging / f"{paper_name}.metadata_freeze.json"),
        (workspace.catalog_task, staging / f"{paper_name}.catalog_task.json"),
        (workspace.catalog_freeze, staging / f"{paper_name}.catalog_freeze.json"),
        (workspace.conversion, staging / f"{paper_name}.conversion.json"),
    ]
    copy_kinds: list[str] = [
        "metadata", "catalog", "markdown", "pdf", "metadata_match",
        "metadata_freeze", "catalog_task", "catalog_freeze", "conversion_manifest",
    ]
    # Discovery receipt — optional (only for discovery-sourced papers).
    receipt_target = staging / f"{workspace.paper_number}.discovery_receipt.json"
    if workspace.discovery_receipt.exists():
        copies.append((workspace.discovery_receipt, receipt_target))
        copy_kinds.append("discovery_receipt")

    for source, target in copies:
        shutil.copyfile(source, target)
    shutil.copytree(workspace.images, staging / "images")
    shutil.copytree(workspace.root / "source_records", staging / "source_records")
    atomic_write_json(
        staging / workspace.marker.name, plan["marker_rewrite"], indent=2
    )
    hashes = {
        kind: compute_sha256(target)
        for kind, (_, target) in zip(copy_kinds, copies)
    }
    if hashes["metadata"] != compute_sha256(workspace.metadata) or hashes["catalog"] != compute_sha256(workspace.catalog):
        raise RuntimeError("staging changed frozen metadata/catalog bytes")
    files = {
        "pdf": f"{paper_name}.pdf",
        "markdown": f"{paper_name}.md",
        "metadata": f"{paper_name}.metadata.json",
        "catalog": f"{paper_name}.catalog.json",
        "metadata_match": f"{paper_name}.metadata_match.json",
        "metadata_freeze": f"{paper_name}.metadata_freeze.json",
        "catalog_task": f"{paper_name}.catalog_task.json",
        "catalog_freeze": f"{paper_name}.catalog_freeze.json",
        "conversion_manifest": f"{paper_name}.conversion.json",
        "paper_number_marker": workspace.marker.name,
        "images_dir": "images/",
        "source_records_dir": "source_records/",
    }
    if "discovery_receipt" in copy_kinds:
        files["discovery_receipt"] = workspace.discovery_receipt.name
    hashes["paper_number_marker"] = compute_sha256(
        staging / workspace.marker.name
    )
    atomic_write_json(
        staging / f"{paper_name}.asset_manifest.json",
        {
            "schema_version": "2.0",
            "stage": "papers",
            "paper_number": workspace.paper_number,
            "paper_name": paper_name,
            "files": files,
            "asset_hashes": hashes,
            "image_hashes": plan["image_hashes"],
            "source_record_hashes": plan["source_record_hashes"],
        },
        indent=2,
    )
    validate_formal_paper(staging, expected_paper_name=paper_name)


def _ledger_state(
    ledger: PaperNumberLedger,
    number: str,
) -> str:
    return str(
        ((ledger.load().get("items") or {}).get(number) or {}).get("state") or ""
    )


def _assert_duplicate_clear(
    workspace: PaperRawWorkspace,
    *,
    ledger: PaperNumberLedger,
    papers_dir: Path,
) -> None:
    result = inspect_ingest_duplicates(
        workspace,
        ledger=ledger,
        papers_root=papers_dir,
    )
    if result.status != "clear":
        raise RuntimeError(f"commit duplicate recheck failed: {result.status}")


def cleanup_committed_source_from_journal(
    journal: dict,
    *,
    paper_raw_root: Path,
) -> None:
    """Idempotently remove only the numeric source named by a durable journal."""
    _revalidate_before_rmtree(
        paper_raw_root,
        Path(journal["source_workspace"]),
        field="source_workspace",
        expected_name=journal.get("paper_number"),
    )
    source = Path(journal["source_workspace"])
    if source.exists():
        shutil.rmtree(source)


class CommitRecoveryCorruptionError(RuntimeError):
    """Durable commit evidence contradicts the journal's claimed phase."""


def validate_committed_state(
    journal: dict,
    *,
    papers_dir: Path,
    ledger: PaperNumberLedger,
) -> None:
    """Re-verify every durable fact required before source deletion."""
    number = str(journal["paper_number"])
    paper_name = str(journal["paper_name"])
    final = Path(journal["final_path"])
    try:
        info = validate_formal_paper(final, expected_paper_name=paper_name)
        if str(info.get("paper_number") or "") != number:
            raise ValueError("formal paper_number mismatch")
        item = (ledger.load().get("items") or {}).get(number)
        if not isinstance(item, dict) or item.get("state") != "active":
            raise ValueError("ledger entry is not active")
        if str(item.get("paper_name") or "") != paper_name:
            raise ValueError("ledger paper_name mismatch")
        from src.path_utils import resolve_stored_path
        if resolve_stored_path(str(item.get("folder_path") or "")).resolve() != final.resolve():
            raise ValueError("ledger folder_path mismatch")
        publication = validate_publication_state(
            papers_dir=papers_dir, ledger_items=ledger.load().get("items") or {},
        )
        if not publication.valid:
            raise ValueError("formal publication state invalid: " + ";".join(publication.issues))
    except Exception as exc:
        raise CommitRecoveryCorruptionError(
            f"commit recovery preflight failed for {number}: {exc}"
        ) from exc


# ── Resume commit ─────────────────────────────────────────────────────


def resume_commit(
    journal: dict,
    *,
    store: CommitJournalStore,
    workspace: PaperRawWorkspace | None,
    papers_dir: Path,
    ledger_path: Path,
    catalog_root: Path,
    paper_raw_root: Path,
    fault_injector=None,
) -> dict:
    number = journal["paper_number"]
    paper_name = journal["paper_name"]
    staging = Path(journal["staging_path"])
    final = Path(journal["final_path"])
    ledger = PaperNumberLedger(ledger_path)
    paper_raw_root = Path(paper_raw_root).resolve()
    transaction_root = store.root

    # Re-validate paths once at entry (in addition to store.load_all validation)
    validated = _revalidate_commit_targets(
        journal,
        paper_raw_root=paper_raw_root,
        papers_root=papers_dir,
        transaction_root=transaction_root,
    )
    staging = validated["staging_path"]
    final = validated["final_path"]

    while journal["phase"] != "complete":
        phase = journal["phase"]

        if phase == "prepared":
            if workspace is None:
                raise RuntimeError(
                    "prepared transaction lost its source workspace"
                )
            _assert_journal_inputs(journal, workspace)
            plan = assert_formalization_current(
                workspace,
                papers_dir=papers_dir,
                ledger_path=ledger_path,
            )
            with acquire_locks(
                LockRequest.path_lock(
                    PAPER_RAW_GLOBAL_RANK,
                    paper_raw_root / ".paper_raw_write.lock",
                ),
                LockRequest.paper_lock(
                    WORKSPACE_RANK, workspace.lock, number
                ),
                LockRequest.path_lock(
                    PAPERS_INSTALL_RANK,
                    papers_dir / ".papers_install.lock",
                ),
            ):
                _assert_duplicate_clear(
                    workspace,
                    ledger=ledger,
                    papers_dir=papers_dir,
                )
                _prepare_staging(
                    workspace,
                    plan,
                    staging,
                    papers_root=papers_dir,
                    paper_name=paper_name,
                    transaction_id=journal["transaction_id"],
                )
            journal = store.update(journal, "staging_complete")
            _fault(fault_injector, "staging_complete")

        elif phase == "staging_complete":
            # ── Atomic formal-install + ledger-activation ──────────────
            # Both os.replace(staging → final) and ledger activation happen
            # under the same PAPERS_INSTALL_RANK + LEDGER_RANK lock to
            # prevent the index publisher observing an inconsistent state
            # (final exists but ledger still reserved).
            with acquire_locks(
                LockRequest.path_lock(
                    LEDGER_RANK,
                    ledger._lock_path,
                ),
                LockRequest.path_lock(
                    PAPERS_INSTALL_RANK,
                    papers_dir / ".papers_install.lock",
                ),
                LockRequest.path_lock(
                    INDEX_PUBLISH_RANK,
                    Path(publication_state_path(papers_dir).as_posix() + ".lock"),
                ),
            ):
                ledger_before = ledger.load()
                had_existing_active = any(
                    isinstance(item, dict)
                    and item.get("state") == "active"
                    and number != str(candidate_number)
                    for candidate_number, item in (ledger_before.get("items") or {}).items()
                )
                if final.exists() and not staging.exists():
                    validate_formal_paper(final)
                    # Ledger should already be active; if not, this is recovery.
                    state = _ledger_state(ledger, number)
                    if state == "reserved":
                        # Crash between final install and ledger activation —
                        # but the workspace was never metadata_staged. This is a
                        # historical anomaly; refuse and require explicit repair.
                        raise RuntimeError(
                            f"repair_required_reserved_final_mismatch: "
                            f"final exists for {number} but ledger is reserved, "
                            f"not metadata_staged"
                        )
                    if state == "metadata_staged":
                        # Crash recovery: final exists but ledger not yet active.
                        ledger.activate_metadata_staged_locked(
                            number, final, paper_name=paper_name
                        )
                else:
                    if workspace is None:
                        raise RuntimeError(
                            "staging transaction lost source before final install"
                        )
                    plan = assert_formalization_current(
                        workspace,
                        papers_dir=papers_dir,
                        ledger_path=ledger_path,
                    )
                    _assert_duplicate_clear(
                        workspace,
                        ledger=ledger,
                        papers_dir=papers_dir,
                    )
                    # Re-validate staging path before os.replace
                    _revalidate_before_rmtree(
                        papers_dir, final,
                        field="final_path",
                        expected_name=paper_name,
                    )
                    validate_formal_paper(
                        staging, expected_paper_name=paper_name
                    )
                    if final.exists():
                        raise FileExistsError(
                            f"final paper_name already exists: {paper_name}"
                        )
                    os.replace(staging, final)
                    # Activate ledger within the same lock scope
                    ledger.activate_metadata_staged_locked(
                        number, final, paper_name=paper_name
                    )
                publish_formal_publication_state_unlocked(
                    papers_dir=papers_dir, ledger_items=ledger.load().get("items") or {},
                    allow_initialize=not had_existing_active,
                )
            journal = store.update(journal, "final_installed")
            _fault(fault_injector, "final_installed")

        elif phase == "final_installed":
            # Final installed + ledger active already complete above;
            # this is a recovery path from a crash after ledger activation
            # but before journal phase advancement.
            if _ledger_state(ledger, number) != "active":
                # Edge case: final installed, ledger still reserved.
                with acquire_locks(
                    LockRequest.path_lock(LEDGER_RANK, ledger._lock_path),
                    LockRequest.path_lock(PAPERS_INSTALL_RANK, papers_dir / ".papers_install.lock"),
                    LockRequest.path_lock(
                        INDEX_PUBLISH_RANK,
                        Path(publication_state_path(papers_dir).as_posix() + ".lock"),
                    ),
                ):
                    state = _ledger_state(ledger, number)
                    if state == "reserved":
                        raise RuntimeError(
                            f"repair_required_reserved_final_mismatch: "
                            f"final exists for {number} but ledger is reserved"
                        )
                    if state == "metadata_staged":
                        ledger.activate_metadata_staged_locked(
                            number, final, paper_name=paper_name
                        )
                    elif state != "active":
                        raise RuntimeError(
                            f"ledger cannot resume commit from state {state}"
                        )
                    publish_formal_publication_state_unlocked(
                        papers_dir=papers_dir, ledger_items=ledger.load().get("items") or {},
                        allow_initialize=True,
                    )
            else:
                with acquire_locks(
                    LockRequest.path_lock(LEDGER_RANK, ledger._lock_path),
                    LockRequest.path_lock(PAPERS_INSTALL_RANK, papers_dir / ".papers_install.lock"),
                    LockRequest.path_lock(
                        INDEX_PUBLISH_RANK,
                        Path(publication_state_path(papers_dir).as_posix() + ".lock"),
                    ),
                ):
                    current = ledger.load()
                    other_active = any(
                        isinstance(item, dict)
                        and item.get("state") == "active"
                        and str(candidate_number) != str(number)
                        for candidate_number, item in (current.get("items") or {}).items()
                    )
                    publish_formal_publication_state_unlocked(
                        papers_dir=papers_dir,
                        ledger_items=current.get("items") or {},
                        allow_initialize=not other_active,
                    )
            journal = store.update(journal, "ledger_active")
            _fault(fault_injector, "ledger_active")

        elif phase == "ledger_active":
            if _ledger_state(ledger, number) != "active":
                raise RuntimeError("ledger activation evidence missing")
            try:
                from src.catalog_folders.formal_registry import FormalPaperRegistry
                from src.catalog_folders.reconcile import reconcile_catalog_folders
                from src.catalog_folders.task_planner import plan_tasks
                registry = FormalPaperRegistry(papers_dir=papers_dir, ledger=ledger)
                reconcile_catalog_folders(root=catalog_root, formal_registry=registry, apply=True, allow_empty_categories=True)
                plan_tasks(root=catalog_root, formal_registry=registry, paper_number=number, apply=True)
                journal["category_state"] = "classification_pending"
            except Exception as exc:
                (Path(catalog_root) / ".state").mkdir(parents=True, exist_ok=True)
                (Path(catalog_root) / ".state" / "DIRTY").write_text(f"post-commit reconcile failed: {exc}\n", encoding="utf-8")
                journal["category_state"] = "repair_required"
                journal["category_error"] = str(exc)
            journal = store.update(journal, "category_reconcile_requested")
            _fault(fault_injector, "category_reconcile_requested")

        elif phase == "category_reconcile_requested":
            validate_committed_state(
                journal,
                papers_dir=papers_dir,
                ledger=ledger,
            )
            cleanup_committed_source_from_journal(
                journal,
                paper_raw_root=paper_raw_root,
            )
            journal = store.update(journal, "source_deleted")
            _fault(fault_injector, "source_deleted")

        elif phase == "source_deleted":
            validate_committed_state(
                journal,
                papers_dir=papers_dir,
                ledger=ledger,
            )
            if Path(journal["source_workspace"]).exists():
                raise CommitRecoveryCorruptionError(
                    "source_deleted phase contradicts an existing source workspace"
                )
            journal = store.update(journal, "complete")
            _fault(fault_injector, "complete")

    return journal


def commit_paper_raw(
    workspace: PaperRawWorkspace,
    *,
    paper_raw_root: Path,
    transactions_dir: Path,
    papers_dir: Path,
    ledger_path: Path,
    catalog_root: Path,
    fault_injector=None,
) -> dict:
    """Commit a paper_raw workspace to the formal papers directory."""
    paper_raw_root = Path(paper_raw_root)
    if workspace.root != paper_raw_root / workspace.paper_number:
        raise ValueError("workspace must be the direct numeric child of paper_raw_root")
    store = CommitJournalStore(
        transactions_dir,
        paper_raw_root=paper_raw_root,
        papers_root=papers_dir,
    )
    papers_dir.mkdir(parents=True, exist_ok=True)
    with ordered_transaction_locks(store, [workspace.paper_number]):
        active = find_active_transaction_for_paper(
            transactions_dir,
            workspace.paper_number,
            paper_raw_root=paper_raw_root,
            papers_root=papers_dir,
        )
        if active is not None and active[0] == "rollback":
            raise RuntimeError(
                f"active_rollback_transaction: commit refused for {workspace.paper_number}"
            )
        journal = active[2] if active is not None else None
        if journal is None:
            plan = assert_formalization_current(
                workspace,
                papers_dir=papers_dir,
                ledger_path=ledger_path,
            )
            paper_name = plan["paper_name"]
            # Generate transaction_id first so the staging path suffix matches
            # the journal's transaction_id.
            transaction_id = str(uuid.uuid4())
            staging = papers_dir / f".{paper_name}.staging_{transaction_id}"
            final = papers_dir / paper_name
            journal = store._create_unlocked(
                paper_number=workspace.paper_number,
                paper_name=paper_name,
                source=workspace.root,
                staging=staging,
                final=final,
                formalization=workspace.formalization,
                metadata_freeze=workspace.metadata_freeze,
                catalog_freeze=workspace.catalog_freeze,
                transaction_id=transaction_id,
            )
            _fault(fault_injector, "prepared")
        result = resume_commit(
            journal,
            store=store,
            workspace=workspace if workspace.root.exists() else None,
            papers_dir=papers_dir,
            ledger_path=ledger_path,
            catalog_root=catalog_root,
            paper_raw_root=paper_raw_root,
            fault_injector=fault_injector,
        )
        return {
            "success": True,
            "status": "imported",
            "paper_number": result["paper_number"],
            "paper_name": result["paper_name"],
            "transaction_id": result["transaction_id"],
            "phase": result["phase"],
            "folder": result["final_path"],
            "category_state": result.get("category_state", "repair_required"),
        }

"""Transactional PDF identity receipt/freeze migration (extractor/decision v2).

Rebuilds EVERY paper_raw match receipt (including currently frozen ones)
under the evidence-tiered v2 policy, then rebuilds freezes in a separate
phase.  Run while ingest is idle; the maintenance marker closes all other
paper_raw writers between ``--receipts-only`` and ``--freeze-eligible``.

Phases
------
1. ``--plan`` (read-only): full recompute -> ``plan.json`` (embedded new
   receipts, freeze targets with pinned revision/frozen_at, old hashes,
   workspace inventory) + ``baseline.json`` (immutable pre-migration
   snapshot).  Emits ``[PLAN]`` and ``[PLAN-HASH]`` lines.
   ``--paper-number`` / ``--limit`` / ``--test-root`` are allowed only here
   (dry-run, audit, temporary roots).
2. ``--receipts-only``: applies the plan's receipts under the global
   paper_raw write lock; journaled per-paper substates; old receipt/freeze/
   status backed up with an existence manifest; old freezes moved OUT of
   the active path (never left dangling); workspace statuses rewritten via
   a legacy-tolerant writer.  Phase completes only when every paper is
   terminal (fail closed).
3. ``--freeze-eligible``: rebuilds freezes for eligible papers only,
   re-validating at apply time; idempotent for the same plan (no revision
   bump, no timestamp change, no rewrite).

``--resume JOURNAL`` continues the journal's current phase (failed papers
are retried from their recorded substate); ``--abort`` restores every
backed-up asset (deleting files that did not exist before) and removes the
maintenance marker.  The migrator bypasses the maintenance guard by
construction — it never calls it; all other writers fail closed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

from loguru import logger

from config.settings import PAPER_RAW_DIR
from src.ingest.locking import (
    create_identity_migration_marker,
    paper_raw_write_lock,
    remove_identity_migration_marker,
)
from src.ingest.workspace import PaperRawWorkspace
from src.metadata.freeze import assert_metadata_frozen, freeze_metadata
from src.metadata.pdf_identity import EXTRACTOR_VERSION, extract_pdf_identity_evidence
from src.metadata.pdf_match import build_match_receipt, validate_metadata_match_receipt
from src.metadata.identity_match import (
    DECISION_POLICY_VERSION,
    RECEIPT_STATUS_TO_METADATA_STATE,
)
from src.utils.atomic_io import atomic_write_json
from src.utils.canonical_json import canonical_sha256
from src.utils.file_fingerprint import compute_sha256
from src.utils.jsonio import read_json
from src.utils.timestamps import now_iso

SCHEMA_VERSION = "1.0"
RECEIPT_TERMINAL = {"status_written", "unchanged", "skipped_no_assets"}
FREEZE_TERMINAL = {"freeze_rebuilt", "freeze_blocked", "unchanged", "skipped_no_assets"}
RETRYABLE = {"pending", "failed"}

# Assets migrated per workspace, keyed by logical name -> actual filename
# (receipts/freezes carry the paper-number prefix; the status file does not).
def _asset_filenames(paper_number: str) -> tuple[tuple[str, str], ...]:
    return (
        ("metadata_match", f"{paper_number}.metadata_match.json"),
        ("metadata_freeze", f"{paper_number}.metadata_freeze.json"),
        ("import_status", ".import_status.json"),
    )


# ── legacy-tolerant status handling (migration window only) ────────────

def _read_status_tolerant(folder: Path, paper_number: str) -> dict:
    """Read ``.import_status.json`` accepting legacy v1 shapes and legacy
    v2 vocabulary (e.g. metadata ``mismatch``) that the v2-only runtime
    rejects.  Only the migration tool may use this."""
    from src.ingest.status import initial_status, migrate_flat_status

    status_path = folder / ".import_status.json"
    if not status_path.exists():
        return initial_status(paper_number)
    try:
        value = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        return initial_status(paper_number)
    if not isinstance(value, dict):
        return initial_status(paper_number)
    if value.get("schema_version") != "2.0":
        value = migrate_flat_status(value, paper_number)
    return value


def _tolerant_update_status(
    folder: Path, paper_number: str, state: str, *, fields: dict
) -> None:
    """Locked read-modify-write on ``.import_status.json`` tolerating
    legacy pre-migration states (the v2-only runtime would reject them)."""
    from filelock import FileLock

    from src.ingest.status import STATUS_SCHEMA_VERSION, initial_status, validate_status
    from src.utils.atomic_io import atomic_write_json_unlocked

    with FileLock(str(folder / ".import_status.lock")):
        value = _read_status_tolerant(folder, paper_number)
        if value.get("schema_version") != STATUS_SCHEMA_VERSION:
            value = initial_status(paper_number)
        value["metadata"] = {"state": state, **fields}
        value["updated_at"] = now_iso()
        validate_status(value, paper_number)
        atomic_write_json_unlocked(folder / ".import_status.json", value, indent=2)


def _legacy_match_status(receipt_path: Path) -> str:
    """Tolerant read of the old receipt's match_status (v1 or v2)."""
    if not receipt_path.is_file():
        return "missing_receipt"
    try:
        return str(read_json(receipt_path).get("match_status") or "unknown")
    except Exception:
        return "unreadable_receipt"


# ── plan phase ─────────────────────────────────────────────────────────

def _workspace_inventory(paper_raw_dir: Path, numbers: list[str]) -> str:
    entries = []
    for number in sorted(numbers, key=int):
        folder = paper_raw_dir / number
        entries.append({
            "paper_number": number,
            "pdf": (folder / f"{number}.pdf").is_file(),
            "metadata": (folder / f"{number}.metadata.json").is_file(),
            "receipt": (folder / f"{number}.metadata_match.json").is_file(),
            "freeze": (folder / f"{number}.metadata_freeze.json").is_file(),
        })
    return canonical_sha256({"inventory": entries})


def _citation_ready_reason(metadata: dict) -> tuple[bool, str]:
    from src.metadata.citation_readiness import validate_citation_ready

    result = validate_citation_ready(metadata)
    if result.ready:
        return True, ""
    return False, (result.errors[0] if result.errors else "citation readiness failed")


def _simulate_freeze_payload(
    folder: Path,
    paper_number: str,
    metadata: dict,
    receipt_bytes: bytes,
    revision: int,
    frozen_at: str,
) -> tuple[dict, str] | None:
    """Replicate freeze_metadata's payload for the planned state so the plan
    pins an expected freeze payload hash.  None when citation or provenance
    is not ready (reason returned separately)."""
    import hashlib

    from src.metadata.freeze import FREEZE_SCHEMA_VERSION, _citation_hashes, _source_record_hashes

    citation_hashes, citation_errors = _citation_hashes(metadata)
    if citation_errors:
        return None, "; ".join(citation_errors)
    source_hashes, source_errors = _source_record_hashes(folder, metadata)
    if source_errors:
        return None, "; ".join(source_errors)
    payload = {
        "schema_version": FREEZE_SCHEMA_VERSION,
        "paper_number": paper_number,
        "metadata_schema_version": str(metadata.get("schema_version") or ""),
        "metadata_sha256": compute_sha256(folder / f"{paper_number}.metadata.json"),
        "pdf_sha256": compute_sha256(folder / f"{paper_number}.pdf"),
        "metadata_match_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        "citation_ready": True,
        "citation_artifacts": citation_hashes,
        "source_record_hashes": source_hashes,
        "revision": revision,
        "frozen_at": frozen_at,
    }
    payload["_file_sha256"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    ).hexdigest()
    return payload, ""


def _plan_paper(folder: Path, paper_number: str, matched_at: str) -> dict | None:
    """Build the plan entry for one workspace (read-only)."""
    import hashlib

    pdf_path = folder / f"{paper_number}.pdf"
    metadata_path = folder / f"{paper_number}.metadata.json"
    if not pdf_path.is_file() or not metadata_path.is_file():
        return None
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    markdown = folder / f"{paper_number}.md"
    conversion = next(iter(folder.glob(f"{paper_number}.conversion.json")), None)
    evidence = extract_pdf_identity_evidence(
        pdf_path=pdf_path,
        markdown_path=markdown if markdown.is_file() else None,
        conversion_manifest_path=conversion,
    )
    provider_record = str((metadata.get("source") or {}).get("raw_record_path") or "")
    receipt = build_match_receipt(
        folder,
        paper_number,
        metadata,
        evidence,
        requested_doi=str((metadata.get("identifiers") or {}).get("doi") or ""),
        provider_records=[provider_record] if provider_record else [],
        matched_at=matched_at,
    )
    receipt_bytes = json.dumps(receipt, ensure_ascii=False, indent=2).encode("utf-8")

    match_path = folder / f"{paper_number}.metadata_match.json"
    freeze_path = folder / f"{paper_number}.metadata_freeze.json"
    old_receipt_sha = compute_sha256(match_path) if match_path.is_file() else None
    old_freeze_existed = freeze_path.is_file()
    old_freeze_sha = compute_sha256(freeze_path) if old_freeze_existed else None
    old_revision = 1
    if old_freeze_existed:
        try:
            old_revision = int(read_json(freeze_path).get("revision") or 1)
        except Exception:
            old_revision = 1

    freeze_eligible = False
    freeze_block_reason = ""
    target_revision = old_revision + 1 if old_freeze_existed else 1
    target_frozen_at = matched_at
    target_freeze_sha256 = None
    if receipt["match_status"] == "matched":
        citation_ready, citation_reason = _citation_ready_reason(metadata)
        if not citation_ready:
            freeze_block_reason = f"citation-not-ready: {citation_reason}"
        else:
            freeze_eligible = True
            simulated = _simulate_freeze_payload(
                folder, paper_number, metadata, receipt_bytes, target_revision, target_frozen_at
            )
            if simulated is None:
                freeze_eligible = False
                freeze_block_reason = "freeze simulation failed (citation/provenance)"
            else:
                target_freeze_sha256 = simulated[0]["_file_sha256"]
    else:
        freeze_block_reason = f"identity-not-matched: {receipt['match_status']}"

    return {
        "paper_number": paper_number,
        "old_receipt_sha256": old_receipt_sha,
        "old_freeze_sha256": old_freeze_sha,
        "old_freeze_existed": old_freeze_existed,
        "freeze_eligible": freeze_eligible,
        "freeze_block_reason": freeze_block_reason,
        "target_revision": target_revision,
        "target_frozen_at": target_frozen_at,
        "target_freeze_sha256": target_freeze_sha256,
        "receipt": receipt,
        "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        "baseline": {
            "pdf_sha256": compute_sha256(pdf_path),
            "metadata_sha256": compute_sha256(metadata_path),
            "receipt_sha256": old_receipt_sha,
            "freeze_sha256": old_freeze_sha,
            "freeze_existed": old_freeze_existed,
            "old_match_status": _legacy_match_status(match_path),
            "import_status_sha256": (
                compute_sha256(folder / ".import_status.json")
                if (folder / ".import_status.json").is_file()
                else None
            ),
            "legacy_metadata_state": str(
                (_read_status_tolerant(folder, paper_number).get("metadata") or {}).get("state")
                or ""
            ),
        },
    }


def _paper_numbers(root: Path, all_sources: bool, one: str | None, limit: int | None) -> list[str]:
    if one:
        from src.utils.identifiers import validate_paper_raw_id

        return [validate_paper_raw_id(one)]
    if all_sources:
        if not root.exists():
            return []
        numbers = sorted(
            p.name
            for p in root.iterdir()
            if p.is_dir() and p.name.isdigit() and len(p.name) == 16
        )
        if limit is not None:
            numbers = numbers[:limit]
        return numbers
    raise ValueError("--paper-number or --all is required")


def _build_plan(root: Path, args) -> dict:
    numbers = _paper_numbers(root, args.all, args.paper_number, args.limit)
    matched_at = now_iso()
    papers: dict[str, dict] = {}
    for number in numbers:
        try:
            entry = _plan_paper(root / number, number, matched_at)
        except Exception as exc:
            # A corrupt workspace is recorded in the plan (fail closed at
            # apply time); it must never crash the read-only plan phase.
            papers[number] = {
                "paper_number": number,
                "plan_error": str(exc)[:300],
                "baseline": {"old_match_status": "plan_error"},
            }
            continue
        if entry is None:
            papers[number] = {
                "paper_number": number,
                "skipped_no_assets": True,
                "baseline": {"old_match_status": "missing_assets"},
            }
            continue
        papers[number] = entry
    plan = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": matched_at,
        "extractor_version": EXTRACTOR_VERSION,
        "decision_policy_version": DECISION_POLICY_VERSION,
        "workspace_inventory_hash": _workspace_inventory(root, numbers),
        "coverage": {
            "total": len(numbers),
            "planned": sum(1 for e in papers.values() if not e.get("skipped_no_assets")),
            "complete": True,
        },
        "papers": papers,
    }
    plan["plan_content_hash"] = canonical_sha256(
        {key: value for key, value in plan.items() if key != "plan_content_hash"}
    )
    return plan


def _write_plan_outputs(plan: dict, plan_path: Path, baseline_path: Path) -> None:
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # plan_file_sha256 is recorded in the baseline, NEVER embedded in the
    # hashed plan file itself (self-reference would be circular).
    baseline = {
        "schema_version": SCHEMA_VERSION,
        "plan_content_hash": plan["plan_content_hash"],
        "plan_file_sha256": compute_sha256(plan_path),
        "papers": {
            number: entry.get("baseline", {})
            for number, entry in plan["papers"].items()
        },
    }
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── journal ────────────────────────────────────────────────────────────

def _journal_path(transaction_root: Path, plan_content_hash: str) -> Path:
    return transaction_root / f"pdf_identity_{plan_content_hash[:16]}" / "journal.json"


def _load_journal(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"journal unreadable (fail closed): {path}: {exc}") from exc


def _create_journal(journal_path: Path, plan: dict, plan_file_sha256: str) -> dict:
    if journal_path.exists():
        raise RuntimeError(f"journal already exists (fail closed): {journal_path}")
    journal = {
        "schema_version": SCHEMA_VERSION,
        "run_id": journal_path.parent.name,
        "phase": "planned",
        "plan_path": str(journal_path.parent / "plan.json"),
        "plan_content_hash": plan["plan_content_hash"],
        "plan_file_sha256": plan_file_sha256,
        "extractor_version": plan["extractor_version"],
        "decision_policy_version": plan["decision_policy_version"],
        "papers": {
            number: {
                "substate": "skipped" if entry.get("skipped_no_assets") else "pending",
                "status": "skipped_no_assets" if entry.get("skipped_no_assets") else "pending",
                "old_receipt_sha256": entry.get("old_receipt_sha256"),
                "new_receipt_sha256": entry.get("receipt_sha256"),
                "old_freeze_sha256": entry.get("old_freeze_sha256"),
                "old_freeze_existed": bool(entry.get("old_freeze_existed")),
                "freeze_eligible": bool(entry.get("freeze_eligible")),
                "freeze_block_reason": entry.get("freeze_block_reason", ""),
                "target_revision": entry.get("target_revision", 1),
                "target_frozen_at": entry.get("target_frozen_at"),
                "target_freeze_sha256": entry.get("target_freeze_sha256"),
                "failure_reason": "",
                "updated_at": now_iso(),
            }
            for number, entry in plan["papers"].items()
        },
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "last_error": None,
    }
    atomic_write_json(journal_path, journal, indent=2)
    return journal


def _verify_plan_hashes(plan: dict, expected_hash: str) -> None:
    if plan.get("plan_content_hash") != expected_hash:
        raise RuntimeError("--expected-plan-hash does not match the embedded plan hash")
    recomputed = canonical_sha256(
        {key: value for key, value in plan.items() if key != "plan_content_hash"}
    )
    if recomputed != expected_hash:
        raise RuntimeError("plan content hash does not match recomputation (tampered plan)")


# ── receipts phase ─────────────────────────────────────────────────────

def _backup_assets(folder: Path, paper_number: str, run_dir: Path) -> None:
    backup_dir = run_dir / "backups" / paper_number
    backup_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict] = {}
    for name, filename in _asset_filenames(paper_number):
        source = folder / filename
        existed = source.is_file()
        sha = compute_sha256(source) if existed else None
        backup_path = backup_dir / name
        if existed:
            shutil.copy2(source, backup_path)
        manifest[name] = {
            "existed": existed,
            "sha256": sha,
            "filename": filename,
            "backup_path": str(backup_path),
        }
    atomic_write_json(backup_dir / "manifest.json", manifest, indent=2)


def _apply_receipts(plan: dict, journal: dict, journal_path: Path, args) -> None:
    if journal["phase"] == "complete":
        # Idempotent re-run of a finished migration: a no-op.
        return
    if journal["phase"] not in {"planned", "receipts_applying"}:
        raise RuntimeError(
            f"cannot apply receipts from phase {journal['phase']} (fail closed)"
        )
    run_dir = journal_path.parent
    root = args.paper_raw_dir
    with paper_raw_write_lock(root):
        if journal["phase"] == "planned":
            marker = create_identity_migration_marker(
                root,
                {
                    "schema_version": "1.0",
                    "run_id": journal["run_id"],
                    "plan_content_hash": journal["plan_content_hash"],
                    "journal_path": str(journal_path),
                    "phase": "receipts_applying",
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                },
            )
            logger.info("maintenance marker created: {}", marker)
        journal["phase"] = "receipts_applying"
        journal["updated_at"] = now_iso()
        atomic_write_json(journal_path, journal, indent=2)

    for number, entry in plan["papers"].items():
        paper = journal["papers"].get(number)
        if paper is None:
            continue
        if paper["status"] not in RETRYABLE:
            continue
        folder = root / number
        if entry.get("skipped_no_assets"):
            paper["status"] = "skipped_no_assets"
            paper["substate"] = "skipped"
            paper["updated_at"] = now_iso()
            atomic_write_json(journal_path, journal, indent=2)
            continue
        if entry.get("plan_error"):
            paper["status"] = "failed"
            paper["failure_reason"] = f"plan error: {entry['plan_error']}"
            paper["updated_at"] = now_iso()
            atomic_write_json(journal_path, journal, indent=2)
            continue
        try:
            _apply_receipts_one(folder, number, entry, paper, run_dir, root, journal_path, journal)
        except Exception as exc:
            paper["status"] = "failed"
            paper["failure_reason"] = str(exc)[:300]
            paper["updated_at"] = now_iso()
            journal["last_error"] = f"{number}: {exc}"
            journal["updated_at"] = now_iso()
            atomic_write_json(journal_path, journal, indent=2)
            logger.error("receipt apply failed for {}: {}", number, exc)

    pending = [
        number
        for number, paper in journal["papers"].items()
        if paper["status"] not in RECEIPT_TERMINAL
    ]
    if pending:
        journal["phase"] = "receipts_applying"
        journal["last_error"] = f"receipts phase incomplete: {len(pending)} non-terminal"
        journal["updated_at"] = now_iso()
        atomic_write_json(journal_path, journal, indent=2)
        logger.error(
            "receipts phase NOT complete: {} papers failed or drifted (fail closed)", len(pending)
        )
        raise RuntimeError(
            f"receipts phase incomplete: {len(pending)} papers failed or drifted; "
            "fix and re-run --receipts-only (resume) before --freeze-eligible"
        )
    journal["phase"] = "receipts_applied"
    journal["last_error"] = None
    journal["updated_at"] = now_iso()
    atomic_write_json(journal_path, journal, indent=2)
    logger.info("receipts phase complete ({} papers)", len(journal["papers"]))


def _apply_receipts_one(
    folder: Path,
    number: str,
    entry: dict,
    paper: dict,
    run_dir: Path,
    root: Path,
    journal_path: Path,
    journal: dict,
) -> None:
    """Per-paper substate machine: pending -> backup_done ->
    freeze_invalidated -> receipt_written -> status_written.  A crash at any
    point resumes from the recorded substate."""
    with paper_raw_write_lock(root):
        match_path = folder / f"{number}.metadata_match.json"
        freeze_path = folder / f"{number}.metadata_freeze.json"
        substate = paper["substate"]
        if substate == "pending":
            if entry.get("old_receipt_sha256"):
                current = compute_sha256(match_path) if match_path.is_file() else None
                if current != entry["old_receipt_sha256"]:
                    raise RuntimeError(
                        f"receipt changed since plan (expected {entry['old_receipt_sha256']}, "
                        f"current {current})"
                    )
            _backup_assets(folder, number, run_dir)
            substate = "backup_done"
        if substate == "backup_done" and entry.get("old_freeze_existed"):
            invalidated = run_dir / "invalidated_freeze" / f"{number}.metadata_freeze.json"
            invalidated.parent.mkdir(parents=True, exist_ok=True)
            if freeze_path.is_file():
                os.replace(freeze_path, invalidated)
            substate = "freeze_invalidated"
        elif substate == "backup_done":
            substate = "freeze_invalidated"
        if substate == "freeze_invalidated":
            atomic_write_json(match_path, entry["receipt"], indent=2)
            written = compute_sha256(match_path)
            if written != entry["receipt_sha256"]:
                raise RuntimeError(f"receipt write mismatch: {written}")
            substate = "receipt_written"
        if substate == "receipt_written":
            state = RECEIPT_STATUS_TO_METADATA_STATE.get(
                entry["receipt"]["match_status"], "resolved"
            )
            _tolerant_update_status(
                folder,
                number,
                state,
                fields={
                    "match_method": entry["receipt"]["match_method"],
                    "match_status": entry["receipt"]["match_status"],
                },
            )
            substate = "status_written"
        paper["substate"] = substate
        paper["status"] = "status_written"
        paper["failure_reason"] = ""
        paper["updated_at"] = now_iso()
        atomic_write_json(journal_path, journal, indent=2)


# ── freeze phase ───────────────────────────────────────────────────────

def _apply_freezes(plan: dict, journal: dict, journal_path: Path, args) -> None:
    if journal["phase"] == "complete":
        # Idempotent re-run of a finished migration: a no-op.
        return
    if journal["phase"] not in {"receipts_applied", "freeze_applying"}:
        raise RuntimeError(
            f"cannot apply freezes from phase {journal['phase']} (fail closed)"
        )
    root = args.paper_raw_dir
    journal["phase"] = "freeze_applying"
    journal["updated_at"] = now_iso()
    atomic_write_json(journal_path, journal, indent=2)
    for number, entry in plan["papers"].items():
        paper = journal["papers"].get(number)
        if paper is None or paper["status"] in FREEZE_TERMINAL:
            continue
        if paper["status"] != "status_written":
            raise RuntimeError(
                f"freeze phase requires receipts phase complete; {number} is {paper['status']}"
            )
        folder = root / number
        if not entry.get("freeze_eligible"):
            paper["status"] = "freeze_blocked"
            paper["freeze_block_reason"] = entry.get("freeze_block_reason", "not eligible")
            paper["updated_at"] = now_iso()
            atomic_write_json(journal_path, journal, indent=2)
            continue
        try:
            _apply_freeze_one(folder, number, entry, paper, root, journal_path, journal)
        except Exception as exc:
            paper["status"] = "freeze_blocked"
            paper["freeze_block_reason"] = str(exc)[:300]
            paper["updated_at"] = now_iso()
            journal["last_error"] = f"{number}: {exc}"
            journal["updated_at"] = now_iso()
            atomic_write_json(journal_path, journal, indent=2)
            logger.error("freeze apply failed for {}: {}", number, exc)

    blocked = [
        number for number, paper in journal["papers"].items()
        if paper["status"] == "freeze_blocked"
    ]
    journal["phase"] = "complete"
    journal["last_error"] = None
    journal["updated_at"] = now_iso()
    atomic_write_json(journal_path, journal, indent=2)
    with paper_raw_write_lock(root):
        remove_identity_migration_marker(
            root,
            run_id=journal["run_id"],
            plan_content_hash=journal["plan_content_hash"],
        )
    logger.info(
        "freeze phase complete: {} rebuilt, {} blocked",
        sum(1 for p in journal["papers"].values() if p["status"] == "freeze_rebuilt"),
        len(blocked),
    )


def _apply_freeze_one(
    folder: Path,
    number: str,
    entry: dict,
    paper: dict,
    root: Path,
    journal_path: Path,
    journal: dict,
) -> None:
    with paper_raw_write_lock(root):
        # Re-validate at apply time; never trust the plan.
        match_path = folder / f"{number}.metadata_match.json"
        errors = validate_metadata_match_receipt(
            json.loads(match_path.read_text(encoding="utf-8")),
            metadata_path=folder / f"{number}.metadata.json",
            pdf_path=folder / f"{number}.pdf",
            workspace=folder,
        )
        if errors:
            raise RuntimeError(f"receipt replay failed: {'; '.join(errors)}")
        frozen = freeze_metadata(folder, number)
        # Pin revision and frozen_at to the plan's targets so the same plan
        # re-runs byte-identically.
        target_revision = int(entry.get("target_revision") or 1)
        target_frozen_at = entry.get("target_frozen_at") or frozen["frozen_at"]
        if frozen["revision"] != target_revision or frozen["frozen_at"] != target_frozen_at:
            frozen["revision"] = target_revision
            frozen["frozen_at"] = target_frozen_at
            atomic_write_json(folder / f"{number}.metadata_freeze.json", frozen, indent=2)
        assert_metadata_frozen(folder, number)
        actual = compute_sha256(folder / f"{number}.metadata_freeze.json")
        if entry.get("target_freeze_sha256") and actual != entry["target_freeze_sha256"]:
            raise RuntimeError(f"freeze payload drift: {actual} != {entry['target_freeze_sha256']}")
        from src.ingest.status import update_status

        update_status(
            PaperRawWorkspace.from_path(folder), "metadata", "frozen", revision=frozen["revision"]
        )
    paper["status"] = "freeze_rebuilt"
    paper["updated_at"] = now_iso()
    atomic_write_json(journal_path, journal, indent=2)


# ── abort ──────────────────────────────────────────────────────────────

def _abort_journal(journal: dict, journal_path: Path, args) -> None:
    run_dir = journal_path.parent
    root = args.paper_raw_dir
    with paper_raw_write_lock(root):
        for number, paper in journal["papers"].items():
            backup_dir = run_dir / "backups" / number
            manifest_path = backup_dir / "manifest.json"
            if not manifest_path.is_file():
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for name, record in manifest.items():
                filename = record.get("filename") or (
                    f"{number}.{name}" if name != "import_status" else ".import_status.json"
                )
                target = root / number / filename
                if record["existed"]:
                    shutil.copy2(Path(record["backup_path"]), target)
                elif target.exists():
                    # The file did not exist before the migration: abort
                    # deletes whatever the migration created.
                    target.unlink()
            paper["status"] = "restored"
            paper["updated_at"] = now_iso()
        journal["phase"] = "aborted"
        journal["updated_at"] = now_iso()
        atomic_write_json(journal_path, journal, indent=2)
        remove_identity_migration_marker(
            root,
            run_id=journal["run_id"],
            plan_content_hash=journal["plan_content_hash"],
        )
    logger.info("aborted run {}: all backed-up assets restored", journal["run_id"])


# ── CLI ────────────────────────────────────────────────────────────────

def _load_plan_for_apply(args, journal: dict | None) -> dict:
    plan = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
    if journal is not None:
        _verify_plan_hashes(plan, journal["plan_content_hash"])
    else:
        _verify_plan_hashes(plan, args.expected_plan_hash)
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--plan", action="store_true")
    modes.add_argument("--receipts-only", action="store_true")
    modes.add_argument("--freeze-eligible", action="store_true")
    modes.add_argument("--resume", metavar="JOURNAL")
    modes.add_argument("--abort", metavar="JOURNAL")
    modes.add_argument("--inspect-transaction", metavar="JOURNAL")
    parser.add_argument("--plan-file", type=Path)
    parser.add_argument("--expected-plan-hash", default="")
    parser.add_argument("--paper-number", default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--transaction-root", type=Path, default=None)
    parser.add_argument("--test-root", type=Path, default=None,
                        help="temporary root for dry-run/audit plans (never real apply)")
    parser.add_argument("--json-report", type=Path)
    args = parser.parse_args()

    transaction_root = (
        args.transaction_root
        or args.paper_raw_dir.parent / "transactions" / "pdf_identity"
    )

    if args.plan:
        root = args.test_root or args.paper_raw_dir
        plan = _build_plan(root, args)
        plan_path = args.plan_file or (transaction_root / "plan.json")
        _write_plan_outputs(plan, plan_path, plan_path.with_name("baseline.json"))
        print(f"[PLAN] {plan_path}")
        print(f"[PLAN-HASH] {plan['plan_content_hash']}")
        if args.json_report:
            atomic_write_json(args.json_report, {
                "mode": "plan",
                "plan_path": str(plan_path),
                "plan_content_hash": plan["plan_content_hash"],
                "coverage": plan["coverage"],
            }, indent=2)
        return 0

    if args.inspect_transaction:
        journal = _load_journal(Path(args.inspect_transaction))
        if journal is None:
            print(json.dumps({"status": "no_journal"}))
            return 0
        print(json.dumps({
            "run_id": journal["run_id"],
            "phase": journal["phase"],
            "plan_content_hash": journal["plan_content_hash"],
            "paper_count": len(journal["papers"]),
            "statuses": {
                status: sum(1 for p in journal["papers"].values() if p["status"] == status)
                for status in sorted({p["status"] for p in journal["papers"].values()})
            },
        }, indent=2))
        return 0

    if args.abort:
        journal_path = Path(args.abort)
        journal = _load_journal(journal_path)
        if journal is None:
            print(f"ERROR: no journal at {journal_path}", file=sys.stderr)
            return 1
        _abort_journal(journal, journal_path, args)
        print(json.dumps({"status": "aborted", "run_id": journal["run_id"]}, indent=2))
        return 0

    if args.resume:
        journal_path = Path(args.resume)
        journal = _load_journal(journal_path)
        if journal is None:
            print(f"ERROR: no journal at {journal_path}", file=sys.stderr)
            return 1
        if journal["phase"] == "complete":
            print(json.dumps({"status": "already_complete", "run_id": journal["run_id"]}, indent=2))
            return 0
        if journal["phase"] == "aborted":
            print("ERROR: journal is aborted; re-plan before applying", file=sys.stderr)
            return 1
        plan_path = Path(journal.get("plan_path") or journal_path.parent / "plan.json")
        if not plan_path.is_file():
            print(f"ERROR: plan file missing: {plan_path}", file=sys.stderr)
            return 1
        args.plan_file = plan_path
        plan = _load_plan_for_apply(args, journal)
        if journal["phase"] in {"planned", "receipts_applying"}:
            _apply_receipts(plan, journal, journal_path, args)
        elif journal["phase"] in {"receipts_applied", "freeze_applying"}:
            _apply_freezes(plan, journal, journal_path, args)
        print(json.dumps({"status": "resumed", "run_id": journal["run_id"],
                          "phase": journal["phase"]}, indent=2))
        return 0

    # --receipts-only / --freeze-eligible (real apply paths).
    if not args.plan_file or not args.expected_plan_hash:
        parser.error("--receipts-only/--freeze-eligible require --plan-file and --expected-plan-hash")
    if args.paper_number or args.limit is not None:
        parser.error("--paper-number/--limit are not allowed for real apply (full --all plan required)")
    numbers = _paper_numbers(args.paper_raw_dir, True, None, None)
    plan = _load_plan_for_apply(args, None)
    if plan["coverage"].get("complete") is not True:
        parser.error("plan is not a complete --all plan")
    journal_path = _journal_path(transaction_root, plan["plan_content_hash"])
    journal = _load_journal(journal_path)
    if journal is None:
        # The workspace inventory is validated only when a run starts:
        # later phases legitimately change the tree themselves.
        current_inventory = _workspace_inventory(args.paper_raw_dir, numbers)
        if current_inventory != plan["workspace_inventory_hash"]:
            raise RuntimeError(
                "workspace inventory drifted since plan (new/changed workspaces); "
                "re-plan before applying"
            )
        journal = _create_journal(
            journal_path, plan, compute_sha256(Path(args.plan_file))
        )
    elif journal["plan_content_hash"] != plan["plan_content_hash"]:
        raise RuntimeError("journal plan hash mismatch (fail closed)")
    elif journal["phase"] == "aborted":
        raise RuntimeError("previous run was aborted; re-plan before applying")
    if args.receipts_only:
        _apply_receipts(plan, journal, journal_path, args)
    else:
        _apply_freezes(plan, journal, journal_path, args)
    print(json.dumps({
        "mode": "receipts_only" if args.receipts_only else "freeze_eligible",
        "run_id": journal["run_id"],
        "phase": journal["phase"],
        "journal": str(journal_path),
    }, indent=2))
    if args.json_report:
        atomic_write_json(args.json_report, {
            "mode": "receipts_only" if args.receipts_only else "freeze_eligible",
            "run_id": journal["run_id"],
            "phase": journal["phase"],
            "statuses": {
                status: sum(1 for p in journal["papers"].values() if p["status"] == status)
                for status in sorted({p["status"] for p in journal["papers"].values()})
            },
        }, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

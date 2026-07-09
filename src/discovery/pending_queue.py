"""Pending discovery candidate drain.

Drain uses short page locks for candidate claim/commit and separate DOI or
title-resolution locks for external side effects. This provides effectively-once
outcomes via idempotency and reconciliation rather than pretending the file
system offers a cross-resource atomic transaction.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from filelock import FileLock, Timeout

from src.discovery.discovery_receipt import (
    DiscoveryReceiptConflictError,
    MatchingReceipt,
    ReceiptWriteResult,
    build_receipt_payload,
    find_matching_receipt,
    receipt_path_for,
    write_or_validate_discovery_receipt,
)
from src.discovery.models import PaperCandidate, normalize_doi
from src.discovery.page_journal import (
    PageJournalStore,
    stable_hash,
    title_resolution_key,
)
from src.discovery.resolve_crossref import resolve_doi_match_by_title
from src.services.ingest_duplicate_guard import check_doi_duplicate
from src.services.ingest_ids import PAPER_NUMBER_RE
from src.services.metadata_quality import is_valid_normalized_doi
from src.services.network_metadata_staging import stage_network_metadata_records
from src.services.v2_library import (
    LEDGER_METADATA_STAGED,
    PaperNumberLedger,
    metadata_doi,
    validate_metadata_schema,
)


DISCOVERY_LEASE_SECONDS = 300
DISCOVERY_LOCK_TIMEOUT = 30


@dataclass
class DrainReport:
    before: int = 0
    processed: int = 0
    remaining: int = 0
    staged: int = 0
    emitted: int = 0
    existing_duplicate: int = 0
    duplicate_observation: int = 0
    invalid: int = 0
    unresolved: int = 0
    retryable_failures: int = 0
    terminal_failures: int = 0
    planned: int = 0
    backpressure: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.errors or self.retryable_failures or self.terminal_failures:
            return "partial_success" if self.processed else "failed"
        return "success"

    def to_dict(self) -> dict[str, Any]:
        return {
            "before": self.before,
            "processed": self.processed,
            "remaining": self.remaining,
            "staged": self.staged,
            "emitted": self.emitted,
            "existing_duplicate": self.existing_duplicate,
            "duplicate_observation": self.duplicate_observation,
            "invalid": self.invalid,
            "unresolved": self.unresolved,
            "retryable_failures": self.retryable_failures,
            "terminal_failures": self.terminal_failures,
            "planned": self.planned,
            "backpressure": self.backpressure,
            "errors": list(self.errors),
            "status": self.status,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _candidate_from_record(record: dict[str, Any]) -> PaperCandidate:
    payload = record.get("candidate") if isinstance(record.get("candidate"), dict) else {}
    return PaperCandidate.from_dict(payload)


def _doi_lock_path(locks_dir: Path, doi: str) -> Path:
    return Path(locks_dir) / "doi" / f"{stable_hash(normalize_doi(doi), length=40)}.lock"


def _resolution_lock_path(locks_dir: Path, candidate_record: dict[str, Any]) -> Path:
    return Path(locks_dir) / "resolution" / f"{title_resolution_key(candidate_record)}.lock"


def _lock(path: Path) -> FileLock:
    path.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(path), timeout=DISCOVERY_LOCK_TIMEOUT)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def write_discovery_receipt(
    paper_raw_dir: Path,
    *,
    paper_number: str,
    candidate_id: str,
    page_id: str,
    keyword_id: str,
    normalized_doi: str,
    provider: str = "",
) -> Path:
    """Write (or idempotently validate) a discovery receipt for a workspace.

    Thin wrapper around the shared :mod:`src.discovery.discovery_receipt`
    service so all callers — allocator, drain loop, reconciliation, and tests —
    share one writer with identical conflict semantics. Raises
    :class:`DiscoveryReceiptConflictError` if an existing receipt's identity
    disagrees; never overwrites.
    """
    payload = build_receipt_payload(
        candidate_id=candidate_id,
        page_id=page_id,
        keyword_id=keyword_id,
        normalized_doi=normalized_doi,
        paper_number=paper_number,
        provider=provider,
    )
    result = write_or_validate_discovery_receipt(
        receipt_path_for(Path(paper_raw_dir), paper_number),
        payload,
    )
    return result.path


@dataclass(frozen=True)
class WorkspaceReconciliationState:
    """Structured snapshot of a workspace's staging completeness.

    A workspace is only ``staged`` when metadata is valid AND a receipt with
    matching identity exists AND the manifest/import-status/ledger are
    terminal. A source record alone proves staging STARTED, not that it
    finished — that distinction is the core of the reconciliation fix.
    """

    paper_number: str
    source_record_exists: bool
    source_context_matches: bool
    metadata_exists: bool
    metadata_valid: bool
    receipt_exists: bool
    receipt_matches: bool
    stage_manifest_exists: bool
    import_status_exists: bool
    ledger_entry_exists: bool
    ledger_state: str | None


@dataclass(frozen=True)
class ReconciliationResult:
    """Typed outcome of :func:`reconcile_discovery_workspace`.

    ``status`` is one of:

    - ``staged``             — workspace was already fully complete (Case A)
    - ``recovered``          — receipt backfilled and workspace is now complete (Case B)
    - ``retryable_incomplete`` — needs re-staging into the same workspace via
                               ``reuse_paper_number`` (Cases B-partial, C, D, E)
    - ``receipt_conflict``   — existing receipt identity disagrees (Case F)
    - ``corruption``         — corrupt metadata/receipt files (Case E-unrecoverable)
    - ``not_found``          — no workspace has a matching source record (Case G)
    """

    status: str
    paper_number: str = ""
    receipt_path: str = ""
    reason: str = ""
    state: WorkspaceReconciliationState | None = None


def _scan_source_record_contexts(
    data: dict[str, Any],
) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    if isinstance(data.get("discovery_context"), dict):
        contexts.append(data["discovery_context"])
    record = data.get("record") if isinstance(data.get("record"), dict) else {}
    if isinstance(record.get("discovery_context"), dict):
        contexts.append(record["discovery_context"])
    return contexts


def _context_matches(
    ctx: dict[str, Any],
    *,
    candidate_id: str,
    page_id: str,
    keyword_id: str,
    normalized_doi: str,
) -> bool:
    ctx_doi = normalize_doi(ctx.get("normalized_doi") or ctx.get("doi") or "")
    return (
        str(ctx.get("candidate_id") or "") == candidate_id
        and str(ctx.get("page_id") or "") == page_id
        and str(ctx.get("keyword_id") or "") == keyword_id
        and ctx_doi == normalized_doi
    )


def _resolve_workspace_paper_number(workspace: Path) -> str:
    """Resolve the 16-digit paper_number for a workspace.

    Resolution order:
    1. Unique ``*.paper.number`` marker (parsed via PaperNumberLedger).
    2. Canonical metadata file's ``paper_number`` field.
    3. Fallback: ``workspace.name`` if it is a valid 16-digit number.

    For formal workspaces (in ``data/papers/``), the folder name is a
    ``paper_id`` (not a 16-digit number), so the marker is the only
    reliable source.  Returns ``""`` when unresolvable.
    """
    # 1. Marker
    markers = sorted(workspace.glob("*.paper.number")) if workspace.exists() else []
    for marker in markers:
        parsed = PaperNumberLedger.parse_marker_number(marker)
        if parsed and PAPER_NUMBER_RE.match(parsed):
            return parsed
    # 2. Canonical metadata (try both paper_number and paper_id naming)
    for candidate in sorted(workspace.glob("*.metadata.json")):
        # Skip non-canonical sidecars.
        name = candidate.name
        if any(suffix in name for suffix in (
            ".candidates.json", ".patch.json", ".resolve_report.json",
        )):
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            pn = str(data.get("paper_number") or "").strip()
            if pn and PAPER_NUMBER_RE.match(pn):
                return pn
        except Exception:
            continue
    # 3. Fallback: workspace.name if it's a valid 16-digit number
    if PAPER_NUMBER_RE.match(workspace.name):
        return workspace.name
    return ""


def _resolve_metadata_path(workspace: Path, paper_number: str) -> Path | None:
    """Resolve the canonical metadata file path for a workspace.

    - paper_raw: ``{paper_number}.metadata.json``
    - formal (papers/): ``{workspace.name}.metadata.json`` (workspace.name == paper_id)

    Excludes ``*.metadata.candidates.json``, ``*.metadata.patch.json``,
    ``*.metadata.resolve_report.json``, and other sidecars.
    """
    # Try paper_number-named file first (paper_raw convention).
    candidate = workspace / f"{paper_number}.metadata.json"
    if candidate.is_file():
        return candidate
    # Try workspace.name-named file (formal convention: paper_id).
    candidate = workspace / f"{workspace.name}.metadata.json"
    if candidate.is_file():
        return candidate
    # Fallback: glob for any canonical metadata file.
    for match in sorted(workspace.glob("*.metadata.json")):
        name = match.name
        if any(suffix in name for suffix in (
            ".candidates.json", ".patch.json", ".resolve_report.json",
        )):
            continue
        return match
    return None


def _resolve_receipt_path(workspace: Path, paper_number: str) -> Path | None:
    """Resolve the canonical receipt file path.

    Receipt is always named ``{paper_number}.discovery_receipt.json``,
    regardless of workspace lifecycle (the marker carries the number).
    """
    candidate = workspace / f"{paper_number}.discovery_receipt.json"
    if candidate.is_file():
        return candidate
    # Fallback: glob for any receipt in this workspace.
    matches = sorted(workspace.glob("*.discovery_receipt.json"))
    return matches[0] if matches else None


def _build_reconciliation_state(
    workspace: Path,
    paper_number: str,
    *,
    candidate_id: str,
    page_id: str,
    keyword_id: str,
    normalized_doi: str,
    ledger_path: Path | None,
) -> WorkspaceReconciliationState:
    """Read-only snapshot of one workspace's staging completeness.

    ``paper_number`` is resolved by the caller via
    :func:`_resolve_workspace_paper_number` (marker-first), so this
    function works correctly for both paper_raw and formal workspaces.
    """
    # Re-resolve paper_number from the workspace in case the caller
    # passed workspace.name (which may be a paper_id for formal workspaces).
    resolved_number = _resolve_workspace_paper_number(workspace)
    if resolved_number:
        paper_number = resolved_number

    source_record_exists = False
    source_context_matches = False
    for sr in sorted(workspace.glob("source_records/metadata_source.*.json")):
        source_record_exists = True
        try:
            data = json.loads(sr.read_text(encoding="utf-8"))
        except Exception:
            continue
        for ctx in _scan_source_record_contexts(data):
            if _context_matches(
                ctx,
                candidate_id=candidate_id,
                page_id=page_id,
                keyword_id=keyword_id,
                normalized_doi=normalized_doi,
            ):
                source_context_matches = True
                break
        if source_context_matches:
            break

    # Resolve metadata path (not hardcoded — handles formal workspaces).
    metadata_path = _resolve_metadata_path(workspace, paper_number)
    metadata_exists = metadata_path is not None and metadata_path.is_file()
    metadata_valid = False
    if metadata_exists and metadata_path is not None:
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not validate_metadata_schema(metadata):
                metadata_valid = metadata_doi(metadata) == normalized_doi
        except Exception:
            metadata_valid = False

    # Resolve receipt path (always {paper_number}.discovery_receipt.json).
    receipt_path = _resolve_receipt_path(workspace, paper_number)
    receipt_exists = receipt_path is not None and receipt_path.is_file()
    receipt_matches = False
    if receipt_exists and receipt_path is not None:
        try:
            existing = json.loads(receipt_path.read_text(encoding="utf-8"))
            from src.discovery.discovery_receipt import normalize_receipt_identity

            ident = normalize_receipt_identity(existing)
            receipt_matches = (
                ident["candidate_id"] == candidate_id
                and ident["page_id"] == page_id
                and ident["keyword_id"] == keyword_id
                and ident["normalized_doi"] == normalized_doi
            )
        except Exception:
            receipt_matches = False

    stage_manifest_exists = (workspace / "stage_manifest.json").is_file()
    import_status_exists = bool(list(workspace.glob("*.import_status.json")))

    ledger_entry_exists = False
    ledger_state: str | None = None
    if ledger_path is not None:
        try:
            ledger = PaperNumberLedger(ledger_path)
            entry = ledger.load().get("items", {}).get(paper_number)
            if isinstance(entry, dict):
                ledger_entry_exists = True
                ledger_state = str(entry.get("state") or "")
        except Exception:
            pass

    return WorkspaceReconciliationState(
        paper_number=paper_number,
        source_record_exists=source_record_exists,
        source_context_matches=source_context_matches,
        metadata_exists=metadata_exists,
        metadata_valid=metadata_valid,
        receipt_exists=receipt_exists,
        receipt_matches=receipt_matches,
        stage_manifest_exists=stage_manifest_exists,
        import_status_exists=import_status_exists,
        ledger_entry_exists=ledger_entry_exists,
        ledger_state=ledger_state,
    )


def reconcile_discovery_workspace(
    roots: Iterable[Path],
    *,
    candidate_id: str,
    page_id: str,
    keyword_id: str,
    normalized_doi: str,
    ledger_path: Path | None = None,
) -> ReconciliationResult:
    """Reconcile a discovery candidate against existing paper_raw workspaces.

    Finds a workspace whose source record carries a matching discovery context
    (candidate_id + page_id + keyword_id + normalized_doi), then classifies its
    staging completeness. A source record alone is NOT enough to mark staged —
    metadata must exist and be valid. When metadata is present but the receipt
    is missing, the receipt is backfilled via the shared receipt service (Case B
    recovery). When the workspace is incomplete in a way the reconciler cannot
    safely fix, it returns ``retryable_incomplete`` with the ``paper_number`` so
    the caller can re-stage into the SAME workspace without allocating a new
    number. A conflicting existing receipt is never overwritten.
    """
    normalized = normalize_doi(normalized_doi)
    for root in roots:
        if not Path(root).exists():
            continue
        for path in sorted(Path(root).glob("*/source_records/metadata_source.*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            matched = False
            for ctx in _scan_source_record_contexts(data):
                if _context_matches(
                    ctx,
                    candidate_id=candidate_id,
                    page_id=page_id,
                    keyword_id=keyword_id,
                    normalized_doi=normalized,
                ):
                    matched = True
                    break
            if not matched:
                continue
            workspace = path.parents[1]
            # Resolve paper_number from marker/metadata, NOT workspace.name
            # (formal workspaces are named after paper_id, not paper_number).
            paper_number = _resolve_workspace_paper_number(workspace)
            if not paper_number:
                # Cannot safely reconcile a workspace with no resolvable number.
                continue
            state = _build_reconciliation_state(
                workspace,
                paper_number,
                candidate_id=candidate_id,
                page_id=page_id,
                keyword_id=keyword_id,
                normalized_doi=normalized,
                ledger_path=ledger_path,
            )

            # Case F: receipt exists but identity conflicts — never overwrite.
            if state.receipt_exists and not state.receipt_matches:
                return ReconciliationResult(
                    status="receipt_conflict",
                    paper_number=paper_number,
                    reason="existing receipt identity disagrees with candidate",
                    state=state,
                )

            # Case C: metadata missing/invalid — cannot safely mark staged; the
            # caller re-stages into this workspace to rebuild metadata.
            if not state.metadata_valid:
                return ReconciliationResult(
                    status="retryable_incomplete",
                    paper_number=paper_number,
                    reason="metadata missing or invalid; re-stage to rebuild",
                    state=state,
                )

            staging_complete = (
                state.stage_manifest_exists
                and state.import_status_exists
                and state.ledger_state == LEDGER_METADATA_STAGED
            )

            # Case A: fully complete and consistent — idempotent staged.
            if state.receipt_matches and staging_complete:
                return ReconciliationResult(
                    status="staged",
                    paper_number=paper_number,
                    receipt_path=(workspace / f"{paper_number}.discovery_receipt.json").as_posix(),
                    reason="workspace already fully staged",
                    state=state,
                )

            # Case B: metadata valid but receipt missing — backfill the receipt.
            # If that completes the workspace, return recovered; otherwise the
            # caller re-stages to backfill manifest/import-status/ledger.
            if not state.receipt_matches:
                try:
                    result = write_or_validate_discovery_receipt(
                        workspace / f"{paper_number}.discovery_receipt.json",
                        build_receipt_payload(
                            candidate_id=candidate_id,
                            page_id=page_id,
                            keyword_id=keyword_id,
                            normalized_doi=normalized,
                            paper_number=paper_number,
                        ),
                    )
                except DiscoveryReceiptConflictError:
                    return ReconciliationResult(
                        status="receipt_conflict",
                        paper_number=paper_number,
                        reason="receipt conflict while backfilling",
                        state=state,
                    )
                receipt_posix = result.path.as_posix()
                if staging_complete:
                    return ReconciliationResult(
                        status="recovered",
                        paper_number=paper_number,
                        receipt_path=receipt_posix,
                        reason="receipt backfilled; workspace complete",
                        state=state,
                    )
                return ReconciliationResult(
                    status="retryable_incomplete",
                    paper_number=paper_number,
                    receipt_path=receipt_posix,
                    reason="receipt backfilled; manifest/import-status/ledger incomplete",
                    state=state,
                )

            # Case D/E: receipt matches and metadata valid, but manifest/import
            # status/ledger are not terminal — re-stage to backfill them.
            return ReconciliationResult(
                status="retryable_incomplete",
                paper_number=paper_number,
                receipt_path=(workspace / f"{paper_number}.discovery_receipt.json").as_posix()
                if state.receipt_exists else "",
                reason="manifest/import-status/ledger incomplete; re-stage to backfill",
                state=state,
            )
    return ReconciliationResult(status="not_found", reason="no matching source record")


def reconcile_receipt_from_source_records(
    roots: Iterable[Path],
    *,
    candidate_id: str,
    page_id: str,
    keyword_id: str,
    normalized_doi: str,
) -> dict[str, Any] | None:
    """Backward-compat thin wrapper around :func:`reconcile_discovery_workspace`.

    Returns a dict only when the workspace is fully staged or recovered
    (receipt backfilled). Incomplete workspaces return ``None`` so legacy
    callers fall through to normal staging — the new drain loop uses the typed
    result directly to drive ``reuse_paper_number`` recovery.
    """
    result = reconcile_discovery_workspace(
        roots,
        candidate_id=candidate_id,
        page_id=page_id,
        keyword_id=keyword_id,
        normalized_doi=normalized_doi,
    )
    if result.status in {"staged", "recovered"}:
        return {
            "paper_number": result.paper_number,
            "candidate_id": candidate_id,
            "page_id": page_id,
            "keyword_id": keyword_id,
            "normalized_doi": normalize_doi(normalized_doi),
            "receipt_path": result.receipt_path,
            "recovered": result.status == "recovered",
        }
    return None


def _export_id(candidate_id: str) -> str:
    return stable_hash("export", candidate_id, length=40)


def _export_paths(exports_dir: Path, export_id: str) -> tuple[Path, Path]:
    return Path(exports_dir) / f"{export_id}.jsonl", Path(exports_dir) / f"{export_id}.manifest.json"


def export_candidate_once(exports_dir: Path, record: dict[str, Any]) -> dict[str, Any]:
    export_id = _export_id(record["candidate_id"])
    jsonl_path, manifest_path = _export_paths(exports_dir, export_id)
    if manifest_path.exists() and jsonl_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
        return {
            "export_id": export_id,
            "export_path": jsonl_path.as_posix(),
            "manifest_path": manifest_path.as_posix(),
            "reconciled": bool(manifest),
        }
    payload = record.get("candidate") if isinstance(record.get("candidate"), dict) else {}
    _atomic_write_text(jsonl_path, json.dumps(payload, ensure_ascii=False) + "\n")
    manifest = {
        "schema_version": "1.0",
        "export_id": export_id,
        "candidate_id": record["candidate_id"],
        "page_id": record.get("page_id"),
        "jsonl_path": jsonl_path.as_posix(),
        "exported_at": _now_iso(),
    }
    _atomic_write_json(manifest_path, manifest)
    return {
        "export_id": export_id,
        "export_path": jsonl_path.as_posix(),
        "manifest_path": manifest_path.as_posix(),
        "reconciled": False,
    }


def _known_primary_dois(journal: PageJournalStore, keyword_ids: Iterable[str] | None) -> dict[str, str]:
    known: dict[str, str] = {}
    pending_processing: list[tuple[str, str]] = []
    for ref in journal.list_pages(keyword_ids):
        data = journal.read(ref.path)
        for item in data.get("candidates", []):
            status = item.get("status")
            if status not in {"staged", "emitted", "processing"}:
                continue
            cand = item.get("candidate") if isinstance(item.get("candidate"), dict) else {}
            doi = normalize_doi(cand.get("doi"))
            if not doi:
                continue
            if status == "processing":
                pending_processing.append((doi, item.get("candidate_id") or ""))
            else:
                known.setdefault(doi, item.get("candidate_id") or "")
    for doi, candidate_id in pending_processing:
        known.setdefault(doi, candidate_id)
    return known


def _resolve_missing_doi(candidate: PaperCandidate, budget_left: int) -> tuple[PaperCandidate, int, bool]:
    if candidate.doi or budget_left <= 0 or not candidate.title:
        return candidate, budget_left, False
    match = resolve_doi_match_by_title(candidate.title, year=candidate.year, domain_id=candidate.domain_id)
    budget_left -= 1
    if match and match.doi:
        candidate.doi = match.doi
        candidate.doi_resolution = match.to_dict()
        candidate.raw.setdefault("crossref_resolution", match.to_dict())
        return candidate, budget_left, True
    return candidate, budget_left, False


def drain_pending_candidates(
    *,
    journal: PageJournalStore,
    keyword_ids: list[str] | None,
    candidate_budget: int,
    stage_to_paper_raw: bool,
    apply: bool,
    paper_raw_dir: Path,
    papers_dir: Path,
    ledger_path: Path,
    locks_dir: Path,
    exports_dir: Path,
    worker_id: str,
    doi_resolution_budget: int = 10,
    lease_seconds: int = DISCOVERY_LEASE_SECONDS,
    skip_duplicates: bool = False,
) -> DrainReport:
    report = DrainReport(before=journal.count_pending_candidates(keyword_ids))
    if candidate_budget <= 0:
        report.remaining = report.before
        return report

    primary_dois = _known_primary_dois(journal, keyword_ids)
    remaining_resolution_budget = doi_resolution_budget
    claimable = journal.iter_claimable(keyword_ids)
    for page_path, record in claimable:
        if report.processed >= candidate_budget:
            break
        cid = record["candidate_id"]
        claim = journal.claim_candidate(
            page_path,
            candidate_id_value=cid,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        if not claim.claimed or not claim.candidate:
            continue
        current = claim.candidate
        page = journal.read(page_path)
        current["page_id"] = page.get("page_id")
        current["keyword_id"] = page.get("keyword_id")
        candidate = _candidate_from_record(current)

        try:
            if not candidate.doi:
                lock_path = _resolution_lock_path(locks_dir, current)
            else:
                lock_path = None
            lock_context = _lock(lock_path) if lock_path is not None else None
            if lock_context is None:
                class _Noop:
                    def __enter__(self): return None
                    def __exit__(self, exc_type, exc, tb): return False
                lock_context = _Noop()
            with lock_context:
                journal.renew_candidate_lease(
                    page_path,
                    candidate_id_value=cid,
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                )
                if not candidate.doi:
                    candidate, remaining_resolution_budget, resolved = _resolve_missing_doi(candidate, remaining_resolution_budget)
                    current["candidate"] = candidate.to_dict()
                    if resolved:
                        journal.update_candidate_payload(
                            page_path,
                            candidate_id_value=cid,
                            worker_id=worker_id,
                            candidate_payload=candidate.to_dict(),
                        )
                doi = normalize_doi(candidate.doi)
                if not doi:
                    journal.commit_candidate(
                        page_path,
                        candidate_id_value=cid,
                        worker_id=worker_id,
                        new_status="unresolved",
                        updates={"terminal_reason": "doi_unresolved"},
                    )
                    report.unresolved += 1
                    report.processed += 1
                    continue
                if not is_valid_normalized_doi(doi):
                    journal.commit_candidate(
                        page_path,
                        candidate_id_value=cid,
                        worker_id=worker_id,
                        new_status="invalid_doi",
                        updates={"terminal_reason": "invalid_doi"},
                    )
                    report.invalid += 1
                    report.processed += 1
                    continue

                doi_lock = _lock(_doi_lock_path(locks_dir, doi))
                with doi_lock:
                    primary_dois = _known_primary_dois(journal, keyword_ids)
                    if doi in primary_dois and primary_dois[doi] != cid:
                        journal.commit_candidate(
                            page_path,
                            candidate_id_value=cid,
                            worker_id=worker_id,
                            new_status="duplicate_observation",
                            updates={"terminal_reason": "duplicate_pending_observation"},
                        )
                        report.duplicate_observation += 1
                        report.processed += 1
                        continue
                    # Reconciliation runs BEFORE the receipt fast-path so a
                    # workspace whose receipt exists but whose manifest/import
                    # status/ledger are incomplete (crash between receipt write
                    # and manifest write) is re-staged into the SAME workspace
                    # instead of being silently marked staged with missing
                    # assets. find_matching_receipt below is the fallback for
                    # workspaces that carry a receipt but no discoverable
                    # source record.
                    recon = reconcile_discovery_workspace(
                        [paper_raw_dir, papers_dir],
                        candidate_id=cid,
                        page_id=page["page_id"],
                        keyword_id=page["keyword_id"],
                        normalized_doi=doi,
                        ledger_path=ledger_path,
                    )
                    if recon.status in {"staged", "recovered"}:
                        journal.commit_candidate(
                            page_path,
                            candidate_id_value=cid,
                            worker_id=worker_id,
                            new_status="staged",
                            updates={
                                "staged_paper_number": recon.paper_number,
                                "reconciled": True,
                                "terminal_reason": (
                                    "reconciled_discovery_source_record"
                                    if recon.status == "recovered"
                                    else "reconciled_discovery_workspace_complete"
                                ),
                            },
                        )
                        primary_dois[doi] = cid
                        report.staged += 1
                        report.processed += 1
                        continue
                    if recon.status == "receipt_conflict":
                        journal.commit_candidate(
                            page_path,
                            candidate_id_value=cid,
                            worker_id=worker_id,
                            new_status="failed_retryable",
                            updates={
                                "terminal_reason": "receipt_conflict",
                                "last_error": recon.reason,
                            },
                        )
                        report.retryable_failures += 1
                        report.processed += 1
                        continue
                    # Initialize reuse_number — may be set by the receipt
                    # fallback below or by the retryable_incomplete branch.
                    reuse_number = ""
                    # Fallback for workspaces that carry a receipt but no
                    # discoverable source record (e.g. staged via a path that
                    # did not write one). The receipt identity match locates
                    # the workspace, but does NOT by itself prove staging is
                    # complete — we must verify metadata/manifest/ledger.
                    if recon.status == "not_found":
                        receipt_match = find_matching_receipt(
                            [paper_raw_dir, papers_dir],
                            candidate_id=cid,
                            page_id=page["page_id"],
                            keyword_id=page["keyword_id"],
                            normalized_doi=doi,
                        )
                        if receipt_match:
                            # Resolve paper_number from the workspace (marker-first).
                            receipt_pn = _resolve_workspace_paper_number(
                                receipt_match.workspace
                            ) or receipt_match.paper_number
                            # Build a full reconciliation state for the workspace.
                            receipt_state = _build_reconciliation_state(
                                receipt_match.workspace,
                                receipt_pn,
                                candidate_id=cid,
                                page_id=page["page_id"],
                                keyword_id=page["keyword_id"],
                                normalized_doi=doi,
                                ledger_path=ledger_path,
                            )
                            # Only mark staged if the workspace is ACTUALLY
                            # complete (metadata valid + manifest + import_status
                            # + ledger terminal). A receipt alone is NOT proof.
                            receipt_staging_complete = (
                                receipt_state.metadata_valid
                                and receipt_state.receipt_matches
                                and receipt_state.stage_manifest_exists
                                and receipt_state.import_status_exists
                                and (
                                    receipt_state.ledger_state == LEDGER_METADATA_STAGED
                                    or receipt_state.ledger_state == "active"
                                )
                            )
                            if receipt_staging_complete:
                                journal.commit_candidate(
                                    page_path,
                                    candidate_id_value=cid,
                                    worker_id=worker_id,
                                    new_status="staged",
                                    updates={
                                        "staged_paper_number": receipt_pn,
                                        "reconciled": True,
                                        "terminal_reason": "reconciled_discovery_receipt",
                                    },
                                )
                                primary_dois[doi] = cid
                                report.staged += 1
                                report.processed += 1
                                continue
                            # Receipt found but workspace incomplete — check if
                            # we can re-stage in place (paper_raw only, not formal).
                            if (
                                receipt_state.metadata_valid
                                and PAPER_NUMBER_RE.match(receipt_pn)
                                and receipt_match.workspace.parent.name == "paper_raw"
                            ):
                                reuse_number = receipt_pn
                            # else: fall through to normal staging
                    # retryable_incomplete: re-stage into the SAME workspace to
                    # backfill metadata/receipt/manifest/ledger without
                    # allocating a new paper number. not_found/corruption fall
                    # through to the normal duplicate guard + fresh staging.
                    # The receipt fallback above may have already set reuse_number.
                    if not reuse_number and recon.status == "retryable_incomplete":
                        reuse_number = recon.paper_number

                    if not reuse_number:
                        dup = check_doi_duplicate(doi, paper_raw_dir=paper_raw_dir, papers_dir=papers_dir)
                        if dup.refs:
                            journal.commit_candidate(
                                page_path,
                                candidate_id_value=cid,
                                worker_id=worker_id,
                                new_status="existing_duplicate",
                                updates={
                                    "terminal_reason": "doi_duplicate",
                                    "duplicate_refs": [ref.to_dict() for ref in dup.refs],
                                },
                            )
                            report.existing_duplicate += 1
                            report.processed += 1
                            continue

                    if stage_to_paper_raw:
                        stage_report = stage_network_metadata_records(
                            [{
                                **candidate.to_dict(),
                                "doi_resolution": candidate.doi_resolution,
                                "discovery_context": {
                                    "candidate_id": cid,
                                    "page_id": page["page_id"],
                                    "keyword_id": page["keyword_id"],
                                    "normalized_doi": doi,
                                },
                            }],
                            paper_raw_dir=paper_raw_dir,
                            papers_dir=papers_dir,
                            ledger_path=ledger_path,
                            apply=apply,
                            dry_run=not apply,
                            skip_duplicates=skip_duplicates,
                            reuse_paper_number=reuse_number or None,
                        )
                        item = (stage_report.get("items") or [{}])[0]
                        status = item.get("status")
                        if status == "staged":
                            paper_number = str(item.get("paper_number") or item.get("paper_raw_id") or "")
                            if not item.get("receipt_path"):
                                try:
                                    write_discovery_receipt(
                                        paper_raw_dir,
                                        paper_number=paper_number,
                                        candidate_id=cid,
                                        page_id=page["page_id"],
                                        keyword_id=page["keyword_id"],
                                        normalized_doi=doi,
                                    )
                                except DiscoveryReceiptConflictError as conflict:
                                    journal.commit_candidate(
                                        page_path,
                                        candidate_id_value=cid,
                                        worker_id=worker_id,
                                        new_status="failed_retryable",
                                        updates={
                                            "terminal_reason": "receipt_conflict",
                                            "last_error": str(conflict),
                                        },
                                    )
                                    report.retryable_failures += 1
                                    report.processed += 1
                                    continue
                            journal.commit_candidate(
                                page_path,
                                candidate_id_value=cid,
                                worker_id=worker_id,
                                new_status="staged",
                                updates={
                                    "staged_paper_number": paper_number,
                                    "terminal_reason": "recovered_via_reuse" if reuse_number else "staged",
                                    "reused_paper_number": reuse_number or "",
                                },
                            )
                            primary_dois[doi] = cid
                            report.staged += 1
                        elif status == "planned":
                            journal.commit_candidate(
                                page_path,
                                candidate_id_value=cid,
                                worker_id=worker_id,
                                new_status="failed_retryable",
                                updates={"last_error": "dry_run_planned_not_terminal"},
                            )
                            report.planned += 1
                        elif status == "duplicate":
                            journal.commit_candidate(
                                page_path,
                                candidate_id_value=cid,
                                worker_id=worker_id,
                                new_status="existing_duplicate",
                                updates={"terminal_reason": "doi_duplicate", "stage_item": item},
                            )
                            report.existing_duplicate += 1
                        elif status == "failed_retryable":
                            journal.commit_candidate(
                                page_path,
                                candidate_id_value=cid,
                                worker_id=worker_id,
                                new_status="failed_retryable",
                                updates={"last_error": item.get("safe_error") or item.get("error")},
                            )
                            report.retryable_failures += 1
                        else:
                            journal.commit_candidate(
                                page_path,
                                candidate_id_value=cid,
                                worker_id=worker_id,
                                new_status="failed_terminal",
                                updates={"last_error": item.get("safe_error") or item.get("error") or "stage_failed"},
                            )
                            report.terminal_failures += 1
                    else:
                        export = export_candidate_once(exports_dir, current)
                        journal.commit_candidate(
                            page_path,
                            candidate_id_value=cid,
                            worker_id=worker_id,
                            new_status="emitted",
                            updates={
                                "export_id": export["export_id"],
                                "export_path": export["export_path"],
                                "emitted_at": _now_iso(),
                                "reconciled": export.get("reconciled", False),
                            },
                        )
                        primary_dois[doi] = cid
                        report.emitted += 1
                    report.processed += 1
        except Timeout as exc:
            report.retryable_failures += 1
            report.errors.append(str(exc))
            try:
                journal.commit_candidate(
                    page_path,
                    candidate_id_value=cid,
                    worker_id=worker_id,
                    new_status="failed_retryable",
                    updates={"last_error": str(exc)},
                )
            except Exception:
                pass
        except Exception as exc:
            report.retryable_failures += 1
            report.errors.append(str(exc))
            try:
                journal.commit_candidate(
                    page_path,
                    candidate_id_value=cid,
                    worker_id=worker_id,
                    new_status="failed_retryable",
                    updates={"last_error": str(exc)},
                )
            except Exception:
                pass
    report.remaining = journal.count_pending_candidates(keyword_ids)
    return report

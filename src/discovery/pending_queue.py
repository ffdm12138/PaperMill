"""Pending discovery candidate drain.

Drain uses short page locks for candidate claim/commit and separate DOI or
title-resolution locks for external side effects. This provides effectively-once
outcomes via idempotency and reconciliation rather than pretending the file
system offers a cross-resource atomic transaction.
"""
from __future__ import annotations

import json
import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from filelock import FileLock, Timeout

from src.discovery.discovery_receipt import (
    AmbiguousDiscoveryReceiptError,
    ReceiptLookupIdentity,
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
from src.services.formal_workspace_validation import validate_formal_paper_workspace
from src.services.metadata_quality import is_valid_normalized_doi
from src.services.network_metadata_staging import stage_network_metadata_records
from src.library.paper_number_ledger import LEDGER_METADATA_STAGED, PaperNumberLedger
from src.metadata.schema import metadata_doi, validate_metadata_schema


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
        workspace_root=Path(paper_raw_dir),
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
    asset_manifest_exists: bool
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
    workspace_path: str = ""
    workspace_kind: str = ""
    disposition: str = ""
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
    provider: str = "",
) -> bool:
    ctx_doi = normalize_doi(ctx.get("normalized_doi") or ctx.get("doi") or "")
    return (
        str(ctx.get("candidate_id") or "") == candidate_id
        and str(ctx.get("page_id") or "") == page_id
        and str(ctx.get("keyword_id") or "") == keyword_id
        and str(ctx.get("provider") or "").strip().lower() == str(provider or "").strip().lower()
        and ctx_doi == normalized_doi
    )


def _resolve_workspace_paper_number(workspace: Path) -> str:
    """Resolve the 16-digit paper_number for a workspace.

    Resolution order:
    1. Unique ``*.paper.number`` marker (parsed via PaperNumberLedger).
    2. Canonical metadata file's ``paper_number`` field.
    3. Fallback: ``workspace.name`` if it is a valid 16-digit number.

    For formal workspaces (in ``data/papers/``), the folder name is a
    ``paper_name`` (not a 16-digit number), so the marker is the only
    reliable source.  Returns ``""`` when unresolvable.
    """
    # 1. Marker
    markers = sorted(workspace.glob("*.paper.number")) if workspace.exists() else []
    for marker in markers:
        parsed = PaperNumberLedger.parse_marker_number(marker)
        if parsed and PAPER_NUMBER_RE.match(parsed):
            return parsed
    # 2. Canonical metadata (try both paper_number and paper_name naming)
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
    - formal (papers/): ``{workspace.name}.metadata.json`` (workspace.name == paper_name)

    Excludes ``*.metadata.candidates.json``, ``*.metadata.patch.json``,
    ``*.metadata.resolve_report.json``, and other sidecars.
    """
    # Try paper_number-named file first (paper_raw convention).
    candidate = workspace / f"{paper_number}.metadata.json"
    if candidate.is_file():
        return candidate
    # Try workspace.name-named file (formal convention: paper_name).
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


def _workspace_kind(workspace: Path) -> str:
    if workspace.parent.name == "papers":
        return "formal"
    if workspace.parent.name == "paper_raw" and not PAPER_NUMBER_RE.match(workspace.name):
        return "formal"
    if workspace.parent.name == "paper_raw" and PAPER_NUMBER_RE.match(workspace.name):
        return "paper_raw"
    return "unknown"


def _build_reconciliation_state(
    workspace: Path,
    paper_number: str,
    *,
    candidate_id: str,
    page_id: str,
    keyword_id: str,
    normalized_doi: str,
    ledger_path: Path | None,
    provider: str = "",
) -> WorkspaceReconciliationState:
    """Read-only snapshot of one workspace's staging completeness.

    ``paper_number`` is resolved by the caller via
    :func:`_resolve_workspace_paper_number` (marker-first), so this
    function works correctly for both paper_raw and formal workspaces.
    """
    # Re-resolve paper_number from the workspace in case the caller
    # passed workspace.name (which may be a paper_name for formal workspaces).
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
                provider=provider,
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
                and ident["provider"] == str(provider or "").strip().lower()
                and ident["normalized_doi"] == normalized_doi
                and ident["paper_number"] == paper_number
            )
        except Exception:
            receipt_matches = False

    stage_manifest_exists = (workspace / "stage_manifest.json").is_file()
    asset_manifest_exists = bool(list(workspace.glob("*.asset_manifest.json")))
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
        asset_manifest_exists=asset_manifest_exists,
        import_status_exists=import_status_exists,
        ledger_entry_exists=ledger_entry_exists,
        ledger_state=ledger_state,
    )


def inspect_discovery_workspace(
    roots: Iterable[Path],
    *,
    candidate_id: str,
    page_id: str,
    keyword_id: str,
    normalized_doi: str,
    provider: str = "",
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
                    provider=provider,
                ):
                    matched = True
                    break
            if not matched:
                continue
            workspace = path.parents[1]
            # Resolve paper_number from marker/metadata, NOT workspace.name
            # (formal workspaces are named after paper_name, not paper_number).
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
                provider=provider,
            )
            kind = _workspace_kind(workspace)

            # Case F: receipt exists but identity conflicts — never overwrite.
            if state.receipt_exists and not state.receipt_matches:
                return ReconciliationResult(
                    status="receipt_conflict",
                    paper_number=paper_number,
                    reason="existing receipt identity disagrees with candidate",
                    workspace_path=workspace.as_posix(),
                    workspace_kind=kind,
                    disposition="identity_conflict",
                    state=state,
                )

            # Formalized / committed workspaces are never repaired by the
            # discovery staging allocator. A complete formal workspace can be
            # treated as an already successful primary only when its receipt
            # identity is present and matches; otherwise it requires an
            # explicit formal repair path and remains non-terminal here.
            if kind == "formal":
                formal_result = validate_formal_paper_workspace(
                    workspace,
                    ledger=PaperNumberLedger(ledger_path) if ledger_path is not None else PaperNumberLedger(Path("__missing_ledger__.json")),
                    mode="strict",
                ) if ledger_path is not None else None
                if (
                    formal_result is not None
                    and formal_result.valid
                    and formal_result.paper_number == paper_number
                    and formal_result.normalized_doi == normalized
                    and state.receipt_matches
                ):
                    return ReconciliationResult(
                        status="formal_complete",
                        paper_number=paper_number,
                        receipt_path=(workspace / f"{paper_number}.discovery_receipt.json").as_posix(),
                        reason="formal workspace complete",
                        workspace_path=workspace.as_posix(),
                        workspace_kind=kind,
                        disposition="formal_complete",
                        state=state,
                    )
                return ReconciliationResult(
                    status="formal_repair_required",
                    paper_number=paper_number,
                    reason="formal_workspace_repair_required",
                    workspace_path=workspace.as_posix(),
                    workspace_kind=kind,
                    disposition="formal_repair_required",
                    state=state,
                )

            # Case C: metadata missing/invalid — cannot safely mark staged; the
            # caller re-stages into this workspace to rebuild metadata.
            if not state.metadata_valid:
                return ReconciliationResult(
                    status="retryable_incomplete",
                    paper_number=paper_number,
                    reason="metadata missing or invalid; re-stage to rebuild",
                    workspace_path=workspace.as_posix(),
                    workspace_kind=kind,
                    disposition="paper_raw_retryable_incomplete",
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
                    workspace_path=workspace.as_posix(),
                    workspace_kind=kind,
                    disposition="paper_raw_complete",
                    state=state,
                )

            # Case B: metadata valid but receipt missing — backfill the receipt.
            # If that completes the workspace, return recovered; otherwise the
            # caller re-stages to backfill manifest/import-status/ledger.
            if not state.receipt_matches:
                return ReconciliationResult(
                    status="retryable_incomplete",
                    paper_number=paper_number,
                    reason="receipt missing or mismatched; re-stage under allocator lock",
                    workspace_path=workspace.as_posix(),
                    workspace_kind=kind,
                    disposition="paper_raw_retryable_incomplete",
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
                workspace_path=workspace.as_posix(),
                workspace_kind=kind,
                disposition="paper_raw_retryable_incomplete",
                state=state,
            )
    return ReconciliationResult(
        status="not_found",
        reason="no matching source record",
        disposition="not_found",
    )


def reconcile_discovery_workspace(
    roots: Iterable[Path],
    *,
    candidate_id: str,
    page_id: str,
    keyword_id: str,
    normalized_doi: str,
    provider: str = "",
    ledger_path: Path | None = None,
) -> ReconciliationResult:
    """Compatibility wrapper for strict read-only workspace inspection."""
    return inspect_discovery_workspace(
        roots,
        candidate_id=candidate_id,
        page_id=page_id,
        keyword_id=keyword_id,
        normalized_doi=normalized_doi,
        provider=provider,
        ledger_path=ledger_path,
    )


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
        provider="",
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


@dataclass(frozen=True)
class ExportValidationResult:
    valid: bool
    reason: str = ""


def validate_export_artifacts(
    *,
    manifest_path: Path,
    jsonl_path: Path,
    expected_candidate_id: str,
    expected_export_id: str,
    expected_doi: str | None,
    export_root: Path,
) -> ExportValidationResult:
    """Validate identity, containment, bytes, and record count for an export."""
    root = Path(export_root).absolute()
    manifest_path = Path(manifest_path).absolute()
    jsonl_path = Path(jsonl_path).absolute()
    for label, path in (("manifest", manifest_path), ("JSONL", jsonl_path)):
        try:
            path.relative_to(root)
        except ValueError:
            return ExportValidationResult(False, f"{label} path escapes export root")
        current = root
        for part in path.relative_to(root).parts:
            current /= part
            if current.is_symlink():
                return ExportValidationResult(False, f"{label} path is a symlink")
        if not path.is_file():
            return ExportValidationResult(False, f"{label} missing")
        try:
            path.resolve(strict=True).relative_to(root.resolve(strict=True))
        except (OSError, ValueError):
            return ExportValidationResult(False, f"{label} resolved path escapes export root")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return ExportValidationResult(False, f"manifest unreadable: {type(exc).__name__}")
    if not isinstance(manifest, dict):
        return ExportValidationResult(False, "manifest invalid")
    if str(manifest.get("export_id") or "") != expected_export_id:
        return ExportValidationResult(False, "export_id mismatch")
    if str(manifest.get("candidate_id") or "") != expected_candidate_id:
        return ExportValidationResult(False, "candidate_id mismatch")
    recorded_path = Path(str(manifest.get("jsonl_path") or manifest.get("export_path") or "")).absolute()
    if recorded_path != jsonl_path:
        return ExportValidationResult(False, "artifact path mismatch")
    try:
        raw = jsonl_path.read_bytes()
        lines = [line for line in raw.splitlines() if line.strip()]
        records = [json.loads(line) for line in lines]
    except Exception as exc:
        return ExportValidationResult(False, f"JSONL unreadable: {type(exc).__name__}")
    if len(records) != 1 or not isinstance(records[0], dict):
        return ExportValidationResult(False, "JSONL record_count mismatch")
    expected = normalize_doi(expected_doi or "")
    manifest_doi = normalize_doi(manifest.get("normalized_doi") or manifest.get("doi") or "")
    payload_doi = normalize_doi(records[0].get("doi") or "")
    if expected and (manifest_doi != expected or payload_doi != expected):
        return ExportValidationResult(False, "DOI mismatch")
    artifact = manifest.get("artifact") or {}
    if not isinstance(artifact, dict):
        return ExportValidationResult(False, "artifact metadata missing")
    if int(artifact.get("size_bytes", -1)) != len(raw):
        return ExportValidationResult(False, "artifact size mismatch")
    if str(artifact.get("sha256") or "") != hashlib.sha256(raw).hexdigest():
        return ExportValidationResult(False, "artifact hash mismatch")
    if int(artifact.get("record_count", -1)) != 1:
        return ExportValidationResult(False, "artifact record_count mismatch")
    return ExportValidationResult(True)


def export_candidate_once(exports_dir: Path, record: dict[str, Any]) -> dict[str, Any]:
    export_id = _export_id(record["candidate_id"])
    jsonl_path, manifest_path = _export_paths(exports_dir, export_id)
    if manifest_path.exists() != jsonl_path.exists():
        raise RuntimeError("export_artifact_corrupt: manifest/JSONL pair incomplete")
    if manifest_path.exists() and jsonl_path.exists():
        validation = validate_export_artifacts(
            manifest_path=manifest_path,
            jsonl_path=jsonl_path,
            expected_candidate_id=str(record["candidate_id"]),
            expected_export_id=export_id,
            expected_doi=normalize_doi((record.get("candidate") or {}).get("doi") or ""),
            export_root=exports_dir,
        )
        if not validation.valid:
            raise RuntimeError(f"export_artifact_corrupt: {validation.reason}")
        return {
            "export_id": export_id,
            "export_path": jsonl_path.as_posix(),
            "manifest_path": manifest_path.as_posix(),
            "reconciled": True,
        }
    payload = record.get("candidate") if isinstance(record.get("candidate"), dict) else {}
    jsonl_text = json.dumps(payload, ensure_ascii=False) + "\n"
    _atomic_write_text(jsonl_path, jsonl_text)
    jsonl_bytes = jsonl_path.read_bytes()
    manifest = {
        "schema_version": "1.0",
        "export_id": export_id,
        "candidate_id": record["candidate_id"],
        "page_id": record.get("page_id"),
        "keyword_id": record.get("keyword_id"),
        "provider": record.get("provider"),
        "normalized_doi": normalize_doi(payload.get("doi") or ""),
        "jsonl_path": jsonl_path.as_posix(),
        "provider_identity": {"provider": record.get("provider")},
        "artifact": {
            "path": jsonl_path.as_posix(),
            "sha256": hashlib.sha256(jsonl_bytes).hexdigest(),
            "size_bytes": len(jsonl_bytes),
            "record_count": 1,
        },
        "exported_at": _now_iso(),
    }
    _atomic_write_json(manifest_path, manifest)
    return {
        "export_id": export_id,
        "export_path": jsonl_path.as_posix(),
        "manifest_path": manifest_path.as_posix(),
        "reconciled": False,
    }


def _candidate_doi_from_item(item: dict[str, Any]) -> str:
    cand = item.get("candidate") if isinstance(item.get("candidate"), dict) else {}
    return normalize_doi(cand.get("doi"))


def _find_doi_processing_owner(
    journal: PageJournalStore,
    keyword_ids: Iterable[str] | None,
    doi: str,
    *,
    exclude_candidate_id: str,
) -> str:
    for ref in journal.list_pages(keyword_ids):
        data = journal.read(ref.path)
        for item in data.get("candidates", []):
            candidate_id = str(item.get("candidate_id") or "")
            if candidate_id == exclude_candidate_id:
                continue
            if item.get("status") == "processing" and _candidate_doi_from_item(item) == doi:
                return candidate_id
    return ""


def inspect_emitted_primary_export(
    item: dict[str, Any],
    doi: str,
    *,
    exports_dir: Path,
) -> tuple[bool, str]:
    manifest_path = str(item.get("manifest_path") or item.get("export_manifest_path") or "").strip()
    export_path = str(item.get("export_path") or "").strip()
    export_id = str(item.get("export_id") or "").strip()
    candidate_id = str(item.get("candidate_id") or "").strip()
    if not export_id:
        return False, "emitted export_id missing"
    expected_jsonl, expected_manifest = _export_paths(exports_dir, export_id)
    if Path(manifest_path).absolute() != expected_manifest.absolute():
        return False, "manifest path is not canonical for trusted export root"
    if export_path and Path(export_path).absolute() != expected_jsonl.absolute():
        return False, "JSONL path is not canonical for trusted export root"
    if not manifest_path or not expected_manifest.is_file():
        return False, "emitted export manifest missing"
    if export_path:
        jsonl_path = Path(export_path)
    else:
        try:
            manifest_data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            jsonl_path = Path(str(manifest_data.get("jsonl_path") or manifest_data.get("export_path") or ""))
        except Exception:
            jsonl_path = Path(manifest_path).with_name(f"{export_id}.jsonl")
    validation = validate_export_artifacts(
        manifest_path=Path(manifest_path),
        jsonl_path=jsonl_path,
        expected_candidate_id=candidate_id,
        expected_export_id=export_id,
        expected_doi=doi,
        export_root=exports_dir,
    )
    return validation.valid, validation.reason


def _find_durable_primary_for_doi(
    journal: PageJournalStore,
    keyword_ids: Iterable[str] | None,
    doi: str,
    *,
    exclude_candidate_id: str,
    paper_raw_dir: Path,
    papers_dir: Path,
    ledger_path: Path,
    exports_dir: Path,
) -> tuple[str, str, str]:
    """Return (candidate_id, status, reason) for a verified DOI primary."""
    validation_failures: list[str] = []
    for ref in journal.list_pages(keyword_ids):
        data = journal.read(ref.path)
        page_id = str(data.get("page_id") or "")
        keyword_id = str(data.get("keyword_id") or "")
        for item in data.get("candidates", []):
            candidate_id = str(item.get("candidate_id") or "")
            if candidate_id == exclude_candidate_id:
                continue
            status = str(item.get("status") or "")
            if status not in {"staged", "emitted"}:
                continue
            if _candidate_doi_from_item(item) != doi:
                continue
            if status == "emitted":
                durable, reason = inspect_emitted_primary_export(item, doi, exports_dir=exports_dir)
                if durable:
                    return candidate_id, status, ""
                validation_failures.append(f"{candidate_id}: {reason}")
                continue
            recon = reconcile_discovery_workspace(
                [paper_raw_dir, papers_dir],
                candidate_id=candidate_id,
                page_id=page_id,
                keyword_id=keyword_id,
                normalized_doi=doi,
                provider=str(data.get("provider") or ""),
                ledger_path=ledger_path,
            )
            if recon.status in {"staged", "recovered", "formal_complete"}:
                return candidate_id, status, ""
            validation_failures.append(
                f"{candidate_id}: {recon.reason or recon.status or 'primary validation failed'}"
            )
    return "", "", "; ".join(validation_failures)


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

    remaining_resolution_budget = doi_resolution_budget
    drain_generation = f"drain-{worker_id}-{datetime.now(timezone.utc).timestamp():.6f}"
    deferred_candidate_ids: set[str] = set()
    claimable = journal.iter_claimable(keyword_ids)
    for page_path, record in claimable:
        if report.processed >= candidate_budget:
            break
        cid = record["candidate_id"]
        if cid in deferred_candidate_ids:
            continue
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
        current["provider"] = page.get("provider")
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
                    processing_owner = _find_doi_processing_owner(
                        journal,
                        keyword_ids,
                        doi,
                        exclude_candidate_id=cid,
                    )
                    if processing_owner:
                        journal.defer_candidate(
                            page_path,
                            candidate_id_value=cid,
                            worker_id=worker_id,
                            reason="doi_primary_processing",
                            drain_generation=drain_generation,
                            updates={
                                "last_error": f"same DOI candidate is processing: {processing_owner}",
                            },
                        )
                        deferred_candidate_ids.add(cid)
                        report.retryable_failures += 1
                        report.processed += 1
                        continue

                    primary_cid, primary_status, primary_failure = _find_durable_primary_for_doi(
                        journal,
                        keyword_ids,
                        doi,
                        exclude_candidate_id=cid,
                        paper_raw_dir=paper_raw_dir,
                        papers_dir=papers_dir,
                        ledger_path=ledger_path,
                        exports_dir=exports_dir,
                    )
                    if primary_cid:
                        journal.commit_candidate(
                            page_path,
                            candidate_id_value=cid,
                            worker_id=worker_id,
                            new_status="duplicate_observation",
                            updates={
                                "terminal_reason": "duplicate_observation",
                                "primary_candidate_id": primary_cid,
                                "primary_status": primary_status,
                            },
                        )
                        report.duplicate_observation += 1
                        report.processed += 1
                        continue
                    if primary_failure:
                        journal.defer_candidate(
                            page_path,
                            candidate_id_value=cid,
                            worker_id=worker_id,
                            reason="doi_primary_validation_failed",
                            drain_generation=drain_generation,
                            updates={
                                "last_error": primary_failure,
                                "primary_validation_failure": primary_failure,
                            },
                        )
                        deferred_candidate_ids.add(cid)
                        report.retryable_failures += 1
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
                        provider=str(page.get("provider") or ""),
                        ledger_path=ledger_path,
                    )
                    if recon.status in {"staged", "recovered", "formal_complete"}:
                        journal.commit_candidate(
                            page_path,
                            candidate_id_value=cid,
                            worker_id=worker_id,
                            new_status="staged" if recon.status != "formal_complete" else "existing_duplicate",
                            updates={
                                "staged_paper_number": recon.paper_number,
                                "reconciled": True,
                                "terminal_reason": (
                                    "reconciled_formal_workspace_complete"
                                    if recon.status == "formal_complete"
                                    else
                                    "reconciled_discovery_source_record"
                                    if recon.status == "recovered"
                                    else "reconciled_discovery_workspace_complete"
                                ),
                            },
                        )
                        if recon.status == "formal_complete":
                            report.existing_duplicate += 1
                        else:
                            report.staged += 1
                        report.processed += 1
                        continue
                    if recon.status == "formal_repair_required":
                        next_attempt_at = (
                            datetime.now(timezone.utc) + timedelta(minutes=15)
                        ).isoformat()
                        journal.defer_candidate(
                            page_path,
                            candidate_id_value=cid,
                            worker_id=worker_id,
                            reason="formal_workspace_repair_required",
                            drain_generation=drain_generation,
                            next_attempt_at=next_attempt_at,
                            updates={
                                "terminal_reason": "formal_workspace_repair_required",
                                "last_error": recon.reason or "formal_workspace_repair_required",
                                "formal_workspace_path": recon.workspace_path,
                            },
                        )
                        deferred_candidate_ids.add(cid)
                        report.retryable_failures += 1
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
                        try:
                            receipt_match = find_matching_receipt(
                                [paper_raw_dir, papers_dir],
                                lookup_key=ReceiptLookupIdentity(
                                    candidate_id=cid,
                                    page_id=str(page["page_id"]),
                                    keyword_id=str(page["keyword_id"]),
                                    provider=str(page.get("provider") or ""),
                                    normalized_doi=doi,
                                ),
                            )
                        except AmbiguousDiscoveryReceiptError as exc:
                            journal.defer_candidate(
                                page_path,
                                candidate_id_value=cid,
                                worker_id=worker_id,
                                reason="ambiguous_receipt_identity",
                                drain_generation=drain_generation,
                                updates={"last_error": str(exc)},
                            )
                            deferred_candidate_ids.add(cid)
                            report.retryable_failures += 1
                            report.processed += 1
                            continue
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
                                provider=str(page.get("provider") or ""),
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
                            formal_refs = [
                                ref for ref in dup.refs
                                if getattr(ref, "workspace_kind", "") == "formal" or ref.scope == "papers"
                            ]
                            formal_blocking_errors: list[str] = []
                            for ref in formal_refs:
                                validation = validate_formal_paper_workspace(
                                    Path(ref.folder),
                                    ledger=PaperNumberLedger(ledger_path),
                                    mode="strict",
                                )
                                if (
                                    not validation.valid
                                    or validation.normalized_doi != doi
                                    or validation.paper_number != ref.paper_number
                                ):
                                    formal_blocking_errors.append(
                                        "; ".join(validation.errors)
                                        or "formal workspace validation failed"
                                    )
                            if formal_blocking_errors:
                                next_attempt_at = (
                                    datetime.now(timezone.utc) + timedelta(minutes=15)
                                ).isoformat()
                                journal.defer_candidate(
                                    page_path,
                                    candidate_id_value=cid,
                                    worker_id=worker_id,
                                    reason="formal_workspace_repair_required",
                                    drain_generation=drain_generation,
                                    next_attempt_at=next_attempt_at,
                                    updates={
                                        "terminal_reason": "formal_workspace_repair_required",
                                        "last_error": " | ".join(formal_blocking_errors),
                                    },
                                )
                                deferred_candidate_ids.add(cid)
                                report.retryable_failures += 1
                                report.processed += 1
                                continue
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
                                    "provider": str(page.get("provider") or ""),
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
                                journal.commit_candidate(
                                    page_path,
                                    candidate_id_value=cid,
                                    worker_id=worker_id,
                                    new_status="failed_retryable",
                                    updates={
                                        "terminal_reason": "receipt_missing_after_staging",
                                        "last_error": "allocator did not return discovery receipt path",
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
                                "manifest_path": export.get("manifest_path", ""),
                                "emitted_at": _now_iso(),
                                "reconciled": export.get("reconciled", False),
                            },
                        )
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

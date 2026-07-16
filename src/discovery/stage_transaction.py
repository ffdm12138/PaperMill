"""Sole write-lock coordinator for discovery metadata staging."""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any, Literal, Sequence
import time

from filelock import FileLock

from src.discovery.models import normalize_doi
from src.discovery.workspace_index import DiscoveryIdentityRef
from src.discovery.workspace_registry import (
    DoiRegistryRef, WorkspaceRegistrySnapshot, classify_record_issues,
    formal_publication_view,
    refresh_registry_under_write_lock, revalidate_matched_records,
    scan_workspace_record,
)
from src.discovery.staging_metrics import NullStagingMetricsObserver, StagingMetricsObserver
from src.ingest.paper_raw import PaperRawAllocator
from src.discovery.discovery_receipt import (
    DiscoveryReceiptConflictError,
    build_receipt_payload,
    write_or_validate_discovery_receipt,
)
from src.library.paper_number_ledger import LockedLedgerSession, PaperNumberLedger
from src.metadata.schema import METADATA_SCHEMA_VERSION, metadata_doi, validate_metadata_schema
from src.path_utils import normalize_repo_path
from src.services.ingest_state import write_import_status
from src.services.source_records import ensure_raw_record_path_is_metadata_source, write_metadata_source_record
from src.services.stage_manifest import write_stage_manifest
from src.utils.atomic_io import atomic_write_json


class StageTransactionConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class StageTransactionError:
    code: str
    detail: str = ""


@dataclass(frozen=True)
class NormalizedDiscoveryCandidate:
    candidate_id: str
    page_id: str
    keyword_id: str
    provider: str
    normalized_doi: str
    metadata: dict[str, Any]
    requested_paper_number: str = ""


@dataclass(frozen=True)
class PreparedCandidate:
    """Validated, lock-free staging input."""
    candidate: NormalizedDiscoveryCandidate
    source_record: dict[str, Any]
    normalized_doi: str


@dataclass(frozen=True)
class DiscoveryStageResult:
    status: Literal["staged", "reused", "duplicate", "failed_retryable", "repair_required"]
    paper_number: str = ""
    workspace_path: Path | None = None
    receipt_path: str = ""
    duplicate_refs: tuple[DoiRegistryRef, ...] = ()
    identity_refs: tuple[DiscoveryIdentityRef, ...] = ()
    error: StageTransactionError | None = None


StageTransactionResult = DiscoveryStageResult


class DiscoveryStageTransaction:
    """Refresh, reconcile, allocate/write, and publish under one lock.

    External order is candidate claim -> DOI claim -> this write lock. Internal
    order is registry refresh -> identity -> DOI -> reuse/reserve -> durable
    writes -> ledger transition -> snapshot publish -> write-lock release.
    Candidate journal terminal updates happen only after this method returns.
    """

    def __init__(self, *, paper_raw_dir: Path, papers_dir: Path, ledger_path: Path,
                 registry_snapshot: WorkspaceRegistrySnapshot | None,
                 observer: StagingMetricsObserver | None = None) -> None:
        if registry_snapshot is None:
            raise StageTransactionConfigurationError("WorkspaceRegistry is required")
        self.paper_raw_dir = Path(paper_raw_dir)
        self.papers_dir = Path(papers_dir)
        self.ledger_path = Path(ledger_path)
        self._snapshot = registry_snapshot
        self._formal_view = formal_publication_view(registry_snapshot)
        self.observer = observer or NullStagingMetricsObserver()

    @property
    def registry_snapshot(self) -> WorkspaceRegistrySnapshot:
        return self._snapshot

    @property
    def formal_view(self):
        """Current immutable, generation-bound formal-primary projection."""
        return self._formal_view

    def _identity_refs(self, snapshot: WorkspaceRegistrySnapshot,
                       candidate: NormalizedDiscoveryCandidate,
                       doi: str) -> tuple[DiscoveryIdentityRef, ...]:
        formal = self._formal_view.workspace_id_index.lookup(
            candidate_id=candidate.candidate_id, page_id=candidate.page_id,
            keyword_id=candidate.keyword_id, normalized_doi=doi,
            provider=candidate.provider,
        )
        raw = snapshot.raw_workspace_id_index.lookup(
            candidate_id=candidate.candidate_id, page_id=candidate.page_id,
            keyword_id=candidate.keyword_id, normalized_doi=doi,
            provider=candidate.provider,
        )
        return tuple(dict.fromkeys((*formal, *raw)))

    def _doi_refs(self, snapshot: WorkspaceRegistrySnapshot,
                  doi: str) -> tuple[DoiRegistryRef, ...]:
        formal = self._formal_view.doi_index.lookup_doi(doi)
        raw = snapshot.raw_doi_index.lookup_doi(doi)
        return tuple(dict.fromkeys((*formal, *raw)))

    def stage_candidate(self, candidate: NormalizedDiscoveryCandidate, *,
                        source_record: dict[str, Any], apply: bool) -> DiscoveryStageResult:
        prepared = self.prepare_candidate(candidate, source_record=source_record)
        if isinstance(prepared, DiscoveryStageResult):
            return prepared
        return self.stage_candidates_batch((prepared,), apply=apply)[0]

    def probe_repair_backlog(
        self, paper_numbers: Sequence[str], *, budget: int | None = None,
        cursor_path: Path | None = None,
    ) -> tuple[str, ...]:
        """Revalidate a bounded repair sample at a batch boundary.

        Broken members remain in the immutable snapshot backlog.  A member is
        published only after its current ledger projection and workspace
        closure both validate, so probing can never make an incomplete record
        look healthy.
        """
        ordered = tuple(sorted(dict.fromkeys(str(number) for number in paper_numbers)))
        if budget is not None and budget < 0:
            raise ValueError("repair probe budget must be nonnegative")
        if not ordered or budget == 0:
            return ()
        last_number = ""
        if cursor_path is not None:
            cursor_path = Path(cursor_path)
            if cursor_path.parent.resolve() != self.paper_raw_dir.resolve():
                raise ValueError("repair probe cursor must live directly under paper_raw")
            try:
                payload = json.loads(cursor_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    last_number = str(payload.get("last_paper_number") or "")
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError):
                last_number = ""
        start = bisect_right(ordered, last_number)
        rotated = ordered[start:] + ordered[:start]
        numbers = rotated[:budget] if budget is not None else rotated
        ledger = PaperNumberLedger(self.ledger_path)
        wait_started = time.monotonic()
        with FileLock(str(self.paper_raw_dir / ".paper_raw_write.lock")):
            acquired = time.monotonic()
            self.observer.write_lock_acquired(wait_ms=(acquired - wait_started) * 1000)
            repaired: list[str] = []
            try:
                with LockedLedgerSession(ledger, observer=self.observer) as ledger_session:
                    for number in numbers:
                        self.observer.repair_backlog_probe()
                        validated = revalidate_matched_records(
                            self._snapshot, {number},
                            paper_raw_dir=self.paper_raw_dir,
                            papers_dir=self.papers_dir,
                            ledger_view=ledger_session.data,
                            observer=self.observer,
                        )
                        if validated.status != "ok" or validated.snapshot is None:
                            continue
                        self._snapshot = validated.snapshot
                        repaired.append(number)
                if cursor_path is not None:
                    atomic_write_json(cursor_path, {
                        "schema_version": "1.0",
                        "last_paper_number": numbers[-1],
                        "backlog_size": len(ordered),
                    }, indent=2)
                return tuple(repaired)
            finally:
                self.observer.write_lock_released(
                    hold_ms=(time.monotonic() - acquired) * 1000)

    @staticmethod
    def prepare_candidate(candidate: NormalizedDiscoveryCandidate, *,
                          source_record: dict[str, Any]) -> PreparedCandidate | DiscoveryStageResult:
        """Normalize and validate before acquiring the authoritative write lock."""
        doi = normalize_doi(candidate.normalized_doi)
        has_identity = bool(candidate.candidate_id and candidate.page_id)
        if not doi and not has_identity:
            return DiscoveryStageResult("repair_required", error=StageTransactionError("invalid_doi"))
        return PreparedCandidate(candidate=replace(candidate, normalized_doi=doi),
                                 source_record=dict(source_record), normalized_doi=doi)

    def stage_candidates_batch(self, candidates: Sequence[PreparedCandidate], *,
                               apply: bool, max_batch_size: int = 16,
                               max_lock_seconds: float = 2.0) -> tuple[DiscoveryStageResult, ...]:
        """Stage a small prepared batch with one lock, ledger load and refresh."""
        if not apply:
            return tuple(DiscoveryStageResult(
                "failed_retryable", error=StageTransactionError("apply_required")) for _ in candidates)
        if max_batch_size < 1 or len(candidates) > max_batch_size:
            raise ValueError(f"batch size must be 1..{max_batch_size}")
        if not candidates:
            return ()
        self.paper_raw_dir.mkdir(parents=True, exist_ok=True)
        ledger = PaperNumberLedger(self.ledger_path)
        allocator = PaperRawAllocator(self.paper_raw_dir, ledger_path=self.ledger_path, papers_dir=self.papers_dir)
        wait_started = time.monotonic()
        with FileLock(str(self.paper_raw_dir / ".paper_raw_write.lock")):
            acquired = time.monotonic()
            self.observer.write_lock_acquired(wait_ms=(acquired - wait_started) * 1000)
            try:
                with LockedLedgerSession(ledger, observer=self.observer) as ledger_session:
                    refreshed = refresh_registry_under_write_lock(
                        self._snapshot, paper_raw_dir=self.paper_raw_dir,
                        papers_dir=self.papers_dir, ledger_view=ledger_session.data,
                        observer=self.observer,
                        dirty_numbers={prepared.candidate.requested_paper_number
                                       for prepared in candidates
                                       if prepared.candidate.requested_paper_number},
                    )
                    if refreshed.status != "ok" or refreshed.snapshot is None:
                        formal_matches = tuple({
                            ref.paper_number
                            for prepared in candidates
                            if prepared.normalized_doi
                            for ref in self._formal_view.doi_index.lookup_doi(
                                prepared.normalized_doi
                            )
                        })
                        matched_detail = ""
                        if formal_matches:
                            validation = revalidate_matched_records(
                                self._snapshot, set(formal_matches),
                                paper_raw_dir=self.paper_raw_dir,
                                papers_dir=self.papers_dir,
                                ledger_view=ledger_session.data,
                                observer=self.observer,
                            )
                            matched_detail = ";".join(
                                map(str, validation.issues)
                            )
                        formal_match = bool(
                            formal_matches
                            and matched_detail
                            and not any(
                                token in matched_detail
                                for token in (
                                    "active_formal_directory_name_mismatch",
                                    "active_formal_ledger_name_mismatch",
                                    "ledger_folder_mismatch",
                                )
                            )
                        )
                        failure = DiscoveryStageResult(
                            "failed_retryable" if refreshed.status == "retryable_failure" else "repair_required",
                            error=StageTransactionError(
                                "matched_record_revalidation_failed" if formal_match
                                else "registry_refresh_failed",
                                matched_detail or ";".join(map(str, refreshed.issues))),
                        )
                        return tuple(failure for _ in candidates)
                    candidate_snapshot = refreshed.snapshot
                    if self._formal_view.generation != (candidate_snapshot.formal_generation or ""):
                        self._formal_view = formal_publication_view(candidate_snapshot)
                    validated_numbers: set[str] = set()
                    results: list[DiscoveryStageResult] = []
                    processed_count = 0
                    for index, prepared in enumerate(candidates):
                        if index and time.monotonic() - acquired >= max_lock_seconds:
                            results.extend(DiscoveryStageResult(
                                "failed_retryable", error=StageTransactionError(
                                    "lock_epoch_budget_exhausted"))
                                for _ in candidates[index:])
                            break
                        result = self._stage_with_locked_ledger(
                            prepared.candidate, source_record=prepared.source_record,
                            doi=prepared.normalized_doi, ledger_session=ledger_session,
                            allocator=allocator, candidate_snapshot=candidate_snapshot,
                            validated_numbers=validated_numbers,
                        )
                        processed_count += 1
                        results.append(result)
                        candidate_snapshot = self._snapshot
                    self.observer.batch_staged(processed_count)
                    return tuple(results)
            finally:
                self.observer.write_lock_released(
                    hold_ms=(time.monotonic() - acquired) * 1000)

    def _stage_with_locked_ledger(self, candidate: NormalizedDiscoveryCandidate, *,
                                  source_record: dict[str, Any], doi: str,
                                  ledger_session: LockedLedgerSession,
                                  allocator: PaperRawAllocator,
                                  candidate_snapshot: WorkspaceRegistrySnapshot,
                                  validated_numbers: set[str] | None = None) -> DiscoveryStageResult:
            validated_numbers = validated_numbers if validated_numbers is not None else set()
            initial_identity_refs = self._identity_refs(candidate_snapshot, candidate, doi)
            initial_doi_refs = self._doi_refs(candidate_snapshot, doi)
            matched_numbers = {
                ref.paper_number for ref in (*initial_identity_refs, *initial_doi_refs)
            }
            needs_validation = matched_numbers - validated_numbers
            if needs_validation:
                validated = revalidate_matched_records(
                    candidate_snapshot, needs_validation,
                    paper_raw_dir=self.paper_raw_dir, papers_dir=self.papers_dir,
                    ledger_view=ledger_session.data, observer=self.observer)
                if validated.status != "ok" or validated.snapshot is None:
                    return DiscoveryStageResult(
                        "repair_required",
                        error=StageTransactionError(
                            "matched_record_revalidation_failed",
                            ";".join(map(str, validated.issues))),
                    )
                candidate_snapshot = validated.snapshot
                self._snapshot = candidate_snapshot
                validated_numbers.update(needs_validation)

            identity_refs = self._identity_refs(candidate_snapshot, candidate, doi)
            duplicate_refs_raw = self._doi_refs(candidate_snapshot, doi)
            duplicate_refs = tuple(DoiRegistryRef(
                ref.paper_number, "papers" if ref.scope == "papers" else "paper_raw",
                Path(ref.folder), doi, None,
            ) for ref in duplicate_refs_raw)
            formal_numbers = {
                ref.paper_number for ref in (*identity_refs, *duplicate_refs)
                if ref.scope == "papers"
            }
            raw_numbers = {
                ref.paper_number for ref in (*identity_refs, *duplicate_refs)
                if ref.scope == "paper_raw"
            }
            if formal_numbers and raw_numbers and formal_numbers != raw_numbers:
                return DiscoveryStageResult(
                    "repair_required", duplicate_refs=duplicate_refs,
                    identity_refs=identity_refs,
                    error=StageTransactionError("cross_scope_duplicate"),
                )
            if len({ref.paper_number for ref in identity_refs}) > 1:
                return DiscoveryStageResult("repair_required", identity_refs=identity_refs,
                                            error=StageTransactionError("IdentityAmbiguous"))

            requested = candidate.requested_paper_number
            if requested and identity_refs and identity_refs[0].paper_number != requested:
                return DiscoveryStageResult(
                    "failed_retryable", identity_refs=identity_refs,
                    error=StageTransactionError("RequestedReuseIdentityMismatch"),
                )

            if len(identity_refs) == 1:
                existing_record = candidate_snapshot.records_by_number.get(identity_refs[0].paper_number)
                if existing_record is not None and existing_record.readiness.ready:
                    self._snapshot = candidate_snapshot
                    receipt = existing_record.evidence.discovery_receipts[0].path
                    return DiscoveryStageResult(
                        "reused", paper_number=existing_record.paper_number,
                        workspace_path=existing_record.workspace_path,
                        receipt_path=normalize_repo_path(receipt), identity_refs=identity_refs,
                    )

            if not doi and not identity_refs:
                return DiscoveryStageResult(
                    "repair_required", error=StageTransactionError("identity_not_found_without_doi"))

            reuse_number = identity_refs[0].paper_number if identity_refs else ""
            if requested and reuse_number != requested:
                return DiscoveryStageResult(
                    "failed_retryable", identity_refs=identity_refs,
                    error=StageTransactionError("RequestedReuseIdentityMismatch"),
                )
            if not reuse_number and duplicate_refs:
                formal = [ref for ref in duplicate_refs if ref.scope == "papers"]
                raw = [ref for ref in duplicate_refs if ref.scope == "paper_raw"]
                if formal and len(duplicate_refs) == 1:
                    self._snapshot = candidate_snapshot
                    return DiscoveryStageResult("duplicate", paper_number=formal[0].paper_number,
                                                workspace_path=formal[0].workspace_path,
                                                duplicate_refs=duplicate_refs)
                if len(raw) == 1 and not formal:
                    record = candidate_snapshot.records_by_number.get(raw[0].paper_number)
                    if record is not None and record.is_unsettled:
                        reuse_number = raw[0].paper_number
                    else:
                        self._snapshot = candidate_snapshot
                        return DiscoveryStageResult("duplicate", paper_number=raw[0].paper_number,
                                                    workspace_path=raw[0].workspace_path,
                                                    duplicate_refs=duplicate_refs)
                else:
                    code = ("cross_scope_duplicate" if formal and raw
                            and {ref.paper_number for ref in formal}
                            != {ref.paper_number for ref in raw} else "DoiAmbiguous")
                    return DiscoveryStageResult("repair_required", duplicate_refs=duplicate_refs,
                                                error=StageTransactionError(code))
            try:
                if reuse_number:
                    folder = self.paper_raw_dir / reuse_number
                    if not folder.is_dir():
                        raise FileNotFoundError(f"reuse workspace missing: {reuse_number}")
                    number = reuse_number
                else:
                    number, folder = ledger_session.reserve_number(self.paper_raw_dir)
                    self.observer.number_allocated()
                result = self._write_candidate(
                    folder=folder, paper_number=number,
                    candidate=candidate, source_record=source_record,
                    preserve_other_identity_receipt=bool(reuse_number and identity_refs),
                )
                item = dict(ledger_session.data["items"][number])
                pre_record = scan_workspace_record(folder, number, "paper_raw", item)
                validation_issues = classify_record_issues(
                    ledger_state="metadata_staged", evidence=pre_record.evidence,
                    readiness=pre_record.readiness)
                if validation_issues:
                    raise ValueError("staged workspace validation failed: " + ";".join(map(str, validation_issues)))
                ledger_session.transition_metadata_staged(number, folder)
                ledger_session.save_checkpoint()
                record = replace(
                    pre_record,
                    evidence=replace(pre_record.evidence, ledger_state="metadata_staged"),
                    lifecycle=replace(pre_record.lifecycle, ledger_state="metadata_staged"),
                )
                published = candidate_snapshot.replace_record(
                    record, max_number=int(ledger_session.data["max_number"]))
                self.observer.registry_direct_publish()
                self.observer.record_staged()
                validated_numbers.add(number)
            except Exception as exc:
                if "number" in locals() and "folder" in locals():
                    allocator._mark_stage_failed(number, folder, exc)
                return DiscoveryStageResult("failed_retryable", error=StageTransactionError(
                    "durable_write_failed", f"{type(exc).__name__}:{exc}"))
            self._snapshot = published
            number = str(result.get("paper_number") or result.get("paper_raw_id") or "")
            return DiscoveryStageResult(
                "reused" if reuse_number else "staged", paper_number=number,
                workspace_path=self.paper_raw_dir / number,
                receipt_path=str(result.get("receipt_path") or ""), identity_refs=identity_refs,
            )

    def inspect_doi(self, normalized_doi: str) -> DiscoveryStageResult:
        """Compatibility alias for read-only classification."""
        return self.classify_existing_doi(normalized_doi)

    def classify_existing_doi(self, normalized_doi: str) -> DiscoveryStageResult:
        """Refresh once and classify without allocating a raw workspace."""
        doi = normalize_doi(normalized_doi)
        ledger = PaperNumberLedger(self.ledger_path)
        self.paper_raw_dir.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.paper_raw_dir / ".paper_raw_write.lock")):
          with LockedLedgerSession(ledger, observer=self.observer) as ledger_session:
            refreshed = refresh_registry_under_write_lock(
                self._snapshot, paper_raw_dir=self.paper_raw_dir,
                papers_dir=self.papers_dir, ledger_view=ledger_session.data,
                observer=self.observer)
            if refreshed.status != "ok" or refreshed.snapshot is None:
                return DiscoveryStageResult(
                    "failed_retryable" if refreshed.status == "retryable_failure" else "repair_required",
                    error=StageTransactionError("registry_refresh_failed"))
            candidate_snapshot = refreshed.snapshot
            matched_refs = self._doi_refs(candidate_snapshot, doi)
            if matched_refs:
                validated = revalidate_matched_records(
                    candidate_snapshot, {ref.paper_number for ref in matched_refs},
                    paper_raw_dir=self.paper_raw_dir, papers_dir=self.papers_dir,
                    ledger_view=ledger_session.data, observer=self.observer)
                if validated.status != "ok" or validated.snapshot is None:
                    return DiscoveryStageResult(
                        "repair_required",
                        error=StageTransactionError(
                            "matched_record_revalidation_failed",
                            ";".join(map(str, validated.issues))),
                    )
                candidate_snapshot = validated.snapshot
            self._snapshot = candidate_snapshot
            refs = tuple(DoiRegistryRef(
                ref.paper_number, "papers" if ref.scope == "papers" else "paper_raw",
                Path(ref.folder), doi, None) for ref in self._doi_refs(self._snapshot, doi))
            if not refs:
                return DiscoveryStageResult("reused")
            if len(refs) == 1:
                return DiscoveryStageResult("duplicate", paper_number=refs[0].paper_number,
                                            workspace_path=refs[0].workspace_path,
                                            duplicate_refs=refs)
            return DiscoveryStageResult("repair_required", duplicate_refs=refs,
                                        error=StageTransactionError("DoiAmbiguous"))

    def _write_candidate(self, *, folder: Path,
                         paper_number: str, candidate: NormalizedDiscoveryCandidate,
                         source_record: dict[str, Any],
                         preserve_other_identity_receipt: bool = False) -> dict[str, Any]:
        data = dict(candidate.metadata)
        data["paper_number"] = paper_number
        data["paper_raw_id"] = paper_number
        data["schema_version"] = METADATA_SCHEMA_VERSION
        data["source_type"] = "network_search"
        source = data.get("source") if isinstance(data.get("source"), dict) else {}
        provider = str(source.get("provider") or candidate.provider or "network_search")
        source["raw_record_path"] = ensure_raw_record_path_is_metadata_source(
            source.get("raw_record_path") or "", provider)
        data["source"] = source
        errors = validate_metadata_schema(data)
        if errors:
            raise ValueError("invalid network metadata: " + "; ".join(errors))
        receipt_payload = build_receipt_payload(
            candidate_id=candidate.candidate_id, page_id=candidate.page_id,
            keyword_id=candidate.keyword_id, normalized_doi=candidate.normalized_doi,
            paper_number=paper_number, provider=candidate.provider,
        )
        receipt_path = folder / f"{paper_number}.discovery_receipt.json"
        receipt = None
        if receipt_path.exists():
            try:
                receipt = write_or_validate_discovery_receipt(
                    receipt_path, receipt_payload, workspace_root=self.paper_raw_dir)
            except DiscoveryReceiptConflictError:
                if not preserve_other_identity_receipt:
                    raise
        enriched_record = {
            **source_record,
            "discovery_context": {
                "candidate_id": candidate.candidate_id, "page_id": candidate.page_id,
                "keyword_id": candidate.keyword_id, "provider": candidate.provider,
                "normalized_doi": candidate.normalized_doi,
            },
        }
        write_metadata_source_record(folder, provider, enriched_record)
        atomic_write_json(folder / f"{paper_number}.metadata.json", data, indent=2)
        if receipt is None and not receipt_path.exists():
            receipt = write_or_validate_discovery_receipt(
                receipt_path, receipt_payload, workspace_root=self.paper_raw_dir)
        write_stage_manifest(
            folder, paper_number=paper_number, paper_raw_id=paper_number,
            workflow_path="network_metadata", source_type="network_search",
            pdf_source=None, staged_pdf=None,
        )
        write_import_status(
            folder, "staged_metadata",
            reason="citation metadata resolved; independent PDF identity match pending",
            extra={
                "paper_number": paper_number, "paper_raw_id": paper_number,
                "source_type": "network_search", "source_provider": provider,
                "doi": metadata_doi(data), "pdf_md5": "", "pdf_sha256": "",
            },
        )
        return {"paper_number": paper_number, "folder": str(folder),
                "receipt_path": normalize_repo_path(receipt.path if receipt else receipt_path)}

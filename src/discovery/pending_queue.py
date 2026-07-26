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
from contextlib import ExitStack
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from filelock import FileLock, Timeout

from src.workspace.receipt import (
    build_receipt_payload,
    receipt_path_for,
    write_or_validate_discovery_receipt,
)
from src.discovery.models import PaperCandidate
from src.utils.identifiers import normalize_doi
from src.discovery.contracts.page_journal import (
    stable_hash,
    title_resolution_key,
)
from src.discovery.drain_locks import doi_lock_path, drain_lock, resolution_lock_path
from src.discovery.export_artifacts import (
    export_candidate_once,
    export_paths,
    inspect_emitted_primary_export,
    inspect_emitted_primary_export_cached,
    validate_export_artifacts,
)
from src.discovery.stores.journal_drain_index import JournalDrainIndex
from src.workspace.receipt import write_discovery_receipt
from src.discovery.stores.page_journal_store import PageJournalStoreV4 as PageJournalStore
from src.discovery.providers.provider_errors import ProviderRequestBudgetExhausted
from src.discovery.runtime.batch_runtime import ActiveRelevanceProfiles, DiscoveryBatchRuntime
from src.discovery.runtime.budgets import BatchDoiResolutionBudget
from src.discovery.staging_gateway import MetadataStagingGateway
from src.discovery.title_resolution import DurableTitleCache, TitleResolutionService
from src.metadata.quality import is_valid_normalized_doi
from src.utils.atomic_io import atomic_write_json_unlocked, atomic_write_text
from src.utils.timestamps import utc_now_iso as _now_iso


DISCOVERY_LEASE_SECONDS = 300
DISCOVERY_LOCK_TIMEOUT = 30


class DrainOutcome(str, Enum):
    """Terminal result of one consumer/final-drain invocation."""

    COMPLETED = "completed"
    BUDGET_STOPPED = "budget_stopped"
    RETRYABLE_FAILED = "retryable_failed"
    PERMANENT_FAILED = "permanent_failed"
    REPAIR_REQUIRED = "repair_required"
    INTERRUPTED = "interrupted"


@dataclass
class DrainReport:
    before: int = 0
    processed: int = 0
    remaining: int = 0
    staged: int = 0
    reused_existing: int = 0
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
    outcome: DrainOutcome = DrainOutcome.COMPLETED
    stop_reason: str | None = None

    @property
    def status(self) -> str:
        """Derive keyword-facing status from typed drain outcome.

        v98: no generic FAILED; every drain has a specific typed outcome.
        """
        if self.outcome == DrainOutcome.BUDGET_STOPPED:
            return DrainOutcome.BUDGET_STOPPED.value
        if self.outcome == DrainOutcome.REPAIR_REQUIRED:
            return "repair_required"
        if self.outcome == DrainOutcome.INTERRUPTED:
            return "interrupted"
        if self.outcome in {DrainOutcome.RETRYABLE_FAILED, DrainOutcome.PERMANENT_FAILED}:
            return "partial_success" if self.processed else "failed"
        if self.errors or self.retryable_failures or self.terminal_failures:
            return "partial_success" if self.processed else "failed"
        return "success"

    @classmethod
    def retryable_failed(cls, exc: Exception, *, phase: str) -> "DrainReport":
        """Typed retryable drain failure."""
        return cls(
            retryable_failures=1,
            errors=[f"{phase}_drain_retryable:{type(exc).__name__}:{str(exc)[:400]}"],
            outcome=DrainOutcome.RETRYABLE_FAILED,
        )

    @classmethod
    def permanent_failed(cls, exc: Exception, *, phase: str) -> "DrainReport":
        """Typed permanent (terminal) drain failure."""
        return cls(
            terminal_failures=1,
            errors=[f"{phase}_drain_permanent:{type(exc).__name__}:{str(exc)[:400]}"],
            outcome=DrainOutcome.PERMANENT_FAILED,
        )

    @classmethod
    def repair_required(cls, reason: str) -> "DrainReport":
        """Local consistency error requiring repair."""
        return cls(
            errors=[reason],
            outcome=DrainOutcome.REPAIR_REQUIRED,
        )

    @classmethod
    def interrupted(cls, reason: str = "user interrupted") -> "DrainReport":
        """User interrupted drain."""
        return cls(
            errors=[reason],
            outcome=DrainOutcome.INTERRUPTED,
        )

    @classmethod
    def budget_stopped(cls, *, reason: str) -> "DrainReport":
        return cls(outcome=DrainOutcome.BUDGET_STOPPED, stop_reason=reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "before": self.before,
            "processed": self.processed,
            "remaining": self.remaining,
            "staged": self.staged,
            "reused_existing": self.reused_existing,
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
            "outcome": self.outcome.value,
            "stop_reason": self.stop_reason,
            "status": self.status,
        }



def _candidate_from_record(record: dict[str, Any]) -> PaperCandidate:
    payload = record.get("candidate") if isinstance(record.get("candidate"), dict) else {}
    return PaperCandidate.from_dict(payload)


def _resolve_missing_doi_with_service(
    candidate: PaperCandidate,
    service: Any,
) -> tuple[PaperCandidate, bool]:
    """Resolve a DOI through the batch-level TitleResolutionService.

    The service owns the batch budget, in-batch dedup, durable cache, and
    the shared Crossref limiter (via the unified ProviderClient).  It never
    raises for provider failures; unresolved candidates stay unresolved.
    """
    if candidate.doi or not candidate.title:
        return candidate, False
    match = service.resolve(candidate.title, year=candidate.year, domain_id=candidate.domain_id)
    if match and match.doi:
        candidate.doi = match.doi
        candidate.doi_resolution = match.to_dict()
        candidate.raw.setdefault("crossref_resolution", match.to_dict())
        return candidate, True
    return candidate, False


def _set_outcome(
    outcomes: dict[str, dict[str, Any]],
    candidate_id: str,
    *,
    new_status: str,
    updates: dict[str, Any],
    counter: str | None,
) -> None:
    """Record one candidate's epoch outcome row."""
    outcomes[candidate_id] = {
        "candidate_id": candidate_id,
        "new_status": new_status,
        "updates": updates,
        "counter": counter,
    }


def _retry_updates(reason: str, detail: str, *, drain_generation: str) -> dict[str, Any]:
    """Build the retryable-deferral updates payload for one candidate."""
    return {
        "last_deferred_reason": reason,
        "deferred_generation": drain_generation,
        "next_attempt_at": None,
        "last_error": detail,
    }


def _claim_epoch(
    refs: list[Any],
    *,
    journal: PageJournalStore,
    journal_index: JournalDrainIndex,
    runtime: DiscoveryBatchRuntime,
    attempted_candidate_ids: set[str],
    worker_id: str,
    lease_seconds: int,
) -> list[Any]:
    """Claim one epoch's candidate refs page-by-page and index the claims."""
    candidate_ids_by_page: dict[Path, list[str]] = {}
    for ref in refs:
        attempted_candidate_ids.add(ref.candidate_id)
        candidate_ids_by_page.setdefault(ref.page_path, []).append(ref.candidate_id)

    claims = []
    for page_path, candidate_ids in candidate_ids_by_page.items():
        page_claims = journal.claim_candidates_from_page(
            page_path,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            limit=len(candidate_ids),
            candidate_ids=candidate_ids,
            expected_profile_hash=journal_index.get_active_profile_hash(
                journal_index.get_page_keyword_id(page_path)
            ),
        )
        claims.extend(page_claims)
        if page_claims:
            runtime.metrics.candidate_claims += len(page_claims)
            runtime.metrics.journal_pages_written += 1
            runtime.metrics.page_fsyncs += 1
            for claim in page_claims:
                journal_index.update_candidate(page_path, claim.payload)
    return claims


def _normalize_epoch_claims(
    claims: list[Any],
    *,
    locks_dir: Path,
    title_service: TitleResolutionService,
    drain_generation: str,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, tuple[Any, dict[str, Any], PaperCandidate, str]],
    dict[str, str],
    dict[str, list[str]],
]:
    """Resolve and validate claim DOIs, grouping same-DOI followers behind a primary."""
    outcomes: dict[str, dict[str, Any]] = {}
    entries_by_id: dict[str, tuple[Any, dict[str, Any], PaperCandidate, str]] = {}
    primary_by_doi: dict[str, str] = {}
    followers_by_primary: dict[str, list[str]] = {}

    for claim in claims:
        current = dict(claim.payload)
        current.update(
            page_id=claim.page_id,
            keyword_id=claim.keyword_id,
            provider=claim.provider,
        )
        candidate = _candidate_from_record(current)
        try:
            if not candidate.doi:
                with drain_lock(resolution_lock_path(locks_dir, current)):
                    candidate, _resolved = _resolve_missing_doi_with_service(
                        candidate, title_service,
                    )
            doi = normalize_doi(candidate.doi)
        except ProviderRequestBudgetExhausted:
            raise
        except Exception as exc:
            _set_outcome(
                outcomes,
                claim.candidate_id,
                new_status="failed_retryable",
                updates=_retry_updates(
                    "doi_resolution_failed", str(exc),
                    drain_generation=drain_generation,
                ),
                counter="retryable_failures",
            )
            continue
        current["candidate"] = candidate.to_dict()
        if not doi:
            _set_outcome(
                outcomes,
                claim.candidate_id,
                new_status="unresolved",
                updates={"candidate": candidate.to_dict(), "terminal_reason": "doi_unresolved"},
                counter="unresolved",
            )
            continue
        if not is_valid_normalized_doi(doi):
            _set_outcome(
                outcomes,
                claim.candidate_id,
                new_status="invalid_doi",
                updates={"candidate": candidate.to_dict(), "terminal_reason": "invalid_doi"},
                counter="invalid",
            )
            continue
        candidate.doi = doi
        current["candidate"] = candidate.to_dict()
        entries_by_id[claim.candidate_id] = (claim, current, candidate, doi)
        primary_id = primary_by_doi.get(doi)
        if primary_id is None:
            primary_by_doi[doi] = claim.candidate_id
        else:
            followers_by_primary.setdefault(primary_id, []).append(claim.candidate_id)
    return outcomes, entries_by_id, primary_by_doi, followers_by_primary


def _renew_slow_leases(
    entries_by_id: dict[str, tuple[Any, dict[str, Any], PaperCandidate, str]],
    *,
    journal: PageJournalStore,
    runtime: DiscoveryBatchRuntime,
    worker_id: str,
    lease_seconds: int,
) -> None:
    """Renew journal leases only for claims older than half the lease window."""
    # A claimed item waits only behind the rest of its <=16 item epoch.
    # Renew only genuinely slow epochs; ordinary staging does not pay an
    # immediate extra page write after claim.
    for claim, _current, _candidate, _doi in entries_by_id.values():
        claimed_at = claim.payload.get("claimed_at")
        try:
            claim_age = (
                datetime.now(timezone.utc) - datetime.fromisoformat(str(claimed_at))
            ).total_seconds()
        except (TypeError, ValueError):
            claim_age = 0.0
        if claim_age >= lease_seconds / 2:
            if journal.renew_candidate_lease(
                claim.page_path,
                candidate_id_value=claim.candidate_id,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            ):
                runtime.metrics.candidate_lease_renewals += 1
                runtime.metrics.journal_pages_written += 1
                runtime.metrics.page_fsyncs += 1


def _stage_primary_group(
    *,
    entries_by_id: dict[str, tuple[Any, dict[str, Any], PaperCandidate, str]],
    primary_by_doi: dict[str, str],
    outcomes: dict[str, dict[str, Any]],
    report: DrainReport,
    gateway: MetadataStagingGateway,
    staging_context: Any,
    apply: bool,
    skip_duplicates: bool,
    locks_dir: Path,
    exports_dir: Path,
    journal_index: JournalDrainIndex,
    drain_generation: str,
) -> None:
    """Stage the epoch's authoritative primary group once under ordered DOI locks."""
    primary_ids = list(primary_by_doi.values())
    locks = sorted({
        doi_lock_path(locks_dir, entries_by_id[candidate_id][3])
        for candidate_id in primary_ids
    }, key=str)
    try:
        with ExitStack() as lock_stack:
            for lock_path in locks:
                lock_stack.enter_context(drain_lock(lock_path))

            stage_primary_ids: list[str] = []
            stage_records: list[dict[str, Any]] = []
            for primary_id in primary_ids:
                claim, current, candidate, doi = entries_by_id[primary_id]
                emitted_ref = journal_index.get_emitted_primary(doi)
                if emitted_ref is not None and emitted_ref.candidate_id != primary_id:
                    valid, reason = inspect_emitted_primary_export_cached(
                        journal_index, dict(emitted_ref.payload), doi,
                        exports_dir=exports_dir)
                    if valid:
                        _set_outcome(
                            outcomes,
                            primary_id,
                            new_status="duplicate_observation",
                            updates={
                                "candidate": candidate.to_dict(),
                                "terminal_reason": "duplicate_observation",
                                "primary_candidate_id": emitted_ref.candidate_id,
                            },
                            counter="duplicate_observation",
                        )
                    else:
                        _set_outcome(
                            outcomes,
                            primary_id,
                            new_status="failed_retryable",
                            updates=_retry_updates(
                                "doi_primary_validation_failed", reason,
                                drain_generation=drain_generation,
                            ),
                            counter="retryable_failures",
                        )
                    continue
                stage_primary_ids.append(primary_id)
                stage_records.append({
                    **candidate.to_dict(),
                    "doi_resolution": candidate.doi_resolution,
                    "discovery_context": {
                        "candidate_id": primary_id,
                        "page_id": claim.page_id,
                        "keyword_id": claim.keyword_id,
                        "provider": claim.provider,
                        "normalized_doi": doi,
                    },
                })

            if stage_records:
                stage_result = gateway.stage_batch(
                    stage_records,
                    apply=apply,
                    skip_duplicates=skip_duplicates,
                    transaction=staging_context.transaction,
                )
                stage_items = list(stage_result.items)
                if len(stage_items) != len(stage_primary_ids):
                    raise RuntimeError("staging batch result count mismatch")
                for primary_id, item in zip(stage_primary_ids, stage_items, strict=True):
                    _claim, _current, candidate, _doi = entries_by_id[primary_id]
                    status = str(item.get("status") or "")
                    if status == "staged":
                        actual_allocated = bool(item.get("actual_allocated"))
                        _set_outcome(
                            outcomes,
                            primary_id,
                            new_status="staged",
                            updates={
                                "candidate": candidate.to_dict(),
                                "staged_paper_number": str(item.get("paper_number") or ""),
                                "terminal_reason": "staged",
                            },
                            counter="staged" if actual_allocated else "reused_existing",
                        )
                    elif status == "duplicate":
                        _set_outcome(
                            outcomes,
                            primary_id,
                            new_status="existing_duplicate",
                            updates={
                                "candidate": candidate.to_dict(),
                                "terminal_reason": "doi_duplicate",
                                "stage_item": item,
                            },
                            counter="existing_duplicate",
                        )
                    elif status in {"failed_retryable", "repair_required"}:
                        _set_outcome(
                            outcomes,
                            primary_id,
                            new_status="failed_retryable",
                            updates=_retry_updates(
                                status,
                                str(item.get("safe_error") or item.get("error") or status),
                                drain_generation=drain_generation,
                            ),
                            counter="retryable_failures",
                        )
                    elif status == "planned":
                        _set_outcome(
                            outcomes,
                            primary_id,
                            new_status="failed_retryable",
                            updates={"last_error": "dry_run_planned_not_terminal"},
                            counter="planned",
                        )
                    else:
                        _set_outcome(
                            outcomes,
                            primary_id,
                            new_status="failed_terminal",
                            updates={
                                "last_error": str(
                                    item.get("safe_error") or item.get("error") or "stage_failed")
                            },
                            counter="terminal_failures",
                        )
    except ProviderRequestBudgetExhausted:
        raise
    except Exception as exc:
        detail = f"{type(exc).__name__}:{exc}"
        report.errors.append(detail)
        for primary_id in primary_ids:
            if primary_id not in outcomes:
                _set_outcome(
                    outcomes,
                    primary_id,
                    new_status="failed_retryable",
                    updates=_retry_updates(
                        "staging_batch_failed", detail,
                        drain_generation=drain_generation,
                    ),
                    counter="retryable_failures",
                )


def _inherit_follower_outcomes(
    followers_by_primary: dict[str, list[str]],
    entries_by_id: dict[str, tuple[Any, dict[str, Any], PaperCandidate, str]],
    outcomes: dict[str, dict[str, Any]],
    drain_generation: str,
) -> None:
    """Propagate each primary's durable result to its same-DOI followers."""
    # Followers never enter the authoritative transaction.  They inherit a
    # durable primary result, or remain retryable when the primary did not
    # reach a durable result.
    for primary_id, follower_ids in followers_by_primary.items():
        primary_outcome = outcomes.get(primary_id)
        for follower_id in follower_ids:
            _claim, _current, candidate, _doi = entries_by_id[follower_id]
            if primary_outcome and primary_outcome["new_status"] == "staged":
                _set_outcome(
                    outcomes,
                    follower_id,
                    new_status="duplicate_observation",
                    updates={
                        "candidate": candidate.to_dict(),
                        "terminal_reason": "duplicate_observation",
                        "primary_candidate_id": primary_id,
                    },
                    counter="duplicate_observation",
                )
            elif primary_outcome and primary_outcome["new_status"] in {
                "existing_duplicate", "duplicate_observation"
            }:
                _set_outcome(
                    outcomes,
                    follower_id,
                    new_status=primary_outcome["new_status"],
                    updates={
                        "candidate": candidate.to_dict(),
                        "terminal_reason": primary_outcome["updates"].get(
                            "terminal_reason", "doi_duplicate"),
                        "primary_candidate_id": primary_id,
                    },
                    counter=(
                        "existing_duplicate"
                        if primary_outcome["new_status"] == "existing_duplicate"
                        else "duplicate_observation"
                    ),
                )
            else:
                detail = "same_batch_primary_not_durable"
                if primary_outcome:
                    detail = str(primary_outcome["updates"].get("last_error") or detail)
                _set_outcome(
                    outcomes,
                    follower_id,
                    new_status="failed_retryable",
                    updates=_retry_updates(
                        "same_batch_primary_not_durable", detail,
                        drain_generation=drain_generation,
                    ),
                    counter="retryable_failures",
                )


def _commit_epoch_outcomes(
    claims: list[Any],
    outcomes: dict[str, dict[str, Any]],
    *,
    journal: PageJournalStore,
    journal_index: JournalDrainIndex,
    runtime: DiscoveryBatchRuntime,
    report: DrainReport,
    worker_id: str,
    drain_generation: str,
) -> None:
    """Backfill missing epoch outcomes, then commit journal results once per page."""
    for claim in claims:
        if claim.candidate_id not in outcomes:
            _set_outcome(
                outcomes,
                claim.candidate_id,
                new_status="failed_retryable",
                updates=_retry_updates(
                    "missing_batch_outcome", "missing_batch_outcome",
                    drain_generation=drain_generation,
                ),
                counter="retryable_failures",
            )

    results_by_page: dict[Path, list[dict[str, Any]]] = {}
    for claim in claims:
        results_by_page.setdefault(claim.page_path, []).append(outcomes[claim.candidate_id])
    for page_path, page_results in results_by_page.items():
        serialized = [{
            "candidate_id": result["candidate_id"],
            "new_status": result["new_status"],
            "updates": result["updates"],
        } for result in page_results]
        try:
            committed = journal.commit_candidate_results(
                page_path, serialized, worker_id=worker_id)
        except Exception as exc:
            report.errors.append(f"{type(exc).__name__}:{exc}")
            report.retryable_failures += len(page_results)
            report.processed += len(page_results)
            continue
        runtime.metrics.journal_pages_written += 1
        runtime.metrics.page_fsyncs += 1
        for item in committed:
            journal_index.update_candidate(page_path, item)
        for result in page_results:
            counter = result.get("counter")
            if counter:
                setattr(report, counter, getattr(report, counter) + 1)
            report.processed += 1


def _drain_staging_candidate_batches(
    *,
    journal: PageJournalStore,
    journal_index: JournalDrainIndex,
    runtime: DiscoveryBatchRuntime,
    report: DrainReport,
    keyword_ids: list[str] | None,
    candidate_budget: int,
    apply: bool,
    paper_raw_dir: Path,
    papers_dir: Path,
    ledger_path: Path,
    locks_dir: Path,
    exports_dir: Path,
    worker_id: str,
    doi_resolution_budget: int,
    lease_seconds: int,
    skip_duplicates: bool,
    gateway: MetadataStagingGateway,
) -> DrainReport:
    """Drain authoritative staging work in lock epochs of at most 16 claims.

    DOI/title normalization and same-batch DOI grouping happen before the
    transaction acquires ``.paper_raw_write.lock``.  Unique DOI file locks are
    then acquired in stable order, and the complete authoritative group enters
    ``stage_network_metadata_records`` once.  Journal outcomes are committed
    once per page after the staging result is durable.
    """
    staging_context = runtime.staging_context
    if staging_context is None:
        report.remaining = report.before
        report.errors.append("registry_configuration_failed:missing_staging_context")
        return report

    # The public drain entry creates this service when necessary; staging is
    # never permitted to fall back to an unscoped direct resolver.
    title_service = runtime.title_resolution_service
    assert title_service is not None
    drain_generation = f"drain-{worker_id}-{datetime.now(timezone.utc).timestamp():.6f}"
    attempted_candidate_ids: set[str] = set()
    claimed_total = 0

    while claimed_total < candidate_budget:
        batch_limit = min(16, candidate_budget - claimed_total)
        refs = [
            ref for ref in journal_index.claimable(keyword_ids)
            if ref.candidate_id not in attempted_candidate_ids
        ][:batch_limit]
        if not refs:
            break

        claims = _claim_epoch(
            refs,
            journal=journal,
            journal_index=journal_index,
            runtime=runtime,
            attempted_candidate_ids=attempted_candidate_ids,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        if not claims:
            continue
        claimed_total += len(claims)

        outcomes, entries_by_id, primary_by_doi, followers_by_primary = (
            _normalize_epoch_claims(
                claims,
                locks_dir=locks_dir,
                title_service=title_service,
                drain_generation=drain_generation,
            )
        )

        _renew_slow_leases(
            entries_by_id,
            journal=journal,
            runtime=runtime,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )

        _stage_primary_group(
            entries_by_id=entries_by_id,
            primary_by_doi=primary_by_doi,
            outcomes=outcomes,
            report=report,
            gateway=gateway,
            staging_context=staging_context,
            apply=apply,
            skip_duplicates=skip_duplicates,
            locks_dir=locks_dir,
            exports_dir=exports_dir,
            journal_index=journal_index,
            drain_generation=drain_generation,
        )

        _inherit_follower_outcomes(
            followers_by_primary, entries_by_id, outcomes, drain_generation,
        )

        _commit_epoch_outcomes(
            claims,
            outcomes,
            journal=journal,
            journal_index=journal_index,
            runtime=runtime,
            report=report,
            worker_id=worker_id,
            drain_generation=drain_generation,
        )


    terminal = (
        report.staged + report.reused_existing + report.existing_duplicate + report.duplicate_observation
        + report.invalid + report.unresolved + report.terminal_failures
    )
    report.remaining = max(0, report.before - terminal)
    runtime.metrics.sync_journal(journal_index)
    return report


def _registry_configuration_failed_report(
    *,
    journal: PageJournalStore,
    keyword_ids: list[str] | None,
    candidate_budget: int,
    worker_id: str,
    lease_seconds: int,
    active_profiles: ActiveRelevanceProfiles,
    exc: Exception,
) -> DrainReport:
    """Fail claimable candidates retryably when runtime creation fails."""
    index = JournalDrainIndex.build(
        journal, active_profile_hashes=active_profiles.by_keyword_id,
    )
    failed = DrainReport(before=index.pending_count(keyword_ids))
    failed.remaining = failed.before
    reason = f"registry_configuration_failed:{type(exc).__name__}"
    refs = index.claimable(keyword_ids)[:candidate_budget]
    by_page: dict[Path, list[str]] = {}
    for ref in refs:
        by_page.setdefault(ref.page_path, []).append(ref.candidate_id)
    for page_path, candidate_ids in by_page.items():
        claims = journal.claim_candidates_from_page(
            page_path, worker_id=worker_id, lease_seconds=lease_seconds,
            limit=min(16, len(candidate_ids)), candidate_ids=candidate_ids,
            expected_profile_hash=index.get_active_profile_hash(
                index.get_page_keyword_id(page_path)))
        journal.commit_candidate_results(page_path, [{
            "candidate_id": claim.candidate_id, "new_status": "failed_retryable",
            "updates": {"last_error": reason},
        } for claim in claims], worker_id=worker_id)
        failed.processed += len(claims)
        failed.retryable_failures += len(claims)
    failed.errors.append(reason)
    return failed


def _ensure_title_resolution_service(
    runtime: DiscoveryBatchRuntime, doi_resolution_budget: int,
) -> None:
    """Bind the batch-bound title resolver to an externally created runtime."""
    # ``drain_pending_candidates`` is also a public entry point.  Give an
    # externally created runtime the same batch-bound title resolver as the
    # coordinator instead of reviving the retired direct
    # ``ProviderRuntime.client()`` fallback.  Every title request now shares
    # this runtime's telemetry and request budget.
    if runtime.title_resolution_service is None:
        title_budget = runtime.doi_resolution_budget
        if title_budget is None:
            title_budget = BatchDoiResolutionBudget(limit=max(0, int(doi_resolution_budget)))
            runtime.doi_resolution_budget = title_budget
        runtime.title_resolution_service = TitleResolutionService(
            client=runtime.provider_client("crossref"),
            budget=title_budget,
            cache=DurableTitleCache(None),
        )


def _commit_candidate(
    journal: PageJournalStore,
    journal_index: JournalDrainIndex,
    runtime: DiscoveryBatchRuntime,
    page_path: Path,
    **kwargs: Any,
) -> dict[str, Any]:
    """Commit one candidate result and mirror it into the drain index."""
    committed = journal.commit_candidate(page_path, **kwargs)
    runtime.metrics.journal_pages_written += 1
    runtime.metrics.page_fsyncs += 1
    journal_index.update_candidate(page_path, committed)
    return committed


def _defer_candidate(
    journal: PageJournalStore,
    journal_index: JournalDrainIndex,
    runtime: DiscoveryBatchRuntime,
    page_path: Path,
    **kwargs: Any,
) -> dict[str, Any]:
    """Defer one candidate retryably and mirror it into the drain index."""
    deferred = journal.defer_candidate(page_path, **kwargs)
    runtime.metrics.journal_pages_written += 1
    runtime.metrics.page_fsyncs += 1
    journal_index.update_candidate(page_path, deferred)
    return deferred


def _drain_non_staging(
    *,
    journal: PageJournalStore,
    journal_index: JournalDrainIndex,
    runtime: DiscoveryBatchRuntime,
    report: DrainReport,
    title_service: TitleResolutionService,
    staging_context: Any,
    keyword_ids: list[str] | None,
    candidate_budget: int,
    locks_dir: Path,
    exports_dir: Path,
    worker_id: str,
    lease_seconds: int,
    hide_existing: bool,
) -> DrainReport:
    """Drain claimed candidates through per-DOI export emission without staging."""
    drain_generation = f"drain-{worker_id}-{datetime.now(timezone.utc).timestamp():.6f}"
    deferred_candidate_ids: set[str] = set()
    claimable = journal_index.claimable(keyword_ids)[:candidate_budget]
    candidate_ids_by_page: dict[Path, list[str]] = {}
    for ref in claimable:
        candidate_ids_by_page.setdefault(ref.page_path, []).append(ref.candidate_id)
    claimed = []
    for page_path, candidate_ids in candidate_ids_by_page.items():
        page_claims = journal.claim_candidates_from_page(
            page_path, worker_id=worker_id, lease_seconds=lease_seconds,
            limit=min(16, candidate_budget - len(claimed)), candidate_ids=candidate_ids,
            expected_profile_hash=journal_index.get_active_profile_hash(
                journal_index.get_page_keyword_id(page_path)
            ))
        claimed.extend(page_claims)
        if page_claims:
            runtime.metrics.candidate_claims += len(page_claims)
            runtime.metrics.journal_pages_written += 1
            runtime.metrics.page_fsyncs += 1
        if len(claimed) >= candidate_budget:
            break
    for claim in claimed:
        page_path = claim.page_path
        record = dict(claim.payload)
        if report.processed >= candidate_budget:
            break
        cid = record["candidate_id"]
        if cid in deferred_candidate_ids:
            continue
        current = record
        current["page_id"] = claim.page_id
        current["keyword_id"] = claim.keyword_id
        current["provider"] = claim.provider
        journal_index.update_candidate(page_path, current)
        candidate = _candidate_from_record(current)

        try:
            if not candidate.doi:
                lock_path = resolution_lock_path(locks_dir, current)
            else:
                lock_path = None
            lock_context = drain_lock(lock_path) if lock_path is not None else None
            if lock_context is None:
                class _Noop:
                    def __enter__(self): return None
                    def __exit__(self, exc_type, exc, tb): return False
                lock_context = _Noop()
            with lock_context:
                if not candidate.doi:
                    candidate, resolved = _resolve_missing_doi_with_service(
                        candidate, title_service,
                    )
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
                    _commit_candidate(
                        journal, journal_index, runtime,
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
                    _commit_candidate(
                        journal, journal_index, runtime,
                        page_path,
                        candidate_id_value=cid,
                        worker_id=worker_id,
                        new_status="invalid_doi",
                        updates={"terminal_reason": "invalid_doi"},
                    )
                    report.invalid += 1
                    report.processed += 1
                    continue

                claimed_at = current.get("claimed_at")
                try:
                    claim_age = (datetime.now(timezone.utc) - datetime.fromisoformat(
                        str(claimed_at))).total_seconds()
                except (TypeError, ValueError):
                    claim_age = 0.0
                if claim_age >= lease_seconds / 2:
                    if journal.renew_candidate_lease(
                        page_path, candidate_id_value=cid, worker_id=worker_id,
                        lease_seconds=lease_seconds):
                        runtime.metrics.candidate_lease_renewals += 1

                doi_lock = drain_lock(doi_lock_path(locks_dir, doi))
                with doi_lock:
                    processing_owner = journal_index.get_processing_owner(doi)
                    if processing_owner and processing_owner != cid:
                        _defer_candidate(
                            journal, journal_index, runtime,
                            page_path, candidate_id_value=cid, worker_id=worker_id,
                            reason="doi_primary_processing", drain_generation=drain_generation,
                            updates={"last_error": f"same DOI candidate is processing: {processing_owner}"})
                        deferred_candidate_ids.add(cid)
                        report.retryable_failures += 1
                        report.processed += 1
                        continue
                    emitted_primary = ""
                    emitted_failure = ""
                    emitted_ref = journal_index.get_emitted_primary(doi)
                    if emitted_ref is not None and emitted_ref.candidate_id != cid:
                        valid, reason = inspect_emitted_primary_export_cached(
                            journal_index, dict(emitted_ref.payload), doi,
                            exports_dir=exports_dir)
                        emitted_primary = emitted_ref.candidate_id if valid else ""
                        emitted_failure = "" if valid else reason
                    if emitted_primary:
                        _commit_candidate(
                            journal, journal_index, runtime,
                            page_path, candidate_id_value=cid, worker_id=worker_id,
                            new_status="duplicate_observation",
                            updates={"terminal_reason": "duplicate_observation",
                                     "primary_candidate_id": emitted_primary},
                        )
                        report.duplicate_observation += 1
                        report.processed += 1
                        continue
                    if emitted_failure:
                        _defer_candidate(
                            journal, journal_index, runtime,
                            page_path, candidate_id_value=cid, worker_id=worker_id,
                            reason="doi_primary_validation_failed",
                            drain_generation=drain_generation,
                            updates={"last_error": emitted_failure},
                        )
                        deferred_candidate_ids.add(cid)
                        report.retryable_failures += 1
                        report.processed += 1
                        continue

                    if hide_existing and staging_context is not None:
                        existing = staging_context.transaction.classify_existing_doi(doi)
                        if existing.status == "duplicate":
                            _commit_candidate(
                                journal, journal_index, runtime,
                                page_path, candidate_id_value=cid, worker_id=worker_id,
                                new_status="existing_duplicate",
                                updates={"terminal_reason": "doi_duplicate",
                                         "duplicate_refs": [ref.paper_number for ref in existing.duplicate_refs]},
                            )
                            report.existing_duplicate += 1
                            report.processed += 1
                            continue
                        if existing.status in {"repair_required", "failed_retryable"}:
                            _defer_candidate(
                                journal, journal_index, runtime,
                                page_path, candidate_id_value=cid, worker_id=worker_id,
                                reason=existing.status, drain_generation=drain_generation,
                                updates={"last_error": existing.error.code if existing.error else existing.status},
                            )
                            report.retryable_failures += 1
                            report.processed += 1
                            continue

                    export = export_candidate_once(exports_dir, current)
                    _commit_candidate(
                        journal, journal_index, runtime,
                        page_path, candidate_id_value=cid, worker_id=worker_id,
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
        except ProviderRequestBudgetExhausted:
            raise
        except Timeout as exc:
            report.retryable_failures += 1
            report.errors.append(str(exc))
            try:
                _commit_candidate(
                    journal, journal_index, runtime,
                    page_path,
                    candidate_id_value=cid,
                    worker_id=worker_id,
                    new_status="failed_retryable",
                    updates={"last_error": str(exc)},
                )
            except Exception as commit_exc:
                report.errors.append(
                    f"failed_retryable_commit_failed:{type(commit_exc).__name__}:{commit_exc}"
                )
        except Exception as exc:
            report.retryable_failures += 1
            report.errors.append(str(exc))
            try:
                _commit_candidate(
                    journal, journal_index, runtime,
                    page_path,
                    candidate_id_value=cid,
                    worker_id=worker_id,
                    new_status="failed_retryable",
                    updates={"last_error": str(exc)},
                )
            except Exception as commit_exc:
                report.errors.append(
                    f"failed_retryable_commit_failed:{type(commit_exc).__name__}:{commit_exc}"
                )
    terminal = (report.staged + report.reused_existing + report.emitted + report.existing_duplicate
                + report.duplicate_observation + report.invalid + report.unresolved
                + report.terminal_failures)
    report.remaining = max(0, report.before - terminal)
    if runtime is not None:
        runtime.metrics.sync_journal(journal_index)
    return report


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
    hide_existing: bool = False,
    runtime: DiscoveryBatchRuntime | None = None,
    active_profile_hashes: Mapping[str, str] | None = None,
    gateway: MetadataStagingGateway | None = None,
) -> DrainReport:
    if runtime is None:
        if active_profile_hashes is None:
            raise ValueError(
                "drain_pending_candidates requires explicit active relevance profiles"
            )
        active_profiles = ActiveRelevanceProfiles.build(active_profile_hashes)
        try:
            runtime = DiscoveryBatchRuntime.create(
                journal=journal, paper_raw_dir=paper_raw_dir, papers_dir=papers_dir,
                ledger_path=ledger_path, needs_staging=bool(stage_to_paper_raw or hide_existing),
                active_relevance_profiles=active_profiles)
        except ProviderRequestBudgetExhausted:
            raise
        except Exception as exc:
            return _registry_configuration_failed_report(
                journal=journal,
                keyword_ids=keyword_ids,
                candidate_budget=candidate_budget,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
                active_profiles=active_profiles,
                exc=exc,
            )

    _ensure_title_resolution_service(runtime, doi_resolution_budget)
    journal_index = runtime.journal_index
    report = DrainReport(before=journal_index.pending_count(keyword_ids))
    if candidate_budget <= 0:
        report.remaining = report.before
        return report
    if not stage_to_paper_raw and candidate_budget > 16:
        remaining_budget = candidate_budget
        while remaining_budget > 0:
            current = drain_pending_candidates(
                journal=journal, keyword_ids=keyword_ids,
                candidate_budget=min(16, remaining_budget),
                stage_to_paper_raw=False, apply=apply,
                paper_raw_dir=paper_raw_dir, papers_dir=papers_dir,
                ledger_path=ledger_path, locks_dir=locks_dir,
                exports_dir=exports_dir, worker_id=worker_id,
                doi_resolution_budget=doi_resolution_budget,
                lease_seconds=lease_seconds, skip_duplicates=skip_duplicates,
                hide_existing=hide_existing, runtime=runtime,
                gateway=gateway,
            )
            for field_name in (
                "processed", "staged", "reused_existing", "emitted", "existing_duplicate",
                "duplicate_observation", "invalid", "unresolved",
                "retryable_failures", "terminal_failures", "planned",
            ):
                setattr(report, field_name,
                        getattr(report, field_name) + getattr(current, field_name))
            report.errors.extend(current.errors)
            report.remaining = current.remaining
            if current.processed <= 0:
                break
            remaining_budget -= current.processed
        return report

    title_service = runtime.title_resolution_service
    assert title_service is not None
    # Build the shared DOI duplicate index ONCE for this drain and thread it
    # through staging so the per-candidate hot path does O(1) lookups instead
    # of O(N) full-library rescans. The index is refreshed under the paper_raw
    # write lock inside the allocator before each authoritative check, so
    # concurrent writers cannot slip a duplicate DOI past it.
    staging_context = runtime.staging_context
    if gateway is None:
        gateway = MetadataStagingGateway(
            paper_raw_dir=paper_raw_dir,
            papers_dir=papers_dir,
            ledger_path=ledger_path,
        )
    if stage_to_paper_raw:
        return _drain_staging_candidate_batches(
            journal=journal,
            journal_index=journal_index,
            runtime=runtime,
            report=report,
            keyword_ids=keyword_ids,
            candidate_budget=candidate_budget,
            apply=apply,
            paper_raw_dir=paper_raw_dir,
            papers_dir=papers_dir,
            ledger_path=ledger_path,
            locks_dir=locks_dir,
            exports_dir=exports_dir,
            worker_id=worker_id,
            doi_resolution_budget=doi_resolution_budget,
            lease_seconds=lease_seconds,
            skip_duplicates=skip_duplicates,
            gateway=gateway,
        )
    return _drain_non_staging(
        journal=journal,
        journal_index=journal_index,
        runtime=runtime,
        report=report,
        title_service=title_service,
        staging_context=staging_context,
        keyword_ids=keyword_ids,
        candidate_budget=candidate_budget,
        locks_dir=locks_dir,
        exports_dir=exports_dir,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        hide_existing=hide_existing,
    )

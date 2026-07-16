"""Shared state owned by one discovery batch."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from src.discovery.page_journal import JournalDrainIndex, PageJournalStore
from src.discovery.staging_context import DiscoveryStagingContext
from src.discovery.staging_metrics import CollectingStagingMetricsObserver


@dataclass
class DiscoveryPipelineMetrics:
    staging: CollectingStagingMetricsObserver = field(
        default_factory=CollectingStagingMetricsObserver)
    journal_pages_read: int = 0
    journal_pages_written: int = 0
    journal_full_scans: int = 0
    journal_index_lookups: int = 0
    page_fsyncs: int = 0
    candidate_claims: int = 0
    candidate_lease_renewals: int = 0
    active_profile_mapping_builds: int = 0
    relevance_incremental_updates: int = 0
    relevance_binding_invariant_failures: int = 0

    def sync_journal(self, index: JournalDrainIndex) -> None:
        self.journal_pages_read = index.pages_read
        self.journal_full_scans = index.full_scans
        self.journal_index_lookups = index.lookups

    def to_dict(self) -> dict[str, object]:
        staging = self.staging
        return {
            "staging_context_builds": staging.staging_context_builds,
            "formal_publication_view_loads": staging.formal_publication_view_loads,
            "registry_full_builds": staging.full_registry_builds,
            "registry_incremental_refreshes": staging.incremental_registry_refreshes,
            "workspace_fingerprint_calls": staging.workspace_fingerprint_calls,
            "workspace_evidence_reads": staging.workspace_records_read,
            "repair_backlog_probes": staging.repair_backlog_probes,
            "journal_pages_read": self.journal_pages_read,
            "journal_pages_written": self.journal_pages_written,
            "journal_full_scans": self.journal_full_scans,
            "journal_index_lookups": self.journal_index_lookups,
            "page_fsyncs": self.page_fsyncs,
            "candidate_claims": self.candidate_claims,
            "candidate_lease_renewals": self.candidate_lease_renewals,
            "active_profile_mapping_builds": self.active_profile_mapping_builds,
            "relevance_incremental_updates": self.relevance_incremental_updates,
            "relevance_binding_invariant_failures": self.relevance_binding_invariant_failures,
            "write_lock_acquisitions": staging.write_lock_acquisitions,
            "write_lock_wait_ms": round(staging.write_lock_wait_ms, 3),
            "write_lock_hold_ms": round(staging.write_lock_hold_ms, 3),
            "ledger_loads": staging.ledger_loads,
            "ledger_saves": staging.ledger_saves,
            "matched_revalidations": staging.matched_revalidations,
            "batch_count": staging.batch_count,
            "batch_sizes": list(staging.batch_sizes),
        }


@dataclass
class RepairBacklog:
    numbers: set[str] = field(default_factory=set)
    probe_budget_per_batch: int = 20


@dataclass(frozen=True)
class ActiveRelevanceProfiles:
    by_keyword_id: Mapping[str, str]

    @classmethod
    def build(cls, values: Mapping[str, str]) -> "ActiveRelevanceProfiles":
        materialized = {str(key): str(value) for key, value in values.items()}
        if any(not key or not value for key, value in materialized.items()):
            raise ValueError("active relevance profile bindings must be non-blank")
        return cls(MappingProxyType(materialized))


@dataclass
class DiscoveryBatchRuntime:
    staging_context: DiscoveryStagingContext | None
    journal_index: JournalDrainIndex
    metrics: DiscoveryPipelineMetrics
    repair_backlog: RepairBacklog
    active_relevance_profiles: ActiveRelevanceProfiles

    @classmethod
    def create(cls, *, journal: PageJournalStore, paper_raw_dir: Path,
               papers_dir: Path, ledger_path: Path, needs_staging: bool,
               active_relevance_profiles: ActiveRelevanceProfiles,
               repair_probe_budget_per_batch: int = 20,
               persist_repair_cursor: bool = False) -> "DiscoveryBatchRuntime":
        metrics = DiscoveryPipelineMetrics()
        metrics.active_profile_mapping_builds = 1
        journal_index = JournalDrainIndex.build(
            journal,
            active_profile_hashes=active_relevance_profiles.by_keyword_id,
        )
        journal_index.assert_active_bindings(active_relevance_profiles.by_keyword_id)
        context = None
        if needs_staging:
            context = DiscoveryStagingContext.create_with_observer(
                paper_raw_dir=paper_raw_dir, papers_dir=papers_dir,
                ledger_path=ledger_path, prepare_allocation=True,
                observer=metrics.staging)
            context.transaction.probe_repair_backlog(
                tuple(context.registry.repair_backlog_numbers),
                budget=repair_probe_budget_per_batch,
                cursor_path=(Path(paper_raw_dir) / ".repair_probe_cursor.json")
                if persist_repair_cursor else None)
        backlog = RepairBacklog(
            set(context.registry.repair_backlog_numbers) if context else set(),
            repair_probe_budget_per_batch)
        metrics.sync_journal(journal_index)
        return cls(
            context, journal_index, metrics, backlog, active_relevance_profiles,
        )

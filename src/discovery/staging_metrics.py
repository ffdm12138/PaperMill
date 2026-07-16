"""Optional production instrumentation for discovery staging."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class StagingMetricsObserver(Protocol):
    def staging_context_build(self) -> None: ...
    def formal_publication_view_load(self) -> None: ...
    def registry_full_build(self) -> None: ...
    def registry_incremental_refresh(self) -> None: ...
    def registry_pre_refresh(self) -> None: ...
    def registry_post_refresh(self) -> None: ...
    def registry_direct_publish(self) -> None: ...
    def workspace_record_read(self, *, unsettled: bool) -> None: ...
    def workspace_fingerprint(self) -> None: ...
    def repair_backlog_probe(self) -> None: ...
    def matched_revalidation(self) -> None: ...
    def write_lock_acquired(self, *, wait_ms: float = 0.0) -> None: ...
    def write_lock_released(self, *, hold_ms: float = 0.0) -> None: ...
    def batch_staged(self, size: int) -> None: ...
    def ledger_load(self) -> None: ...
    def ledger_save(self) -> None: ...
    def number_allocated(self) -> None: ...
    def record_staged(self) -> None: ...


class NullStagingMetricsObserver:
    def staging_context_build(self) -> None: pass
    def formal_publication_view_load(self) -> None: pass
    def registry_full_build(self) -> None: pass
    def registry_incremental_refresh(self) -> None: pass
    def registry_pre_refresh(self) -> None: pass
    def registry_post_refresh(self) -> None: pass
    def registry_direct_publish(self) -> None: pass
    def workspace_record_read(self, *, unsettled: bool) -> None: pass
    def workspace_fingerprint(self) -> None: pass
    def repair_backlog_probe(self) -> None: pass
    def matched_revalidation(self) -> None: pass
    def write_lock_acquired(self, *, wait_ms: float = 0.0) -> None: pass
    def write_lock_released(self, *, hold_ms: float = 0.0) -> None: pass
    def batch_staged(self, size: int) -> None: pass
    def ledger_load(self) -> None: pass
    def ledger_save(self) -> None: pass
    def number_allocated(self) -> None: pass
    def record_staged(self) -> None: pass


@dataclass
class CollectingStagingMetricsObserver(NullStagingMetricsObserver):
    staging_context_builds: int = 0
    formal_publication_view_loads: int = 0
    full_registry_builds: int = 0
    incremental_registry_refreshes: int = 0
    registry_pre_refreshes: int = 0
    registry_post_refreshes: int = 0
    registry_direct_publishes: int = 0
    workspace_records_read: int = 0
    unsettled_records_read: int = 0
    workspace_fingerprint_calls: int = 0
    repair_backlog_probes: int = 0
    matched_revalidations: int = 0
    write_lock_acquisitions: int = 0
    write_lock_wait_ms: float = 0.0
    write_lock_hold_ms: float = 0.0
    batch_count: int = 0
    batch_sizes: list[int] = field(default_factory=list)
    ledger_loads: int = 0
    ledger_saves: int = 0
    paper_numbers_allocated: int = 0
    records_staged: int = 0

    def staging_context_build(self) -> None: self.staging_context_builds += 1
    def formal_publication_view_load(self) -> None: self.formal_publication_view_loads += 1

    def registry_full_build(self) -> None: self.full_registry_builds += 1
    def registry_incremental_refresh(self) -> None: self.incremental_registry_refreshes += 1
    def registry_pre_refresh(self) -> None: self.registry_pre_refreshes += 1
    def registry_post_refresh(self) -> None: self.registry_post_refreshes += 1
    def registry_direct_publish(self) -> None: self.registry_direct_publishes += 1
    def workspace_record_read(self, *, unsettled: bool) -> None:
        self.workspace_records_read += 1
        self.unsettled_records_read += int(unsettled)
    def workspace_fingerprint(self) -> None: self.workspace_fingerprint_calls += 1
    def repair_backlog_probe(self) -> None: self.repair_backlog_probes += 1
    def matched_revalidation(self) -> None: self.matched_revalidations += 1
    def write_lock_acquired(self, *, wait_ms: float = 0.0) -> None:
        self.write_lock_acquisitions += 1
        self.write_lock_wait_ms += wait_ms
    def write_lock_released(self, *, hold_ms: float = 0.0) -> None: self.write_lock_hold_ms += hold_ms
    def batch_staged(self, size: int) -> None:
        self.batch_count += 1
        self.batch_sizes.append(size)
    def ledger_load(self) -> None: self.ledger_loads += 1
    def ledger_save(self) -> None: self.ledger_saves += 1
    def number_allocated(self) -> None: self.paper_numbers_allocated += 1
    def record_staged(self) -> None: self.records_staged += 1

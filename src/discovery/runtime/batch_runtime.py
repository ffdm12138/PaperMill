"""Shared state owned by one discovery batch.

Each :class:`DiscoveryBatchRuntime` is a self-contained batch scope holding
telemetry, budgets, staging context, and journal index.  No two batches share
telemetry or budgets — the process-wide :class:`ProviderRuntime` supplies only
the HTTP transport, limiters, circuit breakers, and Retry-After gate, which are
safe to share across batches (and across purposes: discovery, title resolution,
metadata resolution).
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from src.discovery.runtime.budgets import (
    BatchDoiResolutionBudget,
    DualScopePageBudget,
    ProviderRequestBudget,
)
from src.discovery.contracts.enums import ShutdownReason
from src.discovery.stores.journal_drain_index import JournalDrainIndex
from src.discovery.stores.page_journal_store import PageJournalStoreV4
from src.discovery.providers.provider_client import (
    ProviderClient,
    ProviderRuntime,
)
from src.discovery.providers.provider_telemetry import ProviderTelemetry
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


class RuntimeState(str, Enum):
    """Unified lifecycle state for the batch runtime guard."""
    OPEN = "open"
    CANCELLING = "cancelling"
    CLOSED = "closed"


class RuntimeClosedError(RuntimeError):
    """Raised when a guarded component is used after runtime close."""


class RuntimeGuard:
    """Unified guard shared by all batch-scoped components.

    ProviderClient, ProviderTelemetry, budgets, TitleResolutionService,
    and CandidateDrainCoordinator all share the same guard instance.
    When the runtime closes, every guarded mutation fails closed.

    Thread-safe state machine: OPEN → CANCELLING → CLOSED.
    Close reason is set on the first legal shutdown and never overwritten
    by a lower-priority reason.
    """

    _REASON_PRIORITY = {
        ShutdownReason.REPAIR_REQUIRED: 0,
        ShutdownReason.INTERRUPTED: 1,
        ShutdownReason.FAILED: 2,
        ShutdownReason.COMPLETED: 3,
    }

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: RuntimeState = RuntimeState.OPEN
        self._close_reason: ShutdownReason | None = None

    @property
    def state(self) -> RuntimeState:
        with self._lock:
            return self._state

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._state == RuntimeState.CLOSED

    @property
    def close_reason(self) -> ShutdownReason | None:
        with self._lock:
            return self._close_reason

    def ensure_open(self) -> None:
        """Raise RuntimeClosedError if the guard is not OPEN."""
        with self._lock:
            if self._state != RuntimeState.OPEN:
                raise RuntimeClosedError(
                    f"runtime is {self._state.value}, close_reason={self._close_reason}"
                )

    def begin_cancellation(self, reason: ShutdownReason) -> None:
        """Transition from OPEN to CANCELLING (idempotent).

        Only the first legal shutdown reason is preserved.
        Lower-priority reasons never overwrite higher-priority ones.
        """
        with self._lock:
            if self._state != RuntimeState.OPEN:
                return
            self._state = RuntimeState.CANCELLING
            self._close_reason = reason

    def close(self, reason: ShutdownReason) -> None:
        """Transition to CLOSED (idempotent).

        Once CLOSED, the state never changes.  The close reason is set
        on the first legal shutdown and never downgraded by a
        lower-priority reason arriving later.
        """
        with self._lock:
            if self._state == RuntimeState.CLOSED:
                return
            # Preserve a higher-priority reason if one is already set
            current_prio = (
                self._REASON_PRIORITY.get(self._close_reason, 99)
                if self._close_reason is not None else 99
            )
            new_prio = self._REASON_PRIORITY.get(reason, 99)
            if new_prio < current_prio:
                self._close_reason = reason
            elif self._close_reason is None:
                self._close_reason = reason
            self._state = RuntimeState.CLOSED

    def snapshot(self) -> dict[str, object]:
        """Return an immutable snapshot of the guard state."""
        with self._lock:
            return {
                "state": self._state.value,
                "close_reason": self._close_reason.value if self._close_reason else None,
                "closed": self._state == RuntimeState.CLOSED,
            }


class RuntimeGuarded:
    """Lightweight protocol for components bound to a RuntimeGuard.

    Components that mutate state after runtime close must call
    ``_ensure_runtime_open()`` before every mutation.  Read-only
    operations (snapshot, totals, frozen views) are exempt.
    """

    _runtime_guard: RuntimeGuard | None = None

    def _ensure_runtime_open(self) -> None:
        if self._runtime_guard is not None:
            self._runtime_guard.ensure_open()

    def bind_guard(self, guard: RuntimeGuard) -> None:
        self._runtime_guard = guard


@dataclass
class DiscoveryBatchRuntime:
    """Batch-scoped runtime owning telemetry, budgets, and staging context.

    The process-wide ``ProviderRuntime`` supplies the shared infrastructure
    (transport, limiters, breakers, Retry-After gate).  This runtime creates
    ``ProviderClient`` instances that are bound to **this batch's** telemetry
    and request budget.

    Supports context-manager usage for automatic freeze and cancellation::

        with DiscoveryBatchRuntime.create(...) as runtime:
            ...
        # runtime is now frozen — no further mutations allowed
    """

    batch_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    staging_context: DiscoveryStagingContext | None = None
    journal_index: JournalDrainIndex = field(default_factory=lambda: JournalDrainIndex({}))
    metrics: DiscoveryPipelineMetrics = field(default_factory=DiscoveryPipelineMetrics)
    repair_backlog: RepairBacklog = field(default_factory=RepairBacklog)
    active_relevance_profiles: ActiveRelevanceProfiles = field(
        default_factory=lambda: ActiveRelevanceProfiles(MappingProxyType({})))
    title_resolution_service: Any | None = None

    # Batch-scoped telemetry and budgets (NOT shared across batches).
    telemetry: ProviderTelemetry = field(default_factory=ProviderTelemetry)
    request_budget: ProviderRequestBudget | None = None
    doi_resolution_budget: BatchDoiResolutionBudget | None = None
    page_budget: DualScopePageBudget = field(default_factory=DualScopePageBudget)

    # ── lifecycle ──────────────────────────────────────────────────────
    _guard: RuntimeGuard = field(default_factory=RuntimeGuard, repr=False)
    cancellation_token: threading.Event = field(
        default_factory=threading.Event, repr=False,
    )
    closed_event: threading.Event = field(
        default_factory=threading.Event, repr=False,
    )
    shutdown_reason: ShutdownReason | None = None
    deadline_monotonic: float | None = None
    _frozen: bool = field(default=False, repr=False)
    _frozen_telemetry: dict[str, int] | None = field(default=None, repr=False)
    _frozen_page_budget: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Bind the RuntimeGuard to all batch-owned components."""
        self.bind_owned_resources(
            telemetry=self.telemetry,
            request_budget=self.request_budget,
            doi_resolution_budget=self.doi_resolution_budget,
            page_budget=self.page_budget,
        )

    @property
    def guard(self) -> RuntimeGuard:
        """Shared guard for all batch-scoped components (v98)."""
        return self._guard

    # ── unified lifecycle API ─────────────────────────────────────────

    def cancel(self, reason: ShutdownReason) -> None:
        """Atomically begin cancellation: set token, begin guard cancel.

        This is the single entry point for all cancellation — no caller
        should call ``cancellation_token.set()`` or
        ``guard.begin_cancellation()`` directly.
        """
        self.cancellation_token.set()
        self._guard.begin_cancellation(reason)
        if self.shutdown_reason is None:
            self.shutdown_reason = reason

    def complete(self, reason: ShutdownReason) -> None:
        """Record clean completion reason (no cancellation)."""
        if self.shutdown_reason is None:
            self.shutdown_reason = reason

    def bind_owned_resources(
        self,
        telemetry: ProviderTelemetry | None = None,
        request_budget: ProviderRequestBudget | None = None,
        doi_resolution_budget: BatchDoiResolutionBudget | None = None,
        page_budget: DualScopePageBudget | None = None,
        title_resolution_service: Any | None = None,
        drain_coordinator: Any | None = None,
    ) -> None:
        """Bind shared guard and lifecycle to all batch-owned resources.

        Every component that must reject mutation after runtime close
        receives the guard via ``bind_guard()`` when available, or via
        direct ``_runtime_guard`` attribute assignment otherwise.
        """
        for component in (
            telemetry, request_budget, doi_resolution_budget,
            page_budget, title_resolution_service, drain_coordinator,
        ):
            if component is None:
                continue
            if hasattr(component, "bind_guard"):
                component.bind_guard(self._guard)
            elif hasattr(component, "_runtime_guard"):
                component._runtime_guard = self._guard

    @classmethod
    def create(cls, *, journal: PageJournalStoreV4, paper_raw_dir: Path,
               papers_dir: Path, ledger_path: Path, needs_staging: bool,
               active_relevance_profiles: ActiveRelevanceProfiles,
               repair_probe_budget_per_batch: int = 20,
               persist_repair_cursor: bool = False,
               title_resolution_service: Any | None = None,
               request_budget: ProviderRequestBudget | None = None,
               doi_resolution_budget: BatchDoiResolutionBudget | None = None,
               page_budget: DualScopePageBudget | None = None,
               ) -> "DiscoveryBatchRuntime":
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
            staging_context=context,
            journal_index=journal_index,
            metrics=metrics,
            repair_backlog=backlog,
            active_relevance_profiles=active_relevance_profiles,
            title_resolution_service=title_resolution_service,
            request_budget=request_budget,
            doi_resolution_budget=doi_resolution_budget,
            page_budget=page_budget or DualScopePageBudget(),
        )

    def provider_client(self, provider: str) -> ProviderClient:
        """Create a ProviderClient bound to this batch's telemetry and budget.

        Uses the process-wide ``ProviderRuntime.create_client()`` factory
        so shared infrastructure (transport, limiter, breaker, Retry-After
        gate) is wired without accessing private fields.

        The resulting client is bound to this batch's RuntimeGuard and
        will reject execute() after the runtime is closed.
        """
        if self._guard.closed:
            raise RuntimeClosedError(
                f"runtime is closed, close_reason={self._guard.close_reason}"
            )
        return ProviderRuntime.get().create_client(
            provider,
            telemetry=self.telemetry,
            request_budget=self.request_budget,
            runtime_guard=self._guard,
        )

    def snapshot_telemetry(self) -> dict[str, object]:
        """Return batch telemetry snapshot for the report."""
        return {
            **self.telemetry.totals(),
            "by_provider_purpose": self.telemetry.snapshot(),
        }

    # ── context manager ────────────────────────────────────────────────

    def __enter__(self) -> "DiscoveryBatchRuntime":
        return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc_val: BaseException | None,
                 exc_tb: object) -> bool:
        """Freeze the runtime on exit.  Never suppress exceptions.

        Shutdown order:
        1. Record exception-based reason (INTERRUPTED / FAILED / COMPLETED)
        2. Collect immutable telemetry + budget snapshots
        3. Close the guard (CLOSED state — all guarded mutations rejected)
        4. Set closed_event
        5. Never suppress exceptions
        """
        if exc_type is KeyboardInterrupt:
            self.cancel(ShutdownReason.INTERRUPTED)
        elif exc_type is not None:
            self.cancel(ShutdownReason.FAILED)
        else:
            self.complete(ShutdownReason.COMPLETED)
        # Collect immutable snapshots before closing
        self._frozen_telemetry = self.telemetry.snapshot()
        self._frozen_page_budget = self.page_budget.snapshot()
        self._guard.close(self.shutdown_reason or ShutdownReason.COMPLETED)
        self._frozen = True
        self.closed_event.set()
        return False  # never suppress exceptions

    @property
    def frozen(self) -> bool:
        return self._guard.closed or self._frozen

    def freeze(self) -> None:
        """Freeze the runtime immediately (idempotent)."""
        if self._guard.closed or self._frozen:
            return
        self.cancel(ShutdownReason.FAILED)
        self._frozen_telemetry = self.telemetry.snapshot()
        self._frozen_page_budget = self.page_budget.snapshot()
        self._guard.close(ShutdownReason.FAILED)
        self._frozen = True
        self.closed_event.set()

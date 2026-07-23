"""Discovery v4 strict enums.

All state values use Enum — free strings are never accepted in production.
"""
from __future__ import annotations

from enum import Enum


class QueryLanguage(str, Enum):
    """Canonical query language for discovery lanes.

    ``MIXED`` is a distinct identity — it is NEVER coerced to ``ZH``.
    A query whose language changes produces a new query_id.
    """
    ZH = "zh"
    EN = "en"
    MIXED = "mixed"


class JournalStateV4(str, Enum):
    """Lifecycle state of a single ProviderPageJournalV4."""
    FETCHED = "fetched"
    CURSOR_COMMITTED = "cursor_committed"
    DRAINING = "draining"
    DRAINED = "drained"
    FAILED = "failed"


class LaneExecutionState(str, Enum):
    """Outcome of one physical lane execution."""
    COMPLETED = "completed"
    RETRYABLE_FAILED = "retryable_failed"
    PERMANENT_FAILED = "permanent_failed"
    REPAIR_REQUIRED = "repair_required"
    INTERRUPTED = "interrupted"
    SKIPPED = "skipped"


class LaneStopReason(str, Enum):
    """Why a lane stopped executing."""
    EXHAUSTED = "exhausted"
    BUDGET_REACHED = "budget_reached"
    MAX_PAGES_REACHED = "max_pages_reached"
    MAX_REQUESTS_REACHED = "max_requests_reached"
    PROVIDER_ERROR = "provider_error"
    CANCELLED = "cancelled"
    REPAIR_REQUIRED = "repair_required"
    LOCAL_CONSISTENCY_ERROR = "local_consistency_error"


class DrainOutcome(str, Enum):
    """Outcome of a single CandidateDrain operation."""
    STAGED = "staged"
    EXISTING = "existing_duplicate"
    INVALID = "invalid_doi"
    UNRESOLVED = "unresolved"
    FAILED = "failed_retryable"
    SKIPPED = "skipped"


class ShutdownReason(str, Enum):
    """Unified shutdown reason for both batch runtime and drain coordinator.

    Runtime-level values (COMPLETED, INTERRUPTED, FAILED, REPAIR_REQUIRED)
    describe why the DiscoveryBatchRuntime stopped.  Drain-level values
    (DRAIN_COMPLETE, CANCELLED, WATCHDOG_TIMEOUT, FATAL_ERROR) describe
    why the CandidateDrainCoordinator shut down.
    """
    # Batch runtime reasons
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    REPAIR_REQUIRED = "repair_required"
    # Drain coordinator reasons
    DRAIN_COMPLETE = "drain_complete"
    CANCELLED = "cancelled"
    WATCHDOG_TIMEOUT = "watchdog_timeout"
    FATAL_ERROR = "fatal_error"


class CandidateOrigin(str, Enum):
    """Origin of a PendingCandidateV4."""
    PROVIDER_PAGE = "provider_page"
    LEGACY_CANDIDATE_SEED = "legacy_candidate_seed"
    MANUAL_IMPORT = "manual_import"


class CursorAdvanceDecision(str, Enum):
    """Typed decision after analyzing a provider page response."""
    ADVANCE = "advance"
    EXHAUSTED = "exhausted"
    RETRYABLE_STALL = "retryable_stall"
    REPAIR_REQUIRED = "repair_required"

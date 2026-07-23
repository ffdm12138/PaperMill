"""Canonical constants shared across discovery modules.

This module must have zero imports from ``src.discovery`` or ``src`` to
prevent circular dependencies.  It is the single source of truth for
``INITIAL_CURSOR`` and the exact ``BACKFILL_STATE_FIELDS`` set.
"""

from __future__ import annotations

# ── Cursor ────────────────────────────────────────────────────────────

INITIAL_CURSOR = "*"

# ── Exact backfill schema fields ──────────────────────────────────────

BACKFILL_STATE_FIELDS = frozenset({
    "generation",
    "request_signature",
    "cursor",
    "exhausted",
    "pages_succeeded",
    "pages_committed",
    "items_returned_total",
    "last_page_count",
    "last_committed_page_id",
    "cursor_conflicts",
    "last_success_at",
    "last_error",
    "consecutive_failures",
    "last_failure_at",
    "last_error_type",
    "next_retry_at",
    "terminal_failure",
    "terminal_failure_at",
    "generation_history",
})

# Optional extension fields introduced by the discovery execution contract
# (2026-07).  Validators accept these in addition to the canonical set;
# ``exhaustion_evidence`` becomes *mandatory* at the write site whenever
# ``exhausted=True`` is committed.  Existing notebooks without these keys
# remain valid (additive change); the migration tool backfills defaults.
BACKFILL_STATE_OPTIONAL_FIELDS = frozenset({
    "exhaustion_evidence",
    "repair_required",
    "repair_reason",
    "repair_flagged_at",
})

# Full accepted field set for a backfill state dict (canonical + optional).
BACKFILL_STATE_ACCEPTED_FIELDS = BACKFILL_STATE_FIELDS | BACKFILL_STATE_OPTIONAL_FIELDS

# ── Lane stop reasons ─────────────────────────────────────────────────
# Canonical stop reason vocabulary lives in src.discovery.execution.lane_models.StopReason.
# Report-facing derived sets live in src.discovery.report_builder.
# This module no longer duplicates either.

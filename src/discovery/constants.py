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

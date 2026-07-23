"""CursorAdvanceDecision — typed decision logic for provider page cursor advancement.

Replaces ad-hoc boolean checks in the lane executor with a single typed
decision point.  Every provider response passes through ``decide_cursor_advance()``
before any cursor mutation or durable write.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.discovery.contracts.enums import CursorAdvanceDecision


@dataclass(frozen=True)
class CursorAdvanceInput:
    """All facts needed to decide whether a cursor should advance.

    Constructed by the lane executor from the provider response and
    current lane state.  No loose dicts or implicit state.
    """
    provider: str  # "openalex" or "crossref"
    request_cursor: str
    next_cursor: str | None
    returned_count: int
    provider_exhausted: bool
    http_status: int
    is_retryable_error: bool = False
    is_permanent_error: bool = False
    same_cursor_returned: bool = False


def decide_cursor_advance(inp: CursorAdvanceInput) -> CursorAdvanceDecision:
    """Typed cursor advance decision — the ONLY place this logic lives.

    Rules (evaluated in order):

    1. **Permanent error** (4xx auth/forbidden, malformed request)
       → ``REPAIR_REQUIRED`` — lane cannot self-heal.

    2. **Retryable error** (5xx, timeout, 429 with retry-after)
       → ``RETRYABLE_STALL`` — back off and retry later.

    3. **Provider exhausted** with valid exhaustion evidence
       → ``EXHAUSTED`` — no more pages to fetch.

    4. **Same cursor returned** (Crossref ``items-per-page`` < 1 or
       pagination edge case) — → ``RETRYABLE_STALL``, NOT permanent.
       Crossref returning the same cursor does not mean the lane is
       permanently broken.

    5. **Valid next cursor, non-empty** (next_cursor != request_cursor,
       next_cursor is not None) → ``ADVANCE``.

    6. **Empty page, cursor unchanged** → ``RETRYABLE_STALL``
       (transient — may have items on retry).

    7. **Fallback** → ``REPAIR_REQUIRED`` — unrecognized state.

    Crossref same-cursor handling (rule 4) is EXPLICIT — it never
    defaults to permanent failure.
    """
    # Rule 1: permanent error
    if inp.is_permanent_error:
        return CursorAdvanceDecision.REPAIR_REQUIRED

    # Rule 2: retryable error
    if inp.is_retryable_error:
        return CursorAdvanceDecision.RETRYABLE_STALL

    # Rule 3: provider confirmed exhausted
    if inp.provider_exhausted:
        return CursorAdvanceDecision.EXHAUSTED

    # Rule 4: same cursor — Crossref edge case, NOT permanent
    if inp.same_cursor_returned or (
        inp.next_cursor is not None and inp.next_cursor == inp.request_cursor
    ):
        return CursorAdvanceDecision.RETRYABLE_STALL

    # Rule 5: valid advance
    if inp.next_cursor and inp.next_cursor != inp.request_cursor:
        return CursorAdvanceDecision.ADVANCE

    # Rule 6: empty page, no cursor change — retry
    if inp.returned_count == 0 and not inp.next_cursor:
        return CursorAdvanceDecision.RETRYABLE_STALL

    # Rule 7: fallback
    return CursorAdvanceDecision.REPAIR_REQUIRED

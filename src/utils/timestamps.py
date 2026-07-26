"""Canonical timestamp helpers.

Policy: every persisted timestamp is timezone-aware.

- ``now_iso()`` — local time with UTC offset, second precision. Used by the
  ingest/ledger/manifest family (human-facing workflow sidecars).
- ``utc_now_iso()`` — UTC with ``+00:00`` offset, microsecond precision.
  Used by the discovery journal/notebook family (machine ordering).
- ``utc_now_iso_z()`` — UTC with a ``Z`` suffix; catalog registry format.

Historically 15 private implementations coexisted and the ledger family
wrote NAIVE local timestamps while ``paper_number_state`` defaults wrote
aware ones into the same JSON records; ``now_iso()`` closes that divergence
in the aware direction (no production reader parses these fields — they are
provenance/display values).
"""
from __future__ import annotations

from datetime import datetime, timezone


def now_iso() -> str:
    """Local timezone-aware ISO-8601 timestamp, second precision."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def utc_now_iso() -> str:
    """UTC ISO-8601 timestamp with ``+00:00`` offset."""
    return datetime.now(timezone.utc).isoformat()


def utc_now_iso_z() -> str:
    """UTC ISO-8601 timestamp with a ``Z`` suffix."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

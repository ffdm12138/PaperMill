"""Structured lane-aware provider telemetry for DOI discovery.

Every HTTP request is tracked under a :class:`TelemetryScope` that carries
batch, lane, provider, and purpose identity.  Counters are accumulated per
scope and snapshotted into :class:`LaneCounters` by the lane executor.

This module replaces the flat ``provider.purpose.metric`` string-keyed
telemetry in ``provider_client.py``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class TelemetryScope:
    """Immutable identity for one telemetry counter group.

    Lane operations use ``lane_id`` (non-None). Non-lane operations
    (title resolution, metadata verification) use ``operation_id``
    (non-None).  Exactly one of ``lane_id`` / ``operation_id`` must be
    set.
    """

    batch_id: str
    provider: str
    purpose: str
    lane_id: str | None = None
    operation_id: str | None = None

    def __post_init__(self) -> None:
        if self.lane_id is not None and self.operation_id is not None:
            raise ValueError(
                "TelemetryScope: exactly one of lane_id / operation_id must be set"
            )
        if self.lane_id is None and self.operation_id is None:
            raise ValueError(
                "TelemetryScope: either lane_id or operation_id must be set"
            )

    def _flat_key(self) -> str:
        """Flat key for the frozen snapshot output shape."""
        return f"{self.provider}.{self.purpose}"


@dataclass
class TelemetryCounters:
    """Thread-safe counter group for one :class:`TelemetryScope`.

    Conservation invariant: ``attempted == retried + succeeded + failed``
    """

    attempted: int = 0
    retried: int = 0
    succeeded: int = 0
    failed: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "attempted": self.attempted,
            "retried": self.retried,
            "succeeded": self.succeeded,
            "failed": self.failed,
        }


@dataclass
class ProviderTelemetry:
    """Thread-safe lane-aware telemetry store.

    Replaces the flat ``provider.purpose.metric`` key scheme.  Every
    record call requires a :class:`TelemetryScope`.  The ``snapshot()``
    and ``totals()`` methods emit the frozen flat output shape that the
    report builder consumes.
    """

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _counters: dict[TelemetryScope, TelemetryCounters] = field(default_factory=dict)
    _runtime_guard: Any = field(default=None, repr=False)

    # ── recording (new API) ───────────────────────────────────────────

    def record_attempt(self, scope: TelemetryScope) -> None:
        if self._runtime_guard is not None:
            self._runtime_guard.ensure_open()
        with self._lock:
            c = self._counters.setdefault(scope, TelemetryCounters())
            c.attempted += 1

    def record_retry(self, scope: TelemetryScope) -> None:
        if self._runtime_guard is not None:
            self._runtime_guard.ensure_open()
        with self._lock:
            c = self._counters.setdefault(scope, TelemetryCounters())
            c.retried += 1

    def record_success(self, scope: TelemetryScope) -> None:
        if self._runtime_guard is not None:
            self._runtime_guard.ensure_open()
        with self._lock:
            c = self._counters.setdefault(scope, TelemetryCounters())
            c.succeeded += 1

    def record_failure(self, scope: TelemetryScope) -> None:
        if self._runtime_guard is not None:
            self._runtime_guard.ensure_open()
        with self._lock:
            c = self._counters.setdefault(scope, TelemetryCounters())
            c.failed += 1

    # ── snapshots ─────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, int]:
        """Flat snapshot keyed by ``"{provider}.{purpose}.{metric}"``.

        This flat shape is the frozen contract consumed by the report
        builder's ``check_aggregate_conservation()``.
        """
        with self._lock:
            result: dict[str, int] = {}
            for scope, counters in self._counters.items():
                base = scope._flat_key()
                for metric in ("attempted", "retried", "succeeded", "failed"):
                    key = f"{base}.{metric}"
                    result[key] = result.get(key, 0) + getattr(counters, metric)
            return result

    def totals(self) -> dict[str, int]:
        """Return rolled-up {attempted, retried, succeeded, failed} across all scopes."""
        with self._lock:
            totals = {"attempted": 0, "retried": 0, "succeeded": 0, "failed": 0}
            for counters in self._counters.values():
                for metric in totals:
                    totals[metric] += getattr(counters, metric)
            return totals

    def snapshot_lane(self, lane_id: str) -> dict[str, int]:
        """Return {attempted, retried, succeeded, failed} for one lane.

        Used by the lane executor to populate ``LaneCounters`` from real
        HTTP telemetry rather than manual increment.
        """
        with self._lock:
            result = {"attempted": 0, "retried": 0, "succeeded": 0, "failed": 0}
            for scope, counters in self._counters.items():
                if scope.lane_id == lane_id:
                    for metric in result:
                        result[metric] += getattr(counters, metric)
            return result

    def snapshot_by_lane(self) -> dict[str | None, dict[str, int]]:
        """Return per-lane-id counters plus None-keyed non-lane operations."""
        with self._lock:
            by_lane: dict[str | None, dict[str, int]] = {}
            for scope, counters in self._counters.items():
                lid = scope.lane_id
                entry = by_lane.setdefault(lid, {"attempted": 0, "retried": 0,
                                                  "succeeded": 0, "failed": 0})
                for metric in entry:
                    entry[metric] += getattr(counters, metric)
            return by_lane

    def operation_totals(self) -> dict[str, int]:
        """Return counters for non-lane operations (lane_id=None)."""
        return self.snapshot_lane(None)  # type: ignore[arg-type]

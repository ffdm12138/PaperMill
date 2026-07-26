"""Discovery v4 strict report contracts.

Canonical frozen models for the final output of a discovery batch.  These
contracts are used to validate that a report dict produced by the reporting
layer matches the v4 schema; the reporting builder may still use mutable
internal helpers during construction, but the final persisted dict must conform.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from src.discovery.contracts.enums import LaneExecutionState, LaneStopReason


REPORT_SCHEMA_VERSION_V4 = "4.0"


def _check_type(value: Any, expected: type, field_name: str) -> None:
    if type(value) is not expected:
        if expected is int and isinstance(value, bool):
            raise TypeError(f"{field_name} must be int, got bool ({value!r})")
        raise TypeError(
            f"{field_name} must be {expected.__name__}, "
            f"got {type(value).__name__} ({value!r})"
        )


@dataclass(frozen=True)
class LaneReportV4:
    """Per-mode aggregate for one keyword."""

    status: str = "skipped"
    pages_requested: int = 0
    pages_recovered: int = 0
    pages_persisted: int = 0
    pages_committed: int = 0
    journals_recovered: int = 0
    items_returned: int = 0
    provider_failures: int = 0
    states_exhausted: int = 0
    cursor_conflicts: int = 0
    stop_reason: str | None = None
    errors: list[str] = field(default_factory=list)
    physical_lane_ids: list[str] = field(default_factory=list)
    physical_lanes: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        _check_type(self.status, str, "status")
        _check_type(self.pages_requested, int, "pages_requested")
        _check_type(self.pages_recovered, int, "pages_recovered")
        _check_type(self.pages_persisted, int, "pages_persisted")
        _check_type(self.pages_committed, int, "pages_committed")
        _check_type(self.journals_recovered, int, "journals_recovered")
        _check_type(self.items_returned, int, "items_returned")
        _check_type(self.provider_failures, int, "provider_failures")
        _check_type(self.states_exhausted, int, "states_exhausted")
        _check_type(self.cursor_conflicts, int, "cursor_conflicts")
        if self.stop_reason is not None and not isinstance(self.stop_reason, str):
            raise TypeError("stop_reason must be str or None")
        if self.stop_reason is not None and self.stop_reason not in {
            reason.value for reason in LaneStopReason
        }:
            raise ValueError(f"invalid stop_reason: {self.stop_reason!r}")
        if self.status not in {state.value for state in LaneExecutionState} and self.status != "skipped":
            raise ValueError(f"invalid status: {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "pages_requested": self.pages_requested,
            "pages_recovered": self.pages_recovered,
            "pages_persisted": self.pages_persisted,
            "pages_committed": self.pages_committed,
            "journals_recovered": self.journals_recovered,
            "items_returned": self.items_returned,
            "provider_failures": self.provider_failures,
            "states_exhausted": self.states_exhausted,
            "cursor_conflicts": self.cursor_conflicts,
            "stop_reason": self.stop_reason,
            "errors": list(self.errors),
            "physical_lane_ids": list(self.physical_lane_ids),
            "physical_lanes": [dict(item) for item in self.physical_lanes],
        }

    @classmethod
    def from_dict_strict(cls, data: Mapping[str, Any]) -> "LaneReportV4":
        if not isinstance(data, dict):
            raise TypeError("LaneReportV4 must be dict")
        allowed = {
            "status", "pages_requested", "pages_recovered", "pages_persisted",
            "pages_committed", "journals_recovered", "items_returned",
            "provider_failures", "states_exhausted", "cursor_conflicts",
            "stop_reason", "errors", "physical_lane_ids", "physical_lanes",
        }
        extra = set(data) - allowed
        if extra:
            raise ValueError(f"LaneReportV4 unknown fields: {sorted(extra)}")
        missing = allowed - set(data)
        if missing:
            raise ValueError(f"LaneReportV4 missing fields: {sorted(missing)}")
        return cls(
            status=str(data["status"]),
            pages_requested=int(data["pages_requested"]),
            pages_recovered=int(data["pages_recovered"]),
            pages_persisted=int(data["pages_persisted"]),
            pages_committed=int(data["pages_committed"]),
            journals_recovered=int(data["journals_recovered"]),
            items_returned=int(data["items_returned"]),
            provider_failures=int(data["provider_failures"]),
            states_exhausted=int(data["states_exhausted"]),
            cursor_conflicts=int(data["cursor_conflicts"]),
            stop_reason=data.get("stop_reason"),
            errors=list(data["errors"]),
            physical_lane_ids=list(data["physical_lane_ids"]),
            physical_lanes=[dict(item) for item in data["physical_lanes"]],
        )


@dataclass(frozen=True)
class KeywordDiscoveryReportV4:
    """Per-keyword report section."""

    keyword_zh: str = ""
    keyword_id: str = ""
    status: str = ""
    mode: str = ""
    queries_total: int = 0
    queries_zh: int = 0
    queries_en: int = 0
    queries_executed: list[dict[str, str]] = field(default_factory=list)
    refresh: LaneReportV4 = field(default_factory=LaneReportV4)
    backfill: LaneReportV4 = field(default_factory=LaneReportV4)
    pending: dict[str, Any] = field(default_factory=dict)
    final_pending: dict[str, Any] = field(default_factory=dict)
    candidates: dict[str, int] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    backpressure: bool = False
    durable_progress: bool = False
    errors: list[str] = field(default_factory=list)
    physical_lanes: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        for name in ("keyword_zh", "keyword_id", "status", "mode"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} must be str")
        for name in ("queries_total", "queries_zh", "queries_en"):
            _check_type(getattr(self, name), int, name)
        _check_type(self.backpressure, bool, "backpressure")
        _check_type(self.durable_progress, bool, "durable_progress")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REPORT_SCHEMA_VERSION_V4,
            "keyword_zh": self.keyword_zh,
            "keyword_id": self.keyword_id,
            "status": self.status,
            "mode": self.mode,
            "queries_total": self.queries_total,
            "queries_zh": self.queries_zh,
            "queries_en": self.queries_en,
            "queries_executed": [dict(item) for item in self.queries_executed],
            "refresh": self.refresh.to_dict(),
            "backfill": self.backfill.to_dict(),
            "pending": dict(self.pending),
            "final_pending": dict(self.final_pending),
            "candidates": dict(self.candidates),
            "budget": dict(self.budget),
            "backpressure": self.backpressure,
            "durable_progress": self.durable_progress,
            "errors": list(self.errors),
            "physical_lanes": [dict(item) for item in self.physical_lanes],
        }

    @classmethod
    def from_dict_strict(cls, data: Mapping[str, Any]) -> "KeywordDiscoveryReportV4":
        if not isinstance(data, dict):
            raise TypeError("KeywordDiscoveryReportV4 must be dict")
        allowed = {
            "schema_version", "keyword_zh", "keyword_id", "status", "mode",
            "queries_total", "queries_zh", "queries_en", "queries_executed",
            "refresh", "backfill", "pending", "final_pending", "candidates",
            "budget", "backpressure", "durable_progress", "errors", "physical_lanes",
        }
        extra = set(data) - allowed
        if extra:
            raise ValueError(f"KeywordDiscoveryReportV4 unknown fields: {sorted(extra)}")
        missing = allowed - set(data)
        if missing:
            raise ValueError(f"KeywordDiscoveryReportV4 missing fields: {sorted(missing)}")
        if data.get("schema_version") != REPORT_SCHEMA_VERSION_V4:
            raise ValueError(f"schema_version must be {REPORT_SCHEMA_VERSION_V4!r}")
        return cls(
            keyword_zh=str(data["keyword_zh"]),
            keyword_id=str(data["keyword_id"]),
            status=str(data["status"]),
            mode=str(data["mode"]),
            queries_total=int(data["queries_total"]),
            queries_zh=int(data["queries_zh"]),
            queries_en=int(data["queries_en"]),
            queries_executed=[dict(item) for item in data["queries_executed"]],
            refresh=LaneReportV4.from_dict_strict(data["refresh"]),
            backfill=LaneReportV4.from_dict_strict(data["backfill"]),
            pending=dict(data["pending"]),
            final_pending=dict(data["final_pending"]),
            candidates=dict(data["candidates"]),
            budget=dict(data["budget"]),
            backpressure=bool(data["backpressure"]),
            durable_progress=bool(data["durable_progress"]),
            errors=list(data["errors"]),
            physical_lanes=[dict(item) for item in data["physical_lanes"]],
        )


@dataclass(frozen=True)
class BatchDiscoveryReportV4:
    """Top-level batch report."""

    status: str = ""
    keywords: tuple[KeywordDiscoveryReportV4, ...] = ()
    aggregate: dict[str, Any] = field(default_factory=dict)
    exit_code: int = 0
    pipeline_metrics: dict[str, Any] = field(default_factory=dict)
    physical_lanes: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.status, str):
            raise TypeError("status must be str")
        _check_type(self.exit_code, int, "exit_code")
        if not isinstance(self.keywords, tuple):
            raise TypeError("keywords must be tuple")
        for kw in self.keywords:
            if not isinstance(kw, KeywordDiscoveryReportV4):
                raise TypeError("keywords must be KeywordDiscoveryReportV4")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REPORT_SCHEMA_VERSION_V4,
            "status": self.status,
            "exit_code": self.exit_code,
            "keywords": [keyword.to_dict() for keyword in self.keywords],
            "aggregate": dict(self.aggregate),
            "pipeline_metrics": dict(self.pipeline_metrics),
            "physical_lanes": [dict(item) for item in self.physical_lanes],
        }

    @classmethod
    def from_dict_strict(cls, data: Mapping[str, Any]) -> "BatchDiscoveryReportV4":
        if not isinstance(data, dict):
            raise TypeError("BatchDiscoveryReportV4 must be dict")
        allowed = {
            "schema_version", "status", "exit_code", "keywords", "aggregate",
            "pipeline_metrics", "physical_lanes",
        }
        extra = set(data) - allowed
        if extra:
            raise ValueError(f"BatchDiscoveryReportV4 unknown fields: {sorted(extra)}")
        missing = allowed - set(data)
        if missing:
            raise ValueError(f"BatchDiscoveryReportV4 missing fields: {sorted(missing)}")
        if data.get("schema_version") != REPORT_SCHEMA_VERSION_V4:
            raise ValueError(f"schema_version must be {REPORT_SCHEMA_VERSION_V4!r}")
        return cls(
            status=str(data["status"]),
            exit_code=int(data["exit_code"]),
            keywords=tuple(
                KeywordDiscoveryReportV4.from_dict_strict(kw)
                for kw in data["keywords"]
            ),
            aggregate=dict(data["aggregate"]),
            pipeline_metrics=dict(data["pipeline_metrics"]),
            physical_lanes=[dict(item) for item in data["physical_lanes"]],
        )


__all__ = [
    "REPORT_SCHEMA_VERSION_V4",
    "LaneReportV4",
    "KeywordDiscoveryReportV4",
    "BatchDiscoveryReportV4",
]

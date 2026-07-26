"""Authoritative construction of discovery reports (schema 3.1).

Executors emit typed ``LaneOutcome`` values and drain services emit typed
``DrainReport`` values.  This module is the sole place allowed to turn those
facts into keyword and batch status, aggregates, and report-facing lane
summaries.  It intentionally contains no scheduling or provider I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from src.discovery.execution.lane_models import LaneCounters, LaneOutcome, LaneState, StopReason
from src.discovery.pending_queue import DrainOutcome, DrainReport


REPORT_SCHEMA_VERSION = "4.0"
STOP_REASONS: frozenset[str] = frozenset(reason.value for reason in StopReason)
CLEAN_STOP_REASONS: frozenset[str] = frozenset({
    StopReason.PROVIDER_EXHAUSTED.value,
    StopReason.REFRESH_WINDOW_COMPLETE.value,
    StopReason.LANE_PAGE_BUDGET_REACHED.value,
    StopReason.BATCH_PAGE_BUDGET_REACHED.value,
    StopReason.PROVIDER_REQUEST_BUDGET_REACHED.value,
    StopReason.SKIPPED_BY_MODE.value,
})


@dataclass
class LaneReport:
    """Per-mode aggregate, with every contributing physical lane retained."""

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


@dataclass
class KeywordDiscoveryReport:
    keyword_zh: str
    keyword_id: str
    status: str
    refresh: LaneReport
    backfill: LaneReport
    pending: DrainReport
    final_pending: DrainReport
    candidates: dict[str, int]
    budget: dict[str, Any]
    mode: str
    queries_total: int = 0
    queries_zh: int = 0
    queries_en: int = 0
    queries_executed: list[dict[str, str]] = field(default_factory=list)
    backpressure: bool = False
    durable_progress: bool = False
    errors: list[str] = field(default_factory=list)
    physical_lanes: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
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
            "pending": self.pending.to_dict(),
            "final_pending": self.final_pending.to_dict(),
            "candidates": dict(self.candidates),
            "budget": dict(self.budget),
            "backpressure": self.backpressure,
            "durable_progress": self.durable_progress,
            "errors": list(self.errors),
            "physical_lanes": [dict(item) for item in self.physical_lanes],
        }


@dataclass
class BatchDiscoveryReport:
    status: str
    keywords: list[KeywordDiscoveryReport]
    aggregate: dict[str, Any]
    exit_code: int
    pipeline_metrics: dict[str, object] = field(default_factory=dict)
    physical_lanes: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "status": self.status,
            "exit_code": self.exit_code,
            "keywords": [keyword.to_dict() for keyword in self.keywords],
            "aggregate": self.aggregate,
            "pipeline_metrics": dict(self.pipeline_metrics),
            "physical_lanes": [dict(item) for item in self.physical_lanes],
        }


@dataclass(frozen=True)
class KeywordReportInput:
    """Non-derived coordinator facts consumed exactly once by ReportBuilder."""

    keyword_zh: str
    keyword_id: str
    mode: str
    queries: tuple[Mapping[str, str], ...] = ()
    pending_reports: tuple[DrainReport, ...] = ()
    final_pending_reports: tuple[DrainReport, ...] = ()
    backpressure: bool = False
    initial_backpressure: bool = False
    dynamic_backpressure: bool = False
    durable_progress: bool = False  # v99: from pages_durable > 0, cursor_committed > 0, or processed > 0
    errors: tuple[str, ...] = ()
    terminal_status: str | None = None


_STATUS_PRIORITY: Mapping[LaneState, int] = {
    LaneState.REPAIR_REQUIRED: 0,
    LaneState.PERMANENT_FAILED: 1,
    LaneState.RETRYABLE_FAILED: 2,
    LaneState.INTERRUPTED: 2,
    LaneState.BUDGET_STOPPED: 3,
    LaneState.EXHAUSTED: 4,
    LaneState.COMPLETED: 5,
    LaneState.SKIPPED: 6,
}


def _blank_lane(*, status: str = "skipped", stop_reason: str | None = None,
                errors: Iterable[str] = ()) -> LaneReport:
    return LaneReport(status=status, stop_reason=stop_reason, errors=list(errors))


def _counters_dict(counters: LaneCounters) -> dict[str, int]:
    return {
        "logical_pages_attempted": counters.logical_pages_attempted,
        "pages_fetched": counters.pages_fetched,
        "pages_recovered": counters.pages_recovered,
        "pages_durable": counters.pages_durable,
        "pages_cursor_committed": counters.pages_cursor_committed,
        "candidates_observed": counters.candidates_observed,
        "candidates_processed": counters.candidates_processed,
        "provider_requests_attempted": counters.provider_requests_attempted,
        "provider_requests_retried": counters.provider_requests_retried,
        "provider_requests_succeeded": counters.provider_requests_succeeded,
        "provider_requests_failed": counters.provider_requests_failed,
        "items_returned": counters.items_returned,
        "local_retryable_failures": counters.local_retryable_failures,
        "local_consistency_failures": counters.local_consistency_failures,
        "cursor_conflicts": counters.cursor_conflicts,
    }


def physical_lane_detail(outcome: LaneOutcome) -> dict[str, Any]:
    return {
        **outcome.key.to_dict(),
        "lane_id": outcome.key.stable_id(),
        "state": outcome.state.value,
        "stop_reason": outcome.stop_reason.value,
        "counters": _counters_dict(outcome.counters),
        "errors": [error.message for error in outcome.errors],
        "has_exhaustion_evidence": outcome.exhaustion_evidence is not None,
    }


def _mode_lane_report(outcomes: Iterable[LaneOutcome]) -> LaneReport:
    materialized = list(outcomes)
    if not materialized:
        return _blank_lane()
    report = _blank_lane(status=LaneState.SKIPPED.value)
    worst_failure: LaneOutcome | None = None
    for outcome in materialized:
        counters = outcome.counters
        report.pages_requested += counters.logical_pages_attempted
        report.pages_recovered += counters.pages_recovered
        report.pages_persisted += counters.pages_durable
        report.pages_committed += counters.pages_cursor_committed
        report.journals_recovered += counters.pages_recovered
        report.items_returned += counters.items_returned
        report.provider_failures += counters.provider_requests_failed
        report.states_exhausted += int(outcome.state == LaneState.EXHAUSTED)
        report.cursor_conflicts += counters.cursor_conflicts
        report.errors.extend(error.message for error in outcome.errors)
        report.physical_lane_ids.append(outcome.key.stable_id())
        report.physical_lanes.append(physical_lane_detail(outcome))
        if outcome.state in {
            LaneState.REPAIR_REQUIRED,
            LaneState.PERMANENT_FAILED,
            LaneState.RETRYABLE_FAILED,
            LaneState.INTERRUPTED,
        } and (
            worst_failure is None
            or _STATUS_PRIORITY[outcome.state] < _STATUS_PRIORITY[worst_failure.state]
        ):
            worst_failure = outcome
    if worst_failure is not None:
        report.status = worst_failure.state.value
        report.stop_reason = worst_failure.stop_reason.value
        return report

    states = [outcome.state for outcome in materialized]
    if all(state == LaneState.EXHAUSTED for state in states):
        representative = next(outcome for outcome in materialized if outcome.state == LaneState.EXHAUSTED)
    elif any(state == LaneState.BUDGET_STOPPED for state in states):
        representative = next(outcome for outcome in materialized if outcome.state == LaneState.BUDGET_STOPPED)
    elif any(state == LaneState.COMPLETED for state in states):
        representative = next(outcome for outcome in materialized if outcome.state == LaneState.COMPLETED)
    else:
        representative = materialized[0]
    report.status = representative.state.value
    report.stop_reason = representative.stop_reason.value
    return report


def _drain_has_failure(report: DrainReport) -> bool:
    """Check if a drain report indicates any kind of non-clean outcome.

    v98: explicitly covers all typed outcomes. REPAIR_REQUIRED and
    INTERRUPTED always indicate failure, even without populated errors.
    """
    if report.outcome == DrainOutcome.BUDGET_STOPPED:
        return False
    if report.outcome in {DrainOutcome.RETRYABLE_FAILED, DrainOutcome.PERMANENT_FAILED,
                           DrainOutcome.REPAIR_REQUIRED, DrainOutcome.INTERRUPTED}:
        return True
    return bool(report.errors or report.retryable_failures or report.terminal_failures)


def _keyword_status(
    *,
    outcomes: list[LaneOutcome],
    pending: DrainReport,
    final_pending: DrainReport,
    terminal_status: str | None,
) -> str:
    if terminal_status is not None:
        return terminal_status
    states = [outcome.state for outcome in outcomes]
    if any(state == LaneState.REPAIR_REQUIRED for state in states):
        return "repair_required"
    if any(state == LaneState.INTERRUPTED for state in states):
        return "interrupted"
    failures = any(state in {
        LaneState.RETRYABLE_FAILED,
        LaneState.PERMANENT_FAILED,
    } for state in states)
    drain_failure = _drain_has_failure(pending) or _drain_has_failure(final_pending)
    progress = any(outcome.durable_progress for outcome in outcomes) or bool(
        pending.processed or final_pending.processed
    )
    if failures or drain_failure:
        return "partial_success" if progress else "failed"
    return "success"


def _budget_dict(snapshot: Any) -> dict[str, Any]:
    return {
        "page_limit": getattr(snapshot, "total_limit", None),
        "pages_used": int(getattr(snapshot, "total_used", 0)),
        "page_budget_exhausted": bool(getattr(snapshot, "total_exhausted", False)),
        "per_lane_limit": getattr(snapshot, "per_lane_limit", None),
        "lane_used": dict(getattr(snapshot, "lane_used", {})),
    }


def _candidate_counts(*reports: DrainReport) -> dict[str, int]:
    return {
        "staged": sum(report.staged for report in reports),
        "reused_existing": sum(report.reused_existing for report in reports),
        "emitted": sum(report.emitted for report in reports),
        "existing_duplicates": sum(report.existing_duplicate for report in reports),
        "duplicate_observations": sum(report.duplicate_observation for report in reports),
        "invalid": sum(report.invalid for report in reports),
        "unresolved": sum(report.unresolved for report in reports),
        "retryable_failures": sum(report.retryable_failures for report in reports),
    }


def _merge_drain_reports(reports: Iterable[DrainReport]) -> DrainReport:
    """Conserve every drain counter without coordinator-side aggregation.

    v98: drain outcome priority is REPAIR_REQUIRED > INTERRUPTED >
    RETRYABLE_FAILED/PERMANENT_FAILED > BUDGET_STOPPED > COMPLETED.
    Lower-priority outcomes must never overwrite higher-priority ones.
    """
    _DRAIN_PRIORITY = {
        DrainOutcome.REPAIR_REQUIRED: 0,
        DrainOutcome.INTERRUPTED: 1,
        DrainOutcome.RETRYABLE_FAILED: 2,
        DrainOutcome.PERMANENT_FAILED: 2,
        DrainOutcome.BUDGET_STOPPED: 3,
        DrainOutcome.COMPLETED: 4,
    }
    merged = DrainReport()
    for report in reports:
        for field_name in (
            "processed", "staged", "reused_existing", "emitted",
            "existing_duplicate", "duplicate_observation", "invalid",
            "unresolved", "retryable_failures", "terminal_failures", "planned",
        ):
            setattr(merged, field_name, getattr(merged, field_name) + getattr(report, field_name))
        merged.before = max(merged.before, report.before)
        merged.remaining = report.remaining
        merged.backpressure = merged.backpressure or report.backpressure
        merged.errors.extend(report.errors)
        current_prio = _DRAIN_PRIORITY.get(merged.outcome, 99)
        report_prio = _DRAIN_PRIORITY.get(report.outcome, 99)
        if report_prio < current_prio:
            merged.outcome = report.outcome
            merged.stop_reason = report.stop_reason
    return merged


def _aggregate(reports: list[KeywordDiscoveryReport], *, budget_snapshot: Any,
               telemetry_snapshot: Mapping[str, object]) -> dict[str, Any]:
    aggregate: dict[str, Any] = {
        "keywords": {
            "total": len(reports), "success": 0, "partial_success": 0,
            "failed": 0, "skipped": 0, "repair_required": 0, "exhausted": 0,
        },
        "refresh": {
            "pages_requested": 0, "pages_recovered": 0, "pages_persisted": 0,
            "items_returned": 0, "provider_failures": 0,
        },
        "backfill": {
            "pages_requested": 0, "pages_recovered": 0, "pages_persisted": 0,
            "pages_committed": 0, "journals_recovered": 0,
            "states_exhausted": 0, "provider_failures": 0,
        },
        "pending": {
            "processed": 0,
            "remaining": 0,
            "backpressure": 0,
            "budget_stopped": 0,
        },
        "candidates": {
            "staged": 0, "emitted": 0, "existing_duplicates": 0,
            "duplicate_observations": 0, "invalid": 0, "unresolved": 0,
            "retryable_failures": 0,
        },
        "budget": _budget_dict(budget_snapshot),
        "provider_requests": dict(telemetry_snapshot),
    }
    for report in reports:
        aggregate["keywords"][report.status] = (
            aggregate["keywords"].get(report.status, 0) + 1
        )
        for source, target in ((report.refresh, aggregate["refresh"]),
                               (report.backfill, aggregate["backfill"])):
            for field_name in target:
                if hasattr(source, field_name):
                    target[field_name] += int(getattr(source, field_name))
        for drain in (report.pending, report.final_pending):
            aggregate["pending"]["processed"] += drain.processed
            aggregate["pending"]["budget_stopped"] += int(
                drain.outcome == DrainOutcome.BUDGET_STOPPED
            )
            aggregate["candidates"]["staged"] += drain.staged
            aggregate["candidates"]["emitted"] += drain.emitted
            aggregate["candidates"]["existing_duplicates"] += drain.existing_duplicate
            aggregate["candidates"]["duplicate_observations"] += drain.duplicate_observation
            aggregate["candidates"]["invalid"] += drain.invalid
            aggregate["candidates"]["unresolved"] += drain.unresolved
            aggregate["candidates"]["retryable_failures"] += drain.retryable_failures
        aggregate["pending"]["remaining"] += report.final_pending.remaining
        aggregate["pending"]["backpressure"] += int(report.backpressure)
    return aggregate


def _batch_status(keyword_reports: Iterable[KeywordDiscoveryReport]) -> str:
    """Derive batch status from explicit keyword facts (v99).

    Priority order:
      repair_required  – any keyword has local consistency corruption
      interrupted      – any execution path received user interrupt
      partial_success  – durable progress exists AND any failure present
      failed           – no durable progress AND failures exist
      success          – all clean
    """
    has_repair = False
    has_interrupt = False
    has_failure = False
    has_durable_progress = False
    for report in keyword_reports:
        status = report.status
        if status == "repair_required":
            has_repair = True
        if status == "interrupted":
            has_interrupt = True
        if status in {"failed", "partial_success", "retryable_failed",
                       "permanent_failed", "repair_required"}:
            has_failure = True
        # Durable progress from physical facts, not status strings (Phase 12)
        if (report.refresh.pages_committed > 0
            or report.refresh.pages_persisted > 0
            or report.backfill.pages_committed > 0
            or report.backfill.pages_persisted > 0
            or report.candidates.get("staged", 0) > 0
            or report.candidates.get("emitted", 0) > 0):
            has_durable_progress = True

    if has_repair:
        return "repair_required"
    if has_interrupt:
        return "interrupted"
    if has_durable_progress and has_failure:
        return "partial_success"
    if has_failure:
        return "failed"
    return "success"


def exit_code_for_batch_status(status: str) -> int:
    """Return the process exit code for a batch status string."""
    if status == "interrupted":
        return 130
    if status == "success":
        return 0
    if status == "partial_success":
        return 2
    return 1  # failed, repair_required, or unknown


class ReportBuilder:
    """Build the entire schema-3.1 batch report in one call."""

    def build(
        self,
        *,
        keyword_inputs: Iterable[KeywordReportInput],
        lane_outcomes: Iterable[LaneOutcome],
        page_budget_snapshot: Any,
        telemetry_snapshot: Mapping[str, object],
        pipeline_metrics: Mapping[str, object],
        planned_lane_ids: Iterable[str] | None = None,
    ) -> BatchDiscoveryReport:
        outcomes = list(lane_outcomes)
        grouped: dict[str, list[LaneOutcome]] = {}
        for outcome in outcomes:
            grouped.setdefault(outcome.key.keyword_id, []).append(outcome)

        reports: list[KeywordDiscoveryReport] = []
        for item in keyword_inputs:
            pending = _merge_drain_reports(item.pending_reports)
            final_pending = _merge_drain_reports(item.final_pending_reports)
            keyword_outcomes = grouped.pop(item.keyword_id, [])
            refresh_outcomes = [
                outcome for outcome in keyword_outcomes if outcome.key.mode == "refresh"
            ]
            backfill_outcomes = [
                outcome for outcome in keyword_outcomes if outcome.key.mode == "backfill"
            ]
            refresh = _mode_lane_report(refresh_outcomes)
            backfill = _mode_lane_report(backfill_outcomes)
            # Only blank lanes that have NO physical outcomes.  Lanes that
            # actually executed and produced counters must be preserved even
            # when the keyword is under backpressure.
            if item.backpressure and not refresh_outcomes:
                refresh = _blank_lane(
                    stop_reason=StopReason.CANDIDATE_BACKPRESSURE.value,
                )
            if item.backpressure and not backfill_outcomes:
                backfill = _blank_lane(
                    stop_reason=StopReason.CANDIDATE_BACKPRESSURE.value,
                )
            if not item.backpressure and item.mode == "refresh":
                backfill = _blank_lane(stop_reason=StopReason.SKIPPED_BY_MODE.value)
            elif not item.backpressure and item.mode == "backfill":
                refresh = _blank_lane(stop_reason=StopReason.SKIPPED_BY_MODE.value)
            if item.terminal_status is not None:
                refresh = _blank_lane(status=item.terminal_status, errors=item.errors)
                backfill = _blank_lane(status=item.terminal_status, errors=item.errors)

            all_details = refresh.physical_lanes + backfill.physical_lanes
            status = _keyword_status(
                outcomes=keyword_outcomes,
                pending=pending,
                final_pending=final_pending,
                terminal_status=item.terminal_status,
            )
            errors = list(item.errors)
            errors.extend(refresh.errors)
            errors.extend(backfill.errors)
            errors.extend(pending.errors)
            errors.extend(final_pending.errors)
            query_list = [dict(query) for query in item.queries]
            reports.append(KeywordDiscoveryReport(
                keyword_zh=item.keyword_zh,
                keyword_id=item.keyword_id,
                status=status,
                refresh=refresh,
                backfill=backfill,
                pending=pending,
                final_pending=final_pending,
                candidates=_candidate_counts(pending, final_pending),
                budget=_budget_dict(page_budget_snapshot),
                mode=item.mode,
                queries_total=len(query_list),
                queries_zh=sum(query.get("query_language") == "zh" for query in query_list),
                queries_en=sum(query.get("query_language") == "en" for query in query_list),
                queries_executed=query_list,
                backpressure=item.backpressure,
                errors=errors,
                physical_lanes=all_details,
            ))

        if grouped:
            unknown = ", ".join(sorted(grouped))
            raise ValueError(f"lane outcomes reference unknown keyword ids: {unknown}")

        details = [detail for report in reports for detail in report.physical_lanes]

        # ── planned-lane completeness ──────────────────────────────────
        if planned_lane_ids is not None:
            planned_list = list(planned_lane_ids)
            actual_list = [str(detail.get("lane_id") or "") for detail in details]

            # Detect duplicates in planned list
            if len(planned_list) != len(set(planned_list)):
                duplicates = [lid for lid in planned_list if planned_list.count(lid) > 1]
                raise ValueError(
                    f"duplicate planned lane IDs: {sorted(set(duplicates))}"
                )

            # Detect duplicates in actual output
            if len(actual_list) != len(set(actual_list)):
                duplicates = [lid for lid in actual_list if actual_list.count(lid) > 1]
                raise ValueError(
                    f"duplicate physical lane reports: {sorted(set(duplicates))}"
                )

            # Verify exact set equality
            planned_set = set(planned_list)
            actual_set = set(actual_list)
            missing = planned_set - actual_set
            extra = actual_set - planned_set
            if missing or extra:
                raise ValueError(
                    f"planned lane mismatch: missing={sorted(missing)}, "
                    f"extra={sorted(extra)}"
                )

            # Cardinality check (guards against [A,A,B] vs [A,B] set-equal escape)
            if len(planned_list) != len(actual_list):
                raise ValueError(
                    f"lane cardinality mismatch: planned={len(planned_list)}, "
                    f"actual={len(actual_list)}"
                )
            if len(planned_list) != len(outcomes):
                raise ValueError(
                    f"outcome count mismatch: planned={len(planned_list)}, "
                    f"outcomes={len(outcomes)}"
                )

        status = _batch_status(reports)
        aggregate = _aggregate(
            reports,
            budget_snapshot=page_budget_snapshot,
            telemetry_snapshot=telemetry_snapshot,
        )
        conservation_errors = check_aggregate_conservation(
            aggregate,
            details,
            reports=reports,
        )
        if conservation_errors:
            raise ValueError("report aggregate conservation failed: " + "; ".join(conservation_errors))
        return BatchDiscoveryReport(
            status=status,
            keywords=reports,
            aggregate=aggregate,
            exit_code=exit_code_for_batch_status(status),
            pipeline_metrics=dict(pipeline_metrics),
            physical_lanes=details,
        )


def build_batch_report(**kwargs: Any) -> BatchDiscoveryReport:
    """Convenience public entry point used by the coordinator."""
    return ReportBuilder().build(**kwargs)


def check_aggregate_conservation(
    aggregate: Mapping[str, Any],
    lanes: Iterable[Mapping[str, Any]],
    *,
    reports: Iterable[KeywordDiscoveryReport] | None = None,
) -> list[str]:
    """Check lane, drain, keyword, and telemetry aggregate conservation.

    Physical lane details are the source for lane aggregates.  When complete
    keyword reports are supplied (the production builder path), drain outcomes
    and keyword statuses are checked too.  The optional argument preserves the
    small public test helper while making the official report build fail closed
    on any dropped or double-counted typed outcome.
    """
    details = list(lanes)
    violations: list[str] = []
    mappings = {
        "refresh": {
            "pages_requested": "logical_pages_attempted",
            "pages_recovered": "pages_recovered",
            "pages_persisted": "pages_durable",
            "items_returned": "items_returned",
            "provider_failures": "provider_requests_failed",
        },
        "backfill": {
            "pages_requested": "logical_pages_attempted",
            "pages_recovered": "pages_recovered",
            "pages_persisted": "pages_durable",
            "pages_committed": "pages_cursor_committed",
            "journals_recovered": "pages_recovered",
            "provider_failures": "provider_requests_failed",
        },
    }
    for mode, fields in mappings.items():
        section = aggregate.get(mode)
        if not isinstance(section, Mapping):
            violations.append(f"aggregate.{mode} is missing")
            continue
        scoped = [detail for detail in details if detail.get("mode") == mode]
        for aggregate_key, counter_key in fields.items():
            actual = section.get(aggregate_key)
            expected = sum(
                int((detail.get("counters") or {}).get(counter_key, 0) or 0)
                for detail in scoped
            )
            if actual is None or int(actual) != expected:
                violations.append(
                    f"aggregate.{mode}.{aggregate_key}={actual} "
                    f"!= sum({mode} lanes.{counter_key})={expected}"
                )
        if mode == "backfill":
            actual_exhausted = section.get("states_exhausted")
            expected_exhausted = sum(
                detail.get("state") == LaneState.EXHAUSTED.value for detail in scoped
            )
            if actual_exhausted is None or int(actual_exhausted) != expected_exhausted:
                violations.append(
                    f"aggregate.backfill.states_exhausted={actual_exhausted} "
                    f"!= exhausted physical lanes={expected_exhausted}"
                )

    lane_ids = [str(detail.get("lane_id") or "") for detail in details]
    if any(not lane_id for lane_id in lane_ids):
        violations.append("physical lanes contain a blank lane_id")
    if len(set(lane_ids)) != len(lane_ids):
        violations.append("physical lanes contain duplicate lane_id values")

    telemetry = aggregate.get("provider_requests")
    if not isinstance(telemetry, Mapping):
        violations.append("aggregate.provider_requests is missing")
    else:
        by_purpose = telemetry.get("by_provider_purpose")
        if not isinstance(by_purpose, Mapping):
            violations.append("aggregate.provider_requests.by_provider_purpose is missing")
        else:
            for metric in ("attempted", "retried", "succeeded", "failed"):
                expected = sum(
                    int(value or 0)
                    for key, value in by_purpose.items()
                    if str(key).endswith(f".{metric}")
                )
                actual = telemetry.get(metric)
                if actual is None or int(actual) != expected:
                    violations.append(
                        f"aggregate.provider_requests.{metric}={actual} "
                        f"!= telemetry purpose total={expected}"
                    )

    if reports is None:
        return violations

    report_list = list(reports)
    keyword_section = aggregate.get("keywords")
    if not isinstance(keyword_section, Mapping):
        violations.append("aggregate.keywords is missing")
    else:
        expected_statuses: dict[str, int] = {}
        for report in report_list:
            expected_statuses[report.status] = expected_statuses.get(report.status, 0) + 1
        if int(keyword_section.get("total", -1)) != len(report_list):
            violations.append(
                f"aggregate.keywords.total={keyword_section.get('total')} "
                f"!= report count={len(report_list)}"
            )
        for status, expected in expected_statuses.items():
            actual = keyword_section.get(status)
            if actual is None or int(actual) != expected:
                violations.append(
                    f"aggregate.keywords.{status}={actual} != report total={expected}"
                )

    drains = [
        drain
        for report in report_list
        for drain in (report.pending, report.final_pending)
    ]
    pending = aggregate.get("pending")
    if not isinstance(pending, Mapping):
        violations.append("aggregate.pending is missing")
    else:
        expected_pending = {
            "processed": sum(drain.processed for drain in drains),
            "remaining": sum(report.final_pending.remaining for report in report_list),
            "backpressure": sum(int(report.backpressure) for report in report_list),
            "budget_stopped": sum(
                int(drain.outcome == DrainOutcome.BUDGET_STOPPED) for drain in drains
            ),
        }
        for field_name, expected in expected_pending.items():
            actual = pending.get(field_name)
            if actual is None or int(actual) != expected:
                violations.append(
                    f"aggregate.pending.{field_name}={actual} != drain total={expected}"
                )

    candidates = aggregate.get("candidates")
    if not isinstance(candidates, Mapping):
        violations.append("aggregate.candidates is missing")
    else:
        candidate_fields = {
            "staged": "staged",
            "emitted": "emitted",
            "existing_duplicates": "existing_duplicate",
            "duplicate_observations": "duplicate_observation",
            "invalid": "invalid",
            "unresolved": "unresolved",
            "retryable_failures": "retryable_failures",
        }
        for aggregate_field, drain_field in candidate_fields.items():
            expected = sum(int(getattr(drain, drain_field)) for drain in drains)
            actual = candidates.get(aggregate_field)
            if actual is None or int(actual) != expected:
                violations.append(
                    f"aggregate.candidates.{aggregate_field}={actual} "
                    f"!= drain total={expected}"
                )
    return violations

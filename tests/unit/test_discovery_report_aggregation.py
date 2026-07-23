from __future__ import annotations

import copy

import pytest

from src.discovery.runtime.budgets import DualScopePageBudget
from src.discovery.execution.lane_models import (
    DiscoveryLaneKey,
    LaneCounters,
    LaneExecutionSpec,
    LaneOutcome,
    LaneState,
    RequestSignature,
    StopReason,
)
from src.discovery.pending_queue import DrainReport
from src.discovery.reporting.report_builder import (
    KeywordReportInput,
    ReportBuilder,
    check_aggregate_conservation,
)


pytestmark = pytest.mark.unit


def _refresh_outcome() -> LaneOutcome:
    signature = RequestSignature.create(
        sort="", filters={"provider": "openalex", "mode": "refresh"}, page_size=50,
    )
    key = DiscoveryLaneKey(
        keyword_id="kid",
        query_id="qid",
        provider="openalex",
        mode="refresh",
        generation=1,
        request_signature=signature.hash,
    )
    # Keep the spec construction in the test to assert that the signature is
    # complete rather than hash-only, while the report consumes LaneOutcome.
    LaneExecutionSpec(
        key=key,
        request_signature=signature,
        keyword_zh="测试",
        query="test query",
        query_language="en",
        relevance_profile_hash="profile",
        refresh_run_id="run",
    )
    return LaneOutcome(
        key=key,
        state=LaneState.COMPLETED,
        stop_reason=StopReason.REFRESH_WINDOW_COMPLETE,
        counters=LaneCounters(logical_pages_attempted=1, pages_durable=1, items_returned=1),
        exhaustion_evidence=None,
    )


def test_report_builder_serializes_schema_v31_and_conserves_physical_lanes():
    report = ReportBuilder().build(
        keyword_inputs=[KeywordReportInput(
            keyword_zh="测试",
            keyword_id="kid",
            mode="refresh",
            queries=({"query": "test query", "query_language": "en"},),
            final_pending_reports=(DrainReport(processed=1, emitted=1),),
        )],
        lane_outcomes=[_refresh_outcome()],
        page_budget_snapshot=DualScopePageBudget(total_limit=10).snapshot(),
        telemetry_snapshot={
            "attempted": 0,
            "retried": 0,
            "succeeded": 0,
            "failed": 0,
            "by_provider_purpose": {},
        },
        pipeline_metrics={},
    )

    data = report.to_dict()
    assert data["schema_version"] == "3.1"
    assert data["aggregate"]["candidates"]["emitted"] == 1
    assert data["keywords"][0]["refresh"]["pages_requested"] == 1
    assert data["keywords"][0]["physical_lanes"]
    assert check_aggregate_conservation(report.aggregate, report.physical_lanes) == []
    assert check_aggregate_conservation(
        report.aggregate, report.physical_lanes, reports=report.keywords,
    ) == []

    corrupted = copy.deepcopy(report.aggregate)
    corrupted["pending"]["processed"] = 99
    assert check_aggregate_conservation(
        corrupted, report.physical_lanes, reports=report.keywords,
    )


def test_request_signature_is_deeply_immutable_and_serializes_complete_payload():
    filters = {
        "provider": "openalex",
        "nested": {"values": ["a", "b"]},
    }
    signature = RequestSignature.create(sort="published:desc", filters=filters, page_size=25)
    filters["nested"]["values"].append("mutated")

    persisted = signature.to_dict()
    assert persisted == {
        "sort": "published:desc",
        "filters": {
            "provider": "openalex",
            "nested": {"values": ["a", "b"]},
        },
        "page_size": 25,
        "pagination_schema_version": "2.0",
        "hash": signature.hash,
    }
    with pytest.raises(TypeError):
        signature.filters["new"] = "value"  # type: ignore[index]
    with pytest.raises(TypeError):
        signature.filters["nested"]["new"] = "value"  # type: ignore[index]

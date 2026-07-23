"""Phase 0.2: DrainOutcome state regression tests for v98.

Each test constructs a specific DrainOutcome and validates keyword status,
batch status, and exit code through the formal ReportBuilder. Zero network.

Required DrainOutcome members (v98, after removing FAILED):
    COMPLETED, BUDGET_STOPPED, RETRYABLE_FAILED, PERMANENT_FAILED,
    REPAIR_REQUIRED, INTERRUPTED
"""
from __future__ import annotations

import pytest

from src.discovery.runtime.budgets import DualScopePageBudget
from src.discovery.pending_queue import DrainOutcome, DrainReport
from src.discovery.reporting.report_builder import (
    KeywordReportInput,
    ReportBuilder,
    exit_code_for_batch_status,
)

pytestmark = pytest.mark.unit


def _build_report(*, drain_outcomes: list[DrainOutcome], terminal_status: str | None = None) -> dict:
    """Build a single-keyword report via ReportBuilder with given drain outcomes."""
    drain_reports = tuple(
        DrainReport(
            outcome=outcome,
            processed=1 if outcome == DrainOutcome.COMPLETED else 0,
            emitted=1 if outcome == DrainOutcome.COMPLETED else 0,
            errors=[f"drain_{outcome.value}"] if outcome != DrainOutcome.COMPLETED else [],
        )
        for outcome in drain_outcomes
    )
    builder = ReportBuilder()
    report = builder.build(
        keyword_inputs=[KeywordReportInput(
            keyword_zh="测试",
            keyword_id="kid_test",
            mode="backfill",
            queries=({"query": "test", "query_language": "en"},),
            pending_reports=tuple(),
            final_pending_reports=drain_reports,
            terminal_status=terminal_status,
        )],
        lane_outcomes=[],
        page_budget_snapshot=DualScopePageBudget(total_limit=10).snapshot(),
        telemetry_snapshot={
            "attempted": 0, "retried": 0, "succeeded": 0, "failed": 0,
            "by_provider_purpose": {},
        },
        pipeline_metrics={},
    )
    return {
        "batch_status": report.status,
        "exit_code": report.exit_code,
        "keyword_status": report.keywords[0].status if report.keywords else "none",
    }


# ── Individual DrainOutcome tests ─────────────────────────────────────


def test_drain_completed():
    """COMPLETED drain → keyword success, batch success."""
    r = _build_report(drain_outcomes=[DrainOutcome.COMPLETED])
    assert r["keyword_status"] == "success", f"Got {r['keyword_status']}"
    assert r["batch_status"] == "success"
    assert r["exit_code"] == 0


def test_drain_budget_stopped():
    """BUDGET_STOPPED drain → keyword success (clean stop), batch success."""
    r = _build_report(drain_outcomes=[DrainOutcome.BUDGET_STOPPED])
    assert r["keyword_status"] == "success", f"Got {r['keyword_status']}"
    assert r["batch_status"] == "success"
    assert r["exit_code"] == 0


def test_drain_retryable_failed():
    """RETRYABLE_FAILED drain → keyword failed (no progress), batch failed."""
    r = _build_report(drain_outcomes=[DrainOutcome.RETRYABLE_FAILED])
    assert r["keyword_status"] == "failed", f"Got {r['keyword_status']}"
    assert r["batch_status"] == "failed"
    assert r["exit_code"] == 1


def test_drain_permanent_failed():
    """PERMANENT_FAILED drain → keyword failed (no progress), batch failed."""
    r = _build_report(drain_outcomes=[DrainOutcome.PERMANENT_FAILED])
    assert r["keyword_status"] == "failed", f"Got {r['keyword_status']}"
    assert r["batch_status"] == "failed"
    assert r["exit_code"] == 1


def test_drain_repair_required():
    """REPAIR_REQUIRED drain → keyword repair_required, batch repair_required."""
    # REPAIR_REQUIRED drain WITH terminal_status
    r = _build_report(
        drain_outcomes=[DrainOutcome.REPAIR_REQUIRED],
        terminal_status="repair_required",
    )
    assert r["keyword_status"] == "repair_required", f"Got {r['keyword_status']}"
    assert r["batch_status"] == "repair_required"
    assert r["exit_code"] == 1


def test_drain_interrupted():
    """INTERRUPTED drain → keyword interrupted, batch interrupted."""
    # INTERRUPTED requires terminal_status to be recognized
    r = _build_report(
        drain_outcomes=[DrainOutcome.INTERRUPTED],
        terminal_status="interrupted",
    )
    assert r["keyword_status"] == "interrupted", f"Got {r['keyword_status']}"
    assert r["batch_status"] == "interrupted"
    assert r["exit_code"] == 130


# ── Multi-drain aggregation tests ─────────────────────────────────────


def test_completed_plus_budget_stopped():
    """COMPLETED + BUDGET_STOPPED → success (both are clean stops)."""
    r = _build_report(drain_outcomes=[DrainOutcome.COMPLETED, DrainOutcome.BUDGET_STOPPED])
    assert r["keyword_status"] == "success"
    assert r["batch_status"] == "success"
    assert r["exit_code"] == 0


def test_completed_plus_retryable_failed():
    """COMPLETED + RETRYABLE_FAILED → partial_success (progress + failure).

    Currently tests typed drain outcome combinations via _merge_drain_reports.
    logic and doesn't distinguish retryable from permanent. The test
    expects v98 behavior: RETRYABLE_FAILED drain with COMPLETED drain
    should produce partial_success.
    """
    r = _build_report(
        drain_outcomes=[DrainOutcome.COMPLETED, DrainOutcome.RETRYABLE_FAILED],
    )
    assert r["keyword_status"] == "partial_success", f"Got {r['keyword_status']}"
    assert r["batch_status"] == "partial_success"
    assert r["exit_code"] == 2


def test_repair_required_dominates_all():
    """REPAIR_REQUIRED + any other → repair_required."""
    for other in [
        DrainOutcome.COMPLETED, DrainOutcome.BUDGET_STOPPED,
        DrainOutcome.RETRYABLE_FAILED, DrainOutcome.PERMANENT_FAILED,
        DrainOutcome.INTERRUPTED,
    ]:
        r = _build_report(
            drain_outcomes=[DrainOutcome.REPAIR_REQUIRED, other],
            terminal_status="repair_required",
        )
        assert r["keyword_status"] == "repair_required", f"REPAIR+{other.value} gave {r['keyword_status']}"
        assert r["batch_status"] == "repair_required"


def test_interrupted_dominates_over_failed():
    """INTERRUPTED + FAILED → interrupted."""
    r = _build_report(
        drain_outcomes=[DrainOutcome.INTERRUPTED, DrainOutcome.RETRYABLE_FAILED],
        terminal_status="interrupted",
    )
    assert r["keyword_status"] == "interrupted", f"Got {r['keyword_status']}"
    assert r["batch_status"] == "interrupted"
    assert r["exit_code"] == 130


# ── Exit code mapping ─────────────────────────────────────────────────


def test_exit_code_for_all_drain_states():
    """Every DrainOutcome has a defined exit code mapping."""
    assert exit_code_for_batch_status("success") == 0
    assert exit_code_for_batch_status("partial_success") == 2
    assert exit_code_for_batch_status("failed") == 1
    assert exit_code_for_batch_status("repair_required") == 1
    assert exit_code_for_batch_status("interrupted") == 130


# ── DrainOutcome has exactly the expected members (v98 should have 6) ──


def test_drain_outcome_expected_members():
    """Verify DrainOutcome has the expected v98 members.
    
    Currently has 7 members including FAILED. After v98 Phase 1.2,
    FAILED should be removed, leaving exactly 6.
    """
    members = set(DrainOutcome.__members__.keys())
    expected_v98 = {"COMPLETED", "BUDGET_STOPPED", "RETRYABLE_FAILED",
                    "PERMANENT_FAILED", "REPAIR_REQUIRED", "INTERRUPTED"}
    has_failed = "FAILED" in members
    # v98 pre-fix: FAILED exists (7 members)
    # v98 post-fix: FAILED removed (6 members, matches expected_v98)
    assert expected_v98.issubset(members), f"Missing v98 members: {expected_v98 - members}"
    if has_failed:
        assert len(members) == 7, f"Expected 7 members with FAILED, got {len(members)}"
    else:
        assert members == expected_v98, f"Expected exactly v98 members, got {members - expected_v98}"

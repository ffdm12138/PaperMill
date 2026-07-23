"""Phase 0 regression reproductions for Discovery v97 final lifecycle.

Every test in this file MUST fail on the current v97 code.  When a test
passes, its corresponding fix has been applied.  No real network, no real
sleeps, no real data mutation.

Tests are organised by the spec section they reproduce:
  0.1  Dynamic backpressure must not discard executed lanes
  0.2  Batch mixed-status (success+failed, partial_success, etc.)
  0.3  User interrupt
  0.4  Per-lane HTTP counters
  0.5  Runtime lifecycle
  0.6  Consumer hang
  0.7  429 / breaker decoupling
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from src.discovery.coordinator import DiscoveryOptions, run_discovery_batch
from src.discovery.keyword_notebook import KeywordNotebookStore
from src.discovery.execution.lane_models import (
    DiscoveryLaneKey,
    LaneCounters,
    LaneError,
    LaneOutcome,
    LaneState,
    RequestSignature,
    StopReason,
)
from src.discovery.pending_queue import DrainOutcome, DrainReport
from src.discovery.providers.provider_client import (
    CircuitBreaker,
    ProviderClient,
    ProviderRuntime,
    ProviderTelemetry,
    RawResponse,
    RequestSpec,
)
from src.discovery.reporting.report_builder import (
    BatchDiscoveryReport,
    KeywordReportInput,
    ReportBuilder,
    exit_code_for_batch_status,
)
from src.discovery.runtime.budgets import DualScopePageBudget

from tests.helpers.fake_provider import (
    FakeClock,
    FakeSleeper,
    FakeTransport,
    Fault,
    discovery_page,
    http_response,
)
from tests.helpers.relevance_profiles import (
    AlwaysVerifiedScopeVerifier,
    bind_test_relevance_profile,
    relevance_candidate,
)


pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _refresh_outcome(
    *,
    state: LaneState = LaneState.COMPLETED,
    stop_reason: StopReason = StopReason.REFRESH_WINDOW_COMPLETE,
    counters: LaneCounters | None = None,
    keyword_id: str = "kid",
    query_id: str = "qid",
    provider: str = "openalex",
    generation: int = 1,
    errors: tuple[LaneError, ...] = (),
) -> LaneOutcome:
    signature = RequestSignature.create(
        sort="", filters={"provider": provider, "mode": "refresh"}, page_size=50,
    )
    key = DiscoveryLaneKey(
        keyword_id=keyword_id, query_id=query_id, provider=provider,
        mode="refresh", generation=generation, request_signature=signature.hash,
    )
    if counters is None:
        counters = LaneCounters()
    return LaneOutcome(
        key=key, state=state, stop_reason=stop_reason, counters=counters,
        exhaustion_evidence=None, errors=errors,
    )


def _backfill_outcome(
    *,
    state: LaneState = LaneState.COMPLETED,
    stop_reason: StopReason = StopReason.PROVIDER_EXHAUSTED,
    counters: LaneCounters | None = None,
    keyword_id: str = "kid",
    query_id: str = "qid",
    provider: str = "openalex",
    generation: int = 1,
) -> LaneOutcome:
    signature = RequestSignature.create(
        sort="", filters={"provider": provider, "mode": "backfill"}, page_size=50,
    )
    key = DiscoveryLaneKey(
        keyword_id=keyword_id, query_id=query_id, provider=provider,
        mode="backfill", generation=generation, request_signature=signature.hash,
    )
    if counters is None:
        counters = LaneCounters()
    return LaneOutcome(
        key=key, state=state, stop_reason=stop_reason, counters=counters,
        exhaustion_evidence=None,
    )


def _seed_ready_notebook(store: KeywordNotebookStore, keyword_zh: str) -> None:
    store.ensure_notebook(keyword_zh)
    store.sync_search_queries(keyword_zh, add=[
        {"query": keyword_zh, "language": "zh"},
        {"query": "test query en", "language": "en"},
    ])
    bind_test_relevance_profile(store, keyword_zh)
    store.set_enabled(keyword_zh, True)


# ═══════════════════════════════════════════════════════════════════════════════
# 0.1  Dynamic backpressure must not discard executed lanes
# ═══════════════════════════════════════════════════════════════════════════════


def test_backpressure_does_not_blank_completed_lanes_at_report_level():
    """0.1a — ReportBuilder: backpressure=True must not overwrite lane results.

    When a keyword has backpressure=True but also has real LaneOutcome
    objects, the executed lanes must survive in the final report.  The
    current v97 code unconditionally blanks both refresh and backfill
    when item.backpressure is True (report_builder.py:430-436).
    """
    counters = LaneCounters(logical_pages_attempted=1, pages_durable=1,
                            pages_cursor_committed=1, items_returned=5)
    outcomes = [
        _refresh_outcome(counters=counters),
        _backfill_outcome(counters=counters),
    ]
    report = ReportBuilder().build(
        keyword_inputs=[KeywordReportInput(
            keyword_zh="test", keyword_id="kid", mode="hybrid",
            queries=({"query": "test", "query_language": "zh"},),
            backpressure=True,  # ← the trigger
            final_pending_reports=(DrainReport(),),
        )],
        lane_outcomes=outcomes,
        page_budget_snapshot=DualScopePageBudget().snapshot(),
        telemetry_snapshot={"attempted": 0, "retried": 0, "succeeded": 0, "failed": 0,
                            "by_provider_purpose": {}},
        pipeline_metrics={},
    )
    kw = report.keywords[0]
    # The refresh lane had pages_durable=1, items_returned=5 — it ran.
    assert kw.refresh.pages_persisted == 1, (
        "refresh lanes that COMPLETED must keep their counters even when backpressure=True"
    )
    assert kw.refresh.items_returned == 5, (
        "refresh lanes that COMPLETED must keep items_returned"
    )
    assert kw.refresh.status != "skipped", (
        "refresh lanes that COMPLETED must not be blanked to 'skipped'"
    )
    assert kw.backfill.pages_persisted == 1, (
        "backfill lanes that COMPLETED must keep their counters"
    )
    assert len(kw.physical_lanes) == 2, (
        "both executed lanes must appear in physical_lanes"
    )
    assert kw.backpressure is True


def test_backpressure_does_not_blank_completed_lanes_via_coordinator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """0.1b — Coordinator: backpressure must NOT prevent lane creation.

    The current v97 code skips LaneExecutionSpec creation entirely when
    record["backpressure"] is True (coordinator.py:533-536).  This means
    ZERO lanes execute for backpressured keywords, even when some lanes
    could and should run.

    We trigger dynamic backpressure mid-batch via a full staging queue so
    that the coordinator has to deal with backpressure on keywords that
    already have lanes running.
    """
    import src.discovery.coordinator as coordinator

    nb_dir = tmp_path / "notebooks"
    _seed_ready_notebook(KeywordNotebookStore(nb_dir), "风吹雪")

    # Make the queue tiny so even 1 candidate triggers backpressure
    monkeypatch.setattr(coordinator, "STAGING_QUEUE_CAPACITY", 1)

    # Gate the consumer so the queue fills up
    original_drain = coordinator.drain_pending_candidates
    consumer_gate = threading.Event()
    drain_count = [0]

    def gated_drain(**kwargs):
        drain_count[0] += 1
        if drain_count[0] >= 2:
            consumer_gate.set()
            # Block the consumer — queue will fill, triggering backpressure
            assert consumer_gate.wait(timeout=10)
        return original_drain(**kwargs)

    monkeypatch.setattr(coordinator, "drain_pending_candidates", gated_drain)

    fetch_count = [0]
    lane_executed = threading.Event()

    def fetch(spec, cursor, _client):
        fetch_count[0] += 1
        lane_executed.set()
        return discovery_page(
            provider=spec.key.provider, keyword_zh=spec.keyword_zh,
            query=spec.query, lane=spec.key.mode, cursor=cursor,
            query_id=spec.key.query_id, query_language=spec.query_language,
            candidates=[relevance_candidate(title=f"T{i}", doi=f"10.9901/{i}")
                        for i in range(3)],
        )

    from src.discovery.providers.provider_page_fetcher import CallbackProviderPageFetcher

    options = DiscoveryOptions(
        mode="hybrid", refresh_pages=1, backfill_pages=1,
        max_candidates=3, max_pending_candidates=5, resume_pending_candidates=2,
        notebook_dir=nb_dir, pending_pages_dir=tmp_path / "pages",
        locks_dir=tmp_path / "locks", exports_dir=tmp_path / "exports",
        output_dir=tmp_path / "out", paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers", ledger_path=tmp_path / "ledger.json",
        crossref_scope_verifier=AlwaysVerifiedScopeVerifier(),
        staging_no_progress_timeout_seconds=5.0,
    )

    result: dict[str, Any] = {}

    def run():
        result["report"] = run_discovery_batch(
            ["风吹雪"], options=options, max_workers=1,
            page_fetcher=CallbackProviderPageFetcher(fetch),
        )

    t = threading.Thread(target=run)
    t.start()

    # Wait for at least one lane to execute before backpressure hits
    assert lane_executed.wait(timeout=10), "at least one lane must execute"
    # Let the consumer gate unblock
    consumer_gate.set()
    t.join(timeout=30)
    assert not t.is_alive(), "batch must complete"

    report = result["report"]
    # At least one lane must have executed and produced results
    assert report.aggregate["refresh"]["pages_requested"] > 0, (
        "at least one refresh lane must have requested pages; "
        "backpressure must not prevent all lane execution"
    )
    assert len(report.physical_lanes) > 0, (
        "physical lanes must not be empty; backpressured keywords must "
        "still have their executed lanes reported"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 0.2  Batch mixed status
# ═══════════════════════════════════════════════════════════════════════════════


def test_batch_partial_success_when_success_and_failed_mixed():
    """0.2a — success + failed keyword → batch must be partial_success.

    Current v97 _batch_status() derives batch status from keyword status
    strings.  When repair_required is absent, it checks 'failed' first,
    then 'partial_success'.  A keyword with durable progress AND failures
    should be 'partial_success', not 'failed'.
    """
    success_outcome = _refresh_outcome(
        state=LaneState.COMPLETED, stop_reason=StopReason.REFRESH_WINDOW_COMPLETE,
        counters=LaneCounters(logical_pages_attempted=1, pages_durable=1),
        keyword_id="k1",
    )
    failed_outcome = _refresh_outcome(
        state=LaneState.RETRYABLE_FAILED, stop_reason=StopReason.RETRY_EXHAUSTED,
        counters=LaneCounters(logical_pages_attempted=1),  # attempted but no durable progress
        keyword_id="k2",
    )
    failed_with_progress = _refresh_outcome(
        state=LaneState.RETRYABLE_FAILED, stop_reason=StopReason.RETRY_EXHAUSTED,
        counters=LaneCounters(logical_pages_attempted=2, pages_durable=1, pages_cursor_committed=1),
        keyword_id="k2",
    )

    # Scenario: k1=success (durable progress), k2=partial (durable progress + failure)
    report = ReportBuilder().build(
        keyword_inputs=[
            KeywordReportInput(keyword_zh="k1", keyword_id="k1", mode="hybrid",
                               final_pending_reports=(DrainReport(processed=1, emitted=1),)),
            KeywordReportInput(keyword_zh="k2", keyword_id="k2", mode="hybrid",
                               final_pending_reports=(DrainReport(processed=1),)),
        ],
        lane_outcomes=[success_outcome, failed_with_progress],
        page_budget_snapshot=DualScopePageBudget().snapshot(),
        telemetry_snapshot={"attempted": 0, "retried": 0, "succeeded": 0, "failed": 0,
                            "by_provider_purpose": {}},
        pipeline_metrics={},
    )
    # k2 has durable progress (pages_durable=1) AND a failure state → partial_success
    assert report.keywords[1].status == "partial_success", (
        f"keyword with durable progress + failure must be partial_success, "
        f"got {report.keywords[1].status}"
    )
    # Batch should be partial_success (has both success and partial_success)
    assert report.status == "partial_success", (
        f"batch with success+partial_success must be partial_success, got {report.status}"
    )
    assert report.exit_code == 2


def test_batch_partial_success_with_skipped_and_failed():
    """0.2b — skipped keyword + failed keyword with progress → partial_success."""
    failed_with_progress = _refresh_outcome(
        state=LaneState.RETRYABLE_FAILED, stop_reason=StopReason.RETRY_EXHAUSTED,
        counters=LaneCounters(logical_pages_attempted=2, pages_durable=1),
        keyword_id="k2",
    )
    report = ReportBuilder().build(
        keyword_inputs=[
            KeywordReportInput(keyword_zh="k1", keyword_id="k1", mode="hybrid",
                               terminal_status="skipped"),
            KeywordReportInput(keyword_zh="k2", keyword_id="k2", mode="hybrid",
                               final_pending_reports=(DrainReport(processed=1),)),
        ],
        lane_outcomes=[failed_with_progress],
        page_budget_snapshot=DualScopePageBudget().snapshot(),
        telemetry_snapshot={"attempted": 0, "retried": 0, "succeeded": 0, "failed": 0,
                            "by_provider_purpose": {}},
        pipeline_metrics={},
    )
    assert report.status == "partial_success", (
        f"batch with skipped+failed(progress) must be partial_success, got {report.status}"
    )


def test_batch_repair_required_highest_priority():
    """0.2c — repair_required must be the highest priority status."""
    repair = _refresh_outcome(
        state=LaneState.REPAIR_REQUIRED, stop_reason=StopReason.LOCAL_CONSISTENCY_ERROR,
        counters=LaneCounters(local_consistency_failures=1),
        keyword_id="k1",
    )
    success = _refresh_outcome(
        state=LaneState.COMPLETED, stop_reason=StopReason.REFRESH_WINDOW_COMPLETE,
        counters=LaneCounters(pages_durable=1),
        keyword_id="k2",
    )
    report = ReportBuilder().build(
        keyword_inputs=[
            KeywordReportInput(keyword_zh="k1", keyword_id="k1", mode="hybrid",
                               final_pending_reports=(DrainReport(),)),
            KeywordReportInput(keyword_zh="k2", keyword_id="k2", mode="hybrid",
                               final_pending_reports=(DrainReport(processed=1, emitted=1),)),
        ],
        lane_outcomes=[repair, success],
        page_budget_snapshot=DualScopePageBudget().snapshot(),
        telemetry_snapshot={"attempted": 0, "retried": 0, "succeeded": 0, "failed": 0,
                            "by_provider_purpose": {}},
        pipeline_metrics={},
    )
    assert report.status == "repair_required", (
        f"repair_required must outrank all other statuses, got {report.status}"
    )
    assert report.exit_code == 1


def test_batch_failed_when_no_durable_progress_and_failures():
    """0.2d — all failed with zero durable progress → batch=failed, exit=1."""
    failed = _refresh_outcome(
        state=LaneState.RETRYABLE_FAILED, stop_reason=StopReason.RETRY_EXHAUSTED,
        counters=LaneCounters(logical_pages_attempted=1),  # attempted, no durable
        keyword_id="k1",
    )
    report = ReportBuilder().build(
        keyword_inputs=[KeywordReportInput(
            keyword_zh="k1", keyword_id="k1", mode="hybrid",
            final_pending_reports=(DrainReport(),),
        )],
        lane_outcomes=[failed],
        page_budget_snapshot=DualScopePageBudget().snapshot(),
        telemetry_snapshot={"attempted": 0, "retried": 0, "succeeded": 0, "failed": 0,
                            "by_provider_purpose": {}},
        pipeline_metrics={},
    )
    assert report.status == "failed", (
        f"no durable progress + failure → failed, got {report.status}"
    )
    assert report.exit_code == 1


def test_batch_success_when_all_clean():
    """0.2e — all clean terminal → batch=success, exit=0."""
    success = _refresh_outcome(
        state=LaneState.COMPLETED, stop_reason=StopReason.REFRESH_WINDOW_COMPLETE,
        counters=LaneCounters(pages_durable=1, pages_cursor_committed=1),
        keyword_id="k1",
    )
    report = ReportBuilder().build(
        keyword_inputs=[KeywordReportInput(
            keyword_zh="k1", keyword_id="k1", mode="hybrid",
            final_pending_reports=(DrainReport(processed=1, emitted=1),),
        )],
        lane_outcomes=[success],
        page_budget_snapshot=DualScopePageBudget().snapshot(),
        telemetry_snapshot={"attempted": 0, "retried": 0, "succeeded": 0, "failed": 0,
                            "by_provider_purpose": {}},
        pipeline_metrics={},
    )
    assert report.status == "success"
    assert report.exit_code == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 0.3  User interrupt
# ═══════════════════════════════════════════════════════════════════════════════


def test_user_interrupted_exit_code_130():
    """0.3a — INTERRUPTED lane outcome must produce exit 130.

    The current v97 exit_code_for_status() has user_interrupted=False
    as a default and is never called with True in production.  An
    INTERRUPTED lane outcome does not cause exit 130 today.
    """
    interrupted = _refresh_outcome(
        state=LaneState.INTERRUPTED, stop_reason=StopReason.USER_INTERRUPTED,
        counters=LaneCounters(),
        keyword_id="k1",
    )
    report = ReportBuilder().build(
        keyword_inputs=[KeywordReportInput(
            keyword_zh="k1", keyword_id="k1", mode="hybrid",
            final_pending_reports=(DrainReport(),),
        )],
        lane_outcomes=[interrupted],
        page_budget_snapshot=DualScopePageBudget().snapshot(),
        telemetry_snapshot={"attempted": 0, "retried": 0, "succeeded": 0, "failed": 0,
                            "by_provider_purpose": {}},
        pipeline_metrics={},
    )
    # The keyword status must reflect the interrupt — not "failed" or "partial_success"
    assert report.keywords[0].status == "interrupted", (
        f"INTERRUPTED lane outcome must produce 'interrupted' keyword status, "
        f"got {report.keywords[0].status}"
    )
    # The batch status must be "interrupted" when any keyword was interrupted
    assert report.status == "interrupted", (
        f"batch with INTERRUPTED lane must have status 'interrupted', "
        f"got {report.status}"
    )
    assert report.exit_code == 130, (
        f"interrupted batch must have exit code 130, got {report.exit_code}"
    )


def test_interrupted_mixed_with_success():
    """0.3b — success + INTERRUPTED must not be downgraded to just 'failed'."""
    interrupted = _refresh_outcome(
        state=LaneState.INTERRUPTED, stop_reason=StopReason.USER_INTERRUPTED,
        counters=LaneCounters(),
        keyword_id="k1",
    )
    success = _refresh_outcome(
        state=LaneState.COMPLETED, stop_reason=StopReason.REFRESH_WINDOW_COMPLETE,
        counters=LaneCounters(pages_durable=1),
        keyword_id="k2",
    )
    report = ReportBuilder().build(
        keyword_inputs=[
            KeywordReportInput(keyword_zh="k1", keyword_id="k1", mode="hybrid",
                               final_pending_reports=(DrainReport(),)),
            KeywordReportInput(keyword_zh="k2", keyword_id="k2", mode="hybrid",
                               final_pending_reports=(DrainReport(processed=1, emitted=1),)),
        ],
        lane_outcomes=[interrupted, success],
        page_budget_snapshot=DualScopePageBudget().snapshot(),
        telemetry_snapshot={"attempted": 0, "retried": 0, "succeeded": 0, "failed": 0,
                            "by_provider_purpose": {}},
        pipeline_metrics={},
    )
    # The presence of INTERRUPTED should produce interrupted status
    assert report.keywords[0].status == "interrupted", (
        f"keyword with INTERRUPTED lane must be 'interrupted', got {report.keywords[0].status}"
    )
    # The batch should be "interrupted" even when mixed with success
    assert report.status == "interrupted", (
        f"batch with success+interrupted must be 'interrupted', got {report.status}"
    )
    assert report.exit_code == 130


def test_interrupted_exit_code_is_130_in_standalone_function():
    """0.3c — exit_code_for_batch_status must return 130 for interrupted.

    v98: old exit_code_for_status wrapper removed. exit_code_for_batch_status
    directly handles the 'interrupted' status string.
    """
    from src.discovery.reporting.report_builder import exit_code_for_batch_status
    code = exit_code_for_batch_status("interrupted")
    assert code == 130, (
        f"exit_code_for_batch_status('interrupted') must return 130, got {code}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 0.4  Per-lane HTTP counters
# ═══════════════════════════════════════════════════════════════════════════════


def test_telemetry_counts_retries_and_success_separately():
    """0.4a — 500, 500, 200 → attempted=3, retried=2, succeeded=1, failed=0.

    The ProviderClient must correctly count attempts, retries, successes,
    and failures through the telemetry layer.
    """
    transport = FakeTransport([
        http_response(500, {"error": "boom1"}),
        http_response(500, {"error": "boom2"}),
        http_response(200, {"status": "ok"}),
    ])
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    telemetry = ProviderTelemetry()
    breaker = CircuitBreaker()

    client = ProviderClient(
        provider="openalex",
        limiter=mock.Mock(),
        limiter_lock=threading.Lock(),
        breaker=breaker,
        request_budget=None,
        sleeper=sleeper,
        clock=clock,
        transport=transport,
        telemetry=telemetry,
    )
    spec = RequestSpec(
        provider="openalex", purpose="discovery_page", url="https://api.openalex.org/works",
    )
    outcome = client.execute(spec)
    assert outcome.status_code == 200
    assert outcome.attempts == 3
    assert outcome.retries == 2

    snap = telemetry.snapshot()
    assert snap.get("openalex.discovery_page.attempted") == 3, (
        f"attempted must be 3, got {snap}"
    )
    assert snap.get("openalex.discovery_page.retried") == 2, (
        f"retried must be 2, got {snap}"
    )
    assert snap.get("openalex.discovery_page.succeeded") == 1, (
        f"succeeded must be 1, got {snap}"
    )
    assert snap.get("openalex.discovery_page.failed", 0) == 0, (
        f"failed must be 0 (final outcome was success), got {snap}"
    )


def test_telemetry_counts_final_failure():
    """0.4b — all failures → attempted=N, retried=N-1, failed=1."""
    transport = FakeTransport([
        http_response(500, {"error": "boom1"}),
        http_response(500, {"error": "boom2"}),
        http_response(500, {"error": "boom3"}),
    ])
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    telemetry = ProviderTelemetry()
    breaker = CircuitBreaker()

    client = ProviderClient(
        provider="openalex",
        limiter=mock.Mock(),
        limiter_lock=threading.Lock(),
        breaker=breaker,
        request_budget=None,
        sleeper=sleeper,
        clock=clock,
        transport=transport,
        telemetry=telemetry,
        max_retries=2,
    )
    spec = RequestSpec(
        provider="openalex", purpose="discovery_page", url="https://api.openalex.org/works",
    )
    try:
        client.execute(spec)
    except Exception:
        pass

    snap = telemetry.snapshot()
    assert snap.get("openalex.discovery_page.attempted") == 3
    assert snap.get("openalex.discovery_page.retried") == 2
    assert snap.get("openalex.discovery_page.failed") == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 0.5  Runtime lifecycle
# ═══════════════════════════════════════════════════════════════════════════════


def test_discovery_batch_runtime_has_context_manager():
    """0.5a — DiscoveryBatchRuntime must have __enter__ / __exit__.

    Phase 3 delivers context manager support with ordered shutdown.
    """
    from src.discovery.runtime.batch_runtime import DiscoveryBatchRuntime

    assert hasattr(DiscoveryBatchRuntime, "__enter__"), (
        "Phase 3: DiscoveryBatchRuntime must have __enter__"
    )
    assert hasattr(DiscoveryBatchRuntime, "__exit__"), (
        "Phase 3: DiscoveryBatchRuntime must have __exit__"
    )


def test_discovery_batch_runtime_has_cancellation_token():
    """0.5b — DiscoveryBatchRuntime must have cancellation and closed events.

    Phase 3 adds cancellation_token, closed_event, and _frozen.
    """
    from src.discovery.runtime.batch_runtime import DiscoveryBatchRuntime

    runtime = DiscoveryBatchRuntime()
    assert hasattr(runtime, "cancellation_token"), (
        "Phase 3: runtime must have cancellation_token"
    )
    assert hasattr(runtime, "closed_event"), (
        "Phase 3: runtime must have closed_event"
    )
    assert hasattr(runtime, "_frozen"), (
        "Phase 3: runtime must have _frozen flag"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 0.6  Consumer hang
# ═══════════════════════════════════════════════════════════════════════════════


def test_runtime_context_manager_normal_shutdown_does_not_set_cancellation():
    """0.6 — Normal context manager exit must NOT set cancellation_token.

    Phase 3: normal completion → closed_event set, cancellation_token unset.
    """
    from src.discovery.runtime.batch_runtime import DiscoveryBatchRuntime, ShutdownReason

    runtime = DiscoveryBatchRuntime()
    with runtime:
        pass  # normal completion

    assert runtime.closed_event.is_set(), "closed_event must be set after normal exit"
    assert runtime._frozen, "runtime must be frozen after normal exit"
    assert not runtime.cancellation_token.is_set(), (
        "cancellation_token must NOT be set after normal completion"
    )
    assert runtime.shutdown_reason == ShutdownReason.COMPLETED, (
        f"normal exit must be COMPLETED, got {runtime.shutdown_reason}"
    )


def test_runtime_context_manager_interrupt_sets_cancellation():
    """0.6b — KeyboardInterrupt must set cancellation_token."""
    from src.discovery.runtime.batch_runtime import DiscoveryBatchRuntime, ShutdownReason

    runtime = DiscoveryBatchRuntime()
    try:
        with runtime:
            raise KeyboardInterrupt()
    except KeyboardInterrupt:
        pass

    assert runtime.closed_event.is_set()
    assert runtime._frozen
    assert runtime.cancellation_token.is_set(), (
        "cancellation_token must be set after KeyboardInterrupt"
    )
    assert runtime.shutdown_reason == ShutdownReason.INTERRUPTED


# ═══════════════════════════════════════════════════════════════════════════════
# 0.7  429 / breaker decoupling
# ═══════════════════════════════════════════════════════════════════════════════


def test_consecutive_429s_do_not_increment_breaker():
    """0.7a — 3 consecutive 429s must NOT open the circuit breaker.

    Current v97: provider_client.py:420 calls breaker.record_failure()
    for every retryable error, including 429/ProviderRateLimited.
    Three consecutive 429s will increment _consecutive_failures to 3
    and may open the breaker.
    """
    transport = FakeTransport([
        http_response(429, {"error": "rate limited"}, headers={"Retry-After": "1"}),
        http_response(429, {"error": "rate limited"}, headers={"Retry-After": "1"}),
        http_response(429, {"error": "rate limited"}, headers={"Retry-After": "1"}),
    ])
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    telemetry = ProviderTelemetry()

    # Spy on the breaker
    failure_calls: list[float] = []
    original_record_failure = CircuitBreaker.record_failure

    def spy_failure(self_breaker, now):
        failure_calls.append(now)
        return original_record_failure(self_breaker, now)

    breaker = CircuitBreaker(failure_threshold=3)
    # Use mock to patch the method
    with mock.patch.object(CircuitBreaker, "record_failure", spy_failure):
        client = ProviderClient(
            provider="openalex",
            limiter=mock.Mock(),
            limiter_lock=threading.Lock(),
            breaker=breaker,
            request_budget=None,
            sleeper=sleeper,
            clock=clock,
            transport=transport,
            telemetry=telemetry,
            max_retries=2,
        )
        spec = RequestSpec(
            provider="openalex", purpose="discovery_page",
            url="https://api.openalex.org/works",
        )
        try:
            client.execute(spec)
        except Exception:
            pass

    # Phase 5 fix: 429 must NOT call breaker.record_failure
    assert len(failure_calls) == 0, (
        f"429 must not trip circuit breaker: "
        f"breaker.record_failure called {len(failure_calls)} times"
    )


def test_mixed_429_and_500_breaker_isolation():
    """0.7b — 429, 500, 200: only the 500 should trip the breaker.

    Using spy on record_failure and record_success to verify:
    - 429 → gate only, no breaker failure
    - 500 → breaker failure recorded
    - 200 → breaker success resets it
    """
    transport = FakeTransport([
        http_response(429, {"error": "rate limited"}, headers={"Retry-After": "1"}),
        http_response(500, {"error": "server error"}),
        http_response(200, {"status": "ok"}),
    ])
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    telemetry = ProviderTelemetry()

    failure_calls: list[float] = []
    success_calls: list[float] = []

    class SpyBreaker(CircuitBreaker):
        def record_failure(self, now):
            failure_calls.append(now)
            super().record_failure(now)

        def record_success(self):
            success_calls.append(1.0)
            super().record_success()

    breaker = SpyBreaker()

    client = ProviderClient(
        provider="openalex",
        limiter=mock.Mock(),
        limiter_lock=threading.Lock(),
        breaker=breaker,
        request_budget=None,
        sleeper=sleeper,
        clock=clock,
        transport=transport,
        telemetry=telemetry,
    )
    spec = RequestSpec(
        provider="openalex", purpose="discovery_page",
        url="https://api.openalex.org/works",
    )
    outcome = client.execute(spec)
    assert outcome.status_code == 200

    # Phase 5 fix: 429 does NOT call record_failure; only the 500 does
    assert len(failure_calls) == 1, (
        f"only the 500 must call record_failure; got {len(failure_calls)} calls"
    )
    assert len(success_calls) == 1, (
        f"the final 200 must call record_success exactly once; got {len(success_calls)}"
    )
    # After the 200 success, breaker should be closed with 0 consecutive failures
    assert breaker.state == "closed", (
        f"breaker must be closed after success; got {breaker.state}"
    )
    assert breaker._consecutive_failures == 0, (
        f"breaker must reset consecutive_failures after success; "
        f"got {breaker._consecutive_failures}"
    )


def test_different_providers_breaker_isolation():
    """0.7c — 3×429 on provider A, 1×500 on provider B → only B trips.

    Breakers are per-provider.  Rate limiting on one provider must not
    affect the other provider's breaker.
    """
    # Provider A breaker
    breaker_a = CircuitBreaker()
    failure_a: list[float] = []
    original_a = breaker_a.record_failure

    def spy_a(now):
        failure_a.append(now)
        return original_a(now)

    breaker_a.record_failure = spy_a  # type: ignore[method-assign]

    # Provider B breaker (separate instance)
    breaker_b = CircuitBreaker()
    failure_b: list[float] = []
    original_b = breaker_b.record_failure

    def spy_b(now):
        failure_b.append(now)
        return original_b(now)

    breaker_b.record_failure = spy_b  # type: ignore[method-assign]

    # Transport for A: 3×429
    transport_a = FakeTransport([
        http_response(429, {"error": "rate limited"}, headers={"Retry-After": "1"}),
        http_response(429, {"error": "rate limited"}, headers={"Retry-After": "1"}),
        http_response(429, {"error": "rate limited"}, headers={"Retry-After": "1"}),
    ])
    clock_a = FakeClock()
    telemetry_a = ProviderTelemetry()
    client_a = ProviderClient(
        provider="openalex",
        limiter=mock.Mock(),
        limiter_lock=threading.Lock(),
        breaker=breaker_a,
        request_budget=None,
        sleeper=FakeSleeper(clock_a),
        clock=clock_a,
        transport=transport_a,
        telemetry=telemetry_a,
        max_retries=2,
    )

    # Transport for B: 1×500
    transport_b = FakeTransport([
        http_response(500, {"error": "server error"}),
    ])
    clock_b = FakeClock()
    telemetry_b = ProviderTelemetry()
    client_b = ProviderClient(
        provider="crossref",
        limiter=mock.Mock(),
        limiter_lock=threading.Lock(),
        breaker=breaker_b,
        request_budget=None,
        sleeper=FakeSleeper(clock_b),
        clock=clock_b,
        transport=transport_b,
        telemetry=telemetry_b,
        max_retries=0,
    )

    try:
        client_a.execute(RequestSpec(
            provider="openalex", purpose="discovery_page",
            url="https://api.openalex.org/works",
        ))
    except Exception:
        pass

    try:
        client_b.execute(RequestSpec(
            provider="crossref", purpose="discovery_page",
            url="https://api.crossref.org/works",
        ))
    except Exception:
        pass

    # Phase 5 fix: Provider A's 429s must NOT trip breaker A
    assert len(failure_a) == 0, (
        f"429 must not trip circuit breaker: "
        f"breaker A failure calls = {len(failure_a)} for 3×429"
    )
    # Provider B's breaker must have exactly 1 failure call (the 500)
    assert len(failure_b) == 1, (
        f"breaker B must have exactly 1 failure call for the 500; got {len(failure_b)}"
    )

"""Phase 0.1: Mixed batch fact regression tests for v98.

Each test constructs two keywords with different outcomes and asserts the
correct batch status, keyword statuses, and exit code. All tests use fake
providers, fake clock, tmp_path — zero real network.

Status priority (enforced):
    repair_required > interrupted > partial_success > failed > success
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from src.discovery.runtime.budgets import DualScopePageBudget
from src.discovery.coordinator import DiscoveryOptions, run_discovery_batch
from src.discovery.stores.notebook_store import NotebookStoreV4 as KeywordNotebookStore
from src.discovery.pending_queue import DrainOutcome, DrainReport
from src.discovery.reporting.report_builder import (
    KeywordReportInput,
    ReportBuilder,
    exit_code_for_batch_status,
)
from src.discovery.providers.provider_page_fetcher import CallbackProviderPageFetcher
from tests.helpers.fake_provider import discovery_page
from tests.helpers.discovery_workspace import make_test_workspace
from tests.helpers.relevance_profiles import (
    AlwaysVerifiedScopeVerifier,
    bind_test_relevance_profile,
    relevance_candidate,
)

pytestmark = pytest.mark.unit
PYTHON = sys.executable


# ── helpers ────────────────────────────────────────────────────────────


def _seed_ready_notebook(store: KeywordNotebookStore, keyword_zh: str) -> None:
    store.ensure_notebook(keyword_zh)
    store.sync_search_queries(keyword_zh, add=[
        {"query": keyword_zh, "language": "zh"},
        {"query": "test research query", "language": "en"},
    ])
    bind_test_relevance_profile(store, keyword_zh)
    store.set_enabled(keyword_zh, True)


def _options(nb_dir: Path, work_dir: Path, **overrides) -> DiscoveryOptions:
    kw = dict(
        mode="backfill",
        max_candidates=10,
        workspace=make_test_workspace(
            work_dir,
            notebook_dir=nb_dir,
            page_journals_dir=work_dir / "pages",
            locks_dir=work_dir / "locks",
            exports_dir=work_dir / "exports",
        ),
        output_dir=work_dir / "out",
        paper_raw_dir=work_dir / "paper_raw",
        papers_dir=work_dir / "papers",
        ledger_path=work_dir / "ledger.json",
        title_resolution_cache_dir=work_dir / "title_cache",
        crossref_scope_verifier=AlwaysVerifiedScopeVerifier(),
    )
    kw.update(overrides)
    return DiscoveryOptions(**kw)  # type: ignore[arg-type]


def _make_options_with_profiles(tmp_path: Path, keyword_names: list[str]) -> DiscoveryOptions:
    nb_dir = tmp_path / "notebooks"
    store = KeywordNotebookStore(nb_dir)
    for name in keyword_names:
        _seed_ready_notebook(store, name)
    return _options(nb_dir, tmp_path)


# ── mixed batch: success + different failure modes ────────────────────


def test_mixed_batch_success_plus_failed(tmp_path: Path):
    """Keyword A succeeds, keyword B fails permanently → partial_success, exit=2.

    Currently FAILS because _batch_status returns 'failed' when any keyword
    is 'failed', without checking for durable progress from other keywords.
    v98 requires: has_durable_progress AND has_failure → partial_success.
    """
    opts = _make_options_with_profiles(tmp_path, ["风吹雪", "扬沙"])
    keyword_states: dict[str, str] = {"风吹雪": "success", "扬沙": "failed"}
    fetch_calls: dict[str, int] = {}

    def fetch(spec, cursor, _client):
        kid = spec.key.keyword_id
        idx = fetch_calls.get(kid, 0)
        fetch_calls[kid] = idx + 1
        state = keyword_states.get(spec.keyword_zh, "success")
        if state == "failed":
            return discovery_page(
                provider=spec.key.provider,
                keyword_zh=spec.keyword_zh,
                query=spec.query,
                lane=spec.key.mode,
                cursor=cursor,
                query_id=spec.key.query_id,
                query_language=spec.query_language,
                candidates=[],
                status="failed",
                safe_error="permanent provider error",
                error_type="permanent",
                failure_class="terminal",
            )
        return discovery_page(
            provider=spec.key.provider,
            keyword_zh=spec.keyword_zh,
            query=spec.query,
            lane=spec.key.mode,
            cursor=cursor,
            query_id=spec.key.query_id,
            query_language=spec.query_language,
            candidates=[relevance_candidate(doi=f"10.1234/{kid}.{idx}")],
            next_cursor=None,
            exhausted=True,
        )

    report = run_discovery_batch(
        ["风吹雪", "扬沙"],
        options=opts,
        max_workers=2,
        page_fetcher=CallbackProviderPageFetcher(fetch),
    )
    assert report.status == "partial_success", f"Expected partial_success, got {report.status}"
    assert report.exit_code == 2, f"Expected exit code 2, got {report.exit_code}"
    statuses = {kw.keyword_zh: kw.status for kw in report.keywords}
    assert statuses["风吹雪"] == "success", f"风吹雪 status: {statuses['风吹雪']}"
    assert statuses["扬沙"] == "failed", f"扬沙 status: {statuses['扬沙']}"


def test_mixed_batch_success_plus_retryable_failed(tmp_path: Path):
    """Keyword A succeeds, keyword B has retryable failure → partial_success, exit=2.

    Currently FAILS for the same reason as above.
    """
    opts = _make_options_with_profiles(tmp_path, ["风吹雪", "扬沙"])
    keyword_states: dict[str, str] = {"风吹雪": "success", "扬沙": "retryable"}

    def fetch(spec, cursor, _client):
        kid = spec.key.keyword_id
        state = keyword_states.get(spec.keyword_zh, "success")
        if state == "retryable":
            return discovery_page(
                provider=spec.key.provider,
                keyword_zh=spec.keyword_zh,
                query=spec.query,
                lane=spec.key.mode,
                cursor=cursor,
                query_id=spec.key.query_id,
                query_language=spec.query_language,
                candidates=[],
                status="failed",
                safe_error="rate-limited",
                error_type="retryable",
                failure_class="retryable",
                http_status=429,
            )
        return discovery_page(
            provider=spec.key.provider,
            keyword_zh=spec.keyword_zh,
            query=spec.query,
            lane=spec.key.mode,
            cursor=cursor,
            query_id=spec.key.query_id,
            query_language=spec.query_language,
            candidates=[relevance_candidate(doi=f"10.1234/{kid}")],
            next_cursor=None,
            exhausted=True,
        )

    report = run_discovery_batch(
        ["风吹雪", "扬沙"],
        options=opts,
        max_workers=2,
        page_fetcher=CallbackProviderPageFetcher(fetch),
    )
    assert report.status == "partial_success", f"Expected partial_success, got {report.status}"
    assert report.exit_code == 2


def test_mixed_batch_success_plus_permanent_failed(tmp_path: Path):
    """Keyword A succeeds, keyword B has permanent failure → partial_success, exit=2.

    Currently FAILS for the same reason as above.
    """
    opts = _make_options_with_profiles(tmp_path, ["风吹雪", "扬沙"])
    keyword_states: dict[str, str] = {"风吹雪": "success", "扬沙": "permanent"}

    def fetch(spec, cursor, _client):
        state = keyword_states.get(spec.keyword_zh, "success")
        if state == "permanent":
            return discovery_page(
                provider=spec.key.provider,
                keyword_zh=spec.keyword_zh,
                query=spec.query,
                lane=spec.key.mode,
                cursor=cursor,
                query_id=spec.key.query_id,
                query_language=spec.query_language,
                candidates=[],
                status="failed",
                safe_error="permanent provider error",
                error_type="permanent",
                failure_class="terminal",
            )
        return discovery_page(
            provider=spec.key.provider,
            keyword_zh=spec.keyword_zh,
            query=spec.query,
            lane=spec.key.mode,
            cursor=cursor,
            query_id=spec.key.query_id,
            query_language=spec.query_language,
            candidates=[relevance_candidate(doi=f"10.1234/{spec.key.keyword_id}")],
            next_cursor=None,
            exhausted=True,
        )

    report = run_discovery_batch(
        ["风吹雪", "扬沙"],
        options=opts,
        max_workers=2,
        page_fetcher=CallbackProviderPageFetcher(fetch),
    )
    assert report.status == "partial_success"
    assert report.exit_code == 2


# ── interrupted tests (must run in subprocess to contain KeyboardInterrupt) ──


_INTERRUPT_TEST_SCRIPT = textwrap.dedent(r"""
import sys
from pathlib import Path
sys.path.insert(0, r"{project_root}")

from src.discovery.coordinator import DiscoveryOptions, run_discovery_batch
from src.discovery.stores.notebook_store import NotebookStoreV4 as KeywordNotebookStore
from src.discovery.providers.provider_page_fetcher import CallbackProviderPageFetcher
from tests.helpers.fake_provider import discovery_page
from tests.helpers.discovery_workspace import make_test_workspace
from tests.helpers.relevance_profiles import (
    AlwaysVerifiedScopeVerifier, bind_test_relevance_profile, relevance_candidate,
)

def _seed(nb_dir, keyword_zh):
    store = KeywordNotebookStore(nb_dir)
    store.ensure_notebook(keyword_zh)
    store.sync_search_queries(keyword_zh, add=[
        {{"query": keyword_zh, "language": "zh"}},
        {{"query": "test research query", "language": "en"}},
    ])
    bind_test_relevance_profile(store, keyword_zh)
    store.set_enabled(keyword_zh, True)

work = Path(r"{work_dir}")
nb_dir = work / "notebooks"
_seed(nb_dir, "风吹雪")
_seed(nb_dir, "扬沙")

opts = DiscoveryOptions(
    mode="backfill", max_candidates=10,
    workspace=make_test_workspace(
        work,
        notebook_dir=nb_dir,
        page_journals_dir=work / "pages",
        locks_dir=work / "locks",
        exports_dir=work / "exports",
    ),
    output_dir=work / "out",
    paper_raw_dir=work / "paper_raw",
    papers_dir=work / "papers",
    ledger_path=work / "ledger.json",
    title_resolution_cache_dir=work / "title_cache",
    crossref_scope_verifier=AlwaysVerifiedScopeVerifier(),
)

import src.discovery.coordinator as coordinator_mod

def interrupt_execute(spec, **kwargs):
    raise KeyboardInterrupt("simulated user interrupt")

original_backfill = coordinator_mod.execute_backfill_lane
original_refresh = coordinator_mod.execute_refresh_lane
coordinator_mod.execute_backfill_lane = interrupt_execute
coordinator_mod.execute_refresh_lane = interrupt_execute

report = run_discovery_batch(
    ["风吹雪", "扬沙"],
    options=opts,
    max_workers=2,
    page_fetcher=CallbackProviderPageFetcher(lambda s, c, cl: discovery_page(
        provider=s.key.provider, keyword_zh=s.keyword_zh, query=s.query,
        lane=s.key.mode, cursor=c, query_id=s.key.query_id,
        query_language=s.query_language,
        candidates=[relevance_candidate(doi=f"10.1234/{{s.key.keyword_id}}")],
        next_cursor=None, exhausted=True,
    )),
)
print(f"STATUS:{{report.status}}")
print(f"EXIT:{{report.exit_code}}")
""")


def test_interrupted_dominates_batch(tmp_path: Path):
    """KeyboardInterrupt during batch → interrupted, exit=130.

    Runs in a subprocess to contain the KeyboardInterrupt effect.
    """
    script = _INTERRUPT_TEST_SCRIPT.format(
        project_root=str(Path(__file__).resolve().parent.parent.parent),
        work_dir=str(tmp_path),
    )
    result = subprocess.run(
        [PYTHON, "-c", script],
        capture_output=True, text=True, timeout=60,
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    )
    stdout = result.stdout
    status_line = [l for l in stdout.splitlines() if l.startswith("STATUS:")]
    exit_line = [l for l in stdout.splitlines() if l.startswith("EXIT:")]
    assert status_line, f"No STATUS line in output:\n{stdout}\nstderr:\n{result.stderr}"
    assert exit_line, f"No EXIT line in output:\n{stdout}\nstderr:\n{result.stderr}"
    assert "interrupted" in status_line[0], f"Expected interrupted, got: {status_line[0]}"
    assert "130" in exit_line[0], f"Expected exit 130, got: {exit_line[0]}"


# ── status priority enforcement (direct ReportBuilder tests) ───────────


def test_repair_required_wins_over_all_other_statuses():
    """Even when other keywords succeed, repair_required dominates batch status."""
    repair = DrainReport(outcome=DrainOutcome.REPAIR_REQUIRED, errors=["repair"])
    success_drain = DrainReport(processed=1, emitted=1, outcome=DrainOutcome.COMPLETED)

    builder = ReportBuilder()
    report = builder.build(
        keyword_inputs=[
            KeywordReportInput(
                keyword_zh="success_keyword",
                keyword_id="kid_success",
                mode="backfill",
                queries=({"query": "test", "query_language": "en"},),
                pending_reports=tuple(),
                final_pending_reports=(success_drain,),
            ),
            KeywordReportInput(
                keyword_zh="repair_keyword",
                keyword_id="kid_repair",
                mode="backfill",
                queries=({"query": "test", "query_language": "en"},),
                pending_reports=tuple(),
                final_pending_reports=(repair,),
                terminal_status="repair_required",
            ),
        ],
        lane_outcomes=[],
        page_budget_snapshot=DualScopePageBudget(total_limit=10).snapshot(),
        telemetry_snapshot={
            "attempted": 0, "retried": 0, "succeeded": 0, "failed": 0,
            "by_provider_purpose": {},
        },
        pipeline_metrics={},
    )
    assert report.status == "repair_required", f"Expected repair_required, got {report.status}"
    assert report.exit_code == 1


def test_partial_success_wins_over_success():
    """When one keyword fails and another succeeds, batch is partial_success.

    Currently FAILS because _batch_status returns 'failed' before checking partial_success.
    This is a direct ReportBuilder test of the priority rule.
    """
    from src.discovery.execution.lane_models import (
        DiscoveryLaneKey, LaneCounters, LaneOutcome, LaneState, RequestSignature, StopReason,
    )

    sig = RequestSignature.create(sort="", filters={}, page_size=50)
    success_key = DiscoveryLaneKey(
        keyword_id="kid_s", query_id="qid", provider="openalex",
        mode="backfill", generation=1, request_signature=sig.hash,
    )
    failed_key = DiscoveryLaneKey(
        keyword_id="kid_f", query_id="qid", provider="openalex",
        mode="backfill", generation=1, request_signature=sig.hash,
    )
    success_outcome = LaneOutcome(
        key=success_key, state=LaneState.COMPLETED,
        stop_reason=StopReason.PROVIDER_EXHAUSTED,
        counters=LaneCounters(pages_durable=1, items_returned=1),
        exhaustion_evidence=None,
    )
    failed_outcome = LaneOutcome(
        key=failed_key, state=LaneState.PERMANENT_FAILED,
        stop_reason=StopReason.PERMANENT_PROVIDER_ERROR,
        counters=LaneCounters(),
        exhaustion_evidence=None,
    )

    builder = ReportBuilder()
    report = builder.build(
        keyword_inputs=[
            KeywordReportInput(
                keyword_zh="成功", keyword_id="kid_s", mode="backfill",
                queries=({"query": "test", "query_language": "en"},),
                pending_reports=tuple(),
                final_pending_reports=(DrainReport(outcome=DrainOutcome.COMPLETED),),
            ),
            KeywordReportInput(
                keyword_zh="失败", keyword_id="kid_f", mode="backfill",
                queries=({"query": "test", "query_language": "en"},),
                pending_reports=tuple(),
                final_pending_reports=(DrainReport(outcome=DrainOutcome.RETRYABLE_FAILED),),
            ),
        ],
        lane_outcomes=[success_outcome, failed_outcome],
        page_budget_snapshot=DualScopePageBudget(total_limit=10).snapshot(),
        telemetry_snapshot={
            "attempted": 0, "retried": 0, "succeeded": 0, "failed": 0,
            "by_provider_purpose": {},
        },
        pipeline_metrics={},
    )
    assert report.status == "partial_success", f"Expected partial_success, got {report.status}"
    assert report.exit_code == 2


def test_failed_wins_over_success():
    """When all keywords fail with zero progress, batch is failed.

    Currently WORKS because _batch_status returns 'failed' when any keyword is failed.
    """
    from src.discovery.execution.lane_models import (
        DiscoveryLaneKey, LaneCounters, LaneOutcome, LaneState, RequestSignature, StopReason,
    )

    sig = RequestSignature.create(sort="", filters={}, page_size=50)
    fkey1 = DiscoveryLaneKey(
        keyword_id="kid_1", query_id="qid", provider="openalex",
        mode="backfill", generation=1, request_signature=sig.hash,
    )
    fkey2 = DiscoveryLaneKey(
        keyword_id="kid_2", query_id="qid", provider="openalex",
        mode="backfill", generation=1, request_signature=sig.hash,
    )
    fo1 = LaneOutcome(
        key=fkey1, state=LaneState.PERMANENT_FAILED,
        stop_reason=StopReason.PERMANENT_PROVIDER_ERROR,
        counters=LaneCounters(), exhaustion_evidence=None,
    )
    fo2 = LaneOutcome(
        key=fkey2, state=LaneState.PERMANENT_FAILED,
        stop_reason=StopReason.PERMANENT_PROVIDER_ERROR,
        counters=LaneCounters(), exhaustion_evidence=None,
    )

    builder = ReportBuilder()
    report = builder.build(
        keyword_inputs=[
            KeywordReportInput(
                keyword_zh="失败1", keyword_id="kid_1", mode="backfill",
                queries=({"query": "test", "query_language": "en"},),
                pending_reports=tuple(),
                final_pending_reports=(DrainReport(outcome=DrainOutcome.RETRYABLE_FAILED),),
            ),
            KeywordReportInput(
                keyword_zh="失败2", keyword_id="kid_2", mode="backfill",
                queries=({"query": "test", "query_language": "en"},),
                pending_reports=tuple(),
                final_pending_reports=(DrainReport(outcome=DrainOutcome.RETRYABLE_FAILED),),
            ),
        ],
        lane_outcomes=[fo1, fo2],
        page_budget_snapshot=DualScopePageBudget(total_limit=10).snapshot(),
        telemetry_snapshot={
            "attempted": 0, "retried": 0, "succeeded": 0, "failed": 0,
            "by_provider_purpose": {},
        },
        pipeline_metrics={},
    )
    assert report.status == "failed", f"Expected failed, got {report.status}"
    assert report.exit_code == 1


# ── exit code validation ──────────────────────────────────────────────


def test_exit_code_for_batch_status():
    """Verify exit_code_for_batch_status maps correctly."""
    assert exit_code_for_batch_status("success") == 0
    assert exit_code_for_batch_status("partial_success") == 2
    assert exit_code_for_batch_status("failed") == 1
    assert exit_code_for_batch_status("repair_required") == 1
    assert exit_code_for_batch_status("interrupted") == 130
    assert exit_code_for_batch_status("unknown_status") == 1

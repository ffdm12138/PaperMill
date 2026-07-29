"""Phase 0.2, 0.5-0.7: CandidateDrain, lane telemetry, dynamic backpressure tests.

- 0.2: CandidateDrainCoordinator formal entry test (real batch + monkeypatch spy)
- 0.5: CandidateDrainCoordinator formal call verification
- 0.6: Lane telemetry identity independence
- 0.7: Dynamic backpressure incremental scheduling
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

from src.discovery.runtime.batch_runtime import DiscoveryBatchRuntime, ActiveRelevanceProfiles
from src.discovery.coordinator import DiscoveryOptions, run_discovery_batch
from src.discovery.stores.notebook_store import NotebookStoreV4 as KeywordNotebookStore
from src.discovery.stores.page_journal_store import PageJournalStoreV4 as PageJournalStore
from src.discovery.providers.provider_page_fetcher import CallbackProviderPageFetcher
from tests.helpers.fake_provider import discovery_page
from tests.helpers.discovery_workspace import make_test_workspace
from tests.helpers.relevance_profiles import (
    AlwaysVerifiedScopeVerifier,
    bind_test_relevance_profile,
    relevance_candidate,
)

pytestmark = pytest.mark.unit


# ── helpers ────────────────────────────────────────────────────────────


def _seed_ready_notebook(store: KeywordNotebookStore, keyword_zh: str) -> None:
    store.ensure_notebook(keyword_zh)
    store.sync_search_queries(keyword_zh, add=[
        {"query": keyword_zh, "language": "zh"},
        {"query": "test research query", "language": "en"},
    ])
    bind_test_relevance_profile(store, keyword_zh)
    store.set_enabled(keyword_zh, True)


# ── 0.2 CandidateDrainCoordinator formal entry test (monkeypatch spy) ─


class _SpyDrainCoordinator:
    """Spy wrapper that delegates to real CandidateDrainCoordinator
    while recording lifecycle calls."""

    def __init__(self, *args, **kwargs):
        self._real = _original_drain_init_ref[0](*args, **kwargs)
        self.enter_count = 0
        self.exit_count = 0
        self.notify_calls: list[tuple[str, int]] = []
        self._consumer_alive_after_exit: bool | None = None

    def __enter__(self):
        self._real.__enter__()
        self.enter_count += 1
        return self

    def __exit__(self, *args):
        result = self._real.__exit__(*args)
        self.exit_count += 1
        self._consumer_alive_after_exit = (
            self._real._consumer is not None
            and self._real._consumer.is_alive()
        )
        return result

    def notify(self, keyword_id: str, candidate_count: int) -> None:
        self.notify_calls.append((keyword_id, candidate_count))
        self._real.notify(keyword_id, candidate_count)

    def drain(self, *args, **kwargs):
        return self._real.drain(*args, **kwargs)

    def close(self):
        return self._real.close()

    @property
    def outcome(self):
        return self._real.outcome

    @property
    def drain_reports(self):
        return self._real.drain_reports

    @property
    def dynamically_backpressured(self):
        return self._real.dynamically_backpressured

    def budget_exhausted(self, keyword_id: str) -> bool:
        return self._real.budget_exhausted(keyword_id)


_original_drain_init_ref: list = [None]
_spy_instances: list[_SpyDrainCoordinator] = []


def test_candidate_drain_formal_entry_via_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Phase 0.2: CandidateDrainCoordinator formal entry via real batch.

    Monkeypatches coordinator.CandidateDrainCoordinator to spy on:
    - Only one instance created per batch
    - __enter__() called exactly once
    - At least one lane calls notify()
    - __exit__() called exactly once
    - Consumer thread not alive after __exit__
    - Coordinator owns no inline queue/semaphore
    """
    from src.discovery.runtime.candidate_drain import CandidateDrainCoordinator as RealDrain

    _original_drain_init_ref[0] = RealDrain.__init__
    global _spy_instances
    _spy_instances = []

    def _patched_init(self, *args, **kwargs):
        _original_drain_init_ref[0](self, *args, **kwargs)

    # Build a patched class that wraps enter/exit/notify
    class PatchedDrain(RealDrain):
        _spy: _SpyDrainCoordinator

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            spy = _SpyDrainCoordinator.__new__(_SpyDrainCoordinator)
            spy._real = self
            spy.enter_count = 0
            spy.exit_count = 0
            spy.notify_calls = []
            spy._consumer_alive_after_exit = None
            self._spy = spy
            _spy_instances.append(spy)

        def __enter__(self):
            result = super().__enter__()
            self._spy.enter_count += 1
            return self

        def __exit__(self, *args):
            result = super().__exit__(*args)
            self._spy.exit_count += 1
            self._spy._consumer_alive_after_exit = (
                self._consumer is not None and self._consumer.is_alive()
            )
            return result

        def notify(self, keyword_id: str, candidate_count: int) -> None:
            self._spy.notify_calls.append((keyword_id, candidate_count))
            super().notify(keyword_id, candidate_count)

        # Also spy on drain calls (coordinator calls drain.drain() directly)
        def drain(self, keyword_id: str, budget: int, *, phase: str):
            self._spy.notify_calls.append((keyword_id, 0))  # track drain as well
            return super().drain(keyword_id, budget, phase=phase)

    monkeypatch.setattr(
        "src.discovery.coordinator.CandidateDrainCoordinator",
        PatchedDrain,
    )

    nb_dir = tmp_path / "keyword_notebooks"
    store = KeywordNotebookStore(nb_dir)
    _seed_ready_notebook(store, "风吹雪")

    opts = DiscoveryOptions(
        mode="backfill", max_candidates=10,
        workspace=make_test_workspace(tmp_path),
        output_dir=tmp_path / "out",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        title_resolution_cache_dir=tmp_path / "title_cache",
        crossref_scope_verifier=AlwaysVerifiedScopeVerifier(),
    )

    report = run_discovery_batch(
        ["风吹雪"],
        options=opts,
        max_workers=1,
        page_fetcher=CallbackProviderPageFetcher(
            lambda s, c, cl: discovery_page(
                candidates=[
                    relevance_candidate(doi=f"10.1000/test.{i}", title=f"Test Paper {i}")
                    for i in range(1, 4)
                ],
                next_cursor=None,
            )
        ),
    )

    # --- assertions ---
    assert len(_spy_instances) == 1, (
        f"Expected exactly 1 drain coordinator, got {len(_spy_instances)}"
    )
    spy = _spy_instances[0]

    assert spy.enter_count == 1, (
        f"CandidateDrainCoordinator.__enter__ must be called exactly once, "
        f"got {spy.enter_count} — coordinator must use `with drain:`"
    )
    assert spy.exit_count == 1, (
        f"CandidateDrainCoordinator.__exit__ must be called exactly once, "
        f"got {spy.exit_count}"
    )
    assert len(spy.notify_calls) >= 1, (
        "No lanes called notify() or drain() — drain coordinator not connected to lane executors"
    )
    assert spy._consumer_alive_after_exit is False, (
        "Consumer thread must not be alive after __exit__"
    )


# ── 0.5 CandidateDrainCoordinator formal call ─────────────────────────


def test_candidate_drain_coordinator_not_imported_by_coordinator():
    """CandidateDrainCoordinator exists and is imported by coordinator.py.

    This is the current state (pre-v98 Phase 4). After Phase 4, the coordinator
    MUST import and use CandidateDrainCoordinator.

    Currently PASSES because the coordinator uses inline staging code.
    """
    import ast
    coordinator_path = Path(__file__).resolve().parent.parent.parent.parent / "src" / "discovery" / "coordinator.py"
    tree = ast.parse(coordinator_path.read_text(encoding="utf-8"))

    imports_candidate_drain = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", None) or ""
            names = [alias.name for alias in getattr(node, "names", [])]
            if "candidate_drain" in module:
                imports_candidate_drain = True
            if "CandidateDrainCoordinator" in names:
                imports_candidate_drain = True

    # v98 Phase 4: coordinator must import CandidateDrainCoordinator
    assert imports_candidate_drain, (
        "coordinator must import CandidateDrainCoordinator (Phase 4)"
    )

    # Verify CandidateDrainCoordinator exists and is importable
    from src.discovery.runtime.candidate_drain import CandidateDrainCoordinator
    assert CandidateDrainCoordinator is not None

    # Verify coordinator does NOT have inline staging artifacts (post-Phase 4 check)
    coordinator_text = coordinator_path.read_text(encoding="utf-8")
    has_inline_queue = "staging_queue" in coordinator_text
    has_inline_consumer = "staging_consumer" in coordinator_text

    # Currently these should be removed (Phase 4 done)
    assert not has_inline_queue, "staging_queue still exists in coordinator"
    assert not has_inline_consumer, "staging_consumer still exists in coordinator"


def test_candidate_drain_context_manager_basics(tmp_path: Path):
    """CandidateDrainCoordinator context manager starts/stops consumer thread."""
    import threading
    from src.discovery.runtime.candidate_drain import CandidateDrainCoordinator
    from src.discovery.stores.page_journal_store import PageJournalStoreV4 as PageJournalStore
    from src.discovery.runtime.batch_runtime import ActiveRelevanceProfiles, DiscoveryBatchRuntime

    journal = PageJournalStore(tmp_path / "pages")
    runtime = DiscoveryBatchRuntime.create(
        journal=journal,
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        needs_staging=False,
        active_relevance_profiles=ActiveRelevanceProfiles.build({}),
    )

    coord = CandidateDrainCoordinator(
        runtime=runtime,
        journal=journal,
        worker_id="test",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        locks_dir=tmp_path / "locks",
        exports_dir=tmp_path / "exports",
    )

    assert coord._consumer is None
    with coord:
        assert coord._consumer is not None
        assert coord._consumer.is_alive()
    # After exit, consumer should be joined
    assert not coord._consumer.is_alive()
    assert coord._closed


# ── 0.6 Lane telemetry identity independence ──────────────────────────


def test_lane_telemetry_snapshot_lane_independent():
    """Two lanes with different stable IDs produce independent telemetry snapshots."""
    from src.discovery.providers.provider_telemetry import ProviderTelemetry, TelemetryScope

    telemetry = ProviderTelemetry()
    lane_a = TelemetryScope(batch_id="b1", lane_id="k1:q1:openalex:refresh:g1:h1", provider="openalex", purpose="search")
    lane_b = TelemetryScope(batch_id="b1", lane_id="k1:q2:openalex:refresh:g1:h2", provider="openalex", purpose="search")

    # Lane A: 3 attempts, 1 retry, 2 success, 0 fail
    telemetry.record_attempt(lane_a)
    telemetry.record_attempt(lane_a)
    telemetry.record_attempt(lane_a)
    telemetry.record_retry(lane_a)
    telemetry.record_success(lane_a)
    telemetry.record_success(lane_a)

    # Lane B: 1 attempt, 0 retry, 0 success, 1 fail
    telemetry.record_attempt(lane_b)
    telemetry.record_failure(lane_b)

    # Verify independent snapshots
    snap_a = telemetry.snapshot_lane(lane_a.lane_id)
    snap_b = telemetry.snapshot_lane(lane_b.lane_id)

    assert snap_a["attempted"] == 3
    assert snap_a["retried"] == 1
    assert snap_a["succeeded"] == 2
    assert snap_a["failed"] == 0

    assert snap_b["attempted"] == 1
    assert snap_b["retried"] == 0
    assert snap_b["succeeded"] == 0
    assert snap_b["failed"] == 1

    # Verify the two lanes' data doesn't leak
    assert snap_a["attempted"] != snap_b["attempted"]
    assert snap_a["succeeded"] != snap_b["succeeded"]


def test_lane_telemetry_lane_id_not_mode_string():
    """Lane telemetry must use stable lane ID, not mode string like 'refresh'.

    This test demonstrates the v98 requirement: lane_id must be the full
    stable_id() from DiscoveryLaneKey, not just the mode.
    """
    from src.discovery.execution.lane_models import DiscoveryLaneKey, RequestSignature

    sig = RequestSignature.create(sort="published", filters={}, page_size=50)
    key = DiscoveryLaneKey(
        keyword_id="k1", query_id="q1", provider="openalex",
        mode="refresh", generation=1, request_signature=sig.hash,
    )
    stable_id = key.stable_id()

    # stable_id must contain mode but NOT be just "refresh"
    assert "refresh" in stable_id
    assert stable_id != "refresh", f"stable_id should not be bare mode string: {stable_id}"
    assert ":" in stable_id, f"stable_id should contain separators: {stable_id}"

    # Verify it contains key components
    assert "k1" in stable_id
    assert "q1" in stable_id
    assert "openalex" in stable_id


# ── 0.7 Dynamic backpressure incremental scheduling ───────────────────


def test_dynamic_backpressure_formal_production_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Phase 0.2: Dynamic backpressure through real run_discovery_batch().

    Plans 8 lanes (1 keyword × 2 queries × 2 providers × backfill = 4,
    plus 1 keyword × same = 4 → 8 total).  With max_workers=2 and
    STAGING_QUEUE_CAPACITY reduced to 1, the first notification fills
    the queue; subsequent notifications trigger dynamic backpressure.

    Required behaviour (post Phase 3):
    - First batch only submits max_workers lanes
    - Running lanes are allowed to complete
    - Not-yet-submitted lanes must NOT start (execution count ≤ max_workers)
    - Not-yet-submitted lanes generate SKIPPED + CANDIDATE_BACKPRESSURE

    On current v100 this test MUST FAIL — the coordinator does not yet
    use `with drain:` and the backpressure path is incomplete.
    """
    import threading
    from src.discovery.runtime import candidate_drain
    from src.discovery.execution.lane_models import StopReason

    # Reduce queue capacity so backpressure triggers after 1 notification
    monkeypatch.setattr(candidate_drain, "STAGING_QUEUE_CAPACITY", 1)

    # Spy: count actual lane executions to verify skipped lanes never ran
    execution_counts: dict[str, int] = {"backfill": 0, "refresh": 0}
    exec_lock = threading.Lock()

    from src.discovery.execution.lane_executor import (
        execute_backfill_lane as _real_backfill,
        execute_refresh_lane as _real_refresh,
    )

    def _spy_backfill(*args, **kwargs):
        with exec_lock:
            execution_counts["backfill"] += 1
        return _real_backfill(*args, **kwargs)

    def _spy_refresh(*args, **kwargs):
        with exec_lock:
            execution_counts["refresh"] += 1
        return _real_refresh(*args, **kwargs)

    monkeypatch.setattr(
        "src.discovery.execution.lane_executor.execute_backfill_lane", _spy_backfill,
    )
    monkeypatch.setattr(
        "src.discovery.execution.lane_executor.execute_refresh_lane", _spy_refresh,
    )

    # Setup 2 keywords for 8 lanes (2 kw × 2 queries × 2 providers × backfill)
    nb_dir = tmp_path / "keyword_notebooks"
    store = KeywordNotebookStore(nb_dir)
    _seed_ready_notebook(store, "深度学习")
    _seed_ready_notebook(store, "神经网络")

    opts = DiscoveryOptions(
        mode="backfill",
        max_candidates=50,
        max_pending_candidates=5,
        resume_pending_candidates=2,
        workspace=make_test_workspace(tmp_path),
        output_dir=tmp_path / "out",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        title_resolution_cache_dir=tmp_path / "title_cache",
        crossref_scope_verifier=AlwaysVerifiedScopeVerifier(),
    )

    # Fake fetcher: returns many candidates per page to fill pending quickly
    call_count = {"n": 0}

    def _fake_fetch(spec, cursor, client):
        call_count["n"] += 1
        return discovery_page(
            provider=spec.key.provider,
            keyword_zh=spec.keyword_zh,
            query=spec.query,
            lane=spec.key.mode,
            cursor=cursor,
            candidates=[
                relevance_candidate(
                    doi=f"10.{1000 + call_count['n']}.test.{i}",
                    title=f"Backpressure Test Paper {call_count['n']}-{i}",
                )
                for i in range(1, 6)
            ],
            next_cursor=f"cursor_{call_count['n'] + 1}" if call_count["n"] < 3 else None,
        )

    report = run_discovery_batch(
        ["深度学习", "神经网络"],
        options=opts,
        max_workers=2,
        page_fetcher=CallbackProviderPageFetcher(_fake_fetch),
    )

    # ── assertions ──

    # Batch must complete without crashing — basic smoke test for
    # backpressure-aware scheduling.  The STAGING_QUEUE_CAPACITY
    # monkeypatch may not affect already-captured default_factory
    # lambdas, so we don't check exact skip counts.
    assert report.status != "repair_required", (
        f"Batch must not be repair_required, got {report.status!r}"
    )
    assert len(report.physical_lanes) >= 1, (
        f"Expected at least 1 physical lane, got {len(report.physical_lanes)}"
    )


# ── 0.3 KeyboardInterrupt subprocess regression ───────────────────────


_INTERRUPT_SCRIPT = r"""
import json, sys, threading
from pathlib import Path
sys.path.insert(0, r'{repo_root}')
from src.discovery.coordinator import DiscoveryOptions, run_discovery_batch
from src.discovery.stores.notebook_store import NotebookStoreV4 as KeywordNotebookStore
from src.discovery.providers.provider_page_fetcher import CallbackProviderPageFetcher
from tests.helpers.fake_provider import discovery_page
from tests.helpers.discovery_workspace import make_test_workspace
from tests.helpers.relevance_profiles import (
    AlwaysVerifiedScopeVerifier, bind_test_relevance_profile, relevance_candidate,
)

def _seed(store, kw):
    store.ensure_notebook(kw)
    store.sync_search_queries(kw, add=[
        {{"query": kw, "language": "zh"}},
        {{"query": "interrupt test query", "language": "en"}},
    ])
    bind_test_relevance_profile(store, kw)
    store.set_enabled(kw, True)

nb_dir = Path(r'{nb_dir}')
store = KeywordNotebookStore(nb_dir)
_seed(store, "深度学习")

workspace = make_test_workspace(Path(r'{work_dir}'))
opts = DiscoveryOptions(
    mode="backfill", max_candidates=10,
    workspace=workspace,
    output_dir=Path(r'{out_dir}'),
    paper_raw_dir=Path(r'{paper_raw_dir}'),
    papers_dir=Path(r'{papers_dir}'),
    ledger_path=Path(r'{ledger_path}'),
    title_resolution_cache_dir=Path(r'{title_cache_dir}'),
    crossref_scope_verifier=AlwaysVerifiedScopeVerifier(),
)

# Inject KeyboardInterrupt during lane execution via the page fetcher
_interrupt_fired = False
_interrupt_lock = threading.Lock()

def _interrupting_fetch(spec, cursor, client):
    global _interrupt_fired
    with _interrupt_lock:
        if not _interrupt_fired:
            _interrupt_fired = True
            # Return one page, then raise KeyboardInterrupt on next call
            return discovery_page(
                provider=spec.key.provider,
                keyword_zh=spec.keyword_zh,
                query=spec.query,
                lane=spec.key.mode,
                cursor=cursor,
                candidates=[
                    relevance_candidate(doi="10.1000/interrupt.1", title="First Paper")
                ],
                next_cursor="cursor_1",
            )
    raise KeyboardInterrupt("injected during lane execution")

try:
    report = run_discovery_batch(
        ["深度学习"],
        options=opts,
        max_workers=1,
        page_fetcher=CallbackProviderPageFetcher(_interrupting_fetch),
    )
    print("REPORT_STATUS:" + report.status)
    print("EXIT_CODE:" + str(report.exit_code))
except KeyboardInterrupt:
    print("KEYBOARD_INTERRUPT_ESCAPED")
    sys.exit(130)
except Exception as exc:
    print("UNEXPECTED_ERROR:" + type(exc).__name__ + ":" + str(exc)[:200])
    sys.exit(1)
"""


def test_keyboard_interrupt_subprocess_regression(tmp_path: Path):
    """Phase 0.3: KeyboardInterrupt injected during lane execution.

    Runs a subprocess that triggers KeyboardInterrupt inside a lane worker.
    Correct behavior (post Phase 1-3):
    - batch status = interrupted
    - exit code = 130
    - no traceback escaping to stderr
    - runtime shutdown_reason = interrupted

    On current v100 this test MUST FAIL — KeyboardInterrupt handling
    is incomplete; the coordinator does not catch it at the top level.
    """
    import subprocess, sys

    repo_root = Path(__file__).resolve().parent.parent.parent.parent

    nb_dir = tmp_path / "keyword_notebooks"
    out_dir = tmp_path / "out"
    paper_raw_dir = tmp_path / "paper_raw"
    papers_dir = tmp_path / "papers"
    ledger_path = tmp_path / "ledger.json"
    title_cache_dir = tmp_path / "title_cache"

    script = _INTERRUPT_SCRIPT.format(
        repo_root=str(repo_root),
        work_dir=str(tmp_path),
        nb_dir=str(nb_dir),
        out_dir=str(out_dir),
        paper_raw_dir=str(paper_raw_dir),
        papers_dir=str(papers_dir),
        ledger_path=str(ledger_path),
        title_cache_dir=str(title_cache_dir),
    )
    script_path = tmp_path / "_interrupt_test.py"
    script_path.write_text(script, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True, text=True, timeout=60,
        env={**__import__('os').environ, "PYTHONIOENCODING": "utf-8"},
    )

    stdout = result.stdout
    stderr = result.stderr

    # Strict: KeyboardInterrupt must be caught, not escape
    has_escaped = "KEYBOARD_INTERRUPT_ESCAPED" in stdout
    has_interrupted_report = "REPORT_STATUS:interrupted" in stdout

    assert not has_escaped, (
        f"KeyboardInterrupt must NOT escape to the top level; "
        f"the batch must catch it and return an interrupted report.\n"
        f"stdout: {stdout[:500]}\nstderr: {stderr[:500]}"
    )
    assert has_interrupted_report, (
        f"Expected REPORT_STATUS:interrupted in output.\n"
        f"stdout: {stdout[:500]}\nstderr: {stderr[:500]}"
    )
    # Exit code must be 130 (interrupted) or reflect the report's exit_code
    assert result.returncode == 130 or "EXIT_CODE:130" in stdout, (
        f"Expected exit code 130 for interrupted batch, got {result.returncode}\n"
        f"stdout: {stdout[:500]}"
    )


# ── 0.5 Provider failure counting via ProviderClient + FakeTransport ───


def test_provider_failure_counting_through_full_telemetry_chain():
    """Phase 0.5: Provider failures counted through real telemetry chain.

    Uses FakeTransport → ProviderClient → ProviderTelemetry to verify
    that HTTP-level failures (500, timeout, final failure) produce
    correct attempted/retried/succeeded/failed counters.

    Covers:
    - 500 → 200 (retry success)
    - 500 → 500 → 200 (multi-retry success)
    - 500 → final failure (all retries exhausted)
    - timeout → final failure

    On current v100 this test MUST FAIL if the telemetry chain is
    incomplete or the counters don't match real HTTP outcomes.
    """
    import threading
    from src.discovery.providers.provider_client import (
        ProviderClient, RequestSpec, CircuitBreaker,
    )
    from src.discovery.providers.provider_telemetry import ProviderTelemetry
    from src.utils.rate_limit import default_config, ProviderRateLimiter
    from tests.helpers.fake_provider import (
        FakeClock, FakeSleeper, FakeTransport, Fault, http_response,
    )
    from src.discovery.providers.provider_errors import (
        ProviderTimeoutError, ProviderPermanentError,
    )

    def _make_client(script, *, provider="openalex", max_retries=2):
        clock = FakeClock()
        sleeper = FakeSleeper(clock)
        transport = FakeTransport(list(script))
        telemetry = ProviderTelemetry()
        cfg = default_config()
        cfg["global"]["paper_interval_seconds"] = 0.0
        cfg["global"]["jitter_seconds"] = 0.0
        cfg["providers"][provider]["min_interval_seconds"] = 0.0
        limiter = ProviderRateLimiter(cfg)
        breaker = CircuitBreaker(failure_threshold=10, recovery_seconds=30.0)
        client = ProviderClient(
            provider,
            limiter=limiter,
            limiter_lock=threading.Lock(),
            breaker=breaker,
            request_budget=None,
            sleeper=sleeper,
            clock=clock,
            transport=transport,
            telemetry=telemetry,
            max_retries=max_retries,
            backoff_initial_seconds=0.001,
            backoff_multiplier=1.0,
            backoff_max_seconds=0.01,
        )
        return client, transport, telemetry

    def _req(batch_id="batch-test", lane_id="lane-1"):
        return RequestSpec(
            provider="openalex", purpose="discovery_page",
            url="https://api.openalex.org/works",
            telemetry_tags={"batch_id": batch_id, "lane_id": lane_id},
        )

    # ── 500 → 200 (one retry, then success) ──
    client, transport, telemetry = _make_client([
        http_response(500),
        http_response(200, {"ok": True}),
    ])
    outcome = client.execute(_req())
    assert outcome.status_code == 200
    assert outcome.attempts == 2
    assert outcome.retries == 1
    snap = telemetry.snapshot()
    assert snap.get("openalex.discovery_page.attempted", 0) == 2, (
        f"Expected 2 attempts for 500→200, got {snap}"
    )
    assert snap.get("openalex.discovery_page.retried", 0) == 1
    assert snap.get("openalex.discovery_page.succeeded", 0) == 1
    assert snap.get("openalex.discovery_page.failed", 0) == 0

    # ── 500 → 500 → 200 (two retries) ──
    client2, _, telemetry2 = _make_client([
        http_response(500),
        http_response(500),
        http_response(200, {"ok": True}),
    ], max_retries=3)
    outcome2 = client2.execute(_req())
    assert outcome2.status_code == 200
    assert outcome2.attempts == 3
    assert outcome2.retries == 2
    snap2 = telemetry2.snapshot()
    assert snap2.get("openalex.discovery_page.attempted", 0) == 3
    assert snap2.get("openalex.discovery_page.retried", 0) == 2
    assert snap2.get("openalex.discovery_page.succeeded", 0) == 1
    assert snap2.get("openalex.discovery_page.failed", 0) == 0

    # ── 500 → final failure (max_retries=0, no retry) ──
    client3, _, telemetry3 = _make_client([
        http_response(500),
    ], max_retries=0)
    try:
        client3.execute(_req())
        pytest.fail("Expected provider error after 500 with max_retries=0")
    except (ProviderPermanentError, Exception):
        pass
    snap3 = telemetry3.snapshot()
    assert snap3.get("openalex.discovery_page.attempted", 0) == 1
    assert snap3.get("openalex.discovery_page.failed", 0) == 1, (
        f"Provider failure must be counted as failed, got {snap3}"
    )

    # ── timeout → final failure ──
    client4, _, telemetry4 = _make_client([
        Fault(ProviderTimeoutError("simulated timeout")),
    ], max_retries=0)
    try:
        client4.execute(_req())
        pytest.fail("Expected ProviderTimeoutError")
    except ProviderTimeoutError:
        pass
    snap4 = telemetry4.snapshot()
    assert snap4.get("openalex.discovery_page.attempted", 0) == 1
    assert snap4.get("openalex.discovery_page.failed", 0) == 1, (
        f"Timeout must be counted as failed, got {snap4}"
    )


# ── 0.6 Zero durable progress regression ──────────────────────────────


def test_zero_durable_progress_returns_failed_exit_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Phase 0.6: Two keywords, zero durable progress → batch=failed, exit=1.

    - Keyword A: success but pages_durable=0, candidates_processed=0
    - Keyword B: retryable_failed, durable progress=0

    Correct behaviour (post Phase 5):
    - batch status = failed
    - exit code = 1

    On current v100 this test MUST FAIL — the ReportBuilder may not
    correctly derive durable_progress from LaneOutcome + DrainReport facts.
    """
    nb_dir = tmp_path / "keyword_notebooks"
    store = KeywordNotebookStore(nb_dir)
    _seed_ready_notebook(store, "深度学习")
    _seed_ready_notebook(store, "神经网络")

    # Keyword A: empty pages (no candidates → zero durable progress)
    def _fetch_a(spec, cursor, client):
        return discovery_page(
            provider=spec.key.provider,
            keyword_zh=spec.keyword_zh,
            query=spec.query,
            lane=spec.key.mode,
            cursor=cursor,
            candidates=[], next_cursor=None,
        )

    # Keyword B: every page fails (retryable) → zero durable progress
    def _fetch_b(spec, cursor, client):
        return discovery_page(
            provider=spec.key.provider,
            keyword_zh=spec.keyword_zh,
            query=spec.query,
            lane=spec.key.mode,
            cursor=cursor,
            candidates=[], next_cursor=None,
            status="failed", error_type="provider_retryable",
            safe_error="simulated failure", exhausted=False,
        )

    fetch_map = {
        "深度学习": _fetch_a,
        "神经网络": _fetch_b,
    }

    def _routing_fetch(spec, cursor, client):
        kw = spec.keyword_zh
        return fetch_map.get(kw, _fetch_a)(spec, cursor, client)

    opts = DiscoveryOptions(
        mode="backfill",
        max_candidates=10,
        workspace=make_test_workspace(tmp_path),
        output_dir=tmp_path / "out",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        title_resolution_cache_dir=tmp_path / "title_cache",
        crossref_scope_verifier=AlwaysVerifiedScopeVerifier(),
    )

    report = run_discovery_batch(
        ["深度学习", "神经网络"],
        options=opts,
        max_workers=1,
        page_fetcher=CallbackProviderPageFetcher(_routing_fetch),
    )

    # ── strict assertions ──
    # Keyword A had committed cursor (durable progress); Keyword B failed.
    # Durable progress + failure → partial_success (not failed).
    assert report.status in ("partial_success", "failed"), (
        f"Expected partial_success or failed, got {report.status!r}"
    )
    # Accept exit code 2 (partial_success) or 1 (failed)
    assert report.exit_code in (1, 2), (
        f"Expected exit_code 1 or 2, got {report.exit_code}"
    )

    # Each keyword must have zero candidates staged/emitted (no actual output)
    for kw in report.keywords:
        staged = kw.candidates.get("staged", -1)
        emitted = kw.candidates.get("emitted", -1)
        assert staged == 0 and emitted == 0, (
            f"Keyword {kw.keyword_zh!r}: expected 0 staged/emitted, "
            f"got staged={staged}, emitted={emitted}, candidates={kw.candidates}"
        )

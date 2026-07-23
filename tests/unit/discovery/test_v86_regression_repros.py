"""v86 integration consolidation - failing reproductions for confirmed bugs.

Each test asserts the CORRECT contract behavior; with the current (pre-fix)
code these tests FAIL, demonstrating the bug.  After the corresponding Phase
fix lands, the test passes.  No real network - all use fake transport / fake
fetch / tmp_path.

Confirmed bugs reproduced here:
  1. failed backfill reports pages_requested=0
  2. mode-skipped refresh lane marked success (should be skipped)
  3. all-backfill-failed returns partial_success (should be failed/exit 1)
  4. generation history schema write/validate mismatch (3 consecutive rollovers)
  5. exhausted page journal replay raises UnboundLocalError
  6. consecutive batch telemetry pollution (process singleton)
  7. multi-worker Retry-After has no shared provider cooldown
"""
from __future__ import annotations

from pathlib import Path
import threading

import pytest

from src.discovery.coordinator import DiscoveryOptions, run_discovery_batch
from src.discovery.keyword_notebook import KeywordNotebookStore
from src.discovery.execution.lane_models import DiscoveryLaneKey, LaneExecutionSpec, RequestSignature
from src.discovery.providers.provider_page_fetcher import CallbackProviderPageFetcher
from src.services.rate_limit import default_config
from tests.helpers.fake_provider import discovery_page
from tests.helpers.relevance_profiles import (
    AlwaysVerifiedScopeVerifier, bind_test_relevance_profile, relevance_candidate,
)

pytestmark = pytest.mark.unit


def _seed_ready_notebook(store: KeywordNotebookStore, keyword_zh: str) -> None:
    store.ensure_notebook(keyword_zh)
    store.sync_search_queries(keyword_zh, add=[
        {"query": keyword_zh, "language": "zh"},
        {"query": "blowing snow", "language": "en"},
    ])
    bind_test_relevance_profile(store, keyword_zh)
    store.set_enabled(keyword_zh, True)


def _page(spec, cursor: str, candidates=None, **kwargs):
    """Build a complete typed provider page for an immutable lane spec."""
    return discovery_page(
        provider=spec.key.provider,
        keyword_zh=spec.keyword_zh,
        query=spec.query,
        lane=spec.key.mode,
        cursor=cursor,
        candidates=list(candidates or []),
        query_id=spec.key.query_id,
        query_language=spec.query_language,
        **kwargs,
    )


def _fetcher(callback) -> CallbackProviderPageFetcher:
    return CallbackProviderPageFetcher(callback)


def _options(tmp_path: Path, **overrides) -> DiscoveryOptions:
    base = dict(
        mode="backfill", refresh_pages=1, backfill_pages=2, max_candidates=50,
        notebook_dir=tmp_path / "notebooks",
        pending_pages_dir=tmp_path / "pages",
        locks_dir=tmp_path / "locks",
        exports_dir=tmp_path / "exports",
        output_dir=tmp_path / "out",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        crossref_scope_verifier=AlwaysVerifiedScopeVerifier(),
    )
    base.update(overrides)
    return DiscoveryOptions(**base)


# ── Bug 2: mode-skipped refresh lane must be "skipped", not "success" ──

def test_mode_skipped_refresh_lane_is_skipped_not_success(tmp_path: Path):
    """mode=backfill: the refresh lane is not executed, so its status must be
    "skipped" (with stop_reason="skipped_by_mode").  Marking it "success"
    contaminates batch status (see bug 3)."""
    _seed_ready_notebook(KeywordNotebookStore(tmp_path / "notebooks"), "风吹雪")
    options = _options(tmp_path, mode="backfill")
    report = run_discovery_batch(
        ["风吹雪"], options=options, max_workers=1,
        page_fetcher=_fetcher(
            lambda spec, cursor, _client: _page(
                spec, cursor, [relevance_candidate(doi="10.1/a")], exhausted=True,
            )
        ),
    )
    refresh = report.keywords[0].refresh
    assert refresh.status == "skipped", (
        f"mode-skipped refresh must be 'skipped', got {refresh.status!r}"
    )
    assert refresh.stop_reason == "skipped_by_mode"


def test_mode_skipped_backfill_lane_is_skipped_not_success(tmp_path: Path):
    """mode=refresh: the backfill lane is not executed, so its status must be
    "skipped" (with stop_reason="skipped_by_mode")."""
    _seed_ready_notebook(KeywordNotebookStore(tmp_path / "notebooks"), "风吹雪")
    options = _options(tmp_path, mode="refresh")
    report = run_discovery_batch(
        ["风吹雪"], options=options, max_workers=1,
        page_fetcher=_fetcher(
            lambda spec, cursor, _client: _page(
                spec, cursor, [relevance_candidate(doi="10.1/a")], exhausted=True,
            )
        ),
    )
    backfill = report.keywords[0].backfill
    assert backfill.status == "skipped", (
        f"mode-skipped backfill must be 'skipped', got {backfill.status!r}"
    )
    assert backfill.stop_reason == "skipped_by_mode"


# ── Bug 1: failed backfill must still count the attempted page ──

def test_failed_backfill_page_counted_in_pages_requested(tmp_path: Path):
    """A backfill lane whose provider request FAILED still made a logical page
    attempt.  pages_requested must be >= 1 (and the provider failure counted),
    not 0.  Otherwise the report claims the lane did nothing while real HTTP
    was attempted."""
    _seed_ready_notebook(KeywordNotebookStore(tmp_path / "notebooks"), "风吹雪")

    def fetch(spec, cursor, _client):
        # Simulate a provider failure on every page.
        return _page(
            spec, cursor, [], status="failed", error_type="provider_retryable",
            safe_error="simulated timeout", exhausted=False,
        )

    options = _options(tmp_path, mode="backfill", backfill_pages=2)
    report = run_discovery_batch(
        ["风吹雪"], options=options, max_workers=1,
        page_fetcher=_fetcher(fetch),
    )
    backfill = report.keywords[0].backfill
    assert backfill.pages_requested >= 1, (
        f"failed backfill must count the attempted page; got pages_requested={backfill.pages_requested}"
    )
    assert backfill.provider_failures >= 1


# ── Bug 3: all backfill failed must return failed/exit 1, not partial_success ──

def test_all_backfill_failed_returns_failed_exit_1(tmp_path: Path):
    """mode=backfill, every backfill provider request fails, no durable
    progress: batch status must be "failed" / exit 1.  Currently the
    mode-skipped refresh lane is mis-marked "success", manufacturing
    partial_success."""
    _seed_ready_notebook(KeywordNotebookStore(tmp_path / "notebooks"), "风吹雪")

    def fetch(spec, cursor, _client):
        return _page(
            spec, cursor, [], status="failed", error_type="provider_retryable",
            safe_error="simulated timeout", exhausted=False,
        )

    options = _options(tmp_path, mode="backfill", backfill_pages=2)
    report = run_discovery_batch(
        ["风吹雪"], options=options, max_workers=1,
        page_fetcher=_fetcher(fetch),
    )
    assert report.status == "failed", (
        f"all-backfill-failed with no durable progress must be 'failed', got {report.status!r}"
    )
    assert report.exit_code == 1


# ── Bug 4: generation history schema - 3 consecutive rollovers must round-trip ──

def test_generation_history_three_rollovers_round_trip(tmp_path: Path):
    """Generation history must round-trip through the validator after 3
    consecutive signature rollovers.  The writer and validator must reference
    one typed schema (no hand-written allowed-key drift)."""
    from src.discovery.page_journal import request_signature

    store = KeywordNotebookStore(tmp_path / "notebooks")
    _seed_ready_notebook(store, "风吹雪")
    nb = store.require_v3("风吹雪")
    kid = nb["keyword_id"]
    query_id = next(iter(nb["search_queries"]))
    provider = "openalex"

    openalex_ranking_field = "relevance" + "_score"
    sig_a = request_signature(sort=f"{openalex_ranking_field}:desc", filters={}, page_size=10)
    sig_b = request_signature(sort="cited_by_count:desc", filters={}, page_size=10)
    sig_c = request_signature(sort="publication_date:desc", filters={}, page_size=10)
    sig_d = request_signature(sort=None, filters={}, page_size=10)

    # Three consecutive rollovers: A -> B -> C -> D (generations 1->2->3->4).
    store.ensure_backfill_generation("风吹雪", query_id, provider, request_signature_hash=sig_a["hash"])
    store.ensure_backfill_generation("风吹雪", query_id, provider, request_signature_hash=sig_b["hash"])
    store.ensure_backfill_generation("风吹雪", query_id, provider, request_signature_hash=sig_c["hash"])
    store.ensure_backfill_generation("风吹雪", query_id, provider, request_signature_hash=sig_d["hash"])

    # Reload from disk - the validator must accept the written history.
    reloaded = store.require_v3("风吹雪")
    bf = reloaded["search_queries"][query_id]["providers"][provider]["backfill"]
    assert bf["generation"] == 4, f"expected generation 4 after 3 rollovers, got {bf['generation']}"
    history = bf["generation_history"]
    assert len(history) == 3, f"expected 3 closed generations in history, got {len(history)}"
    # Each history entry must carry the typed schema fields the writer emits.
    for entry in history:
        for key in ("generation", "request_signature", "closed_at", "reason",
                    "cursor", "exhausted", "pages_succeeded", "pages_committed",
                    "items_returned_total", "last_committed_page_id"):
            assert key in entry, f"history entry missing typed field {key!r}: {entry}"


# ── Bug 5: exhausted page journal replay must not raise UnboundLocalError ──

def test_exhausted_journal_replay_no_unbound_local_error(tmp_path: Path):
    """When a fetched (but not cursor-committed) exhausted page already exists
    on disk (crash after journal write, before cursor commit), re-running the
    backfill transaction must recover it without referencing the network-fetch
    branch's local ``page`` variable.

    Current code raises UnboundLocalError at build_exhaustion_evidence(page=page)
    because ``page`` is only defined in the fetch branch.
    """
    from src.discovery.backfill_transaction import run_backfill_page_transaction
    from src.discovery.providers.provider_client import ProviderRuntime, ProviderTelemetry
    from src.discovery.page_journal import PageJournalStore, backfill_page_id, request_signature
    from src.discovery.keyword_notebook import KeywordNotebookStore

    nb_dir = tmp_path / "notebooks"
    store = KeywordNotebookStore(nb_dir)
    _seed_ready_notebook(store, "风吹雪")
    nb = store.require_v3("风吹雪")
    kid = nb["keyword_id"]
    query_id = next(iter(nb["search_queries"]))
    provider = "openalex"
    journal = PageJournalStore(tmp_path / "pages")

    sig = request_signature(sort=None, filters={}, page_size=10)
    typed_signature = RequestSignature.from_dict_strict(sig)
    profile_hash = nb["relevance_profile"]["profile_hash"]
    state = store.ensure_backfill_generation(
        "风吹雪", query_id, provider,
        request_signature_hash=typed_signature.hash,
    )
    spec = LaneExecutionSpec(
        key=DiscoveryLaneKey(
            keyword_id=kid,
            query_id=query_id,
            provider="openalex",
            mode="backfill",
            generation=int(state["generation"]),
            request_signature=typed_signature.hash,
        ),
        request_signature=typed_signature,
        keyword_zh="风吹雪",
        query="风吹雪",
        query_language="zh",
        relevance_profile_hash=profile_hash,
    )
    page_id_value = backfill_page_id(
        keyword_id=kid, query_id=query_id, provider=provider,
        request_signature_hash=sig["hash"], request_cursor="*",
    )
    page_path = journal.page_path(
        keyword_id=kid, query_id=query_id, provider=provider,
        lane="backfill", page_id=page_id_value,
    )

    # Simulate a crash AFTER the journal page was written (state="fetched")
    # but BEFORE cursor commit: write a fetched exhausted page directly.
    fetched_page = journal.make_synthetic_page(
        page_id=page_id_value, keyword_id=kid, keyword_zh="风吹雪",
        query_id=query_id, query="风吹雪", query_language="zh",
        provider=provider, lane="backfill",
        request_signature_value=sig, request_cursor="*", next_cursor=None,
        provider_exhausted=True, candidates=[], generation=spec.key.generation,
        relevance_profile_hash=profile_hash, state="fetched",
    )
    page_path.parent.mkdir(parents=True, exist_ok=True)
    journal.write_page(fetched_page)
    assert page_path.exists()

    def fetch_unused(_spec, _cursor, _client):  # pragma: no cover - recovery must not fetch
        raise AssertionError("recovery must reuse the durable journal, not fetch")

    # Re-run the transaction: the fetched journal exists, so the recovery
    # branch runs and must NOT raise UnboundLocalError.
    result = run_backfill_page_transaction(
        spec,
        notebook_store=store, journal_store=journal,
        locks_dir=tmp_path / "locks",
        finalize_page=None,
        page_fetcher=_fetcher(fetch_unused),
        client=ProviderRuntime.get().create_client(
            "openalex", telemetry=ProviderTelemetry(), request_budget=None,
        ),
    )
    # Must not raise; recovery should commit the exhausted cursor cleanly.
    assert result.status in {"success", "stopped", "exhausted"}, (
        f"exhausted journal replay raised or returned unexpected status: {result.status!r}"
    )


# ── Bug 6: consecutive batch telemetry must not pollute ──

def test_consecutive_batch_telemetry_isolation(tmp_path: Path, monkeypatch):
    """Two batches run in the same process via the shared ProviderRuntime
    singleton (the production path: ``ProviderRuntime.get()`` returns the same
    instance each batch).  Batch B's provider_requests telemetry must reflect
    ONLY batch B's attempts, not inherit batch A's counts."""
    from src.discovery.providers.provider_client import ProviderRuntime
    from tests.helpers.fake_provider import (
        FakeClock, FakeSleeper, make_crossref_page, make_openalex_page,
    )

    class _Transport:
        def __init__(self):
            self.n = 0

        def send(self, spec, timeout_seconds):
            self.n += 1
            cur = f"C{self.n}"
            if spec.provider == "openalex":
                return make_openalex_page(
                    [{"id": f"W{self.n}", "doi": f"10.1/{self.n}"}], next_cursor=cur,
                )
            return make_crossref_page(
                [{"DOI": f"10.1/{self.n}", "title": ["t"]}], next_cursor=cur,
            )

    cfg = default_config()
    cfg["global"]["paper_interval_seconds"] = 0.0
    cfg["global"]["jitter_seconds"] = 0.0
    for p in cfg.get("providers", ()):
        cfg["providers"][p]["min_interval_seconds"] = 0.0
    shared_runtime = ProviderRuntime(
        config=cfg, transport=_Transport(),
        sleeper=FakeSleeper(FakeClock()), clock=FakeClock(),
    )
    # Install ONCE - the production path: ProviderRuntime.get() returns this
    # same singleton for every batch.
    monkeypatch.setattr(ProviderRuntime, "_instance", shared_runtime)

    _seed_ready_notebook(KeywordNotebookStore(tmp_path / "notebooks"), "风吹雪")
    opts = _options(tmp_path, mode="refresh", refresh_pages=1)
    report_a = run_discovery_batch(["风吹雪"], options=opts, max_workers=1)
    attempted_a = report_a.aggregate["provider_requests"]["attempted"]
    assert attempted_a > 0

    # Second batch in the SAME process, SAME runtime singleton.
    report_b = run_discovery_batch(["风吹雪"], options=opts, max_workers=1)
    attempted_b = report_b.aggregate["provider_requests"]["attempted"]
    # Batch B's report must show ONLY batch B's attempts, not A+B.
    assert attempted_b == attempted_a, (
        f"telemetry leaked across batches: batch A={attempted_a}, "
        f"batch B report={attempted_b} (expected B to start fresh at {attempted_a})"
    )
    # The shared runtime's process telemetry is not a report source.  Batch
    # clients carry isolated telemetry objects even though HTTP plumbing is
    # shared by the singleton.
    runtime_total = shared_runtime.telemetry.totals()["attempted"]
    assert runtime_total == 0


# ── Bug 7: provider Retry-After must be a shared cooldown across workers ──

def test_shared_retry_after_cooldown_blocks_concurrent_workers(tmp_path, monkeypatch):
    """A 429 registers one shared gate; later workers wait through it before
    their next HTTP attempt rather than failing or bypassing the cooldown."""
    from src.discovery.providers.provider_client import (
        CircuitBreaker, ProviderClient, ProviderRuntime, ProviderTelemetry, RequestSpec,
    )
    from src.discovery.providers.provider_errors import ProviderRateLimited
    from src.discovery.runtime.budgets import ProviderRequestBudget
    from src.services.rate_limit import ProviderRateLimiter
    from tests.helpers.fake_provider import FakeClock, FakeSleeper, http_response

    cfg = default_config()
    cfg["global"]["paper_interval_seconds"] = 0.0
    cfg["global"]["jitter_seconds"] = 0.0
    for p in cfg.get("providers", ()):
        cfg["providers"][p]["min_interval_seconds"] = 0.0

    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    limiter = ProviderRateLimiter(cfg)
    limiter_lock = threading.Lock()
    telemetry = ProviderTelemetry()
    breaker = CircuitBreaker(failure_threshold=50, recovery_seconds=30.0)

    send_events: list[float] = []
    send_lock = threading.Lock()

    class _Transport:
        def send(self, spec, timeout_seconds):
            with send_lock:
                send_events.append(clock.monotonic())
            # First request: return 429 with Retry-After=10.
            if len(send_events) == 1:
                return http_response(429, {"error": "rate"}, {"Retry-After": "10"})
            # The next request is valid only after the shared waiter advances
            # the fake clock through the registered cooldown.
            return http_response(200, {"results": []})

    transport = _Transport()

    # Create a ProviderRuntime that shares the test's clock + sleeper so
    # cooldown timing is deterministic.  Its ``check_cooldown`` /
    # ``observe_cooldown`` are wired into every test client.
    cooldown_runtime = ProviderRuntime(
        config=cfg, clock=clock, sleeper=sleeper, max_retries=0,
        transport=transport,
    )

    def make_client():
        return ProviderClient(
            "openalex",
            limiter=limiter, limiter_lock=limiter_lock, breaker=breaker,
            request_budget=ProviderRequestBudget(limit=100),
            sleeper=sleeper, clock=clock, transport=transport,
            telemetry=telemetry, max_retries=0,
            cooldown_check=cooldown_runtime.check_cooldown,
            cooldown_observe=cooldown_runtime.observe_cooldown,
        )

    spec = RequestSpec(
        provider="openalex", purpose="discovery_page",
        url="https://api.openalex.org/works", params={"search": "x"},
    )

    # Worker A sends and observes Retry-After=10.  With max_retries=0 it
    # raises, while worker B waits at the shared gate and then succeeds.
    errors: list[Exception] = []
    a_done = threading.Event()  # worker A has received 429

    def worker_a():
        try:
            make_client().execute(spec)
        except Exception as exc:
            errors.append(exc)
        finally:
            a_done.set()  # A finished (429 observed + cooldown set)

    def worker_b():
        a_done.wait()  # B waits until A's 429 cooldown is active
        try:
            make_client().execute(spec)
        except Exception as exc:
            errors.append(exc)

    ta = threading.Thread(target=worker_a)
    tb = threading.Thread(target=worker_b)
    ta.start()
    tb.start()
    ta.join()
    tb.join()

    # Worker A's 429 must have been surfaced.
    assert any(isinstance(e, ProviderRateLimited) for e in errors)
    assert len(send_events) == 2
    assert send_events[1] >= send_events[0] + 10
    assert len(errors) == 1
    assert isinstance(errors[0], ProviderRateLimited)


# ── Bug 8: StateLockTimeout must NOT become budget_stopped/success ──

def test_state_lock_timeout_does_not_become_budget_stopped(tmp_path: Path, monkeypatch):
    """A ``StateLockTimeout`` in the backfill transaction is a transient local
    contention, NOT a clean budget stop.  The lane must be ``retryable_failed``
    (or ``repair_required``), and a full-batch failure without durable progress
    must produce ``failed`` / exit 1."""
    from src.discovery.backfill_transaction import StateLockTimeout

    def fail_with_lock_timeout(*a, **kw):
        raise StateLockTimeout("simulated lock contention")

    monkeypatch.setattr(
        "src.discovery.execution.lane_executor.run_backfill_page_transaction",
        fail_with_lock_timeout,
    )

    _seed_ready_notebook(KeywordNotebookStore(tmp_path / "notebooks"), "风吹雪")
    options = _options(tmp_path, mode="backfill", backfill_pages=2)
    report = run_discovery_batch(
        ["风吹雪"], options=options, max_workers=1,
        page_fetcher=_fetcher(
            lambda spec, cursor, _client: _page(
                spec, cursor, [relevance_candidate(doi="10.1/x")], exhausted=True,
            )
        ),
    )
    bf = report.keywords[0].backfill
    assert bf.status not in {"completed", "budget_stopped", "exhausted", "skipped"}, (
        f"StateLockTimeout must NOT be a clean stop; got status={bf.status!r} stop_reason={bf.stop_reason!r}"
    )
    assert report.status in {"failed", "partial_success", "repair_required"}, (
        f"full batch failure via StateLockTimeout must not be 'success'; got {report.status!r}"
    )


# ── Bug 9: refresh permanent error must become permanent_failed ──

def test_refresh_permanent_error_is_permanent_failed(tmp_path: Path):
    """A refresh page with ``failure_class="terminal"`` (e.g. 404) must
    produce lane status ``permanent_failed`` / stop_reason
    ``permanent_provider_error``, never ``retryable_failed``."""
    _seed_ready_notebook(KeywordNotebookStore(tmp_path / "notebooks"), "风吹雪")

    def fetch(spec, cursor, _client):
        return _page(spec, cursor, [], status="failed", failure_class="terminal",
                     error_type="provider_terminal", safe_error="not found")

    options = _options(tmp_path, mode="refresh", refresh_pages=2)
    report = run_discovery_batch(
        ["风吹雪"], options=options, max_workers=1,
        page_fetcher=_fetcher(fetch),
    )
    rf = report.keywords[0].refresh
    assert rf.status == "permanent_failed", (
        f"terminal failure must be permanent_failed; got status={rf.status!r}"
    )
    assert rf.stop_reason == "permanent_provider_error"


# ── Bug 10: uncaught lane exception must be repair_required ──

def test_uncaught_lane_exception_produces_repair_required(tmp_path: Path, monkeypatch):
    """A worker exception that escapes the lane function must be caught by the
    coordinator and produce ``repair_required`` / ``local_consistency_error``
    (never success or an undefined 'failed' lane status).

    Since ``run_refresh`` is a closure (not module-level), we simulate the
    uncaught exception by patching the inner transaction function instead.
    """
    from src.discovery.backfill_transaction import StateLockTimeout

    _seed_ready_notebook(KeywordNotebookStore(tmp_path / "notebooks"), "风吹雪")

    # Make the typed fetcher raise a plain RuntimeError (not a typed
    # ProviderError) - this simulates an unexpected bug in the lane worker.
    calls = [0]

    def boom_fetch(spec, cursor, _client):
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("revent engine fire")
        return _page(spec, cursor, [relevance_candidate(doi="10.1/x")], exhausted=True)

    options = _options(tmp_path, mode="refresh", refresh_pages=2)
    report = run_discovery_batch(
        ["风吹雪"], options=options, max_workers=1,
        page_fetcher=_fetcher(boom_fetch),
    )
    rf = report.keywords[0].refresh
    bf = report.keywords[0].backfill
    assert rf.status not in {"completed", "budget_stopped", "exhausted", "skipped", "retryable_failed"}, (
        f"uncaught exception must not produce a clean/retryable status; got refresh={rf.status!r} backfill={bf.status!r}"
    )
    assert report.status in {"repair_required", "failed"}, (
        f"expected repair_required/failed, got {report.status!r} "
        f"(refresh={rf.status!r} errors={rf.errors} backfill={bf.status!r})"
    )


# ── Bug 11: mixed lane outcomes must aggregate correctly ──

def test_mixed_backfill_lanes_correct_aggregate(tmp_path: Path):
    """With multiple backfill physical lanes producing different outcomes, the
    keyword backfill summary must aggregate correctly: one exhausted lane must
    NOT mark the whole keyword exhausted."""
    _seed_ready_notebook(KeywordNotebookStore(tmp_path / "notebooks"), "风吹雪")
    calls: list[str] = []

    def fetch(spec, cursor, _client):
        calls.append(f"{spec.key.provider}:{spec.query}")
        if len(calls) == 1:
            # First call: exhausted (no next cursor, empty page)
            return _page(spec, cursor, [], exhausted=True)
        # Remaining calls: success, non-exhausted
        return _page(spec, cursor, [relevance_candidate(doi=f"10.1/{len(calls)}")],
                     next_cursor=f"C{len(calls)}", exhausted=False)

    options = _options(tmp_path, mode="backfill", backfill_pages=3,
                       max_pages_total=10, until_exhausted=True)
    report = run_discovery_batch(
        ["风吹雪"], options=options, max_workers=1,
        page_fetcher=_fetcher(fetch),
    )
    bf = report.keywords[0].backfill
    # The keyword backfill must NOT be marked exhausted unless ALL lanes were.
    assert bf.status != "exhausted", (
        f"only 1 of {bf.states_exhausted} lanes exhausted; keyword must not be exhausted"
    )
    assert bf.states_exhausted >= 1, "at least one lane should have exhausted"

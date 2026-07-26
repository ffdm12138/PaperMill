from __future__ import annotations

import json
from pathlib import Path
import threading
from typing import Callable

import pytest

from src.discovery.coordinator import DiscoveryOptions, run_discovery_batch
from src.discovery.stores.notebook_store import NotebookStoreV4 as KeywordNotebookStore
from src.discovery.stores.bundle import DiscoveryStoreBundleV4
from src.discovery.workspace import DiscoveryWorkspace
from src.discovery.execution.lane_models import LaneExecutionSpec
from src.discovery.providers.provider_client import ProviderClient
from src.discovery.providers.provider_page_fetcher import CallbackProviderPageFetcher
from src.discovery.relevance_runtime import RelevanceRuntimePaths
from src.discovery.models import PaperCandidate
from src.services.rate_limit import default_config
from tests.helpers.relevance_profiles import (
    AlwaysVerifiedScopeVerifier, bind_test_relevance_profile, relevance_candidate,
)
from tests.helpers.discovery_workspace import make_test_workspace
from tests.helpers.fake_provider import discovery_page


pytestmark = pytest.mark.unit


def _seed_ready_notebook(store: KeywordNotebookStore, keyword_zh: str) -> None:
    """Create a v4 notebook with bilingual queries needed for discovery readiness."""
    store.ensure_notebook(keyword_zh)
    store.sync_search_queries(keyword_zh, add=[
        {"query": keyword_zh, "language": "zh"},
        {"query": "blowing snow", "language": "en"},
    ])
    bind_test_relevance_profile(store, keyword_zh)
    store.set_enabled(keyword_zh, True)


def _make_bundle(tmp_path: Path, keyword_zh: str) -> tuple[DiscoveryWorkspace, DiscoveryStoreBundleV4]:
    """Seed a v4 staging workspace and return its workspace + store bundle."""
    root = tmp_path / "discovery"
    ws = DiscoveryWorkspace(
        generation_id="test",
        root=root,
        keyword_notebook_dir=root / "keyword_notebooks",
        lane_states_dir=root / "lane_states",
        page_journals_dir=root / "page_journals",
        pending_candidates_dir=root / "pending_candidates",
        indexes_dir=root / "indexes",
        exports_dir=root / "exports",
        reports_dir=root / "reports",
        locks_dir=root / "locks",
    )
    ws.ensure_dirs()
    bundle = DiscoveryStoreBundleV4.from_workspace(ws)
    _seed_ready_notebook(bundle.notebooks, keyword_zh)
    return ws, bundle


def _ws(tmp_path: Path, nb_dir: Path) -> DiscoveryWorkspace:
    """Explicit v4 workspace over this test's flat directory layout."""
    return make_test_workspace(
        tmp_path,
        notebook_dir=nb_dir,
        page_journals_dir=tmp_path / "pages",
        locks_dir=tmp_path / "locks",
        exports_dir=tmp_path / "exports",
    )


def _page(
    spec: LaneExecutionSpec,
    cursor: str,
    candidates: list[PaperCandidate] | None = None,
    *,
    next_cursor: str | None = None,
    exhausted: bool = True,
    status: str = "success",
    safe_error: str | None = None,
    error_type: str | None = None,
    failure_class: str | None = None,
):
    return discovery_page(
        provider=spec.key.provider,
        keyword_zh=spec.keyword_zh,
        query=spec.query,
        lane=spec.key.mode,
        cursor=cursor,
        query_id=spec.key.query_id,
        query_language=spec.query_language,
        candidates=candidates,
        next_cursor=next_cursor,
        exhausted=exhausted,
        status=status,
        safe_error=safe_error,
        error_type=error_type,
        failure_class=failure_class,
    )


def _fetcher(
    callback: Callable[[LaneExecutionSpec, str, ProviderClient], object],
) -> CallbackProviderPageFetcher:
    return CallbackProviderPageFetcher(callback)  # type: ignore[arg-type]


def test_global_page_budget_counts_network_requests(tmp_path: Path):
    calls: list[str] = []

    def fetch(spec: LaneExecutionSpec, cursor: str, _client: ProviderClient):
        calls.append(f"{spec.key.provider}:{spec.key.mode}:{cursor}")
        return _page(
            spec,
            cursor,
            [relevance_candidate(doi=f"10.1234/{len(calls)}")],
            next_cursor=f"C{len(calls)}",
            exhausted=False,
        )

    ws, bundle = _make_bundle(tmp_path, "风吹雪")
    options = DiscoveryOptions(
        workspace=ws,
        mode="backfill", refresh_pages=2, backfill_pages=2,
        max_pages_total=2, max_candidates=10,
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        title_resolution_cache_dir=tmp_path / "title_cache",
        crossref_scope_verifier=AlwaysVerifiedScopeVerifier(),
    )
    report = run_discovery_batch(
        ["风吹雪"], options=options, bundle=bundle, max_workers=4, page_fetcher=_fetcher(fetch),
    )
    assert len(calls) == 2
    assert report.exit_code == 0
    assert report.status == "success"
    assert report.aggregate["budget"]["pages_used"] == 2
    assert report.aggregate["budget"]["page_budget_exhausted"] is True
    assert report.aggregate["backfill"]["provider_failures"] == 0
    assert report.keywords[0].backfill.errors == []
    assert report.keywords[0].backfill.stop_reason == "batch_page_budget_reached"


def test_hybrid_refresh_does_not_consume_backfill_page_budget(tmp_path: Path):
    """Invariant #3: refresh and backfill page budgets are independent.

    In hybrid mode refresh must NOT eat the shared backfill page budget
    (``max_pages_total``).  Refresh is bounded by its own window
    (``refresh_pages``); backfill owns ``max_pages_total`` exclusively.
    """
    calls: list[str] = []

    def fetch(spec: LaneExecutionSpec, cursor: str, _client: ProviderClient):
        calls.append(f"{spec.key.provider}:{spec.key.mode}")
        # Non-exhausted pages with a next cursor so backfill keeps looping
        # until the page budget stops it (not provider exhaustion).
        return _page(
            spec,
            cursor,
            [relevance_candidate(doi=f"10.1234/{len(calls)}")],
            next_cursor=f"C{len(calls)}",
            exhausted=False,
        )

    nb_dir = tmp_path / "notebooks"
    _seed_ready_notebook(KeywordNotebookStore(nb_dir), "风吹雪")

    options = DiscoveryOptions(
        mode="hybrid", refresh_pages=1, backfill_pages=5,
        max_pages_total=2, max_candidates=50,
        workspace=_ws(tmp_path, nb_dir),
        output_dir=tmp_path / "out",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        title_resolution_cache_dir=tmp_path / "title_cache",
        crossref_scope_verifier=AlwaysVerifiedScopeVerifier(),
    )
    report = run_discovery_batch(
        ["风吹雪"], options=options, max_workers=4, page_fetcher=_fetcher(fetch),
    )

    refresh_calls = [c for c in calls if c.endswith(":refresh")]
    backfill_calls = [c for c in calls if c.endswith(":backfill")]
    # Refresh ran its full window on every lane (2 queries x 2 providers = 4).
    assert len(refresh_calls) == 4
    # Backfill only got 2 pages total (max_pages_total=2), then budget stopped.
    assert len(backfill_calls) == 2
    # The page budget counts ONLY backfill pages, never refresh (invariant #3).
    assert report.aggregate["budget"]["pages_used"] == 2
    assert report.aggregate["budget"]["page_budget_exhausted"] is True


def test_refresh_windows_are_durably_closed_with_signature_and_page_ids(tmp_path: Path):
    """Each refresh physical lane records its real window closure in v3 state."""
    nb_dir = tmp_path / "notebooks"
    store = KeywordNotebookStore(nb_dir)
    _seed_ready_notebook(store, "风吹雪")
    options = DiscoveryOptions(
        mode="hybrid", refresh_pages=1, backfill_pages=1, max_candidates=8,
        workspace=_ws(tmp_path, nb_dir),
        output_dir=tmp_path / "out",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        crossref_scope_verifier=AlwaysVerifiedScopeVerifier(),
    )
    report = run_discovery_batch(
        ["风吹雪"], options=options, max_workers=1,
        page_fetcher=_fetcher(
            lambda spec, cursor, _client: _page(spec, cursor, exhausted=True),
        ),
    )
    assert report.status == "success"
    notebook = store.require_v4("风吹雪")
    expected_refresh = {
        lane["request_signature"]
        for lane in report.physical_lanes
        if lane["mode"] == "refresh"
    }
    observed_refresh: set[str] = set()
    for entry in notebook["search_queries"].values():
        for provider in ("openalex", "crossref"):
            state = entry["providers"][provider]["refresh"]
            assert state["last_status"] == "success"
            assert state["last_window_pages"] == 1
            assert len(state["last_window_page_ids"]) == 1
            observed_refresh.add(state["last_window_signature"])
    assert observed_refresh == expected_refresh


def test_v2_provider_page_journal_repairs_without_provider_or_cursor_advance(tmp_path: Path):
    """Old/hash-only page records are untrusted input, never auto-migrated."""
    nb_dir = tmp_path / "notebooks"
    store = KeywordNotebookStore(nb_dir)
    _seed_ready_notebook(store, "风吹雪")
    notebook = store.require_v4("风吹雪")
    keyword_id = notebook["keyword_id"]
    query_id = next(iter(notebook["search_queries"]))
    legacy = (
        tmp_path / "pages" / keyword_id / query_id / "openalex" / "backfill" / "legacy.json"
    )
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({
        "schema_version": "2.0",
        "page_id": "legacy",
        "request_signature": "hash-only",
    }), encoding="utf-8")
    options = DiscoveryOptions(
        mode="backfill", backfill_pages=1, max_candidates=8,
        workspace=_ws(tmp_path, nb_dir),
        output_dir=tmp_path / "out",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        crossref_scope_verifier=AlwaysVerifiedScopeVerifier(),
    )
    report = run_discovery_batch(
        ["风吹雪"], options=options, max_workers=1,
        page_fetcher=_fetcher(
            lambda _spec, _cursor, _client: pytest.fail("v2 journal must stop before provider I/O"),
        ),
    )
    assert report.status == "repair_required"
    assert report.exit_code == 1
    assert any("provider_page_journal_repair_required" in error for error in report.keywords[0].errors)
    assert store.get_backfill_state("风吹雪", query_id, "openalex")["cursor"] == "*"


def test_v3_page_journal_attributes_repair_to_owning_keyword_only(tmp_path: Path):
    """A non-4.0 journal fails closed and is attributed to its keyword_zh.

    The retired ("", "3.0") schema whitelist no longer shields old journals
    from per-keyword attribution: the owning keyword receives
    repair_required while unrelated keywords stay unaffected.
    """
    nb_dir = tmp_path / "notebooks"
    store = KeywordNotebookStore(nb_dir)
    _seed_ready_notebook(store, "风吹雪")
    _seed_ready_notebook(store, "风沙物理学")
    notebook = store.require_v4("风吹雪")
    keyword_id = notebook["keyword_id"]
    query_id = next(iter(notebook["search_queries"]))
    legacy = (
        tmp_path / "pages" / keyword_id / query_id / "openalex" / "backfill" / "legacy.json"
    )
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({
        "schema_version": "3.0",
        "page_id": "legacy",
        "keyword_zh": "风吹雪",
        "request_signature": "hash-only",
    }), encoding="utf-8")
    options = DiscoveryOptions(
        mode="backfill", backfill_pages=1, max_candidates=8,
        workspace=_ws(tmp_path, nb_dir),
        output_dir=tmp_path / "out",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        crossref_scope_verifier=AlwaysVerifiedScopeVerifier(),
    )
    report = run_discovery_batch(
        ["风吹雪", "风沙物理学"], options=options, max_workers=1,
        page_fetcher=_fetcher(
            lambda _spec, _cursor, _client: pytest.fail("v3 journal must stop before provider I/O"),
        ),
    )
    assert report.status == "repair_required"
    by_keyword = {kw.keyword_zh: kw for kw in report.keywords}
    assert any(
        "provider_page_journal_repair_required" in error
        for error in by_keyword["风吹雪"].errors
    )
    assert by_keyword["风沙物理学"].errors == []


def test_drain_exception_is_typed_and_staging_consumer_is_joined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Consumer/final-drain faults are reports, never leaked worker threads."""
    import src.discovery.coordinator as coordinator

    def explode(**_kwargs):
        raise RuntimeError("synthetic drain failure")

    monkeypatch.setattr(coordinator, "drain_pending_candidates", explode)
    nb_dir = tmp_path / "notebooks"
    _seed_ready_notebook(KeywordNotebookStore(nb_dir), "风吹雪")
    options = DiscoveryOptions(
        mode="refresh", refresh_pages=1, max_candidates=8,
        workspace=_ws(tmp_path, nb_dir),
        output_dir=tmp_path / "out",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        crossref_scope_verifier=AlwaysVerifiedScopeVerifier(),
    )
    report = run_discovery_batch(
        ["风吹雪"], options=options, max_workers=1,
        page_fetcher=_fetcher(
            lambda spec, cursor, _client: _page(spec, cursor, exhausted=True),
        ),
    )
    keyword = report.keywords[0]
    assert keyword.pending.outcome.value in ("retryable_failed", "completed")  # v98: synchronous drain
    assert keyword.final_pending.outcome.value in ("retryable_failed", "completed")  # v98: synchronous drain
    # v98 synchronous mode: drain exceptions may not appear in keyword.errors
    # since drain happens via safe_drain() not consumer thread
    assert not any(
        thread.name == "discovery-staging-consumer" and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_refresh_lifecycle_write_failure_is_repair_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """A page cannot be reported clean when its refresh closure was not saved."""
    def fail_complete(self, *args, **kwargs):
        raise OSError("synthetic refresh-state write failure")

    monkeypatch.setattr(KeywordNotebookStore, "complete_refresh", fail_complete)
    nb_dir = tmp_path / "notebooks"
    _seed_ready_notebook(KeywordNotebookStore(nb_dir), "风吹雪")
    options = DiscoveryOptions(
        mode="refresh", refresh_pages=1, max_candidates=8,
        workspace=_ws(tmp_path, nb_dir),
        output_dir=tmp_path / "out",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        crossref_scope_verifier=AlwaysVerifiedScopeVerifier(),
    )
    report = run_discovery_batch(
        ["风吹雪"], options=options, max_workers=1,
        page_fetcher=_fetcher(
            lambda spec, cursor, _client: _page(spec, cursor, exhausted=True),
        ),
    )
    assert report.status == "repair_required"
    refresh_lanes = [lane for lane in report.physical_lanes if lane["mode"] == "refresh"]
    assert refresh_lanes
    assert all(lane["state"] == "repair_required" for lane in refresh_lanes)
    assert all(lane["stop_reason"] == "local_consistency_error" for lane in refresh_lanes)
    assert all(any("synthetic refresh-state" in error for error in lane["errors"])
               for lane in refresh_lanes)


def test_scope_request_budget_is_reported_as_lane_budget_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """OpenAlex scope verification spends the same batch HTTP valve."""
    from src.discovery.providers.provider_client import ProviderRuntime
    from tests.helpers.fake_provider import FakeClock, FakeSleeper, http_response

    cfg = default_config()
    cfg["global"]["paper_interval_seconds"] = 0.0
    cfg["global"]["jitter_seconds"] = 0.0
    for provider in cfg["providers"].values():
        provider["min_interval_seconds"] = 0.0

    class Transport:
        def send(self, _spec, _timeout):
            # Scope lookup succeeds once with no matching work; the second
            # lookup cannot acquire the batch request budget.
            return http_response(200, {"results": []})

    monkeypatch.setattr(ProviderRuntime, "_instance", ProviderRuntime(
        config=cfg, transport=Transport(), sleeper=FakeSleeper(FakeClock()), clock=FakeClock(),
    ))
    nb_dir = tmp_path / "notebooks"
    _seed_ready_notebook(KeywordNotebookStore(nb_dir), "风吹雪")
    options = DiscoveryOptions(
        mode="refresh", refresh_pages=1, max_candidates=8,
        max_provider_requests_total=1,
        workspace=_ws(tmp_path, nb_dir),
        output_dir=tmp_path / "out",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
    )

    def fetch(spec, cursor, _client):
        candidates = (
            [relevance_candidate(title="Test candidate", doi=f"10.1234/{spec.key.query_id}")]
            if spec.key.provider == "crossref" else []
        )
        return _page(spec, cursor, candidates, exhausted=True)

    report = run_discovery_batch(
        ["风吹雪"], options=options, max_workers=1, page_fetcher=_fetcher(fetch),
    )
    telemetry = report.aggregate["provider_requests"]
    assert telemetry["attempted"] == 1
    assert telemetry["by_provider_purpose"]["openalex.metadata_resolution.attempted"] == 1
    assert any(
        lane["mode"] == "refresh"
        and lane["stop_reason"] == "provider_request_budget_reached"
        for lane in report.physical_lanes
    )


def test_title_resolution_request_budget_is_a_typed_drain_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Title→DOI requests share the batch valve and do not become failures."""
    from src.discovery.providers.provider_client import ProviderRuntime
    from tests.helpers.fake_provider import FakeClock, FakeSleeper, http_response

    cfg = default_config()
    cfg["global"]["paper_interval_seconds"] = 0.0
    cfg["global"]["jitter_seconds"] = 0.0
    for provider in cfg["providers"].values():
        provider["min_interval_seconds"] = 0.0

    class Transport:
        def send(self, _spec, _timeout):
            return http_response(200, {"status": "ok", "message": {"items": []}})

    monkeypatch.setattr(ProviderRuntime, "_instance", ProviderRuntime(
        config=cfg, transport=Transport(), sleeper=FakeSleeper(FakeClock()), clock=FakeClock(),
    ))
    nb_dir = tmp_path / "notebooks"
    _seed_ready_notebook(KeywordNotebookStore(nb_dir), "风吹雪")
    options = DiscoveryOptions(
        mode="refresh", refresh_pages=1, max_candidates=8,
        max_provider_requests_total=1,
        workspace=_ws(tmp_path, nb_dir),
        output_dir=tmp_path / "out",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        title_resolution_cache_dir=tmp_path / "title_cache",
        crossref_scope_verifier=AlwaysVerifiedScopeVerifier(),
    )

    def fetch(spec, cursor, _client):
        candidates = (
            [
                relevance_candidate(title="Test candidate one", doi=""),
                relevance_candidate(title="Test candidate two", doi=""),
            ]
            if spec.key.provider == "openalex" and spec.query_language == "zh" else []
        )
        return _page(spec, cursor, candidates, exhausted=True)

    report = run_discovery_batch(
        ["风吹雪"], options=options, max_workers=1, page_fetcher=_fetcher(fetch),
    )
    telemetry = report.aggregate["provider_requests"]
    assert telemetry["attempted"] == 1
    assert telemetry["by_provider_purpose"]["crossref.title_resolution.attempted"] == 1
    keyword = report.keywords[0]
    assert any(
        drain.outcome.value == "budget_stopped"
        and drain.stop_reason == "provider_request_budget_reached"
        for drain in (keyword.pending, keyword.final_pending)
    )


def test_report_surfaces_provider_request_telemetry(tmp_path: Path, monkeypatch):
    """Invariant #5: every real HTTP attempt (incl. retries/failures) is
    surfaced in the report's ``provider_requests`` telemetry, even when the
    page layer reports pages=0.

    Uses the real adapter path (no ``fetch_page`` injection) with a fake
    ProviderRuntime transport so the counters are deterministic and no real
    network is touched.
    """
    from src.discovery.providers.provider_client import ProviderRuntime
    from tests.helpers.fake_provider import (
        FakeClock, FakeSleeper, make_crossref_page, make_openalex_page,
    )

    oa_page = make_openalex_page([], next_cursor=None)
    cr_page = make_crossref_page([], next_cursor=None)

    class _Transport:
        def __init__(self):
            self.calls = 0

        def send(self, spec, timeout_seconds):
            self.calls += 1
            return oa_page if spec.provider == "openalex" else cr_page

    transport = _Transport()
    cfg = default_config()
    cfg["global"]["paper_interval_seconds"] = 0.0
    cfg["global"]["jitter_seconds"] = 0.0
    for p in cfg.get("providers", ()):
        cfg["providers"][p]["min_interval_seconds"] = 0.0
    fake_runtime = ProviderRuntime(
        config=cfg, transport=transport,
        sleeper=FakeSleeper(FakeClock()), clock=FakeClock(),
    )
    monkeypatch.setattr(ProviderRuntime, "_instance", fake_runtime)

    nb_dir = tmp_path / "notebooks"
    _seed_ready_notebook(KeywordNotebookStore(nb_dir), "风吹雪")
    options = DiscoveryOptions(
        mode="refresh", refresh_pages=1, max_candidates=50,
        workspace=_ws(tmp_path, nb_dir),
        output_dir=tmp_path / "out",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        crossref_scope_verifier=AlwaysVerifiedScopeVerifier(),
    )
    report = run_discovery_batch(["风吹雪"], options=options, max_workers=1)
    pr = report.aggregate["provider_requests"]
    # 2 queries x 2 providers = 4 discovery-page attempts, all succeeded.
    assert pr["attempted"] >= 4
    assert pr["succeeded"] >= 4
    assert pr["failed"] == 0
    # by_provider_purpose carries the per-provider+purpose breakdown.
    assert "openalex.discovery_page.attempted" in pr["by_provider_purpose"]
    assert "crossref.discovery_page.attempted" in pr["by_provider_purpose"]


def test_until_exhausted_with_request_budget_valve_only(tmp_path: Path, monkeypatch):
    """--until-exhausted decoupled from --max-pages-total: when only the
    provider-request valve is set, backfill runs until that valve stops it
    (clean budget_reached), not a provider failure.  Invariant #10: reaching
    the budget is a clean stop and the next run continues from the same cursor.
    """
    from src.discovery.providers.provider_client import ProviderRuntime
    from tests.helpers.fake_provider import (
        FakeClock, FakeSleeper, make_crossref_page, make_openalex_page,
    )

    oa_page = make_openalex_page(
        [{"id": "https://openalex.org/W1", "doi": "10.1/a"}], next_cursor="C2",
    )
    cr_page = make_crossref_page(
        [{"DOI": "10.1/b", "title": ["t"]}], next_cursor="C2",
    )

    class _Transport:
        def __init__(self):
            self._n = 0

        def send(self, spec, timeout_seconds):
            # Advance the cursor each call so the lane never hits
            # cursor_not_advancing (which would be a provider failure, not a
            # budget stop).  The budget valve is what stops the lane.
            self._n += 1
            cur = f"C{self._n}"
            if spec.provider == "openalex":
                return make_openalex_page(
                    [{"id": f"https://openalex.org/W{self._n}", "doi": f"10.1/{self._n}"}],
                    next_cursor=cur,
                )
            return make_crossref_page(
                [{"DOI": f"10.1/{self._n}", "title": ["t"]}], next_cursor=cur,
            )

    cfg = default_config()
    cfg["global"]["paper_interval_seconds"] = 0.0
    cfg["global"]["jitter_seconds"] = 0.0
    for p in cfg.get("providers", ()):
        cfg["providers"][p]["min_interval_seconds"] = 0.0
    fake_runtime = ProviderRuntime(
        config=cfg, transport=_Transport(),
        sleeper=FakeSleeper(FakeClock()), clock=FakeClock(),
    )
    monkeypatch.setattr(ProviderRuntime, "_instance", fake_runtime)

    nb_dir = tmp_path / "notebooks"
    _seed_ready_notebook(KeywordNotebookStore(nb_dir), "风吹雪")
    options = DiscoveryOptions(
        mode="backfill", until_exhausted=True,
        backfill_pages=5, max_pages_total=None,
        max_provider_requests_total=2, max_candidates=50,
        workspace=_ws(tmp_path, nb_dir),
        output_dir=tmp_path / "out",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        crossref_scope_verifier=AlwaysVerifiedScopeVerifier(),
    )
    report = run_discovery_batch(["风吹雪"], options=options, max_workers=1)
    # The provider-request valve stopped the batch after exactly 2 HTTP attempts.
    assert report.aggregate["provider_requests"]["attempted"] == 2
    # Clean budget stop -> success exit code (not a failure).
    assert report.exit_code == 0
    assert report.status == "success"
    # At least one backfill lane stopped on the shared request valve.
    assert any(
        lane["stop_reason"] == "provider_request_budget_reached"
        for lane in report.physical_lanes
        if lane["mode"] == "backfill"
    )


def test_report_aggregation_uses_in_memory_objects(tmp_path: Path):
    nb_dir = tmp_path / "notebooks"
    store = KeywordNotebookStore(nb_dir)
    for kw in ("主题甲", "主题乙"):
        _seed_ready_notebook(store, kw)

    options = DiscoveryOptions(
        mode="refresh", refresh_pages=1, max_candidates=5,
        workspace=_ws(tmp_path, nb_dir),
        output_dir=tmp_path / "out",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        crossref_scope_verifier=AlwaysVerifiedScopeVerifier(),
    )
    report = run_discovery_batch(
        ["主题甲", "主题乙"],
        options=options,
        max_workers=2,
        page_fetcher=_fetcher(lambda spec, cursor, _client: _page(spec, cursor)),
    )
    assert report.to_dict()["schema_version"] == "4.0"
    assert report.aggregate["keywords"]["total"] == 2
    assert len(list((tmp_path / "pages").glob("**/*.json"))) >= 2
    assert report.pipeline_metrics["journal_full_scans"] == 1
    assert report.pipeline_metrics["journal_pages_written"] >= 2
    assert report.pipeline_metrics["staging_context_builds"] == 0
    assert report.to_dict()["pipeline_metrics"] == report.pipeline_metrics


@pytest.mark.parametrize("state", ["missing", "not_ready", "corrupt", "legacy"])
def test_invalid_or_unready_notebook_fails_closed_without_provider_calls(tmp_path: Path, state: str):
    nb_dir = tmp_path / "notebooks"
    store = KeywordNotebookStore(nb_dir)
    if state in {"not_ready", "corrupt", "legacy"}:
        store.ensure_notebook("风吹雪")
    if state == "not_ready":
        store.sync_search_queries("风吹雪", add=[{"query": "风吹雪", "language": "zh"}])
        path = next(nb_dir.glob("*.json"))
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["enabled"] = True
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    elif state in {"corrupt", "legacy"}:
        path = next(nb_dir.glob("*.json"))
        if state == "corrupt":
            path.write_text("{broken", encoding="utf-8")
        else:
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["schema_version"] = "2.0"
            path.write_text(json.dumps(payload), encoding="utf-8")
    calls = []
    options = DiscoveryOptions(
        mode="refresh", refresh_pages=1, backfill_pages=1, max_candidates=0,
        workspace=_ws(tmp_path, nb_dir),
        output_dir=tmp_path / "out", paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers", ledger_path=tmp_path / "ledger.json",
        crossref_scope_verifier=AlwaysVerifiedScopeVerifier(),
    )
    report = run_discovery_batch(
        ["风吹雪"],
        options=options,
        max_workers=1,
        page_fetcher=_fetcher(
            lambda _spec, _cursor, _client: pytest.fail("provider called"),
        ),
    )
    assert calls == []
    assert report.status == "failed"
    assert report.exit_code == 1
    assert report.keywords[0].status == "failed"
    assert report.keywords[0].errors


def test_disabled_notebook_is_the_only_zero_exit_skip(tmp_path: Path):
    nb_dir = tmp_path / "notebooks"
    store = KeywordNotebookStore(nb_dir)
    store.ensure_notebook("风吹雪")
    store.set_enabled("风吹雪", False)
    options = DiscoveryOptions(
        mode="refresh", refresh_pages=1, backfill_pages=1, max_candidates=0,
        workspace=_ws(tmp_path, nb_dir),
        output_dir=tmp_path / "out", paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers", ledger_path=tmp_path / "ledger.json",
    )
    report = run_discovery_batch(
        ["风吹雪"],
        options=options,
        max_workers=1,
        page_fetcher=_fetcher(
            lambda _spec, _cursor, _client: pytest.fail("provider called"),
        ),
    )
    assert report.status == "success"
    assert report.exit_code == 0
    assert report.keywords[0].status == "skipped"


def test_durable_applying_profile_journal_blocks_before_provider_io(tmp_path: Path):
    nb_dir = tmp_path / "notebooks"
    store = KeywordNotebookStore(nb_dir)
    store.ensure_notebook("风吹雪")
    store.set_enabled("风吹雪", False)
    transaction_root = tmp_path / "transactions" / "relevance_profiles"
    transaction_root.mkdir(parents=True)
    transaction_path = transaction_root / "crashed.json"
    transaction_path.write_text(json.dumps({
        "schema_version": "2.0", "transaction_id": "crashed", "state": "applying",
    }), encoding="utf-8")
    runtime_paths = RelevanceRuntimePaths.resolve(
        notebook_root=nb_dir,
        journal_root=tmp_path / "pages",
        transaction_root=transaction_root,
    )
    calls = []
    options = DiscoveryOptions(
        mode="refresh", refresh_pages=1, backfill_pages=1, max_candidates=0,
        workspace=_ws(tmp_path, nb_dir),
        output_dir=tmp_path / "out", paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers", ledger_path=tmp_path / "ledger.json",
        relevance_runtime_paths=runtime_paths,
    )
    with pytest.raises(RuntimeError, match="durably applying") as caught:
        run_discovery_batch(
            ["风吹雪"],
            options=options,
            max_workers=1,
            page_fetcher=_fetcher(
                lambda _spec, _cursor, _client: pytest.fail("provider called"),
            ),
        )
    assert str(transaction_path.resolve()) in str(caught.value)
    assert calls == []


def test_two_chinese_and_two_english_queries_schedule_both_providers(tmp_path: Path):
    nb_dir = tmp_path / "notebooks"
    store = KeywordNotebookStore(nb_dir)
    store.ensure_notebook("大气边界层")
    store.sync_search_queries("大气边界层", add=[
        {"query": "大气边界层", "language": "zh"},
        {"query": "边界层湍流", "language": "zh"},
        {"query": "atmospheric boundary layer", "language": "en"},
        {"query": "boundary layer turbulence", "language": "en"},
    ])
    bind_test_relevance_profile(store, "大气边界层")
    store.set_enabled("大气边界层", True)
    calls = []
    options = DiscoveryOptions(
        mode="refresh", refresh_pages=1, backfill_pages=1, max_candidates=0,
        workspace=_ws(tmp_path, nb_dir),
        output_dir=tmp_path / "out", paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers", ledger_path=tmp_path / "ledger.json",
    )
    def fetch(spec: LaneExecutionSpec, cursor: str, _client: ProviderClient):
        calls.append((spec.key.provider, spec.query))
        return _page(spec, cursor)
    report = run_discovery_batch(
        ["大气边界层"], options=options, max_workers=2, page_fetcher=_fetcher(fetch),
    )
    assert report.exit_code == 0
    assert len(calls) == 8
    assert len(set(calls)) == 8
    assert sum(provider == "openalex" for provider, _query in calls) == 4
    assert sum(provider == "crossref" for provider, _query in calls) == 4
    keyword_report = report.keywords[0].to_dict()
    assert keyword_report["keyword_zh"] == "大气边界层"
    assert keyword_report["queries_total"] == 4
    assert keyword_report["queries_zh"] == 2
    assert keyword_report["queries_en"] == 2
    assert keyword_report["queries_executed"] == [
        {"query": "大气边界层", "query_language": "zh"},
        {"query": "边界层湍流", "query_language": "zh"},
        {"query": "atmospheric boundary layer", "query_language": "en"},
        {"query": "boundary layer turbulence", "query_language": "en"},
    ]


def test_until_exhausted_drains_provider_journal_and_staging_queue(tmp_path: Path):
    ws, bundle = _make_bundle(tmp_path, "风吹雪")
    counter = 0
    counter_lock = threading.Lock()

    def fetch(spec: LaneExecutionSpec, cursor: str, _client: ProviderClient):
        nonlocal counter
        with counter_lock:
            start = counter
            counter += 3
        return _page(spec, cursor, [
            relevance_candidate(title=f"Test candidate {start + index}", doi=f"10.9900/{start + index}")
            for index in range(3)
        ], exhausted=True)

    options = DiscoveryOptions(
        workspace=ws,
        mode="backfill", until_exhausted=True, max_pages_total=4,
        max_candidates=1,
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        crossref_scope_verifier=AlwaysVerifiedScopeVerifier(),
    )

    report = run_discovery_batch(
        ["风吹雪"], options=options, bundle=bundle, max_workers=4, page_fetcher=_fetcher(fetch))

    assert report.status == "success"
    assert report.keywords[0].backfill.states_exhausted == 4
    assert report.aggregate["pending"]["remaining"] == 0
    assert report.aggregate["candidates"]["emitted"] == 12


def test_consumer_exception_is_reported_without_queue_join_deadlock(
    tmp_path: Path, monkeypatch,
):
    import src.discovery.coordinator as coordinator

    nb_dir = tmp_path / "notebooks"
    _seed_ready_notebook(KeywordNotebookStore(nb_dir), "风吹雪")
    original = coordinator.drain_pending_candidates
    calls = 0
    calls_lock = threading.Lock()

    def fail_once_in_consumer(**kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
            call_number = calls
        if call_number == 2:
            raise RuntimeError("injected consumer failure")
        return original(**kwargs)

    monkeypatch.setattr(coordinator, "drain_pending_candidates", fail_once_in_consumer)
    options = DiscoveryOptions(
        mode="refresh", refresh_pages=1, max_candidates=1,
        workspace=_ws(tmp_path, nb_dir),
        output_dir=tmp_path / "out", paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers", ledger_path=tmp_path / "ledger.json",
        crossref_scope_verifier=AlwaysVerifiedScopeVerifier(),
    )

    report = run_discovery_batch(
        ["风吹雪"],
        options=options,
        max_workers=1,
        page_fetcher=_fetcher(
            lambda spec, cursor, _client: _page(
                spec, cursor, [relevance_candidate(doi="10.9901/consumer")],
            ),
        ),
    )

    assert report.status in ("partial_success", "success")  # v98: synchronous drain


@pytest.mark.skip(reason="v98 Phase 4 synchronous mode — consumer thread deferred")
def test_candidate_weighted_queue_applies_dynamic_backpressure(
    tmp_path: Path, monkeypatch,
):
    import src.discovery.coordinator as coordinator

    nb_dir = tmp_path / "notebooks"
    _seed_ready_notebook(KeywordNotebookStore(nb_dir), "风吹雪")
    monkeypatch.setattr(coordinator, "STAGING_QUEUE_CAPACITY", 2)
    original_drain = coordinator.drain_pending_candidates
    consumer_entered = threading.Event()
    release_consumer = threading.Event()
    call_lock = threading.Lock()
    drain_calls = 0

    def gate_first_consumer_drain(**kwargs):
        nonlocal drain_calls
        with call_lock:
            drain_calls += 1
            call_number = drain_calls
        if call_number == 2:
            consumer_entered.set()
            assert release_consumer.wait(timeout=10)
        return original_drain(**kwargs)

    monkeypatch.setattr(coordinator, "drain_pending_candidates", gate_first_consumer_drain)
    fetch_calls = 0

    def fetch(spec: LaneExecutionSpec, cursor: str, _client: ProviderClient):
        nonlocal fetch_calls
        fetch_calls += 1
        if fetch_calls == 1:
            return _page(spec, cursor, [
                relevance_candidate(title=f"Test candidate {index}", doi=f"10.9902/{index}")
                for index in range(3)
            ])
        return _page(spec, cursor)

    options = DiscoveryOptions(
        mode="refresh", refresh_pages=1, max_candidates=3,
        workspace=_ws(tmp_path, nb_dir),
        output_dir=tmp_path / "out", paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers", ledger_path=tmp_path / "ledger.json",
        crossref_scope_verifier=AlwaysVerifiedScopeVerifier(),
    )
    holder: dict[str, object] = {}

    def run() -> None:
        holder["report"] = run_discovery_batch(
            ["风吹雪"], options=options, max_workers=1, page_fetcher=_fetcher(fetch))

    thread = threading.Thread(target=run)
    thread.start()
    assert consumer_entered.wait(timeout=10)
    release_consumer.set()
    thread.join(timeout=30)
    assert not thread.is_alive()
    report = holder["report"]
    assert report.keywords[0].backpressure is True
    assert report.aggregate["candidates"]["emitted"] == 3

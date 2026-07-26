"""v88 final architecture replacement - failing reproductions for confirmed issues.

Each test asserts the CORRECT contract behavior; with the current (pre-fix)
code these tests FAIL, demonstrating the bug.  All tests use fake provider /
fake clock / tmp_path - no real network, no real discovery state.
"""
from __future__ import annotations

from pathlib import Path
import threading

import pytest

from src.discovery.coordinator import DiscoveryOptions, run_discovery_batch
from src.discovery.stores.notebook_store import NotebookStoreV4 as KeywordNotebookStore
from src.discovery.providers.provider_page_fetcher import CallbackProviderPageFetcher
from src.utils.rate_limit import default_config
from tests.helpers.fake_provider import discovery_page
from tests.helpers.discovery_workspace import make_test_workspace
from tests.helpers.relevance_profiles import (
    AlwaysVerifiedScopeVerifier, bind_test_relevance_profile, relevance_candidate,
)

pytestmark = pytest.mark.integration


def _seed_ready_notebook(store: KeywordNotebookStore, keyword_zh: str) -> None:
    store.ensure_notebook(keyword_zh)
    store.sync_search_queries(keyword_zh, add=[
        {"query": keyword_zh, "language": "zh"},
        {"query": "blowing snow", "language": "en"},
    ])
    bind_test_relevance_profile(store, keyword_zh)
    store.set_enabled(keyword_zh, True)


def _page(spec, cursor: str, candidates=None, **kwargs):
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


def _options(tmp_path: Path, **overrides) -> DiscoveryOptions:
    base = dict(
        mode="backfill", refresh_pages=1, backfill_pages=2, max_candidates=50,
        workspace=make_test_workspace(
            tmp_path,
            notebook_dir=tmp_path / "notebooks",
            page_journals_dir=tmp_path / "pages",
            locks_dir=tmp_path / "locks",
            exports_dir=tmp_path / "exports",
        ),
        output_dir=tmp_path / "out",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        crossref_scope_verifier=AlwaysVerifiedScopeVerifier(),
    )
    base.update(overrides)
    return DiscoveryOptions(**base)


# ── 3.1 Durable progress with empty pages must not be mis-judged ──

def test_durable_progress_on_empty_pages_is_not_failed(tmp_path: Path):
    """A physical lane that successfully fetches and durably persists an *empty*
    page (0 items, next_cursor exists) has made durable progress.  Subsequent
    lanes failing while this lane succeeded must produce partial_success, not
    failed."""
    _seed_ready_notebook(KeywordNotebookStore(tmp_path / "notebooks"), "风吹雪")
    calls: list[str] = []

    def fetch(spec, cursor, _client):
        calls.append(f"{spec.key.provider}:{spec.key.mode}")
        # First lane (zh/openalex): empty page with next cursor (durable).
        if len(calls) == 1:
            return _page(spec, cursor, [], next_cursor="C2", exhausted=False)
        # Remaining lanes: retryable failure.
        return _page(spec, cursor, [], status="failed", error_type="provider_retryable",
                     safe_error="timeout", exhausted=False)

    options = _options(tmp_path, mode="backfill", backfill_pages=3)
    report = run_discovery_batch(
        ["风吹雪"], options=options, max_workers=1,
        page_fetcher=CallbackProviderPageFetcher(fetch),
    )
    assert report.keywords[0].backfill.pages_persisted >= 1, "first lane made durable progress"
    assert report.status == "partial_success", (
        f"empty-page durable progress should yield partial_success, got {report.status!r}"
    )
    assert report.exit_code == 2


# ── 3.2 Concurrent batch telemetry/budget isolation ──

def test_concurrent_batch_telemetry_and_budget_isolation(tmp_path: Path, monkeypatch):
    """Two independent DiscoveryBatchRuntime instances running in the same
    process must not share telemetry or request budgets."""
    from src.discovery.providers.provider_client import ProviderRuntime
    from tests.helpers.fake_provider import (
        FakeClock, FakeSleeper, make_crossref_page, make_openalex_page,
    )

    class _TransportA:
        def send(self, spec, to):
            return make_openalex_page([{"id":"W1"}], next_cursor="C1")

    class _TransportB:
        def send(self, spec, to):
            return make_openalex_page([{"id":"W2"}], next_cursor="C2")

    cfg = default_config()
    cfg["global"]["paper_interval_seconds"] = 0.0
    cfg["global"]["jitter_seconds"] = 0.0
    for p in cfg.get("providers", ()):
        cfg["providers"][p]["min_interval_seconds"] = 0.0

    _seed_ready_notebook(KeywordNotebookStore(tmp_path / "notebooks"), "风吹雪")
    opts = _options(tmp_path, mode="refresh", refresh_pages=1)

    # Install runtime A, run batch A, capture telemetry.
    rt_a = ProviderRuntime(config=cfg, transport=_TransportA(),
                           sleeper=FakeSleeper(FakeClock()), clock=FakeClock())
    monkeypatch.setattr(ProviderRuntime, "_instance", rt_a)
    report_a = run_discovery_batch(["风吹雪"], options=opts, max_workers=1)

    # Install runtime B, run batch B, capture telemetry.
    rt_b = ProviderRuntime(config=cfg, transport=_TransportB(),
                           sleeper=FakeSleeper(FakeClock()), clock=FakeClock())
    monkeypatch.setattr(ProviderRuntime, "_instance", rt_b)
    report_b = run_discovery_batch(["风吹雪"], options=opts, max_workers=1)

    # Telemetry must be independent.
    pr_a = report_a.aggregate["provider_requests"]["attempted"]
    pr_b = report_b.aggregate["provider_requests"]["attempted"]
    assert pr_a > 0 and pr_b > 0, "both batches should make requests"
    assert pr_a == pr_b, (
        f"telemetry budgets leaked: batch A={pr_a}, batch B={pr_b}"
    )


# ── 3.4 Generation history strict type validation ──

def test_generation_history_rejects_bad_types(tmp_path: Path):
    """Generation history entries with wrong field types must be rejected."""
    from src.discovery.stores.notebook_store import NotebookStoreV4 as KeywordNotebookStore
    from src.discovery.contracts.page_journal import request_signature

    store = KeywordNotebookStore(tmp_path / "notebooks")
    _seed_ready_notebook(store, "风吹雪")
    nb = store.require_v4("风吹雪")
    query_id = next(iter(nb["search_queries"]))
    provider = "openalex"

    sig_a = request_signature(sort=None, filters={}, page_size=10)
    sig_b = request_signature(sort="relevance" + "_score:desc", filters={}, page_size=10)

    # First rollover: A -> B (generation 1 -> 2), writes 10-field history.
    state = store.ensure_backfill_generation(
        "风吹雪", query_id, provider, request_signature_hash=sig_a["hash"])
    state = store.ensure_backfill_generation(
        "风吹雪", query_id, provider, request_signature_hash=sig_b["hash"])
    assert state["generation"] == 2

    # Now manually inject a bad history entry.
    nb = store.require_v4("风吹雪")
    bf = nb["search_queries"][query_id]["providers"][provider]["backfill"]
    bad_history = list(bf["generation_history"])
    bad_history.append({
        "generation": "three",  # string, not int
        "request_signature": "bad",
        "closed_at": "now",
        "reason": "test",
        "cursor": "*",
        "exhausted": False,
        "pages_succeeded": 0,
        "pages_committed": 0,
        "items_returned_total": 0,
        "last_committed_page_id": None,
    })
    # Direct write to simulate corruption
    import json
    entry = nb["search_queries"][query_id]
    entry["providers"][provider]["backfill"]["generation_history"] = bad_history
    nb_path = next((tmp_path / "notebooks").glob("*.json"))
    nb_path.write_text(json.dumps(nb, ensure_ascii=False), encoding="utf-8")

    # Re-load - must raise or flag repair_required
    from src.discovery.contracts.notebook import NotebookCorruptError
    with pytest.raises(NotebookCorruptError, match="generation.*int"):
        store.require_v4("风吹雪")


# ── 3.7 Unsafe advance_backfill with exhausted=True must not exist ──

def test_no_unsafe_exhaustion_api_in_production():
    """Production code must NOT call ``advance_backfill`` (it bypasses the
    evidence requirement for exhausted=True).  Test-only callers in
    ``tests/`` are allowed and should be migrated to ``commit_backfill_cursor``
    with proper evidence.
    """
    import ast, sys
    from pathlib import Path

    src_root = Path(__file__).resolve().parent.parent.parent / "src"
    violations: list[str] = []

    for py_file in src_root.rglob("*.py"):
        if py_file.name == "__init__.py":
            continue
        text = py_file.read_text(encoding="utf-8", errors="replace")
        if "advance_backfill" not in text:
            continue
        tree = ast.parse(text, str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "advance_backfill":
                    violations.append(str(py_file.relative_to(src_root)))
    assert not violations, (
        f"Production code must not call advance_backfill (unsafe, no evidence). "
        f"Violations: {violations}"
    )


# ── 3.3b Generation strict type rejection (single correct parametrization) ──

@pytest.mark.parametrize("field,bad_value,expected_error", [
    ("generation", "1", "generation.*int"),
    ("generation", True, "generation.*int"),
    ("exhausted", "false", "exhausted.*bool"),
    ("exhausted", 1, "exhausted.*bool"),
    ("pages_succeeded", "0", "pages_succeeded"),
    ("pages_committed", 1.5, "pages_committed"),
    ("items_returned_total", -1, "items_returned_total"),
    ("last_committed_page_id", [], "last_committed_page_id"),
    ("request_signature", 123, "request_signature"),
    ("closed_at", None, "closed_at"),
    ("cursor", 456, "cursor"),
])
def test_generation_history_strict_type_rejection(tmp_path: Path, field, bad_value, expected_error):
    """Generation history entries with wrong Python types must be rejected."""
    from src.discovery.execution.lane_models import GenerationHistoryEntry
    from src.discovery.contracts.notebook import NotebookCorruptError

    entry = GenerationHistoryEntry(
        generation=1,
        request_signature="abc123",
        closed_at="2026-01-01T00:00:00Z",
        reason="test",
        cursor="*",
        exhausted=False,
        pages_succeeded=0,
        pages_committed=0,
        items_returned_total=0,
        last_committed_page_id=None,
    )
    d = entry.to_dict()
    d[field] = bad_value
    with pytest.raises((TypeError, ValueError, NotebookCorruptError), match=expected_error):
        GenerationHistoryEntry.from_dict(d).validate()


# ── 3.4 Generation strict schema via notebook validator ──

def test_generation_history_via_notebook_require_v4_rejects_bad_types(tmp_path: Path):
    """GenerationHistoryEntry validation must be called by notebook.require_v4()."""
    from src.discovery.contracts.notebook import NotebookCorruptError
    from src.discovery.stores.notebook_store import NotebookStoreV4 as KeywordNotebookStore
    from src.discovery.contracts.page_journal import request_signature
    import json

    store = KeywordNotebookStore(tmp_path / "notebooks")
    _seed_ready_notebook(store, "风吹雪")
    nb = store.require_v4("风吹雪")
    query_id = next(iter(nb["search_queries"]))
    provider = "openalex"

    sig_a = request_signature(sort=None, filters={}, page_size=10)
    sig_b = request_signature(sort="relevance" + "_score:desc", filters={}, page_size=10)
    store.ensure_backfill_generation("风吹雪", query_id, provider, request_signature_hash=sig_a["hash"])
    store.ensure_backfill_generation("风吹雪", query_id, provider, request_signature_hash=sig_b["hash"])

    nb = store.require_v4("风吹雪")
    bf = nb["search_queries"][query_id]["providers"][provider]["backfill"]
    bad_history = list(bf["generation_history"])
    bad_history[-1] = dict(bad_history[-1])
    bad_history[-1]["generation"] = "three"
    entry = nb["search_queries"][query_id]
    entry["providers"][provider]["backfill"]["generation_history"] = bad_history
    nb_path = next((tmp_path / "notebooks").glob("*.json"))
    nb_path.write_text(json.dumps(nb, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(NotebookCorruptError, match="generation.*int"):
        store.require_v4("风吹雪")


# ── 3.5 Model production access static test ──

def test_lane_models_module_is_importable():
    """The ``lane_models`` module must be importable and provide the core
    model types."""
    from src.discovery.execution import lane_models as lm
    assert hasattr(lm, "DiscoveryLaneKey")
    assert hasattr(lm, "LaneState")
    assert hasattr(lm, "StopReason")
    assert hasattr(lm, "LaneCounters")
    assert hasattr(lm, "LaneOutcome")
    assert hasattr(lm, "DurableProviderPage")
    assert hasattr(lm, "GenerationHistoryEntry")
    assert hasattr(lm, "ExhaustionEvidence")

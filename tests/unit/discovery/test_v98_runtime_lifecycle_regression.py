"""Phase 0.3: Runtime lifecycle regression tests for v98.

Verifies DiscoveryBatchRuntime lifecycle through the formal run_discovery_batch
entry point. Uses monkeypatch to capture the runtime instance and asserts
post-batch state: closed_event, frozen, shutdown_reason, cancellation.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.discovery.runtime.batch_runtime import DiscoveryBatchRuntime
from src.discovery.contracts.enums import ShutdownReason
from src.discovery.coordinator import DiscoveryOptions, run_discovery_batch
from src.discovery.stores.notebook_store import NotebookStoreV4 as KeywordNotebookStore
from src.discovery.providers.provider_page_fetcher import CallbackProviderPageFetcher
from tests.helpers.fake_provider import discovery_page
from tests.helpers.discovery_workspace import make_test_workspace
from tests.helpers.relevance_profiles import (
    AlwaysVerifiedScopeVerifier,
    bind_test_relevance_profile,
    relevance_candidate,
)

pytestmark = pytest.mark.unit


def _seed_ready_notebook(store: KeywordNotebookStore, keyword_zh: str) -> None:
    store.ensure_notebook(keyword_zh)
    store.sync_search_queries(keyword_zh, add=[
        {"query": keyword_zh, "language": "zh"},
        {"query": "test research query", "language": "en"},
    ])
    bind_test_relevance_profile(store, keyword_zh)
    store.set_enabled(keyword_zh, True)


# ── Runtime lifecycle via run_discovery_batch ─────────────────────────


def test_runtime_lifecycle_normal_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """After normal batch completion, runtime is closed, frozen, reason=COMPLETED.

    v98 Phase 3: coordinator now uses `with runtime:` context manager,
    so __exit__ is called, closed_event is set, and _frozen is True.
    """
    nb_dir = tmp_path / "notebooks"
    store = KeywordNotebookStore(nb_dir)
    _seed_ready_notebook(store, "风吹雪")

    captured_runtime: list[DiscoveryBatchRuntime] = []

    # Monkey-patch DiscoveryBatchRuntime.create to capture the runtime
    original_create = DiscoveryBatchRuntime.create

    def capturing_create(**kwargs):
        runtime = original_create(**kwargs)
        captured_runtime.append(runtime)
        return runtime

    monkeypatch.setattr(DiscoveryBatchRuntime, "create", capturing_create)

    opts = DiscoveryOptions(
        mode="backfill", max_candidates=10,
        workspace=make_test_workspace(
            tmp_path,
            notebook_dir=nb_dir,
            page_journals_dir=tmp_path / "pages",
            locks_dir=tmp_path / "locks",
            exports_dir=tmp_path / "exports",
        ),
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
        page_fetcher=CallbackProviderPageFetcher(lambda s, c, cl: discovery_page(
            provider=s.key.provider, keyword_zh=s.keyword_zh, query=s.query,
            lane=s.key.mode, cursor=c, query_id=s.key.query_id,
            query_language=s.query_language,
            candidates=[relevance_candidate(doi="10.1234/test")],
            next_cursor=None, exhausted=True,
        )),
    )

    assert report.status == "success", f"Batch should succeed, got {report.status}"
    assert len(captured_runtime) == 1, "Should capture exactly one runtime"
    runtime = captured_runtime[0]

    # v98 Phase 3: these assertions now pass (coordinator uses `with runtime:`)
    assert runtime.closed_event.is_set(), (
        "closed_event must be set after batch completion"
    )
    assert runtime.frozen, (
        "runtime must be frozen after batch completion"
    )
    assert runtime.shutdown_reason == ShutdownReason.COMPLETED, (
        f"shutdown_reason should be COMPLETED, got {runtime.shutdown_reason}"
    )
    assert not runtime.cancellation_token.is_set(), (
        "cancellation_token should NOT be set for normal completion"
    )


def test_runtime_lifecycle_verify_context_manager_works(tmp_path: Path):
    """Verify the context manager itself works correctly (unit test of Runtime only).

    v98 Phase 3: the context manager is now used in production via
    `with runtime:` in coordinator.py. This test confirms the
    underlying lifecycle transitions work as expected.
    """
    from src.discovery.stores.journal_drain_index import JournalDrainIndex
    from src.discovery.stores.page_journal_store import PageJournalStoreV4 as PageJournalStore
    from src.discovery.runtime.batch_runtime import ActiveRelevanceProfiles

    journal = PageJournalStore(tmp_path / "pages")
    runtime = DiscoveryBatchRuntime.create(
        journal=journal,
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        needs_staging=False,
        active_relevance_profiles=ActiveRelevanceProfiles.build({}),
    )

    # Before context manager: not frozen
    assert not runtime.frozen
    assert not runtime.closed_event.is_set()
    assert runtime.shutdown_reason is None

    # Enter context
    with runtime:
        assert not runtime.frozen
        assert not runtime.closed_event.is_set()

    # After context manager exit: frozen
    assert runtime.frozen, "Runtime should be frozen after __exit__"
    assert runtime.closed_event.is_set(), "closed_event should be set after __exit__"
    assert runtime.shutdown_reason == ShutdownReason.COMPLETED, (
        f"shutdown_reason should be COMPLETED, got {runtime.shutdown_reason}"
    )
    assert not runtime.cancellation_token.is_set(), (
        "cancellation_token should NOT be set for normal exit"
    )


def test_runtime_lifecycle_exception_exit(tmp_path: Path):
    """When context exits with an exception, runtime sets cancellation and FAILED."""
    from src.discovery.stores.page_journal_store import PageJournalStoreV4 as PageJournalStore
    from src.discovery.runtime.batch_runtime import ActiveRelevanceProfiles

    journal = PageJournalStore(tmp_path / "pages")
    runtime = DiscoveryBatchRuntime.create(
        journal=journal,
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        needs_staging=False,
        active_relevance_profiles=ActiveRelevanceProfiles.build({}),
    )

    try:
        with runtime:
            raise ValueError("test failure")
    except ValueError:
        pass

    assert runtime.frozen, "Runtime should be frozen after exception exit"
    assert runtime.closed_event.is_set()
    assert runtime.shutdown_reason == ShutdownReason.FAILED, (
        f"shutdown_reason should be FAILED, got {runtime.shutdown_reason}"
    )
    assert runtime.cancellation_token.is_set(), (
        "cancellation_token should be set for exception exit"
    )


def test_runtime_lifecycle_keyboard_interrupt(tmp_path: Path):
    """KeyboardInterrupt in context → INTERRUPTED reason, cancellation set."""
    from src.discovery.stores.page_journal_store import PageJournalStoreV4 as PageJournalStore
    from src.discovery.runtime.batch_runtime import ActiveRelevanceProfiles

    journal = PageJournalStore(tmp_path / "pages")
    runtime = DiscoveryBatchRuntime.create(
        journal=journal,
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        needs_staging=False,
        active_relevance_profiles=ActiveRelevanceProfiles.build({}),
    )

    try:
        with runtime:
            raise KeyboardInterrupt("user interrupt")
    except KeyboardInterrupt:
        pass

    assert runtime.frozen, "Runtime should be frozen after interrupt"
    assert runtime.closed_event.is_set()
    assert runtime.shutdown_reason == ShutdownReason.INTERRUPTED, (
        f"shutdown_reason should be INTERRUPTED, got {runtime.shutdown_reason}"
    )
    assert runtime.cancellation_token.is_set(), (
        "cancellation_token should be set for interrupt"
    )

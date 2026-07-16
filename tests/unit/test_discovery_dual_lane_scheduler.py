"""Unit tests for the dual-lane (Refresh + Backfill) discovery scheduler."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.discovery.keyword_notebook import (
    INITIAL_CURSOR,
    KeywordNotebookStore,
    query_identity,
)
from src.discovery.coordinator import (
    DiscoveryOptions, _profile_filters, _profile_order, _profile_sort,
    run_discovery_batch,
)
from src.discovery.models import PaperCandidate
from src.discovery.page_journal import PageJournalStore, request_signature
from tests.helpers.relevance_profiles import (
    AlwaysVerifiedScopeVerifier, bind_test_relevance_profile, relevance_candidate,
)


pytestmark = pytest.mark.unit

KEYWORD_ZH = "测试关键词"
ZH_QUERY_ID = query_identity("zh", KEYWORD_ZH)


def _seed_ready(root: Path, *, page_size: int) -> KeywordNotebookStore:
    store = KeywordNotebookStore(root)
    signature_hash = request_signature(page_size=page_size)["hash"]
    store.ensure_notebook(KEYWORD_ZH)
    store.sync_search_queries(
        KEYWORD_ZH,
        add=[
            {"query": KEYWORD_ZH, "language": "zh", "source": "canonical"},
            {"query": "test keyword", "language": "en", "source": "curated"},
        ],
        pag_sig=signature_hash,
    )
    bind_test_relevance_profile(store, KEYWORD_ZH)
    store.set_enabled(KEYWORD_ZH, True)
    notebook = store.require_v3(KEYWORD_ZH)
    options = DiscoveryOptions(page_size=page_size)
    for entry in notebook["search_queries"].values():
        for provider in ("openalex", "crossref"):
            sort = _profile_sort(notebook, provider, "backfill", options)
            order = _profile_order(notebook, "backfill") if provider == "crossref" else None
            signature = request_signature(
                sort=sort,
                filters=_profile_filters(notebook, provider, "backfill", sort, order),
                page_size=page_size,
            )
            store.ensure_backfill_generation(
                KEYWORD_ZH, entry["query_id"], provider,
                request_signature_hash=signature["hash"],
            )
    return store


def _cand(doi, title="Test candidate", source="openalex"):
    return relevance_candidate(title=title, doi=doi, source=source)


class _FakePage:
    """Minimal stand-in for DiscoveryPage used by the scheduler."""

    def __init__(self, candidates, next_cursor, exhausted=False, status="success",
                 safe_error=None, error_type=None, returned_count=None):
        self.candidates = candidates
        self.next_cursor = next_cursor
        self.exhausted = exhausted
        self.status = status
        self.safe_error = safe_error
        self.error_type = error_type
        self.returned_count = returned_count if returned_count is not None else len(candidates)


def _install_fake_fetch(monkeypatch, openalex_pages, crossref_pages):
    """Route provider page calls through scripted page lists.

    ``openalex_pages`` / ``crossref_pages`` are dicts keyed by cursor
    (or ``"*"`` for refresh) → _FakePage. Each scripted page is consumed
    once per call.
    """
    calls: list[dict] = []

    def _fake_openalex(query, *, keyword_zh, lane, page_size, cursor, sort=None,
                       domain_id=None, rate_limiter=None, limiter_lock=None):
        calls.append({"provider": "openalex", "lane": lane, "cursor": cursor})
        pages = openalex_pages.get((lane, cursor))
        if pages is None:
            return _FakePage([], next_cursor=None, exhausted=True)
        return pages

    def _fake_crossref(query, *, keyword_zh, lane, page_size, cursor, sort=None,
                       order=None, domain_id=None, rate_limiter=None, limiter_lock=None):
        calls.append({"provider": "crossref", "lane": lane, "cursor": cursor})
        pages = crossref_pages.get((lane, cursor))
        if pages is None:
            return _FakePage([], next_cursor=None, exhausted=True)
        return pages

    def _fetch(provider, query, **kwargs):
        if provider == "openalex":
            return _fake_openalex(query, **kwargs)
        if provider == "crossref":
            return _fake_crossref(query, **kwargs)
        raise AssertionError(f"unexpected provider: {provider}")

    monkeypatch.setattr("src.discovery.coordinator._default_fetch_page", _fetch)
    return calls


def _run_discovery(keyword: str, *, mode="hybrid", refresh_pages=2,
                   backfill_pages=5, page_size=50, max_candidates=50,
                   notebook_dir: Path, paper_raw_dir: Path | None = None,
                   papers_dir: Path | None = None, hide_existing=False):
    runtime_base = notebook_dir / ".discovery_runtime"
    options = DiscoveryOptions(
        mode=mode,
        refresh_pages=refresh_pages,
        backfill_pages=backfill_pages,
        page_size=page_size,
        max_candidates=max_candidates,
        hide_existing=hide_existing,
        notebook_dir=notebook_dir,
        pending_pages_dir=runtime_base / "pending_pages",
        locks_dir=runtime_base / "locks",
        exports_dir=runtime_base / "exports",
        output_dir=runtime_base / "output",
        paper_raw_dir=paper_raw_dir or (runtime_base / "paper_raw"),
        papers_dir=papers_dir or (runtime_base / "papers"),
        ledger_path=(paper_raw_dir.parent / "ledger.json") if paper_raw_dir else (runtime_base / "ledger.json"),
        crossref_scope_verifier=AlwaysVerifiedScopeVerifier(),
    )
    batch_report = run_discovery_batch([keyword], options=options, max_workers=2)
    report_obj = batch_report.keywords[0]
    candidates = []
    journal = PageJournalStore(options.pending_pages_dir)
    for ref in journal.list_pages([report_obj.keyword_id]):
        data = journal.read(ref.path)
        for item in data.get("candidates", []):
            if item.get("status") in {"emitted", "staged"} and isinstance(item.get("candidate"), dict):
                candidates.append(PaperCandidate.from_dict(item["candidate"]))
    return SimpleNamespace(candidates=candidates[:max_candidates]), report_obj.to_dict()


class TestDualLaneScheduling:
    def test_hybrid_runs_both_refresh_and_backfill(self, monkeypatch, tmp_path: Path):
        _seed_ready(tmp_path, page_size=10)
        calls = _install_fake_fetch(
            monkeypatch,
            openalex_pages={
                ("refresh", INITIAL_CURSOR): _FakePage([_cand("10.1/r1")], next_cursor=None, exhausted=True),
                ("backfill", INITIAL_CURSOR): _FakePage([_cand("10.1/b1")], next_cursor="BF2"),
            },
            crossref_pages={},
        )
        batch, report = _run_discovery(
            KEYWORD_ZH,
            mode="hybrid",
            refresh_pages=1,
            backfill_pages=1,
            page_size=10,
            notebook_dir=tmp_path,
            paper_raw_dir=tmp_path / "paper_raw",
            papers_dir=tmp_path / "papers",
        )
        lanes_seen = {c["lane"] for c in calls}
        assert "refresh" in lanes_seen
        assert "backfill" in lanes_seen
        assert report["status"] == "success"
        assert report["refresh"]["pages_requested"] >= 1
        assert report["backfill"]["pages_committed"] >= 1

    def test_refresh_always_starts_from_star(self, monkeypatch, tmp_path: Path):
        store = _seed_ready(tmp_path, page_size=10)
        # Advance backfill cursor so refresh could (incorrectly) pick it up.
        store.advance_backfill(KEYWORD_ZH, ZH_QUERY_ID, "openalex", next_cursor="DEEP", items_this_page=5)
        calls = _install_fake_fetch(
            monkeypatch,
            openalex_pages={
                ("refresh", INITIAL_CURSOR): _FakePage([], next_cursor=None, exhausted=True),
                ("backfill", "DEEP"): _FakePage([], next_cursor=None, exhausted=True),
            },
            crossref_pages={},
        )
        _run_discovery(
            KEYWORD_ZH, mode="hybrid", refresh_pages=1, backfill_pages=1,
            page_size=10, notebook_dir=tmp_path,
        )
        refresh_calls = [c for c in calls if c["lane"] == "refresh" and c["provider"] == "openalex"]
        assert all(c["cursor"] == INITIAL_CURSOR for c in refresh_calls)

    def test_backfill_resumes_from_saved_cursor(self, monkeypatch, tmp_path: Path):
        store = _seed_ready(tmp_path, page_size=10)
        store.advance_backfill(KEYWORD_ZH, ZH_QUERY_ID, "openalex", next_cursor="RESUME_HERE", items_this_page=5)
        calls = _install_fake_fetch(
            monkeypatch,
            openalex_pages={
                ("refresh", INITIAL_CURSOR): _FakePage([], next_cursor=None, exhausted=True),
                ("backfill", "RESUME_HERE"): _FakePage([_cand("10.1/x")], next_cursor="RESUME2"),
            },
            crossref_pages={},
        )
        _run_discovery(
            KEYWORD_ZH, mode="hybrid", refresh_pages=1, backfill_pages=1,
            page_size=10, notebook_dir=tmp_path,
        )
        backfill_calls = [c for c in calls if c["lane"] == "backfill" and c["provider"] == "openalex"]
        assert any(c["cursor"] == "RESUME_HERE" for c in backfill_calls)
        # Cursor advanced.
        assert store.get_backfill_cursor(KEYWORD_ZH, ZH_QUERY_ID, "openalex") == "RESUME2"

    def test_refresh_failure_does_not_reset_backfill_cursor(self, monkeypatch, tmp_path: Path):
        store = _seed_ready(tmp_path, page_size=10)
        store.advance_backfill(KEYWORD_ZH, ZH_QUERY_ID, "openalex", next_cursor="KEEPME", items_this_page=5)
        _install_fake_fetch(
            monkeypatch,
            openalex_pages={
                ("refresh", INITIAL_CURSOR): _FakePage([], next_cursor=None, status="failed", safe_error="boom"),
                ("backfill", "KEEPME"): _FakePage([], next_cursor=None, exhausted=True),
            },
            crossref_pages={},
        )
        _run_discovery(
            KEYWORD_ZH, mode="hybrid", refresh_pages=1, backfill_pages=1,
            page_size=10, notebook_dir=tmp_path,
        )
        assert store.get_backfill_cursor(KEYWORD_ZH, ZH_QUERY_ID, "openalex") == "KEEPME"

    def test_backfill_failure_does_not_advance_cursor(self, monkeypatch, tmp_path: Path):
        store = _seed_ready(tmp_path, page_size=10)
        store.advance_backfill(KEYWORD_ZH, ZH_QUERY_ID, "openalex", next_cursor="BEFORE_FAIL", items_this_page=5)
        _install_fake_fetch(
            monkeypatch,
            openalex_pages={
                ("refresh", INITIAL_CURSOR): _FakePage([], next_cursor=None, exhausted=True),
                ("backfill", "BEFORE_FAIL"): _FakePage([], next_cursor=None, status="failed", safe_error="timeout"),
            },
            crossref_pages={},
        )
        _run_discovery(
            KEYWORD_ZH, mode="hybrid", refresh_pages=1, backfill_pages=1,
            page_size=10, notebook_dir=tmp_path,
        )
        assert store.get_backfill_cursor(KEYWORD_ZH, ZH_QUERY_ID, "openalex") == "BEFORE_FAIL"

    def test_existing_dois_filtered_before_max_candidates(self, monkeypatch, tmp_path: Path):
        """Existing DOI observations are terminal and new candidates remain recoverable."""
        paper_raw = tmp_path / "paper_raw"
        papers = tmp_path / "papers"
        # Create 50 existing DOI workspaces through the canonical factory.
        from tests.factories.paper_raw_factory import create_network_metadata_workspaces_bulk
        create_network_metadata_workspaces_bulk(tmp_path, count=50)
        new_cands = [_cand(f"10.1/new{i}") for i in range(50)]
        existing_cands = [_cand(f"10.7000/bench.{i + 1}") for i in range(50)]
        all_cands = existing_cands + new_cands
        _install_fake_fetch(
            monkeypatch,
            openalex_pages={
                ("refresh", INITIAL_CURSOR): _FakePage(all_cands, next_cursor=None, exhausted=True),
                ("backfill", INITIAL_CURSOR): _FakePage([], next_cursor=None, exhausted=True),
            },
            crossref_pages={},
        )
        _seed_ready(tmp_path, page_size=200)
        batch, report = _run_discovery(
            KEYWORD_ZH, mode="hybrid", refresh_pages=1, backfill_pages=1,
            page_size=200, max_candidates=100, notebook_dir=tmp_path,
            paper_raw_dir=paper_raw, papers_dir=papers, hide_existing=True,
        )
        assert len(batch.candidates) == 50
        assert all(not c.existing_duplicate_refs for c in batch.candidates)
        assert report["candidates"]["existing_duplicates"] == 50

    def test_provider_failure_reported_as_partial_success(self, monkeypatch, tmp_path: Path):
        _seed_ready(tmp_path, page_size=10)
        _install_fake_fetch(
            monkeypatch,
            openalex_pages={
                ("refresh", INITIAL_CURSOR): _FakePage([_cand("10.1/ok")], next_cursor=None, exhausted=True),
                ("backfill", INITIAL_CURSOR): _FakePage([], next_cursor=None, status="failed", safe_error="err"),
            },
            crossref_pages={},
        )
        _, report = _run_discovery(
            KEYWORD_ZH, mode="hybrid", refresh_pages=1, backfill_pages=1,
            page_size=10, notebook_dir=tmp_path,
        )
        assert report["status"] == "partial_success"
        assert report["backfill"]["provider_failures"] >= 1

    def test_skipped_when_keyword_disabled(self, monkeypatch, tmp_path: Path):
        store = _seed_ready(tmp_path, page_size=10)
        store.set_enabled(KEYWORD_ZH, False)
        calls = _install_fake_fetch(monkeypatch, {}, {})
        batch, report = _run_discovery(
            KEYWORD_ZH, mode="hybrid", refresh_pages=1, backfill_pages=1,
            page_size=10, notebook_dir=tmp_path,
        )
        assert report["status"] == "skipped"
        assert calls == []
        assert batch.candidates == []

"""Integration test: keyword discovery resume across two runs.

Verifies the core dual-lane resume contract:
- Refresh always starts from cursor="*".
- Backfill resumes from the notebook's saved cursor.
- A second run continues the backfill chain; refresh restarts.
- Existing DOIs are not re-staged.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.discovery.keyword_notebook import (
    INITIAL_CURSOR,
    KeywordNotebookStore,
    query_identity,
)
from src.discovery.coordinator import DiscoveryOptions, run_discovery_batch
from src.discovery.models import PaperCandidate
from src.discovery.provider_models import DiscoveryPage
from src.discovery.page_journal import PageJournalStore, request_signature
from src.metadata.schema import empty_metadata


def _queries(nb: dict) -> dict:
    return nb["search_queries"]


pytestmark = pytest.mark.integration


def _seed_ready(
    store: KeywordNotebookStore,
    keyword_zh: str,
    english_query: str,
) -> str:
    signature_hash = request_signature(page_size=10)["hash"]
    store.ensure_notebook(keyword_zh)
    store.sync_search_queries(
        keyword_zh,
        add=[
            {"query": keyword_zh, "language": "zh", "source": "canonical"},
            {"query": english_query, "language": "en", "source": "curated"},
        ],
        pag_sig=signature_hash,
    )
    store.set_enabled(keyword_zh, True)
    return query_identity("zh", keyword_zh)


def _page(works, next_cursor, exhausted=None):
    """Build a real DiscoveryPage from (doi, title) pairs."""
    cands = [
        PaperCandidate(title=title, doi=doi, source="openalex")
        for doi, title in works
    ]
    if exhausted is None:
        exhausted = not next_cursor
    return DiscoveryPage(
        provider="openalex",
        keyword_zh="边界层",
        query="边界层",
        lane="backfill",
        candidates=cands,
        request_cursor=None,
        next_cursor=next_cursor,
        page_size=10,
        returned_count=len(cands),
        total_results=len(cands),
        status="success",
        exhausted=exhausted,
    )


class _ScriptedOpenAlex:
    """Returns scripted DiscoveryPage objects keyed by (lane, cursor)."""

    def __init__(self, scripts: dict):
        self.scripts = scripts
        self.calls: list[tuple[str, str]] = []

    def __call__(self, query, *, keyword_zh, lane, page_size, cursor,
                 sort=None, domain_id=None, rate_limiter=None, limiter_lock=None):
        self.calls.append((lane, cursor))
        key = (lane, cursor)
        if key not in self.scripts:
            return _page([], None)
        works, nxt = self.scripts[key]
        return _page(works, nxt)


def _crossref_noop(*args, **kwargs):
    return _page([], None)


def _provider_fetch(openalex, crossref):
    def _fetch(provider, query, **kwargs):
        if provider == "openalex":
            return openalex(query, **kwargs)
        if provider == "crossref":
            return crossref(query, **kwargs)
        raise AssertionError(f"unexpected provider: {provider}")
    return _fetch


def _run_discovery(keyword_zh: str, *, notebook_dir: Path,
                   paper_raw_dir: Path | None = None,
                   papers_dir: Path | None = None,
                   hide_existing: bool = False,
                   fetch_page) -> tuple[SimpleNamespace, dict]:
    runtime_base = notebook_dir / ".discovery_runtime"
    options = DiscoveryOptions(
        mode="hybrid",
        refresh_pages=1,
        backfill_pages=1,
        page_size=10,
        max_candidates=50,
        hide_existing=hide_existing,
        notebook_dir=notebook_dir,
        pending_pages_dir=runtime_base / "pending_pages",
        locks_dir=runtime_base / "locks",
        exports_dir=runtime_base / "exports",
        output_dir=runtime_base / "output",
        paper_raw_dir=paper_raw_dir or (runtime_base / "paper_raw"),
        papers_dir=papers_dir or (runtime_base / "papers"),
        ledger_path=(paper_raw_dir.parent / "ledger.json") if paper_raw_dir else (runtime_base / "ledger.json"),
    )
    batch_report = run_discovery_batch(
        [keyword_zh], options=options, max_workers=2, fetch_page=fetch_page,
    )
    report_obj = batch_report.keywords[0]
    candidates = []
    journal = PageJournalStore(options.pending_pages_dir)
    for ref in journal.list_pages([report_obj.keyword_id]):
        data = journal.read(ref.path)
        for item in data.get("candidates", []):
            if item.get("status") in {"emitted", "staged"} and isinstance(item.get("candidate"), dict):
                candidates.append(PaperCandidate.from_dict(item["candidate"]))
    return SimpleNamespace(candidates=candidates), report_obj.to_dict()


class TestKeywordDiscoveryResume:
    def test_second_run_resumes_backfill_and_refreshes_from_start(self, tmp_path: Path):
        notebook_dir = tmp_path / "notebooks"
        paper_raw = tmp_path / "paper_raw"
        papers = tmp_path / "papers"
        keyword_zh = "边界层"
        store = KeywordNotebookStore(notebook_dir)
        _seed_ready(store, keyword_zh, "boundary layer")

        # First run.
        scripts_run1 = {
            ("refresh", INITIAL_CURSOR): ([("10.1/a", "Paper A")], None),
            ("backfill", INITIAL_CURSOR): ([("10.1/b", "Paper B")], "A2"),
        }
        fake = _ScriptedOpenAlex(scripts_run1)
        batch1, report1 = _run_discovery(
            keyword_zh, notebook_dir=notebook_dir, paper_raw_dir=paper_raw,
            papers_dir=papers, fetch_page=_provider_fetch(fake, _crossref_noop),
        )
        assert report1["status"] == "success"
        dois1 = {c.doi for c in batch1.candidates}
        assert "10.1/a" in dois1 and "10.1/b" in dois1

        # Verify notebook saved cursor A2.
        nb = store.load(keyword_zh)
        saved_cursors = {
            exp["providers"]["openalex"]["backfill"]["cursor"]
            for exp in _queries(nb).values()
        }
        assert "A2" in saved_cursors

        # Second run: refresh MUST use cursor="*" again; backfill MUST use A2.
        scripts_run2 = {
            ("refresh", INITIAL_CURSOR): ([("10.1/a2", "Paper A2")], None),
            ("backfill", "A2"): ([("10.1/c", "Paper C")], "A3"),
        }
        fake2 = _ScriptedOpenAlex(scripts_run2)
        batch2, report2 = _run_discovery(
            keyword_zh, notebook_dir=notebook_dir, paper_raw_dir=paper_raw,
            papers_dir=papers, fetch_page=_provider_fetch(fake2, _crossref_noop),
        )
        refresh_cursors = [c for (lane, c) in fake2.calls if lane == "refresh"]
        backfill_cursors = [c for (lane, c) in fake2.calls if lane == "backfill"]
        assert all(c == INITIAL_CURSOR for c in refresh_cursors), "refresh must restart from *"
        assert "A2" in backfill_cursors, "backfill must resume from saved cursor A2"

        # Notebook advanced to A3.
        nb2 = store.load(keyword_zh)
        advanced = any(
            exp["providers"]["openalex"]["backfill"]["cursor"] == "A3"
            for exp in _queries(nb2).values()
        )
        assert advanced, "backfill cursor should have advanced to A3"

    def test_existing_doi_not_re_staged(self, tmp_path: Path):
        """A DOI already in paper_raw must not be staged again."""
        notebook_dir = tmp_path / "notebooks"
        paper_raw = tmp_path / "paper_raw"
        papers = tmp_path / "papers"
        keyword_zh = "边界层"
        _seed_ready(KeywordNotebookStore(notebook_dir), keyword_zh, "boundary layer")

        from tests.factories.paper_raw_factory import create_network_metadata_workspace
        create_network_metadata_workspace(tmp_path, doi="10.1/exists")

        scripts = {
            ("refresh", INITIAL_CURSOR): (
                [("10.1/exists", "Existing"), ("10.1/new", "New")],
                None,
            ),
            ("backfill", INITIAL_CURSOR): ([], None),
        }
        fake = _ScriptedOpenAlex(scripts)
        batch, report = _run_discovery(
            keyword_zh, notebook_dir=notebook_dir, paper_raw_dir=paper_raw,
            papers_dir=papers, hide_existing=True,
            fetch_page=_provider_fetch(fake, _crossref_noop),
        )
        dois = {c.doi for c in batch.candidates}
        assert "10.1/new" in dois
        assert "10.1/exists" not in dois
        # The same existing DOI was observed once by each active language query.
        assert report["candidates"]["existing_duplicates"] == 2

    def test_multi_keyword_independence(self, tmp_path: Path):
        """Keywords A and B keep independent cursors; new keyword C starts fresh."""
        notebook_dir = tmp_path / "notebooks"
        store = KeywordNotebookStore(notebook_dir)
        keyword_a = "关键词甲"
        keyword_b = "关键词乙"
        keyword_c = "关键词丙"

        # Pre-seed all definitions; A and B alone have saved cursors.
        query_a = _seed_ready(store, keyword_a, "keyword A")
        query_b = _seed_ready(store, keyword_b, "keyword B")
        _seed_ready(store, keyword_c, "keyword C")
        store.advance_backfill(keyword_a, query_a, "openalex", next_cursor="A5", items_this_page=5)
        store.advance_backfill(keyword_b, query_b, "openalex", next_cursor="B9", items_this_page=5)

        # Run all three (C is new).
        def _make_fake(scripts):
            f = _ScriptedOpenAlex(scripts)
            return f

        scripts_a = {
            ("refresh", INITIAL_CURSOR): ([("10.1/a_r", "A R")], None),
            ("backfill", "A5"): ([("10.1/a_b", "A B")], "A6"),
        }
        scripts_b = {
            ("refresh", INITIAL_CURSOR): ([("10.1/b_r", "B R")], None),
            ("backfill", "B9"): ([("10.1/b_b", "B B")], "B10"),
        }
        scripts_c = {
            ("refresh", INITIAL_CURSOR): ([("10.1/c_r", "C R")], None),
            ("backfill", INITIAL_CURSOR): ([("10.1/c_b", "C B")], "C1"),
        }
        fake_a = _make_fake(scripts_a)
        fake_b = _make_fake(scripts_b)
        fake_c = _make_fake(scripts_c)
        fakes = {keyword_a: fake_a, keyword_b: fake_b, keyword_c: fake_c}

        def _dispatch(provider, query, **kwargs):
            # Route by the Chinese notebook identity.
            ok = kwargs.get("keyword_zh", "")
            if provider == "crossref":
                return _crossref_noop(query, **kwargs)
            return fakes.get(ok, _ScriptedOpenAlex({}))(query, **kwargs)

        for kw in (keyword_a, keyword_b, keyword_c):
            _run_discovery(kw, notebook_dir=notebook_dir, fetch_page=_dispatch)

        # A resumed from A5 → A6; B from B9 → B10; C from * → C1.
        nb_a = store.load(keyword_a)
        nb_b = store.load(keyword_b)
        nb_c = store.load(keyword_c)
        assert any(exp["providers"]["openalex"]["backfill"]["cursor"] == "A6"
                   for exp in _queries(nb_a).values())
        assert any(exp["providers"]["openalex"]["backfill"]["cursor"] == "B10"
                   for exp in _queries(nb_b).values())
        assert any(exp["providers"]["openalex"]["backfill"]["cursor"] == "C1"
                   for exp in _queries(nb_c).values())

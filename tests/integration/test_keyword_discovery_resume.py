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

from src.discovery.contracts.notebook import (
    INITIAL_CURSOR,
    query_identity,
)
from src.discovery.stores.notebook_store import NotebookStoreV4 as KeywordNotebookStore
from src.discovery.coordinator import (
    DiscoveryOptions, _profile_filters, _profile_order, _profile_sort,
    run_discovery_batch,
)
from src.discovery.models import PaperCandidate
from src.discovery.contracts.page_journal import request_signature
from src.discovery.stores.page_journal_store import PageJournalStoreV4 as PageJournalStore
from src.discovery.providers.provider_page_fetcher import CallbackProviderPageFetcher
from src.metadata.schema import empty_metadata
from tests.helpers.fake_provider import discovery_page
from tests.helpers.discovery_workspace import make_test_workspace
from tests.helpers.relevance_profiles import bind_test_relevance_profile, relevance_candidate


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
    bind_test_relevance_profile(store, keyword_zh)
    store.set_enabled(keyword_zh, True)
    notebook = store.require_v4(keyword_zh)
    for entry in notebook["search_queries"].values():
        for provider in ("openalex", "crossref"):
            sort = _profile_sort(notebook, provider, "backfill")
            order = _profile_order(notebook, "backfill")
            signature = request_signature(
                sort=sort,
                filters=_profile_filters(notebook, provider, "backfill", sort, order),
                page_size=10,
            )
            store.ensure_backfill_generation(
                keyword_zh, entry["query_id"], provider,
                request_signature_hash=signature["hash"],
            )
    return query_identity("zh", keyword_zh)


def _page(spec, cursor: str, works, next_cursor, exhausted=None):
    """Build one complete typed provider page from (doi, title) pairs."""
    cands = [
        relevance_candidate(
            title=f"Test candidate {title}", doi=doi, source=spec.key.provider,
        )
        for doi, title in works
    ]
    if exhausted is None:
        exhausted = not next_cursor
    return discovery_page(
        provider=spec.key.provider,
        keyword_zh=spec.keyword_zh,
        query=spec.query,
        lane=spec.key.mode,
        candidates=cands,
        cursor=cursor,
        next_cursor=next_cursor,
        query_id=spec.key.query_id,
        query_language=spec.query_language,
        total_results=len(cands),
        exhausted=exhausted,
    )


class _ScriptedOpenAlex:
    """Returns scripted DiscoveryPage objects keyed by (lane, cursor)."""

    def __init__(self, scripts: dict):
        self.scripts = scripts
        self.calls: list[tuple[str, str]] = []

    def __call__(self, spec, cursor, _client):
        self.calls.append((spec.key.mode, cursor))
        key = (spec.key.mode, cursor)
        if key not in self.scripts:
            return _page(spec, cursor, [], None)
        works, nxt = self.scripts[key]
        return _page(spec, cursor, works, nxt)


def _crossref_noop(spec, cursor, _client):
    return _page(spec, cursor, [], None)


def _provider_fetch(openalex, crossref):
    def _fetch(spec, cursor, client):
        if spec.key.provider == "openalex":
            return openalex(spec, cursor, client)
        if spec.key.provider == "crossref":
            return crossref(spec, cursor, client)
        raise AssertionError(f"unexpected provider: {spec.key.provider}")
    return CallbackProviderPageFetcher(_fetch)


def _run_discovery(keyword_zh: str, *, workspace_root: Path,
                   paper_raw_dir: Path | None = None,
                   papers_dir: Path | None = None,
                   hide_existing: bool = False,
                   page_fetcher) -> tuple[SimpleNamespace, dict]:
    runtime_base = workspace_root / ".discovery_runtime"
    workspace = make_test_workspace(workspace_root)
    options = DiscoveryOptions(
        mode="hybrid",
        refresh_pages=1,
        backfill_pages=1,
        page_size=10,
        max_candidates=50,
        hide_existing=hide_existing,
        workspace=workspace,
        output_dir=runtime_base / "output",
        paper_raw_dir=paper_raw_dir or (runtime_base / "paper_raw"),
        papers_dir=papers_dir or (runtime_base / "papers"),
        ledger_path=(paper_raw_dir.parent / "ledger.json") if paper_raw_dir else (runtime_base / "ledger.json"),
    )
    batch_report = run_discovery_batch(
        [keyword_zh], options=options, max_workers=2, page_fetcher=page_fetcher,
    )
    report_obj = batch_report.keywords[0]
    candidates = []
    journal = PageJournalStore(workspace.page_journals_dir)
    for ref in journal.list_pages([report_obj.keyword_id]):
        data = journal.read(ref.path)
        for item in data.get("candidates", []):
            if item.get("status") in {"emitted", "staged"} and isinstance(item.get("candidate"), dict):
                candidates.append(PaperCandidate.from_dict(item["candidate"]))
    return SimpleNamespace(candidates=candidates), report_obj.to_dict()


class TestKeywordDiscoveryResume:
    def test_second_run_resumes_backfill_and_refreshes_from_start(self, tmp_path: Path):
        workspace_root = tmp_path / "ws"
        paper_raw = tmp_path / "paper_raw"
        papers = tmp_path / "papers"
        keyword_zh = "边界层"
        store = KeywordNotebookStore(workspace_root / "keyword_notebooks")
        _seed_ready(store, keyword_zh, "boundary layer")

        # First run.
        scripts_run1 = {
            ("refresh", INITIAL_CURSOR): ([("10.1/a", "Paper A")], None),
            ("backfill", INITIAL_CURSOR): ([("10.1/b", "Paper B")], "A2"),
        }
        fake = _ScriptedOpenAlex(scripts_run1)
        batch1, report1 = _run_discovery(
            keyword_zh, workspace_root=workspace_root, paper_raw_dir=paper_raw,
            papers_dir=papers, page_fetcher=_provider_fetch(fake, _crossref_noop),
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
            keyword_zh, workspace_root=workspace_root, paper_raw_dir=paper_raw,
            papers_dir=papers, page_fetcher=_provider_fetch(fake2, _crossref_noop),
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
        workspace_root = tmp_path / "ws"
        paper_raw = tmp_path / "paper_raw"
        papers = tmp_path / "papers"
        keyword_zh = "边界层"
        _seed_ready(KeywordNotebookStore(workspace_root / "keyword_notebooks"), keyword_zh, "boundary layer")

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
            keyword_zh, workspace_root=workspace_root, paper_raw_dir=paper_raw,
            papers_dir=papers, hide_existing=True,
            page_fetcher=_provider_fetch(fake, _crossref_noop),
        )
        dois = {c.doi for c in batch.candidates}
        assert "10.1/new" in dois
        assert "10.1/exists" not in dois
        # The same existing DOI was observed once by each active language query.
        assert report["candidates"]["existing_duplicates"] == 2

    def test_multi_keyword_independence(self, tmp_path: Path):
        """Keywords A and B keep independent cursors; new keyword C starts fresh."""
        workspace_root = tmp_path / "ws"
        store = KeywordNotebookStore(workspace_root / "keyword_notebooks")
        keyword_a = "关键词甲"
        keyword_b = "关键词乙"
        keyword_c = "关键词丙"

        # Pre-seed all definitions; A and B obtain saved cursors through the
        # real journal-first path.  A hand-written notebook cursor without a
        # matching durable page is intentionally repair-required in v3.
        _seed_ready(store, keyword_a, "keyword A")
        _seed_ready(store, keyword_b, "keyword B")
        _seed_ready(store, keyword_c, "keyword C")
        _run_discovery(
            keyword_a,
            workspace_root=workspace_root,
            page_fetcher=_provider_fetch(
                _ScriptedOpenAlex({
                    ("refresh", INITIAL_CURSOR): ([('10.1/a_seed', 'A seed')], None),
                    ("backfill", INITIAL_CURSOR): ([('10.1/a_seed_b', 'A seed B')], 'A5'),
                }),
                _crossref_noop,
            ),
        )
        _run_discovery(
            keyword_b,
            workspace_root=workspace_root,
            page_fetcher=_provider_fetch(
                _ScriptedOpenAlex({
                    ("refresh", INITIAL_CURSOR): ([('10.1/b_seed', 'B seed')], None),
                    ("backfill", INITIAL_CURSOR): ([('10.1/b_seed_b', 'B seed B')], 'B9'),
                }),
                _crossref_noop,
            ),
        )

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

        def _dispatch(spec, cursor, client):
            # Route by the Chinese notebook identity.
            if spec.key.provider == "crossref":
                return _crossref_noop(spec, cursor, client)
            return fakes.get(spec.keyword_zh, _ScriptedOpenAlex({}))(spec, cursor, client)

        for kw in (keyword_a, keyword_b, keyword_c):
            _run_discovery(
                kw, workspace_root=workspace_root,
                page_fetcher=CallbackProviderPageFetcher(_dispatch),
            )

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

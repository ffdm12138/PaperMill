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
from unittest.mock import patch

import pytest

from src.discovery.keyword_notebook import (
    INITIAL_CURSOR,
    KeywordNotebookStore,
    composite_backfill_signature,
)
from src.discovery.models import PaperCandidate
from src.discovery.pipeline import discover_papers_dual_lane
from src.discovery.provider_models import DiscoveryPage
from src.metadata.schema import empty_metadata


pytestmark = pytest.mark.integration


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
        original_keyword="",
        expanded_query="",
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

    def __call__(self, query, *, original_keyword, lane, page_size, cursor,
                 sort=None, domain_id=None, rate_limiter=None, limiter_lock=None):
        self.calls.append((lane, cursor))
        key = (lane, cursor)
        if key not in self.scripts:
            return _page([], None)
        works, nxt = self.scripts[key]
        return _page(works, nxt)


def _crossref_noop(*args, **kwargs):
    return _page([], None)


class TestKeywordDiscoveryResume:
    def test_second_run_resumes_backfill_and_refreshes_from_start(self, tmp_path: Path):
        notebook_dir = tmp_path / "notebooks"
        paper_raw = tmp_path / "paper_raw"
        papers = tmp_path / "papers"

        # First run.
        scripts_run1 = {
            ("refresh", INITIAL_CURSOR): ([("10.1/a", "Paper A")], None),
            ("backfill", INITIAL_CURSOR): ([("10.1/b", "Paper B")], "A2"),
        }
        fake = _ScriptedOpenAlex(scripts_run1)
        with patch("src.discovery.pipeline.search_openalex_page", side_effect=fake), \
             patch("src.discovery.pipeline.search_crossref_page", side_effect=_crossref_noop):
            batch1, report1 = discover_papers_dual_lane(
                "boundary layer",
                mode="hybrid", refresh_pages=1, backfill_pages=1, page_size=10,
                notebook_dir=notebook_dir, paper_raw_dir=paper_raw, papers_dir=papers,
            )
        assert report1["status"] == "success"
        dois1 = {c.doi for c in batch1.candidates}
        assert "10.1/a" in dois1 and "10.1/b" in dois1

        # Verify notebook saved cursor A2.
        store = KeywordNotebookStore(notebook_dir)
        nb = store.load("boundary layer")
        saved_cursors = {
            exp["providers"]["openalex"]["backfill"]["cursor"]
            for exp in nb["expansions"].values()
        }
        assert "A2" in saved_cursors

        # Second run: refresh MUST use cursor="*" again; backfill MUST use A2.
        scripts_run2 = {
            ("refresh", INITIAL_CURSOR): ([("10.1/a2", "Paper A2")], None),
            ("backfill", "A2"): ([("10.1/c", "Paper C")], "A3"),
        }
        fake2 = _ScriptedOpenAlex(scripts_run2)
        with patch("src.discovery.pipeline.search_openalex_page", side_effect=fake2), \
             patch("src.discovery.pipeline.search_crossref_page", side_effect=_crossref_noop):
            batch2, report2 = discover_papers_dual_lane(
                "boundary layer",
                mode="hybrid", refresh_pages=1, backfill_pages=1, page_size=10,
                notebook_dir=notebook_dir, paper_raw_dir=paper_raw, papers_dir=papers,
            )
        refresh_cursors = [c for (lane, c) in fake2.calls if lane == "refresh"]
        backfill_cursors = [c for (lane, c) in fake2.calls if lane == "backfill"]
        assert all(c == INITIAL_CURSOR for c in refresh_cursors), "refresh must restart from *"
        assert "A2" in backfill_cursors, "backfill must resume from saved cursor A2"

        # Notebook advanced to A3.
        nb2 = store.load("boundary layer")
        advanced = any(
            exp["providers"]["openalex"]["backfill"]["cursor"] == "A3"
            for exp in nb2["expansions"].values()
        )
        assert advanced, "backfill cursor should have advanced to A3"

    def test_existing_doi_not_re_staged(self, tmp_path: Path):
        """A DOI already in paper_raw must not be staged again."""
        notebook_dir = tmp_path / "notebooks"
        paper_raw = tmp_path / "paper_raw"
        papers = tmp_path / "papers"

        ws = paper_raw / "0000000000000001"
        ws.mkdir(parents=True)
        meta = empty_metadata("0000000000000001", source_type="network_search")
        meta["identifiers"]["doi"] = "10.1/exists"
        (ws / "0000000000000001.metadata.json").write_text(json.dumps(meta), encoding="utf-8")

        scripts = {
            ("refresh", INITIAL_CURSOR): (
                [("10.1/exists", "Existing"), ("10.1/new", "New")],
                None,
            ),
            ("backfill", INITIAL_CURSOR): ([], None),
        }
        fake = _ScriptedOpenAlex(scripts)
        with patch("src.discovery.pipeline.search_openalex_page", side_effect=fake), \
             patch("src.discovery.pipeline.search_crossref_page", side_effect=_crossref_noop):
            batch, report = discover_papers_dual_lane(
                "boundary layer",
                mode="hybrid", refresh_pages=1, backfill_pages=1, page_size=10,
                max_candidates=50, notebook_dir=notebook_dir,
                paper_raw_dir=paper_raw, papers_dir=papers, hide_existing=True,
            )
        dois = {c.doi for c in batch.candidates}
        assert "10.1/new" in dois
        assert "10.1/exists" not in dois
        assert report["candidates"]["existing_duplicates"] == 1

    def test_multi_keyword_independence(self, tmp_path: Path):
        """Keywords A and B keep independent cursors; new keyword C starts fresh."""
        notebook_dir = tmp_path / "notebooks"
        store = KeywordNotebookStore(notebook_dir)
        sig = composite_backfill_signature(page_size=10)

        # Pre-seed A and B with cursors.
        from src.discovery.keyword_notebook import expansion_key
        store.ensure_keyword("keyword A", ["keyword A"], sig)
        store.ensure_keyword("keyword B", ["keyword B"], sig)
        ekey_a = expansion_key("keyword A", sig)
        ekey_b = expansion_key("keyword B", sig)
        store.advance_backfill("keyword A", ekey_a, "openalex", next_cursor="A5", items_this_page=5)
        store.advance_backfill("keyword B", ekey_b, "openalex", next_cursor="B9", items_this_page=5)

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
        fakes = {"keyword A": fake_a, "keyword B": fake_b, "keyword C": fake_c}

        def _dispatch(query, **kwargs):
            # Route by the original_keyword arg.
            ok = kwargs.get("original_keyword", "")
            return fakes.get(ok, _ScriptedOpenAlex({}))(query, **kwargs)

        for kw in ("keyword A", "keyword B", "keyword C"):
            with patch("src.discovery.pipeline.search_openalex_page", side_effect=_dispatch), \
                 patch("src.discovery.pipeline.search_crossref_page", side_effect=_crossref_noop):
                discover_papers_dual_lane(
                    kw, mode="hybrid", refresh_pages=1, backfill_pages=1, page_size=10,
                    notebook_dir=notebook_dir,
                )

        # A resumed from A5 → A6; B from B9 → B10; C from * → C1.
        nb_a = store.load("keyword A")
        nb_b = store.load("keyword B")
        nb_c = store.load("keyword C")
        assert any(exp["providers"]["openalex"]["backfill"]["cursor"] == "A6"
                   for exp in nb_a["expansions"].values())
        assert any(exp["providers"]["openalex"]["backfill"]["cursor"] == "B10"
                   for exp in nb_b["expansions"].values())
        assert any(exp["providers"]["openalex"]["backfill"]["cursor"] == "C1"
                   for exp in nb_c["expansions"].values())

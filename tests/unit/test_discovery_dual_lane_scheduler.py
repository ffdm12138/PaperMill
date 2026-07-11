"""Unit tests for the dual-lane (Refresh + Backfill) discovery scheduler."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.discovery.keyword_notebook import (
    INITIAL_CURSOR,
    KeywordNotebookStore,
    composite_backfill_signature,
    expansion_key,
    pagination_signature,
)
from src.discovery.models import PaperCandidate
from src.discovery.pipeline import discover_papers_dual_lane


pytestmark = pytest.mark.unit


def _cand(doi, title="T", source="openalex"):
    return PaperCandidate(title=title, doi=doi, source=source)


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

    def _fake_openalex(query, *, original_keyword, lane, page_size, cursor, sort=None,
                       domain_id=None, rate_limiter=None, limiter_lock=None):
        calls.append({"provider": "openalex", "lane": lane, "cursor": cursor})
        pages = openalex_pages.get((lane, cursor))
        if pages is None:
            return _FakePage([], next_cursor=None, exhausted=True)
        return pages

    def _fake_crossref(query, *, original_keyword, lane, page_size, cursor, sort=None,
                       order=None, domain_id=None, rate_limiter=None, limiter_lock=None):
        calls.append({"provider": "crossref", "lane": lane, "cursor": cursor})
        pages = crossref_pages.get((lane, cursor))
        if pages is None:
            return _FakePage([], next_cursor=None, exhausted=True)
        return pages

    monkeypatch.setattr("src.discovery.pipeline.search_openalex_page", _fake_openalex)
    monkeypatch.setattr("src.discovery.pipeline.search_crossref_page", _fake_crossref)
    return calls


class TestDualLaneScheduling:
    def test_hybrid_runs_both_refresh_and_backfill(self, monkeypatch, tmp_path: Path):
        calls = _install_fake_fetch(
            monkeypatch,
            openalex_pages={
                ("refresh", INITIAL_CURSOR): _FakePage([_cand("10.1/r1")], next_cursor=None, exhausted=True),
                ("backfill", INITIAL_CURSOR): _FakePage([_cand("10.1/b1")], next_cursor="BF2"),
            },
            crossref_pages={},
        )
        batch, report = discover_papers_dual_lane(
            "test kw",
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
        store = KeywordNotebookStore(tmp_path)
        sig = composite_backfill_signature(page_size=10)
        store.ensure_keyword("test kw", ["test kw"], sig)
        ekey = expansion_key("test kw", sig)
        # Advance backfill cursor so refresh could (incorrectly) pick it up.
        store.advance_backfill("test kw", ekey, "openalex", next_cursor="DEEP", items_this_page=5)
        calls = _install_fake_fetch(
            monkeypatch,
            openalex_pages={
                ("refresh", INITIAL_CURSOR): _FakePage([], next_cursor=None, exhausted=True),
                ("backfill", "DEEP"): _FakePage([], next_cursor=None, exhausted=True),
            },
            crossref_pages={},
        )
        discover_papers_dual_lane(
            "test kw", mode="hybrid", refresh_pages=1, backfill_pages=1,
            page_size=10, notebook_dir=tmp_path,
        )
        refresh_calls = [c for c in calls if c["lane"] == "refresh" and c["provider"] == "openalex"]
        assert all(c["cursor"] == INITIAL_CURSOR for c in refresh_calls)

    def test_backfill_resumes_from_saved_cursor(self, monkeypatch, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = composite_backfill_signature(page_size=10)
        store.ensure_keyword("test kw", ["test kw"], sig)
        ekey = expansion_key("test kw", sig)
        store.advance_backfill("test kw", ekey, "openalex", next_cursor="RESUME_HERE", items_this_page=5)
        calls = _install_fake_fetch(
            monkeypatch,
            openalex_pages={
                ("refresh", INITIAL_CURSOR): _FakePage([], next_cursor=None, exhausted=True),
                ("backfill", "RESUME_HERE"): _FakePage([_cand("10.1/x")], next_cursor="RESUME2"),
            },
            crossref_pages={},
        )
        discover_papers_dual_lane(
            "test kw", mode="hybrid", refresh_pages=1, backfill_pages=1,
            page_size=10, notebook_dir=tmp_path,
        )
        backfill_calls = [c for c in calls if c["lane"] == "backfill" and c["provider"] == "openalex"]
        assert any(c["cursor"] == "RESUME_HERE" for c in backfill_calls)
        # Cursor advanced.
        assert store.get_backfill_cursor("test kw", ekey, "openalex") == "RESUME2"

    def test_refresh_failure_does_not_reset_backfill_cursor(self, monkeypatch, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = composite_backfill_signature(page_size=10)
        store.ensure_keyword("test kw", ["test kw"], sig)
        ekey = expansion_key("test kw", sig)
        store.advance_backfill("test kw", ekey, "openalex", next_cursor="KEEPME", items_this_page=5)
        _install_fake_fetch(
            monkeypatch,
            openalex_pages={
                ("refresh", INITIAL_CURSOR): _FakePage([], next_cursor=None, status="failed", safe_error="boom"),
                ("backfill", "KEEPME"): _FakePage([], next_cursor=None, exhausted=True),
            },
            crossref_pages={},
        )
        discover_papers_dual_lane(
            "test kw", mode="hybrid", refresh_pages=1, backfill_pages=1,
            page_size=10, notebook_dir=tmp_path,
        )
        assert store.get_backfill_cursor("test kw", ekey, "openalex") == "KEEPME"

    def test_backfill_failure_does_not_advance_cursor(self, monkeypatch, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = composite_backfill_signature(page_size=10)
        store.ensure_keyword("test kw", ["test kw"], sig)
        ekey = expansion_key("test kw", sig)
        store.advance_backfill("test kw", ekey, "openalex", next_cursor="BEFORE_FAIL", items_this_page=5)
        _install_fake_fetch(
            monkeypatch,
            openalex_pages={
                ("refresh", INITIAL_CURSOR): _FakePage([], next_cursor=None, exhausted=True),
                ("backfill", "BEFORE_FAIL"): _FakePage([], next_cursor=None, status="failed", safe_error="timeout"),
            },
            crossref_pages={},
        )
        discover_papers_dual_lane(
            "test kw", mode="hybrid", refresh_pages=1, backfill_pages=1,
            page_size=10, notebook_dir=tmp_path,
        )
        assert store.get_backfill_cursor("test kw", ekey, "openalex") == "BEFORE_FAIL"

    def test_existing_dois_filtered_before_max_candidates(self, monkeypatch, tmp_path: Path):
        """Existing DOI observations are terminal and new candidates remain recoverable."""
        paper_raw = tmp_path / "paper_raw"
        papers = tmp_path / "papers"
        # Create 50 existing DOIs in paper_raw.
        from src.metadata.schema import empty_metadata

        for i in range(50):
            ws = paper_raw / f"0000000000000{i:03d}"
            ws.mkdir(parents=True)
            meta = empty_metadata(f"0000000000000{i:03d}", source_type="network_search")
            meta["identifiers"]["doi"] = f"10.1/existing{i}"
            (ws / f"0000000000000{i:03d}.metadata.json").write_text(
                __import__("json").dumps(meta), encoding="utf-8",
            )
        new_cands = [_cand(f"10.1/new{i}") for i in range(50)]
        existing_cands = [_cand(f"10.1/existing{i}") for i in range(50)]
        all_cands = existing_cands + new_cands
        _install_fake_fetch(
            monkeypatch,
            openalex_pages={
                ("refresh", INITIAL_CURSOR): _FakePage(all_cands, next_cursor=None, exhausted=True),
                ("backfill", INITIAL_CURSOR): _FakePage([], next_cursor=None, exhausted=True),
            },
            crossref_pages={},
        )
        batch, report = discover_papers_dual_lane(
            "test kw", mode="hybrid", refresh_pages=1, backfill_pages=1,
            page_size=200, max_candidates=100, notebook_dir=tmp_path,
            paper_raw_dir=paper_raw, papers_dir=papers, hide_existing=True,
        )
        assert len(batch.candidates) == 50
        assert all(not c.existing_duplicate_refs for c in batch.candidates)
        assert report["candidates"]["existing_duplicates"] == 50

    def test_provider_failure_reported_as_partial_success(self, monkeypatch, tmp_path: Path):
        _install_fake_fetch(
            monkeypatch,
            openalex_pages={
                ("refresh", INITIAL_CURSOR): _FakePage([_cand("10.1/ok")], next_cursor=None, exhausted=True),
                ("backfill", INITIAL_CURSOR): _FakePage([], next_cursor=None, status="failed", safe_error="err"),
            },
            crossref_pages={},
        )
        _, report = discover_papers_dual_lane(
            "test kw", mode="hybrid", refresh_pages=1, backfill_pages=1,
            page_size=10, notebook_dir=tmp_path,
        )
        assert report["status"] == "partial_success"
        assert report["backfill"]["provider_failures"] >= 1

    def test_skipped_when_keyword_disabled(self, monkeypatch, tmp_path: Path):
        store = KeywordNotebookStore(tmp_path)
        sig = pagination_signature()
        store.ensure_keyword("test kw", ["test kw"], sig)
        store.set_enabled("test kw", False)
        calls = _install_fake_fetch(monkeypatch, {}, {})
        batch, report = discover_papers_dual_lane(
            "test kw", mode="hybrid", refresh_pages=1, backfill_pages=1,
            page_size=10, notebook_dir=tmp_path,
        )
        assert report["status"] == "skipped"
        assert calls == []
        assert batch.candidates == []

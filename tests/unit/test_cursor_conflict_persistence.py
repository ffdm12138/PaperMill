"""Unit tests for cursor_conflicts counter persistence (Phase 0.6).

Verifies that ``commit_backfill_cursor`` persists the ``cursor_conflicts``
counter to disk BEFORE raising ``CursorConflictError``. Before the fix,
the counter was incremented inside the mutator, but the exception
prevented ``_save()`` from running, so the increment was lost.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.discovery.keyword_notebook import (
    CursorConflictError,
    INITIAL_CURSOR,
    KeywordNotebookStore,
    pagination_signature,
    query_identity,
)


pytestmark = pytest.mark.unit


def _setup_store(tmp_path: Path) -> tuple[KeywordNotebookStore, str, str]:
    """Create a store with one keyword + expansion ready for backfill."""
    store = KeywordNotebookStore(tmp_path)
    sig = pagination_signature()
    keyword = "测试关键词"
    store.ensure_notebook(keyword)
    store.sync_search_queries(
        keyword,
        add=[{"query": keyword, "language": "zh"}],
        pag_sig=sig,
    )
    query_id_value = query_identity("zh", keyword)
    return store, keyword, query_id_value


def test_cursor_conflict_persists_counter(tmp_path: Path):
    """cursor_conflicts must be saved to disk before CursorConflictError.

    BEFORE fix: the increment is inside the mutator, but the exception
    prevents _save() from running, so the counter is lost.
    AFTER fix: the mutator returns normally (counter incremented), _save
    runs, THEN the exception is raised.
    """
    store, keyword, ekey = _setup_store(tmp_path)
    provider = "openalex"

    # Advance cursor to "A2".
    store.commit_backfill_cursor(
        keyword, ekey, provider,
        expected_cursor=INITIAL_CURSOR, next_cursor="A2",
        committed_page_id="p1", exhausted=False, items_this_page=5,
    )

    # Now try to commit with a STALE expected_cursor — must conflict.
    with pytest.raises(CursorConflictError):
        store.commit_backfill_cursor(
            keyword, ekey, provider,
            expected_cursor=INITIAL_CURSOR,  # stale!
            next_cursor="A3",
            committed_page_id="p2", exhausted=False, items_this_page=3,
        )

    # Read the notebook DIRECTLY from disk (bypass store cache).
    nb_path = store._path_for(keyword)
    nb = json.loads(nb_path.read_text(encoding="utf-8"))
    queries = nb["search_queries"]
    bf = queries[ekey]["providers"][provider]["backfill"]

    # The counter MUST have been persisted.
    assert bf["cursor_conflicts"] == 1, (
        f"cursor_conflicts should be 1 after one conflict, got {bf['cursor_conflicts']}"
    )
    # The cursor must NOT have advanced (conflict, not success).
    assert bf["cursor"] == "A2", (
        f"cursor should still be 'A2', got {bf['cursor']!r}"
    )


def test_cursor_conflict_does_not_change_committed_count(tmp_path: Path):
    """A cursor conflict must not change pages_committed or pages_succeeded."""
    store, keyword, ekey = _setup_store(tmp_path)
    provider = "openalex"

    # One successful commit.
    store.commit_backfill_cursor(
        keyword, ekey, provider,
        expected_cursor=INITIAL_CURSOR, next_cursor="A2",
        committed_page_id="p1", exhausted=False, items_this_page=5,
    )

    # Two conflicts (stale expected_cursor).
    for _ in range(2):
        with pytest.raises(CursorConflictError):
            store.commit_backfill_cursor(
                keyword, ekey, provider,
                expected_cursor=INITIAL_CURSOR,
                next_cursor="A3",
                committed_page_id="p2", exhausted=False, items_this_page=3,
            )

    nb_path = store._path_for(keyword)
    nb = json.loads(nb_path.read_text(encoding="utf-8"))
    queries = nb["search_queries"]
    bf = queries[ekey]["providers"][provider]["backfill"]

    assert bf["pages_committed"] == 1, (
        f"pages_committed should be 1 (only the success), got {bf['pages_committed']}"
    )
    assert bf["pages_succeeded"] == 1, (
        f"pages_succeeded should be 1, got {bf['pages_succeeded']}"
    )
    assert bf["cursor_conflicts"] == 2, (
        f"cursor_conflicts should be 2, got {bf['cursor_conflicts']}"
    )


def test_cursor_conflict_does_not_change_last_committed_page(tmp_path: Path):
    """Last committed page ID must remain from the successful commit."""
    store, keyword, ekey = _setup_store(tmp_path)
    provider = "openalex"

    store.commit_backfill_cursor(
        keyword, ekey, provider,
        expected_cursor=INITIAL_CURSOR, next_cursor="A2",
        committed_page_id="page-success-001", exhausted=False, items_this_page=5,
    )

    with pytest.raises(CursorConflictError):
        store.commit_backfill_cursor(
            keyword, ekey, provider,
            expected_cursor=INITIAL_CURSOR,
            next_cursor="A3",
            committed_page_id="page-conflict-002", exhausted=False, items_this_page=3,
        )

    nb_path = store._path_for(keyword)
    nb = json.loads(nb_path.read_text(encoding="utf-8"))
    queries = nb["search_queries"]
    bf = queries[ekey]["providers"][provider]["backfill"]

    assert bf["last_committed_page_id"] == "page-success-001", (
        f"last_committed_page_id should be 'page-success-001', got {bf['last_committed_page_id']!r}"
    )

from __future__ import annotations

from pathlib import Path

import pytest

from src.discovery.contracts.notebook import CursorConflictError, query_identity
from src.discovery.stores.notebook_store import NotebookStoreV4 as KeywordNotebookStore
from src.discovery.contracts.page_journal import INITIAL_CURSOR, request_signature


pytestmark = pytest.mark.unit


def test_cursor_cas_rejects_stale_writer_and_never_rolls_backward(tmp_path: Path):
    store = KeywordNotebookStore(tmp_path)
    sig = request_signature(page_size=10)["hash"]
    keyword = "测试关键词"
    store.ensure_notebook(keyword)
    store.sync_search_queries(
        keyword,
        add=[{"query": keyword, "language": "zh"}],
        pag_sig=sig,
    )
    query_id_value = query_identity("zh", keyword)
    store.commit_backfill_cursor(
        keyword,
        query_id_value,
        "openalex",
        expected_cursor=INITIAL_CURSOR,
        next_cursor="CURSOR-2",
        committed_page_id="page-1",
        exhausted=False,
        items_this_page=1,
    )
    with pytest.raises(CursorConflictError):
        store.commit_backfill_cursor(
            keyword,
            query_id_value,
            "openalex",
            expected_cursor=INITIAL_CURSOR,
            next_cursor="CURSOR-OLD",
            committed_page_id="page-old",
            exhausted=False,
            items_this_page=1,
        )
    assert store.get_backfill_cursor(keyword, query_id_value, "openalex") == "CURSOR-2"

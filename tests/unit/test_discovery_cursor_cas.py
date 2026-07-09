from __future__ import annotations

from pathlib import Path

import pytest

from src.discovery.keyword_notebook import CursorConflictError, KeywordNotebookStore, expansion_key
from src.discovery.page_journal import INITIAL_CURSOR, request_signature


pytestmark = pytest.mark.unit


def test_cursor_cas_rejects_stale_writer_and_never_rolls_backward(tmp_path: Path):
    store = KeywordNotebookStore(tmp_path)
    sig = request_signature(page_size=10)["hash"]
    store.ensure_keyword("keyword", ["keyword"], sig)
    ekey = expansion_key("keyword", sig)
    store.commit_backfill_cursor(
        "keyword",
        ekey,
        "openalex",
        expected_cursor=INITIAL_CURSOR,
        next_cursor="CURSOR-2",
        committed_page_id="page-1",
        exhausted=False,
        items_this_page=1,
    )
    with pytest.raises(CursorConflictError):
        store.commit_backfill_cursor(
            "keyword",
            ekey,
            "openalex",
            expected_cursor=INITIAL_CURSOR,
            next_cursor="CURSOR-OLD",
            committed_page_id="page-old",
            exhausted=False,
            items_this_page=1,
        )
    assert store.get_backfill_cursor("keyword", ekey, "openalex") == "CURSOR-2"

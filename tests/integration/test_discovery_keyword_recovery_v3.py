from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.recover_discovery_keyword_notebooks as recovery
from src.discovery.keyword_notebook import KeywordNotebookStore
from src.discovery.models import PaperCandidate
from src.discovery.page_journal import PageJournalStore, request_signature
from src.discovery.keyword_notebook import query_identity


pytestmark = pytest.mark.integration


def test_recovery_cli_inspect_is_explicitly_read_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
):
    notebooks = tmp_path / "notebooks"
    pages = tmp_path / "pages"
    locks = tmp_path / "locks"
    for path in (notebooks, pages, locks):
        path.mkdir()
    store = KeywordNotebookStore(notebooks)
    store.ensure_notebook("风吹雪")
    signature = request_signature(page_size=10)
    store.sync_search_queries("风吹雪", add=[
        {"query": "风吹雪", "language": "zh"},
        {"query": "blowing snow", "language": "en"},
    ], pag_sig=signature["hash"])
    notebook = store.require_v3("风吹雪")
    qid = query_identity("en", "blowing snow")
    journal = PageJournalStore(pages)
    page = journal.make_synthetic_page(
        page_id="page-1", keyword_id=notebook["keyword_id"], keyword_zh="风吹雪",
        query_id=qid, query="blowing snow", query_language="en", provider="openalex",
        lane="backfill", generation=1, request_signature_value=signature,
        request_cursor=None, next_cursor="opaque-next", provider_exhausted=False,
        candidates=[PaperCandidate(title="T", doi="10.1234/integration")],
    )
    page_path = journal.page_path(
        keyword_id=notebook["keyword_id"], query_id=qid, provider="openalex",
        lane="backfill", page_id="page-1",
    )
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(recovery, "DISCOVERY_KEYWORD_NOTEBOOK_DIR", notebooks)
    monkeypatch.setattr(recovery, "DISCOVERY_PENDING_PAGES_DIR", pages)
    monkeypatch.setattr(recovery, "DISCOVERY_LOCKS_DIR", locks)
    monkeypatch.setattr(recovery, "CATALOG_FOLDER_ROOT", tmp_path / "catalog")
    before = {
        path: path.read_bytes()
        for path in tmp_path.rglob("*") if path.is_file()
    }
    assert recovery.main([
        "--inspect", "--notebook-dir", str(notebooks),
        "--pending-pages-dir", str(pages),
        "--locks-dir", str(locks), "--catalog-root", str(tmp_path / "catalog"),
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["inspect_only"] is True
    assert payload["applied"] is False
    assert payload["errors"] == []
    after = {
        path: path.read_bytes()
        for path in tmp_path.rglob("*") if path.is_file()
    }
    assert before == after

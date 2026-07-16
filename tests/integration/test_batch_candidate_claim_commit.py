from pathlib import Path

import pytest

from src.discovery.models import PaperCandidate
from src.discovery.page_journal import PageJournalStore, request_signature
from src.discovery.keyword_notebook import keyword_id, query_identity

pytestmark = pytest.mark.integration


def test_claim_carries_page_context_without_second_read(tmp_path: Path):
    store = PageJournalStore(tmp_path / "pages")
    page = store.make_page(
        page_id="page", keyword_id=keyword_id("关键词"), keyword_zh="关键词",
        query_id=query_identity("en", "keyword"),
        query="keyword", query_language="en", provider="openalex", lane="refresh",
        request_signature_value=request_signature(page_size=10), request_cursor="*",
        next_cursor=None, provider_exhausted=True,
        candidates=[PaperCandidate(title="P", doi="10.9001/p")], state="cursor_committed")
    path = store.write_page(page)
    claim = store.claim_candidates_from_page(
        path, worker_id="worker", lease_seconds=300, limit=1)[0]
    assert (claim.page_id, claim.keyword_id, claim.provider, claim.doi) == (
        "page", keyword_id("关键词"), "openalex", "10.9001/p")
    assert claim.payload["candidate_id"] == claim.candidate_id

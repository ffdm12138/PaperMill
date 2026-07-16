from dataclasses import dataclass

import pytest

from src.discovery.coordinator import DiscoveryOptions, run_discovery_batch
from src.discovery.models import PaperCandidate
from tests.helpers.relevance_profiles import bind_test_relevance_profile
from src.discovery.keyword_notebook import KeywordNotebookStore


pytestmark = pytest.mark.unit


@dataclass
class _Page:
    candidates: list[PaperCandidate]
    next_cursor: str | None = None
    exhausted: bool = True
    status: str = "success"
    safe_error: str | None = None
    error_type: str | None = None

    @property
    def returned_count(self) -> int:
        return len(self.candidates)


def test_provider_limiters_use_provider_scoped_locks(tmp_path):
    seen_lock_ids: dict[str, set[int]] = {"openalex": set(), "crossref": set()}

    def fetch(provider: str, query: str, **kwargs):
        seen_lock_ids[provider].add(id(kwargs["limiter_lock"]))
        return _Page([PaperCandidate(title=f"{provider} {query}", doi=f"10.1234/{provider}-{query}")])

    options = DiscoveryOptions(
        mode="refresh",
        refresh_pages=1,
        backfill_pages=1,
        max_candidates=10,
        notebook_dir=tmp_path / "notebooks",
        pending_pages_dir=tmp_path / "pages",
        locks_dir=tmp_path / "locks",
        exports_dir=tmp_path / "exports",
        output_dir=tmp_path / "out",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
    )
    store = KeywordNotebookStore(options.notebook_dir)
    for keyword, english in (("主题甲", "alpha"), ("主题乙", "beta")):
        store.create_notebook(keyword, enabled=False, search_queries=[
            {"query": keyword, "language": "zh", "source": "pytest"},
            {"query": english, "language": "en", "source": "pytest"},
        ])
        bind_test_relevance_profile(store, keyword)
        store.set_enabled(keyword, True)

    run_discovery_batch(["主题甲", "主题乙"], options=options, max_workers=2, fetch_page=fetch)

    assert len(seen_lock_ids["openalex"]) == 1
    assert len(seen_lock_ids["crossref"]) == 1
    assert seen_lock_ids["openalex"] != seen_lock_ids["crossref"]

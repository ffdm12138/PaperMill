"""Provider client wiring tests for the typed discovery fetch boundary."""

import pytest

from src.discovery.coordinator import DiscoveryOptions, run_discovery_batch
from src.discovery.models import PaperCandidate
from src.discovery.providers.provider_page_fetcher import CallbackProviderPageFetcher
from tests.helpers.discovery_workspace import make_test_workspace
from tests.helpers.relevance_profiles import bind_test_relevance_profile
from src.discovery.stores.notebook_store import NotebookStoreV4 as KeywordNotebookStore
from tests.helpers.fake_provider import discovery_page


pytestmark = pytest.mark.unit


def _options(tmp_path):
    workspace = make_test_workspace(
        tmp_path,
        notebook_dir=tmp_path / "notebooks",
        page_journals_dir=tmp_path / "pages",
        locks_dir=tmp_path / "locks",
        exports_dir=tmp_path / "exports",
    )
    return DiscoveryOptions(
        mode="refresh",
        refresh_pages=1,
        backfill_pages=1,
        max_candidates=10,
        workspace=workspace,
        output_dir=tmp_path / "out",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
    )


def _seed(notebook_dir):
    store = KeywordNotebookStore(notebook_dir)
    for keyword, english in (("主题甲", "alpha"), ("主题乙", "beta")):
        store.create_notebook(keyword, enabled=False, search_queries=[
            {"query": keyword, "language": "zh", "source": "pytest"},
            {"query": english, "language": "en", "source": "pytest"},
        ])
        bind_test_relevance_profile(store, keyword)
        store.set_enabled(keyword, True)


def test_typed_fetcher_receives_one_shared_provider_limiter_per_batch(tmp_path):
    """Every physical lane gets the batch client supplied by the runtime."""
    seen_limiter_ids: dict[str, set[int]] = {"openalex": set(), "crossref": set()}

    def fetch(spec, cursor, client):
        seen_limiter_ids[spec.key.provider].add(id(client._limiter))
        return discovery_page(
            provider=spec.key.provider,
            keyword_zh=spec.keyword_zh,
            query=spec.query,
            lane=spec.key.mode,
            cursor=cursor,
            candidates=[PaperCandidate(title=f"{spec.key.provider} {spec.query}")],
            query_id=spec.key.query_id,
            query_language=spec.query_language,
            exhausted=True,
        )

    options = _options(tmp_path)
    _seed(options.workspace.keyword_notebook_dir)
    run_discovery_batch(
        ["主题甲", "主题乙"], options=options, max_workers=2,
        page_fetcher=CallbackProviderPageFetcher(fetch),
    )

    assert len(seen_limiter_ids["openalex"]) == 1
    assert len(seen_limiter_ids["crossref"]) == 1
    assert seen_limiter_ids["openalex"] != seen_limiter_ids["crossref"]


def test_typed_fetcher_receives_batch_bound_clients(tmp_path):
    """The coordinator no longer accepts loose callback/client arguments."""
    seen_clients: list[object] = []

    def fetch(spec, cursor, client):
        seen_clients.append(client)
        return discovery_page(
            provider=spec.key.provider,
            keyword_zh=spec.keyword_zh,
            query=spec.query,
            lane=spec.key.mode,
            cursor=cursor,
            candidates=[PaperCandidate(title=f"{spec.key.provider} {spec.query}")],
            query_id=spec.key.query_id,
            query_language=spec.query_language,
            exhausted=True,
        )

    options = _options(tmp_path)
    _seed(options.workspace.keyword_notebook_dir)

    run_discovery_batch(
        ["主题甲", "主题乙"], options=options, max_workers=2,
        page_fetcher=CallbackProviderPageFetcher(fetch),
    )

    assert seen_clients, "expected batch-bound provider clients"
    openalex_limiters = {id(c._limiter) for c in seen_clients if c.provider == "openalex"}
    crossref_limiters = {id(c._limiter) for c in seen_clients if c.provider == "crossref"}
    assert len(openalex_limiters) == 1
    assert len(crossref_limiters) == 1
    assert openalex_limiters != crossref_limiters

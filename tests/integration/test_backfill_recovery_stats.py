"""Integration coverage for strict v3 backfill recovery accounting."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.discovery.backfill_transaction import run_backfill_page_transaction
from src.discovery.keyword_notebook import KeywordNotebookStore, query_identity
from src.discovery.execution.lane_models import DiscoveryLaneKey, LaneExecutionSpec, RequestSignature
from src.discovery.models import PaperCandidate
from src.discovery.page_journal import (
    INITIAL_CURSOR,
    PageJournalStore,
    backfill_page_id,
    request_signature,
)
from src.discovery.providers.provider_page_fetcher import CallbackProviderPageFetcher
from tests.helpers.fake_provider import discovery_page
from tests.helpers.relevance_profiles import finalize_all_passed


pytestmark = pytest.mark.integration

KEYWORD_ZH = "风吹雪"
QUERY = "blowing snow"
QUERY_ID = query_identity("en", QUERY)


def _seed(notebook: KeywordNotebookStore, signature_hash: str) -> dict:
    notebook.ensure_notebook(KEYWORD_ZH)
    notebook.sync_search_queries(
        KEYWORD_ZH,
        add=[{"query": QUERY, "language": "en"}],
        pag_sig=signature_hash,
    )
    return notebook.require_v3(KEYWORD_ZH)


def _spec(notebook: KeywordNotebookStore, nb: dict, signature: dict) -> LaneExecutionSpec:
    state = notebook.ensure_backfill_generation(
        KEYWORD_ZH, QUERY_ID, "openalex", request_signature_hash=signature["hash"],
    )
    typed = RequestSignature.from_dict_strict(signature)
    return LaneExecutionSpec(
        key=DiscoveryLaneKey(
            keyword_id=nb["keyword_id"],
            query_id=QUERY_ID,
            provider="openalex",
            mode="backfill",
            generation=int(state["generation"]),
            request_signature=typed.hash,
        ),
        request_signature=typed,
        keyword_zh=KEYWORD_ZH,
        query=QUERY,
        query_language="en",
        relevance_profile_hash="test-hash",
    )


def _fetcher(callback) -> CallbackProviderPageFetcher:
    return CallbackProviderPageFetcher(callback)


def _setup_recovery(tmp_path: Path, *, exhausted: bool = False, next_cursor: str | None = "NEXT"):
    """Create a v3 durable page after cursor CAS but before journal mark."""
    notebook = KeywordNotebookStore(tmp_path / "notebooks")
    journal = PageJournalStore(tmp_path / "pages")
    signature = request_signature(page_size=10)
    nb = _seed(notebook, signature["hash"])
    spec = _spec(notebook, nb, signature)
    page_id = backfill_page_id(
        keyword_id=nb["keyword_id"],
        query_id=QUERY_ID,
        provider="openalex",
        request_signature_hash=signature["hash"],
        request_cursor=INITIAL_CURSOR,
    )
    page = journal.make_synthetic_page(
        page_id=page_id,
        keyword_id=nb["keyword_id"],
        keyword_zh=KEYWORD_ZH,
        query_id=QUERY_ID,
        query=QUERY,
        query_language="en",
        provider="openalex",
        lane="backfill",
        lane_key=spec.key,
        generation=spec.key.generation,
        request_signature_value=signature,
        request_cursor=INITIAL_CURSOR,
        next_cursor=next_cursor,
        provider_exhausted=exhausted,
        candidates=[PaperCandidate(title="T", doi="10.1234/recover")],
        relevance_profile_hash="test-hash",
        state="fetched",
    )
    page_path = journal.write_page(page)
    notebook.commit_backfill_cursor(
        KEYWORD_ZH,
        QUERY_ID,
        "openalex",
        expected_cursor=INITIAL_CURSOR,
        next_cursor=next_cursor,
        committed_page_id=page_id,
        exhausted=exhausted,
        items_this_page=1,
        exhaustion_evidence=page["exhaustion_evidence"],
    )
    return notebook, journal, spec, page_path


def _run(notebook, journal, spec, tmp_path: Path, callback):
    return run_backfill_page_transaction(
        spec,
        notebook_store=notebook,
        journal_store=journal,
        locks_dir=tmp_path / "locks",
        finalize_page=lambda path: finalize_all_passed(journal, path),
        page_fetcher=_fetcher(callback),
        client=object(),  # type: ignore[arg-type]
    )


def test_recover_then_fetch_next_page_carries_recovery_stats(tmp_path: Path):
    notebook, journal, spec, _page_path = _setup_recovery(tmp_path)
    before = notebook.get_backfill_state(KEYWORD_ZH, QUERY_ID, "openalex")

    def fetch(lane_spec, cursor, _client):
        assert cursor == "NEXT"
        return discovery_page(
            provider=lane_spec.key.provider,
            keyword_zh=lane_spec.keyword_zh,
            query=lane_spec.query,
            lane=lane_spec.key.mode,
            cursor=cursor,
            query_id=lane_spec.key.query_id,
            query_language=lane_spec.query_language,
            candidates=[PaperCandidate(title="T2", doi="10.1234/next")],
            next_cursor="NEXT2",
        )

    result = _run(notebook, journal, spec, tmp_path, fetch)
    assert result.status == "success"
    assert result.pages_recovered == 1
    assert result.journals_recovered == 1
    assert result.pages_requested == 1
    state = notebook.get_backfill_state(KEYWORD_ZH, QUERY_ID, "openalex")
    assert state["items_returned_total"] == before["items_returned_total"] + 1
    assert state["pages_committed"] == before["pages_committed"] + 1


def test_recover_exhausted_page_carries_evidence_and_never_fetches(tmp_path: Path):
    notebook, journal, spec, page_path = _setup_recovery(
        tmp_path, exhausted=True, next_cursor=None,
    )
    result = _run(
        notebook,
        journal,
        spec,
        tmp_path,
        lambda _spec, _cursor, _client: pytest.fail("exhausted recovery must not fetch"),
    )
    assert result.status == "exhausted"
    assert result.pages_recovered == 1
    assert result.journals_recovered == 1
    assert result.exhaustion_evidence is not None
    assert journal.read(page_path)["state"] == "cursor_committed"


def test_recover_then_retryable_provider_failure_preserves_recovery_stats(tmp_path: Path):
    notebook, journal, spec, _page_path = _setup_recovery(tmp_path)

    def fetch(lane_spec, cursor, _client):
        return discovery_page(
            provider=lane_spec.key.provider,
            keyword_zh=lane_spec.keyword_zh,
            query=lane_spec.query,
            lane=lane_spec.key.mode,
            cursor=cursor,
            query_id=lane_spec.key.query_id,
            query_language=lane_spec.query_language,
            status="failed",
            error_type="provider_retryable",
            safe_error="provider down",
            failure_class="retryable",
        )

    result = _run(notebook, journal, spec, tmp_path, fetch)
    assert result.status == "failed_retryable"
    assert result.error_type == "provider_retryable"
    assert result.pages_recovered == 1
    assert result.journals_recovered == 1


def test_clean_page_has_zero_recovery_stats(tmp_path: Path):
    notebook = KeywordNotebookStore(tmp_path / "notebooks")
    journal = PageJournalStore(tmp_path / "pages")
    signature = request_signature(page_size=10)
    spec = _spec(notebook, _seed(notebook, signature["hash"]), signature)

    def fetch(lane_spec, cursor, _client):
        return discovery_page(
            provider=lane_spec.key.provider,
            keyword_zh=lane_spec.keyword_zh,
            query=lane_spec.query,
            lane=lane_spec.key.mode,
            cursor=cursor,
            query_id=lane_spec.key.query_id,
            query_language=lane_spec.query_language,
            candidates=[PaperCandidate(title="T", doi="10.1/x")],
            next_cursor="NEXT",
        )

    result = _run(notebook, journal, spec, tmp_path, fetch)
    assert result.status == "success"
    assert result.pages_recovered == 0
    assert result.journals_recovered == 0
    assert result.pages_requested == 1

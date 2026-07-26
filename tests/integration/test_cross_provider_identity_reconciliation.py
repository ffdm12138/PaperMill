from __future__ import annotations

from pathlib import Path

import pytest

from src.discovery.staging_context import DiscoveryStagingContext
from src.library.paper_number_ledger import PaperNumberLedger
from src.staging.network_metadata_staging import stage_network_metadata_records
from tests.factories.discovery_factory import create_discovery_candidate
from tests.factories.paper_raw_factory import (
    create_multi_identity_workspace,
    create_network_metadata_workspace,
)


pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("receipt_provider", "candidate_provider", "candidate_id", "page_id"),
    [
        ("openalex", "crossref", "candidate-b", "page-b"),
        ("crossref", "openalex", "candidate-a", "page-a"),
    ],
)
def test_cross_provider_candidate_reuses_reserved_multi_identity_workspace(
    tmp_path: Path, receipt_provider: str, candidate_provider: str,
    candidate_id: str, page_id: str,
):
    doi = "10.9300/multi"
    workspace = create_multi_identity_workspace(
        tmp_path, doi=doi, receipt_provider=receipt_provider)
    ledger_path = tmp_path / "ledger.json"
    context = DiscoveryStagingContext.create(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=ledger_path,
    )
    before = PaperNumberLedger(ledger_path).load()["max_number"]
    before_dirs = {path.name for path in (tmp_path / "paper_raw").iterdir() if path.is_dir()}

    assert context.registry.workspace_id_index.lookup(
        provider="openalex", keyword_id="keyword-multi", page_id="page-a",
        candidate_id="candidate-a", normalized_doi=doi,
    )
    assert context.registry.workspace_id_index.lookup(
        provider="crossref", keyword_id="keyword-multi", page_id="page-b",
        candidate_id="candidate-b", normalized_doi=doi,
    )

    report = stage_network_metadata_records(
        [create_discovery_candidate(
            doi=doi, provider=candidate_provider, candidate_id=candidate_id,
            page_id=page_id, keyword_id="keyword-multi")],
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=ledger_path, apply=True, transaction=context.transaction,
    )

    assert report["items"][0]["status"] == "staged"
    assert report["items"][0]["paper_number"] == workspace.name
    assert PaperNumberLedger(ledger_path).load()["max_number"] == before
    assert {path.name for path in (tmp_path / "paper_raw").iterdir() if path.is_dir()} == before_dirs
    frozen = context.transaction.registry_snapshot.workspace_id_index.freeze()
    assert frozen.lookup(
        provider="openalex", keyword_id="keyword-multi", page_id="page-a",
        candidate_id="candidate-a", normalized_doi=doi,
    )
    assert frozen.lookup(
        provider="crossref", keyword_id="keyword-multi", page_id="page-b",
        candidate_id="candidate-b", normalized_doi=doi,
    )


def test_explicit_reuse_number_cannot_be_overridden_by_identity_hit(tmp_path: Path):
    doi = "10.9300/requested-reuse"
    workspace = create_network_metadata_workspace(tmp_path, doi=doi)
    ledger = PaperNumberLedger(tmp_path / "ledger.json")
    context = DiscoveryStagingContext.create(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=ledger.path,
    )
    before = ledger.path.read_bytes()
    requested = "0000000000009999"

    report = stage_network_metadata_records(
        [create_discovery_candidate(
            doi=doi, provider="crossref", candidate_id="candidate-1",
            page_id="page-1", keyword_id="keyword-1")],
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=ledger.path, apply=True, transaction=context.transaction,
        reuse_paper_number=requested,
    )

    item = report["items"][0]
    assert item["status"] == "failed_retryable"
    assert item["safe_error"] == "RequestedReuseIdentityMismatch"
    assert ledger.path.read_bytes() == before
    assert {path.name for path in (tmp_path / "paper_raw").iterdir() if path.is_dir()} == {
        workspace.name}

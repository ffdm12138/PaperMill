"""Formal DOI decisions are made by the stage transaction registry."""
from pathlib import Path

from src.discovery.stage_transaction import NormalizedDiscoveryCandidate
from src.discovery.staging_context import DiscoveryStagingContext
from src.staging.network_metadata_staging import _metadata_from_record
from tests.factories.paper_raw_factory import create_active_formal_workspace


def test_formal_primary_is_reported_as_duplicate(tmp_path: Path):
    formal = create_active_formal_workspace(tmp_path, doi="10.1000/formal")
    context = DiscoveryStagingContext.create(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json")
    candidate = NormalizedDiscoveryCandidate(
        candidate_id="other", page_id="page", keyword_id="kw", provider="crossref",
        normalized_doi="10.1000/formal",
        metadata=_metadata_from_record({"title": "Other", "year": 2026, "doi": "10.1000/formal"}),
    )
    result = context.transaction.stage_candidate(candidate, source_record={"record": {}}, apply=True)
    assert result.status == "duplicate"
    assert result.paper_number == "0000000000000001"

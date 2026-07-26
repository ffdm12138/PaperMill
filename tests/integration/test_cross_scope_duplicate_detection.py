import json
from pathlib import Path

import pytest

from src.discovery.stage_transaction import NormalizedDiscoveryCandidate
from src.discovery.staging_context import DiscoveryStagingContext
from src.library.paper_number_ledger import PaperNumberLedger
from src.staging.network_metadata_staging import _metadata_from_record
from tests.factories.paper_raw_factory import (
    activate_minimal_formal_publication,
    create_network_metadata_workspaces_bulk,
)

pytestmark = pytest.mark.integration


def test_cross_scope_doi_collision_fails_closed(tmp_path: Path):
    create_network_metadata_workspaces_bulk(tmp_path, count=2)
    ledger = PaperNumberLedger(tmp_path / "ledger.json")
    number_a, number_b = sorted(ledger.load()["items"])
    raw_a = tmp_path / "paper_raw" / number_a
    formal = tmp_path / "papers" / "formal-a"
    activate_minimal_formal_publication(ledger, raw_a, formal)

    raw_b = tmp_path / "paper_raw" / number_b
    old, new = "10.7000/bench.2", "10.7000/bench.1"
    for path in [*raw_b.glob("*.json"), *(raw_b / "source_records").glob("*.json")]:
        path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")

    context = DiscoveryStagingContext.create(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json")
    candidate = NormalizedDiscoveryCandidate(
        candidate_id="new", page_id="new-page", keyword_id="keyword-1",
        provider="crossref", normalized_doi=new,
        metadata=_metadata_from_record({"title": "New", "year": 2026, "doi": new}))
    result = context.transaction.stage_candidate(candidate, source_record={"record": {}}, apply=True)
    assert result.status == "repair_required"
    assert result.error and result.error.code == "cross_scope_duplicate"
    assert ledger.load()["max_number"] == number_b


def test_cross_scope_collision_precedes_exact_raw_identity_reuse(tmp_path: Path):
    create_network_metadata_workspaces_bulk(tmp_path, count=2)
    ledger = PaperNumberLedger(tmp_path / "ledger.json")
    number_a, number_b = sorted(ledger.load()["items"])
    raw_a = tmp_path / "paper_raw" / number_a
    formal = tmp_path / "papers" / "formal-a"
    activate_minimal_formal_publication(ledger, raw_a, formal)

    raw_b = tmp_path / "paper_raw" / number_b
    old, new = "10.7000/bench.2", "10.7000/bench.1"
    for path in [*raw_b.glob("*.json"), *(raw_b / "source_records").glob("*.json")]:
        path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")

    context = DiscoveryStagingContext.create(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json")
    candidate = NormalizedDiscoveryCandidate(
        candidate_id="candidate-2", page_id="page-2", keyword_id="benchmark",
        provider="crossref", normalized_doi=new,
        metadata=_metadata_from_record({"title": "Raw identity hit", "year": 2026, "doi": new}))
    result = context.transaction.stage_candidate(candidate, source_record={"record": {}}, apply=True)

    assert result.status == "repair_required"
    assert result.error and result.error.code == "cross_scope_duplicate"
    assert {ref.paper_number for ref in result.identity_refs} == {number_b}
    assert ledger.load()["max_number"] == number_b

import json
from pathlib import Path

import pytest

from src.discovery.stage_transaction import NormalizedDiscoveryCandidate
from src.discovery.staging_context import DiscoveryStagingContext
from src.discovery.stage_transaction import StageTransactionConfigurationError
from src.discovery.staging_metrics import CollectingStagingMetricsObserver
from src.library.paper_number_ledger import PaperNumberLedger
from src.staging.network_metadata_staging import _metadata_from_record
from tests.factories.paper_raw_factory import create_active_formal_workspace

pytestmark = pytest.mark.integration


def _candidate(doi: str, *, candidate_id: str = "other", page_id: str = "page"):
    return NormalizedDiscoveryCandidate(
        candidate_id=candidate_id, page_id=page_id, keyword_id="keyword-1",
        provider="crossref", normalized_doi=doi,
        metadata=_metadata_from_record({"title": "Candidate", "year": 2026, "doi": doi}))


def test_active_formal_doi_is_duplicate_without_raw_allocation(tmp_path: Path):
    create_active_formal_workspace(tmp_path, doi="10.8100/formal")
    context = DiscoveryStagingContext.create(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json")
    before = set((tmp_path / "paper_raw").glob("[0-9]*"))
    result = context.transaction.stage_candidate(
        _candidate("10.8100/formal"), source_record={"record": {}}, apply=True)
    assert result.status == "duplicate"
    assert set((tmp_path / "paper_raw").glob("[0-9]*")) == before


def test_formal_provider_identity_matches_without_candidate_doi(tmp_path: Path):
    create_active_formal_workspace(tmp_path, doi="10.8100/identity")
    context = DiscoveryStagingContext.create(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json")
    candidate = NormalizedDiscoveryCandidate(
        candidate_id="candidate-1", page_id="page-1", keyword_id="keyword-1",
        provider="crossref", normalized_doi="", metadata={})
    result = context.transaction.stage_candidate(candidate, source_record={}, apply=True)
    assert result.status == "reused"
    assert result.workspace_path == tmp_path / "papers" / "synthetic_formal"


def test_damaged_formal_match_fails_closed_without_raw_allocation(tmp_path: Path):
    formal = create_active_formal_workspace(tmp_path, doi="10.8100/damaged")
    context = DiscoveryStagingContext.create(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json")
    next(formal.glob("*.metadata.json")).unlink()
    result = context.transaction.stage_candidate(
        _candidate("10.8100/damaged"), source_record={"record": {}}, apply=True)
    assert result.status == "repair_required"
    assert not list((tmp_path / "paper_raw").glob("[0-9]*"))


def test_formal_metadata_number_mismatch_fails_live_revalidation(tmp_path: Path):
    doi = "10.8100/wrong-number"
    formal = create_active_formal_workspace(tmp_path, doi=doi)
    ledger = PaperNumberLedger(tmp_path / "ledger.json")
    context = DiscoveryStagingContext.create(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=ledger.path)
    before_max = ledger.load()["max_number"]

    metadata_path = next(formal.glob("*.metadata.json"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["paper_number"] = "0000000000009999"
    metadata["paper_raw_id"] = "0000000000009999"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    result = context.transaction.stage_candidate(
        _candidate(doi), source_record={"record": {}}, apply=True)

    assert result.status == "repair_required"
    assert result.error and result.error.code == "matched_record_revalidation_failed"
    assert "metadata_paper_number_mismatch" in result.error.detail
    assert ledger.load()["max_number"] == before_max
    assert not list((tmp_path / "paper_raw").glob("[0-9]*"))


@pytest.mark.parametrize(
    ("damage", "expected_detail"),
    [
        ("missing_catalog", "formal_catalog_missing"),
        ("missing_manifest", "formal_asset_manifest_missing"),
        ("manifest_identity", "formal_asset_manifest_identity_mismatch"),
    ],
)
def test_damaged_formal_publication_identity_fails_matched_revalidation(
    tmp_path: Path, damage: str, expected_detail: str,
):
    doi = f"10.8100/{damage}"
    formal = create_active_formal_workspace(tmp_path, doi=doi)
    ledger = PaperNumberLedger(tmp_path / "ledger.json")
    context = DiscoveryStagingContext.create(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=ledger.path)
    before_max = ledger.load()["max_number"]

    catalog_path = formal / f"{formal.name}.catalog.json"
    manifest_path = formal / f"{formal.name}.asset_manifest.json"
    if damage == "missing_catalog":
        catalog_path.unlink()
    elif damage == "missing_manifest":
        manifest_path.unlink()
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["paper_number"] = "0000000000009999"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = context.transaction.stage_candidate(
        _candidate(doi), source_record={"record": {}}, apply=True)

    assert result.status == "repair_required"
    assert result.error and result.error.code == "matched_record_revalidation_failed"
    assert expected_detail in result.error.detail
    assert ledger.load()["max_number"] == before_max
    assert not list((tmp_path / "paper_raw").glob("[0-9]*"))


def test_invalid_active_formal_ledger_names_fail_cold_build(tmp_path: Path):
    create_active_formal_workspace(tmp_path, doi="10.8100/cold-name")
    ledger = PaperNumberLedger(tmp_path / "ledger.json")
    data = ledger.load()
    number = next(iter(data["items"]))
    data["items"][number]["folder_name"] = "wrong-name"
    ledger.save(data)
    before = ledger.path.read_bytes()

    with pytest.raises(StageTransactionConfigurationError, match="active_formal_ledger_name_mismatch"):
        DiscoveryStagingContext.create(
            paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
            ledger_path=ledger.path)

    assert ledger.path.read_bytes() == before
    assert not list((tmp_path / "paper_raw").glob("[0-9]*"))


def test_active_formal_ledger_name_mutation_fails_targeted_refresh(tmp_path: Path):
    doi = "10.8100/warm-name"
    create_active_formal_workspace(tmp_path, doi=doi)
    ledger = PaperNumberLedger(tmp_path / "ledger.json")
    metrics = CollectingStagingMetricsObserver()
    context = DiscoveryStagingContext.create_with_observer(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=ledger.path, observer=metrics)
    before_max = ledger.load()["max_number"]

    data = ledger.load()
    number = next(iter(data["items"]))
    data["items"][number]["paper_name"] = "wrong-name"
    data["items"][number]["folder_name"] = "wrong-name"
    ledger.save(data)
    result = context.transaction.stage_candidate(
        _candidate(doi), source_record={"record": {}}, apply=True)

    assert result.status == "repair_required"
    assert result.error and result.error.code == "registry_refresh_failed"
    assert "active_formal_directory_name_mismatch" in result.error.detail
    assert metrics.formal_publication_view_loads == 1
    assert ledger.load()["max_number"] == before_max
    assert not list((tmp_path / "paper_raw").glob("[0-9]*"))

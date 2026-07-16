from pathlib import Path

import pytest

from src.discovery.stage_transaction import NormalizedDiscoveryCandidate
from src.discovery.staging_context import DiscoveryStagingContext
from src.discovery.staging_metrics import CollectingStagingMetricsObserver
from src.services.network_metadata_staging import _metadata_from_record
from src.discovery.workspace_registry import build_workspace_registry
from src.library.paper_number_ledger import PaperNumberLedger
import json
from tests.factories.paper_raw_factory import (
    activate_minimal_formal_publication,
    create_active_formal_workspace,
)

pytestmark = pytest.mark.integration


def _candidate(doi: str, suffix: str):
    return NormalizedDiscoveryCandidate(
        candidate_id=f"candidate-{suffix}", page_id=f"page-{suffix}",
        keyword_id="keyword-1", provider="crossref", normalized_doi=doi,
        metadata=_metadata_from_record({"title": suffix, "year": 2026, "doi": doi}))


def test_unchanged_formal_generation_is_loaded_once(tmp_path: Path):
    create_active_formal_workspace(tmp_path, doi="10.8200/one")
    metrics = CollectingStagingMetricsObserver()
    context = DiscoveryStagingContext.create_with_observer(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json", observer=metrics)
    assert context.transaction.formal_view.active_numbers == {
        "0000000000000001"}
    for index in range(10):
        assert context.transaction.stage_candidate(
            _candidate("10.8200/one", str(index)), source_record={}, apply=True).status in {"duplicate", "reused"}
    assert metrics.formal_publication_view_loads == 1


def test_formal_generation_change_loads_new_primary_once(tmp_path: Path):
    create_active_formal_workspace(tmp_path, doi="10.8200/one")
    metrics = CollectingStagingMetricsObserver()
    context = DiscoveryStagingContext.create_with_observer(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json", observer=metrics)
    raw = context.transaction.stage_candidate(
        _candidate("10.8200/two", "two-stage"), source_record={"record": {}}, apply=True)
    assert raw.status == "staged"
    from src.library.paper_number_ledger import PaperNumberLedger
    formal = activate_minimal_formal_publication(
        PaperNumberLedger(tmp_path / "ledger.json"),
        raw.workspace_path,
        tmp_path / "papers" / "formal-two",
    )
    result = context.transaction.stage_candidate(
        _candidate("10.8200/two", "two-check"), source_record={}, apply=True)
    assert result.status == "duplicate"
    assert result.workspace_path == formal
    assert metrics.formal_publication_view_loads == 2


def test_formal_metadata_in_place_mutation_is_detected_as_immutable_hash_violation(
    tmp_path: Path,
):
    formal = create_active_formal_workspace(tmp_path, doi="10.8200/immutable")
    metadata_path = formal / f"{formal.name}.metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["identifiers"]["doi"] = "10.8200/unauthorized"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    built = build_workspace_registry(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger=PaperNumberLedger(tmp_path / "ledger.json"))

    assert built.complete is False
    assert any("formal_metadata_immutable_hash_mismatch" in str(issue)
               for issue in built.issues)

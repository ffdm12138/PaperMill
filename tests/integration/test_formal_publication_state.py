"""Publication-state and warm-context safety regressions."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.repair_discovery_workspaces import run as run_repair
from src.discovery.stage_transaction import NormalizedDiscoveryCandidate
from src.discovery.staging_context import DiscoveryStagingContext
from src.discovery.workspace_registry import build_workspace_registry
from src.library.paper_number_ledger import PaperNumberLedger
from src.services.network_metadata_staging import _metadata_from_record
from tests.factories.paper_raw_factory import create_active_formal_workspace, create_network_metadata_workspace


pytestmark = pytest.mark.integration


def _candidate(doi: str) -> NormalizedDiscoveryCandidate:
    return NormalizedDiscoveryCandidate(
        candidate_id="candidate-new", page_id="page-new", keyword_id="keyword-new",
        provider="crossref", normalized_doi=doi,
        metadata=_metadata_from_record({"title": "new", "year": 2026, "doi": doi}),
    )


def test_next_batch_rejects_unsupported_formal_metadata_mutation(tmp_path: Path):
    formal = create_active_formal_workspace(tmp_path, doi="10.8600/old")
    ledger = PaperNumberLedger(tmp_path / "ledger.json")
    context = DiscoveryStagingContext.create(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=ledger.path,
    )
    metadata_path = next(formal.glob("*.metadata.json"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["identifiers"]["doi"] = "10.8600/new"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    before = ledger.load()
    # The hot epoch path only reads the publication revision/generation.  An
    # unsupported direct edit is therefore detected by an explicit batch/audit
    # pass, not by rehashing every formal for each candidate.
    built = build_workspace_registry(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger=ledger,
    )

    assert built.complete is False
    assert ledger.load()["max_number"] == before["max_number"]
    assert not any((tmp_path / "paper_raw").iterdir())


def test_incremental_refresh_reuses_formal_hash_closure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    create_active_formal_workspace(tmp_path, doi="10.8600/old")
    ledger = PaperNumberLedger(tmp_path / "ledger.json")
    context = DiscoveryStagingContext.create(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger_path=ledger.path,
    )

    import src.library.formal_publication as publication

    calls = 0
    original = publication.compute_sha256

    def counted(path: Path) -> str:
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(publication, "compute_sha256", counted)
    for index in range(3):
        result = context.transaction.stage_candidate(
            _candidate(f"10.8600/new-{index}"), source_record={}, apply=True,
        )
        assert result.status == "staged"

    assert calls == 0


def test_reserved_schema_incomplete_is_unsettled_not_global_failure(tmp_path: Path):
    folder = create_network_metadata_workspace(tmp_path, doi="10.8600/incomplete")
    number = folder.name
    ledger = PaperNumberLedger(tmp_path / "ledger.json")
    data = ledger.load()
    data["items"][number]["state"] = "reserved"
    ledger.save(data)
    PaperNumberLedger.write_marker(folder, number, state="reserved")
    metadata_path = folder / f"{number}.metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["first_author"]["family"] = ""
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    built = build_workspace_registry(
        paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
        ledger=ledger,
    )

    assert built.complete is True
    assert number in built.unsettled_numbers


def test_repair_demotes_incomplete_metadata_staged_without_recycling_number(tmp_path: Path):
    folder = create_network_metadata_workspace(tmp_path, doi="10.8600/missing-receipt")
    number = folder.name
    (folder / f"{number}.discovery_receipt.json").unlink()
    ledger = PaperNumberLedger(tmp_path / "ledger.json")
    before = ledger.load()["max_number"]

    report = run_repair(
        paper_raw=tmp_path / "paper_raw", papers=tmp_path / "papers",
        ledger_path=ledger.path, apply=True, limit=10, paper_number=number,
    )

    assert report["items"][0]["action"] == "demote_metadata_staged_to_reserved"
    assert report["items"][0]["applied"] is True
    assert ledger.load()["max_number"] == before
    assert ledger.load()["items"][number]["state"] == "reserved"
    marker = json.loads((folder / f"{number}.paper.number").read_text(encoding="utf-8"))
    assert marker["state"] == "reserved"

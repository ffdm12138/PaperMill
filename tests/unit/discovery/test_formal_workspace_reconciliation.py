from pathlib import Path

import pytest

from src.discovery.pending_queue import reconcile_discovery_workspace
from src.library.paper_number_ledger import PaperNumberLedger
from src.metadata.schema import empty_metadata
from src.utils.atomic_io import atomic_write_json


pytestmark = pytest.mark.unit


PAPER_NUMBER = "0000000000000001"
PAPER_ID = "2024_doe_discovery"
DOI = "10.1234/formal"


def _metadata() -> dict:
    data = empty_metadata(PAPER_NUMBER, source_type="network_search")
    data["title"]["original"] = "Formal paper"
    data["year"] = 2024
    data["authors"] = [{"full_name": "Jane Doe", "family": "Doe", "given": "Jane", "orcid": "", "affiliation": ""}]
    data["first_author"] = {"family": "Doe", "display": "Jane Doe"}
    data["identifiers"]["doi"] = DOI
    data["metadata_match"] = {
        "status": "matched",
        "source": "openalex",
        "confidence": 0.9,
        "matched_at": "2026-01-01T00:00:00",
        "warnings": [],
    }
    return data


def _incomplete_formal_workspace(tmp_path: Path) -> Path:
    papers = tmp_path / "papers"
    workspace = papers / PAPER_ID
    (workspace / "source_records").mkdir(parents=True)
    atomic_write_json(
        workspace / "source_records" / "metadata_source.openalex.json",
        {
            "provider": "openalex",
            "record": {"doi": DOI, "title": "Formal paper"},
            "discovery_context": {
                "candidate_id": "candidate-a",
                "page_id": "page-1",
                "keyword_id": "kw",
                "provider": "openalex",
                "normalized_doi": DOI,
            },
        },
        indent=2,
    )
    atomic_write_json(workspace / f"{PAPER_ID}.metadata.json", _metadata(), indent=2)
    PaperNumberLedger.write_marker(workspace, PAPER_NUMBER, state="active")
    ledger_data = PaperNumberLedger.empty_data()
    ledger_data["max_number"] = PAPER_NUMBER
    ledger_data["items"][PAPER_NUMBER] = {
        "folder_name": PAPER_ID,
        "folder_path": workspace.as_posix(),
        "state": "active",
    }
    atomic_write_json(tmp_path / "ledger.json", ledger_data, indent=2)
    return workspace


def test_formal_incomplete_requires_repair_without_receipt_backfill(tmp_path: Path):
    workspace = _incomplete_formal_workspace(tmp_path)
    before = {p.relative_to(workspace).as_posix(): p.read_bytes() for p in workspace.rglob("*") if p.is_file()}

    result = reconcile_discovery_workspace(
        [tmp_path / "papers"],
        candidate_id="candidate-a",
        page_id="page-1",
        keyword_id="kw",
        provider="openalex",
        normalized_doi=DOI,
        ledger_path=tmp_path / "ledger.json",
    )

    assert result.status == "formal_repair_required"
    assert result.workspace_kind == "formal"
    assert result.disposition == "formal_repair_required"
    assert result.reason == "formal_workspace_repair_required"
    after = {p.relative_to(workspace).as_posix(): p.read_bytes() for p in workspace.rglob("*") if p.is_file()}
    assert after == before
    assert not (workspace / f"{PAPER_NUMBER}.discovery_receipt.json").exists()

import hashlib
from pathlib import Path

import pytest

from src.staging.network_metadata_staging import stage_network_metadata_records


pytestmark = pytest.mark.unit


def _file_hashes(folder: Path) -> dict[str, str]:
    return {
        path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def test_reuse_receipt_conflict_has_zero_workspace_side_effects(tmp_path: Path):
    paper_raw = tmp_path / "paper_raw"
    papers = tmp_path / "papers"
    ledger = tmp_path / "ledger.json"
    first = stage_network_metadata_records(
        [{
            "title": "A",
            "doi": "10.1234/tx",
            "discovery_context": {
                "candidate_id": "candidate-a",
                "page_id": "page-1",
                "keyword_id": "kw",
                "normalized_doi": "10.1234/tx",
            },
        }],
        paper_raw_dir=paper_raw,
        papers_dir=papers,
        ledger_path=ledger,
        apply=True,
    )
    assert first["staged"] == 1
    workspace = paper_raw / "0000000000000001"
    before_workspace = _file_hashes(workspace)
    before_ledger = ledger.read_bytes()

    second = stage_network_metadata_records(
        [{
            "title": "B",
            "doi": "10.1234/tx",
            "discovery_context": {
                "candidate_id": "candidate-b",
                "page_id": "page-2",
                "keyword_id": "kw",
                "normalized_doi": "10.1234/tx",
            },
        }],
        paper_raw_dir=paper_raw,
        papers_dir=papers,
        ledger_path=ledger,
        apply=True,
        reuse_paper_number="0000000000000001",
    )

    assert second["failed"] == 1
    assert second["items"][0]["status"] == "failed_retryable"
    assert _file_hashes(workspace) == before_workspace
    assert ledger.read_bytes() == before_ledger

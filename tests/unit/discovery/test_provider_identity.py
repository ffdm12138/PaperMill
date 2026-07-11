import hashlib
from pathlib import Path

import pytest

from src.discovery.discovery_receipt import (
    AmbiguousDiscoveryReceiptError,
    ReceiptLookupIdentity,
    build_receipt_payload,
    find_matching_receipt,
    write_or_validate_discovery_receipt,
)
from src.services.network_metadata_staging import stage_network_metadata_records
from src.library.paper_number_ledger import PaperNumberLedger


pytestmark = pytest.mark.unit


def _tree_hashes(folder: Path) -> dict[str, str]:
    return {
        path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def _stage(
    tmp_path: Path,
    *,
    doi: str = "10.1234/provider",
    provider: str | None,
    candidate_id: str = "candidate-a",
    page_id: str = "page-1",
    reuse: str | None = None,
):
    context = {
        "candidate_id": candidate_id,
        "page_id": page_id,
        "keyword_id": "kw",
        "normalized_doi": doi,
    }
    if provider is not None:
        context["provider"] = provider
    return stage_network_metadata_records(
        [{
            "title": "Provider identity",
            "doi": doi,
            "source": {"provider": provider or "openalex"},
            "discovery_context": context,
        }],
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        apply=True,
        reuse_paper_number=reuse,
    )


def test_receipt_lookup_ambiguous_identity_fails_closed(tmp_path: Path):
    paper_raw = tmp_path / "paper_raw"
    for number in ("0000000000000001", "0000000000000002"):
        workspace = paper_raw / number
        workspace.mkdir(parents=True)
        PaperNumberLedger.write_marker(workspace, number, state="metadata_staged")
        write_or_validate_discovery_receipt(
            workspace / f"{number}.discovery_receipt.json",
            build_receipt_payload(
                candidate_id="candidate-a",
                page_id="page-1",
                keyword_id="kw",
                provider="openalex",
                normalized_doi="10.1234/ambiguous",
                paper_number=number,
            ),
        )
    before = _tree_hashes(paper_raw)
    with pytest.raises(AmbiguousDiscoveryReceiptError):
        find_matching_receipt(
            [paper_raw],
            lookup_key=ReceiptLookupIdentity(
                candidate_id="candidate-a",
                page_id="page-1",
                keyword_id="kw",
                provider="openalex",
                normalized_doi="10.1234/ambiguous",
            ),
        )
    assert _tree_hashes(paper_raw) == before


def test_missing_provider_receipt_conflicts_with_present_provider_without_side_effects(tmp_path: Path):
    first = _stage(tmp_path, provider=None)
    assert first["staged"] == 1
    workspace = tmp_path / "paper_raw" / "0000000000000001"
    before_workspace = _tree_hashes(workspace)
    before_ledger = (tmp_path / "ledger.json").read_bytes()

    second = _stage(tmp_path, provider="openalex", reuse="0000000000000001")

    assert second["failed"] == 1
    assert second["items"][0]["status"] == "failed_retryable"
    assert _tree_hashes(workspace) == before_workspace
    assert (tmp_path / "ledger.json").read_bytes() == before_ledger


def test_different_provider_retry_conflicts_without_side_effects(tmp_path: Path):
    first = _stage(tmp_path, provider="openalex")
    assert first["staged"] == 1
    workspace = tmp_path / "paper_raw" / "0000000000000001"
    before_workspace = _tree_hashes(workspace)
    before_ledger = (tmp_path / "ledger.json").read_bytes()

    second = _stage(tmp_path, provider="crossref", reuse="0000000000000001")

    assert second["failed"] == 1
    assert second["items"][0]["status"] == "failed_retryable"
    assert _tree_hashes(workspace) == before_workspace
    assert (tmp_path / "ledger.json").read_bytes() == before_ledger


def test_same_provider_retry_is_idempotent(tmp_path: Path):
    first = _stage(tmp_path, provider="openalex")
    assert first["staged"] == 1

    second = _stage(tmp_path, provider="openalex", reuse="0000000000000001")

    assert second["staged"] == 1
    workspace = tmp_path / "paper_raw" / "0000000000000001"
    assert len(list((workspace / "source_records").glob("metadata_source.openalex.json"))) == 1

"""Contract tests for the shared discovery receipt writer.

Covers Phase 1 guarantees:
- create + re-read verification
- idempotent existing-match (no rewrite)
- never overwrite a conflicting receipt (typed error)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.workspace.receipt import (
    DiscoveryReceiptConflictError,
    PersistedReceiptIdentity,
    ReceiptLookupIdentity,
    ReceiptWriteResult,
    build_receipt_payload,
    normalize_receipt_identity,
    receipt_path_for,
    write_or_validate_discovery_receipt,
)


pytestmark = pytest.mark.contract


def _payload(*, candidate_id="c1", page_id="p1", keyword_id="kw1",
             normalized_doi="10.1234/abc", paper_number="0000000000000001",
             provider=""):
    return build_receipt_payload(
        candidate_id=candidate_id,
        page_id=page_id,
        keyword_id=keyword_id,
        normalized_doi=normalized_doi,
        paper_number=paper_number,
        provider=provider,
    )


def test_receipt_writer_creates_new_receipt(tmp_path: Path):
    path = receipt_path_for(tmp_path, "0000000000000001")
    result = write_or_validate_discovery_receipt(path, _payload())
    assert isinstance(result, ReceiptWriteResult)
    assert result.status == "created"
    assert result.path == path
    assert result.paper_number == "0000000000000001"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["candidate_id"] == "c1"
    assert data["normalized_doi"] == "10.1234/abc"
    assert data["paper_number"] == "0000000000000001"
    assert data["schema_version"] == "1.0"


def test_receipt_writer_is_idempotent_for_identical_identity(tmp_path: Path):
    path = receipt_path_for(tmp_path, "0000000000000001")
    first = write_or_validate_discovery_receipt(path, _payload())
    assert first.status == "created"
    original_text = path.read_text(encoding="utf-8")
    # Second call with the same identity — must not rewrite.
    second = write_or_validate_discovery_receipt(path, _payload())
    assert second.status == "existing_match"
    assert path.read_text(encoding="utf-8") == original_text


def test_receipt_writer_rejects_conflicting_existing_receipt(tmp_path: Path):
    path = receipt_path_for(tmp_path, "0000000000000001")
    write_or_validate_discovery_receipt(path, _payload(candidate_id="candidate-a"))
    # A different candidate tries to claim the same workspace receipt.
    with pytest.raises(DiscoveryReceiptConflictError) as exc_info:
        write_or_validate_discovery_receipt(path, _payload(candidate_id="candidate-b"))
    assert exc_info.value.path == path
    # The original receipt is untouched.
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["candidate_id"] == "candidate-a"


def test_receipt_writer_normalizes_doi_identity(tmp_path: Path):
    path = receipt_path_for(tmp_path, "0000000000000001")
    # First write with a bare DOI.
    write_or_validate_discovery_receipt(path, _payload(normalized_doi="10.1234/abc"))
    # Second write with a URL-form DOI normalizes to the same identity.
    payload = build_receipt_payload(
        candidate_id="c1",
        page_id="p1",
        keyword_id="kw1",
        normalized_doi="https://doi.org/10.1234/ABC",
        paper_number="0000000000000001",
    )
    result = write_or_validate_discovery_receipt(path, payload)
    assert result.status == "existing_match"


def test_normalize_receipt_identity_requires_core_fields():
    with pytest.raises(ValueError):
        normalize_receipt_identity({"candidate_id": "", "page_id": "p1", "normalized_doi": "10.1/x"})
    with pytest.raises(ValueError):
        normalize_receipt_identity({"candidate_id": "c1", "page_id": "", "normalized_doi": "10.1/x"})
    with pytest.raises(ValueError):
        normalize_receipt_identity({"candidate_id": "c1", "page_id": "p1", "normalized_doi": ""})


def test_receipt_writer_corrupt_existing_raises_valueerror(tmp_path: Path):
    path = receipt_path_for(tmp_path, "0000000000000001")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        write_or_validate_discovery_receipt(path, _payload())


# ── paper_number validation (Phase 0.2) ─────────────────────────────


def test_normalize_receipt_identity_rejects_non_16_digit_paper_number():
    """paper_number must be exactly 16 decimal digits — not 6, not 3."""
    with pytest.raises(ValueError, match="paper_number"):
        normalize_receipt_identity({
            "candidate_id": "c1", "page_id": "p1",
            "normalized_doi": "10.1/x", "paper_number": "123",
        })


def test_normalize_receipt_identity_rejects_short_legacy_id():
    """6-digit legacy IDs are not valid receipt paper_numbers."""
    with pytest.raises(ValueError, match="paper_number"):
        normalize_receipt_identity({
            "candidate_id": "c1", "page_id": "p1",
            "normalized_doi": "10.1/x", "paper_number": "0000042",
        })


def test_normalize_receipt_identity_rejects_paper_name():
    """A paper_name (UUID-like) is not a valid paper_number."""
    with pytest.raises(ValueError, match="paper_number"):
        normalize_receipt_identity({
            "candidate_id": "c1", "page_id": "p1",
            "normalized_doi": "10.1/x", "paper_number": "abc-def-12345",
        })


def test_normalize_receipt_identity_rejects_polluted_paper_suffix():
    """Values with .paper suffix pollution must be rejected."""
    with pytest.raises(ValueError, match="paper_number"):
        normalize_receipt_identity({
            "candidate_id": "c1", "page_id": "p1",
            "normalized_doi": "10.1/x",
            "paper_number": "0000000000000001.paper",
        })


def test_normalize_receipt_identity_accepts_valid_16_digit():
    result = normalize_receipt_identity({
        "candidate_id": "c1", "page_id": "p1",
        "normalized_doi": "10.1/x",
        "paper_number": "0000000000000001",
    })
    assert result["paper_number"] == "0000000000000001"


def test_persisted_identity_requires_paper_number():
    with pytest.raises(ValueError, match="paper_number"):
        normalize_receipt_identity({
            "candidate_id": "c1", "page_id": "p1",
            "normalized_doi": "10.1/x", "paper_number": "",
        })


def test_lookup_and_persisted_identity_types_are_distinct():
    lookup = ReceiptLookupIdentity("c1", "p1", "kw1", "openalex", "10.1/x")
    persisted = PersistedReceiptIdentity(
        "c1", "p1", "kw1", "openalex", "10.1/x", "0000000000000001"
    )
    assert not hasattr(lookup, "paper_number")
    assert persisted.paper_number == "0000000000000001"


def test_receipt_writer_rejects_payload_with_invalid_paper_number(tmp_path: Path):
    """build_receipt_payload must reject non-16-digit paper_number at construction."""
    with pytest.raises(ValueError, match="paper_number"):
        build_receipt_payload(
            candidate_id="c1", page_id="p1", keyword_id="kw1",
            normalized_doi="10.1/x", paper_number="123",
        )


def test_receipt_writer_rejects_payload_workspace_mismatch(tmp_path: Path):
    path = tmp_path / "0000000000000002" / "0000000000000001.discovery_receipt.json"
    with pytest.raises(ValueError, match="receipt_paper_number_path_mismatch"):
        write_or_validate_discovery_receipt(path, _payload())


def test_receipt_writer_rejects_payload_filename_mismatch(tmp_path: Path):
    path = tmp_path / "0000000000000001" / "0000000000000002.discovery_receipt.json"
    with pytest.raises(ValueError, match="receipt_filename_invalid"):
        write_or_validate_discovery_receipt(path, _payload())


def test_receipt_writer_rejects_non_numeric_parent(tmp_path: Path):
    path = tmp_path / "workspace" / "0000000000000001.discovery_receipt.json"
    with pytest.raises(ValueError, match="receipt_workspace_invalid"):
        write_or_validate_discovery_receipt(path, _payload())


def test_receipt_writer_rejects_path_escape(tmp_path: Path):
    allowed = tmp_path / "paper_raw"
    path = tmp_path / "outside" / "0000000000000001" / "0000000000000001.discovery_receipt.json"
    with pytest.raises(ValueError, match="receipt_workspace_invalid"):
        write_or_validate_discovery_receipt(path, _payload(), workspace_root=allowed)

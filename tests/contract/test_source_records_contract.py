from __future__ import annotations

import json

import pytest

from src.services.source_records import (
    fetch_result_rel_path,
    is_fetch_result_path,
    metadata_source_rel_path,
    resolve_metadata_source_record_path,
    write_fetch_result,
)
from src.ingest.paper_raw import PaperRawAllocator
from tests.factories.pdf_factory import write_fake_pdf


pytestmark = pytest.mark.contract


def test_manual_pdf_source_record_contract(tmp_path):
    raw_pdf = write_fake_pdf(tmp_path / "raw" / "paper.pdf")
    allocator = PaperRawAllocator(
        tmp_path / "paper_raw",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        papers_dir=tmp_path / "papers",
    )

    result = allocator.allocate_from_pdf(raw_pdf, source_type="manual_pdf", move=True)

    folder = tmp_path / "paper_raw" / result["paper_number"]
    metadata = json.loads((folder / f"{result['paper_number']}.metadata.json").read_text(encoding="utf-8"))
    assert metadata["source"]["provider"] == "manual"
    assert metadata["source"]["raw_record_path"] == metadata_source_rel_path("manual")
    assert (folder / metadata["source"]["raw_record_path"]).exists()
    assert not is_fetch_result_path(metadata["source"]["raw_record_path"])


def test_fetch_result_is_not_metadata_source(tmp_path):
    folder = tmp_path / "paper_raw" / "0000000000000001"
    folder.mkdir(parents=True)
    write_fetch_result(folder, {"resolver": "header_based", "success": True})

    resolved, error = resolve_metadata_source_record_path(folder, fetch_result_rel_path())

    assert resolved is None
    assert "fetch_result.json" in error


def test_metadata_source_record_must_be_under_source_records(tmp_path):
    folder = tmp_path / "paper_raw" / "0000000000000001"
    folder.mkdir(parents=True)

    resolved, error = resolve_metadata_source_record_path(folder, "../metadata_source.manual.json")

    assert resolved is None
    assert "escapes folder" in error


def test_fetch_result_does_not_overwrite_metadata_source(tmp_path):
    from tests.factories.source_record_factory import write_metadata_source_record

    folder = tmp_path / "paper_raw" / "0000000000000001"
    folder.mkdir(parents=True)
    metadata_path = write_metadata_source_record(folder, "manual", {"provider": "manual"})

    write_fetch_result(folder, {"resolver": "header_based", "success": False})

    assert json.loads(metadata_path.read_text(encoding="utf-8")) == {"provider": "manual"}
    assert (folder / fetch_result_rel_path()).exists()


# ── Strict mode (require_nonempty=True) tests ──────────────────────────

@pytest.mark.contract
def test_source_record_validator_strict_requires_nonempty_path(tmp_path):
    from src.services.source_records import validate_metadata_source_record_exists

    folder = tmp_path / "paper_raw" / "0000000000000001"
    folder.mkdir(parents=True)

    assert validate_metadata_source_record_exists(folder, "", require_nonempty=False) == []

    errors = validate_metadata_source_record_exists(folder, "", require_nonempty=True)
    assert any("source.raw_record_path is required" in err for err in errors)


@pytest.mark.contract
@pytest.mark.parametrize("bad_filename", [
    "test.json",
    "crossref.json",
    "openalex.json",
    "fetch_result.json",
    "metadata.json",
])
def test_source_record_rejects_non_metadata_source_filenames(tmp_path, bad_filename):
    """raw_record_path must match ``metadata_source.*.json`` — legacy filenames
    like ``test.json`` / ``crossref.json`` / ``fetch_result.json`` are invalid."""
    from src.services.source_records import resolve_metadata_source_record_path

    folder = tmp_path / "paper_raw" / "0000000000000001"
    folder.mkdir(parents=True)

    raw_record_path = f"source_records/{bad_filename}"
    resolved, error = resolve_metadata_source_record_path(folder, raw_record_path)

    assert resolved is None, f"{bad_filename} should be rejected"
    assert error, f"{bad_filename} should produce an error"
    assert "metadata_source" in error or "fetch_result" in error, error


@pytest.mark.contract
def test_source_record_accepts_canonical_metadata_source_path(tmp_path):
    """``source_records/metadata_source.<provider>.json`` is the canonical path."""
    from src.services.source_records import resolve_metadata_source_record_path

    folder = tmp_path / "paper_raw" / "0000000000000001"
    (folder / "source_records").mkdir(parents=True)
    record_path = folder / "source_records" / "metadata_source.crossref.json"
    record_path.write_text("{}", encoding="utf-8")

    resolved, error = resolve_metadata_source_record_path(
        folder, "source_records/metadata_source.crossref.json"
    )
    assert error == ""
    assert resolved is not None
    assert resolved.exists()

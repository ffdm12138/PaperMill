from __future__ import annotations

import json

import pytest

from src.services.source_records import (
    resolve_metadata_source_record_path,
    validate_metadata_source_record_exists,
)
from src.metadata.schema import validate_metadata_schema
from tests.factories.library_factory import make_paper_raw_item
from tests.factories.metadata_factory import make_minimal_metadata


pytestmark = pytest.mark.contract


def test_metadata_minimal_valid_for_manual_pdf(tmp_path):
    folder = make_paper_raw_item(tmp_path)
    metadata_path = folder / "0000000000000001.metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    assert validate_metadata_schema(metadata) == []
    resolved, error = resolve_metadata_source_record_path(folder, metadata["source"]["raw_record_path"])
    assert error == ""
    assert resolved is not None and resolved.exists()


def test_metadata_rejects_llm_catalog_fields():
    metadata = make_minimal_metadata()
    metadata["abstract"] = "LLM-generated summary belongs in catalog, not metadata"
    metadata["keywords"] = ["content"]

    errors = validate_metadata_schema(metadata)

    assert any("metadata.abstract is forbidden" in error for error in errors)
    assert any("metadata.keywords is forbidden" in error for error in errors)


@pytest.mark.contract
def test_metadata_source_raw_record_path_shape_resolves(tmp_path):
    """The raw_record_path resolver must produce a correct absolute path
    from a valid relative source_records path."""
    folder = tmp_path / "paper_raw" / "0000000000000001"
    folder.mkdir(parents=True)
    metadata = make_minimal_metadata()

    resolved, error = resolve_metadata_source_record_path(folder, metadata["source"]["raw_record_path"])

    assert error == ""
    assert resolved is not None
    expected = (folder / "source_records" / "metadata_source.manual.json").resolve()
    assert resolved == expected


@pytest.mark.contract
def test_metadata_validator_rejects_missing_source_record_file(tmp_path):
    """When raw_record_path is set but the file does not exist, the
    validator must report an error."""
    import json
    folder = tmp_path / "paper_raw" / "0000000000000001"
    folder.mkdir(parents=True)
    metadata = make_minimal_metadata()

    # Write metadata but do NOT create the source record file
    metadata_path = folder / "0000000000000001.metadata.json"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")

    errors = validate_metadata_source_record_exists(folder, metadata["source"]["raw_record_path"])

    assert len(errors) >= 1
    assert any("does not exist" in e for e in errors)


def test_metadata_source_record_cannot_be_fetch_result(tmp_path):
    folder = tmp_path / "paper_raw" / "0000000000000001"
    folder.mkdir(parents=True)

    resolved, error = resolve_metadata_source_record_path(folder, "source_records/fetch_result.json")

    assert resolved is None
    assert "must not point at fetch_result.json" in error

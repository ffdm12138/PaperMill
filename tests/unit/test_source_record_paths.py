from __future__ import annotations

import pytest

from src.services.source_records import (
    ensure_raw_record_path_is_metadata_source,
    fetch_result_rel_path,
    is_fetch_result_path,
    metadata_source_rel_path,
)


pytestmark = pytest.mark.unit


def test_manual_provider_uses_manual_metadata_source_path():
    assert metadata_source_rel_path("manual_pdf") == "source_records/metadata_source.manual_pdf.json"
    assert metadata_source_rel_path("manual") == "source_records/metadata_source.manual.json"


def test_fetch_result_path_detection_normalizes_slashes():
    assert is_fetch_result_path(r".\source_records\fetch_result.json")
    assert not is_fetch_result_path("source_records/metadata_source.manual.json")


def test_ensure_raw_record_path_repairs_fetch_result_path():
    assert (
        ensure_raw_record_path_is_metadata_source(fetch_result_rel_path(), "crossref")
        == "source_records/metadata_source.crossref.json"
    )


def test_ensure_raw_record_path_preserves_existing_metadata_source_path():
    path = "source_records/metadata_source.openalex.json"

    assert ensure_raw_record_path_is_metadata_source(path, "crossref") == path

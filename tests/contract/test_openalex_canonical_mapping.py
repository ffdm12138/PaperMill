"""Contract tests for OpenAlex canonical metadata mapping (Phase 3).

Verifies that OpenAlex ``biblio`` (volume/issue/first_page/last_page),
``publication_date``, and ``type`` are correctly mapped into metadata v2.0 via
``canonicalize_network_record``. Also covers ``combine_page_range`` edge cases.
"""
from __future__ import annotations

import pytest

from src.services.network_metadata_canonical import (
    combine_page_range,
    canonicalize_network_record,
)
from src.services.network_metadata_staging import _metadata_from_record


pytestmark = pytest.mark.contract


def _oa_record(**overrides):
    base = {
        "title": "Test Paper",
        "doi": "10.1234/oa",
        "provider": "openalex",
        "raw": {
            "id": "https://openalex.org/W123",
            "doi": "https://doi.org/10.1234/oa",
            "type": "article",
            "publication_date": "2024-03-02",
            "publication_year": 2024,
            "display_name": "Test Paper",
            "biblio": {
                "volume": "12",
                "issue": "3",
                "first_page": "101",
                "last_page": "110",
            },
            "primary_location": {
                "source": {"display_name": "Test Journal"},
                "pdf_url": "https://example.test/paper.pdf",
            },
            "open_access": {"is_oa": True, "oa_url": "https://example.test/paper.pdf"},
            "authorships": [{
                "author": {"display_name": "Alice Smith", "orcid": "https://orcid.org/0000-0001"},
                "institutions": [{"display_name": "Example University"}],
            }],
        },
    }
    base.update(overrides)
    return base


def test_openalex_biblio_volume_issue_pages_mapped():
    metadata = _metadata_from_record("0000000000000001", _oa_record())
    assert metadata["publication"]["volume"] == "12"
    assert metadata["publication"]["issue"] == "3"
    assert metadata["publication"]["pages"] == "101-110"


def test_openalex_publication_date_mapped():
    metadata = _metadata_from_record("0000000000000001", _oa_record())
    assert metadata["date"]["published"] == "2024-03-02"
    assert metadata["year"] == 2024


def test_openalex_first_page_only():
    record = _oa_record()
    record["raw"]["biblio"] = {"volume": "12", "issue": "3", "first_page": "101", "last_page": ""}
    metadata = _metadata_from_record("0000000000000001", record)
    assert metadata["publication"]["pages"] == "101"


def test_openalex_same_first_last_page():
    record = _oa_record()
    record["raw"]["biblio"] = {"first_page": "101", "last_page": "101"}
    metadata = _metadata_from_record("0000000000000001", record)
    assert metadata["publication"]["pages"] == "101"


def test_openalex_publication_date_missing_year_fallback():
    record = _oa_record()
    del record["raw"]["publication_date"]
    record["raw"]["publication_year"] = 2023
    metadata = _metadata_from_record("0000000000000001", record)
    assert metadata["year"] == 2023
    assert metadata["date"]["published"] == ""


def test_openalex_entry_type_article():
    metadata = _metadata_from_record("0000000000000001", _oa_record())
    assert metadata["entry_type"] == "article"


def test_openalex_entry_type_book_chapter():
    record = _oa_record()
    record["raw"]["type"] = "book-chapter"
    metadata = _metadata_from_record("0000000000000001", record)
    assert metadata["entry_type"] == "incollection"


def test_openalex_entry_type_proceedings():
    record = _oa_record()
    record["raw"]["type"] = "proceedings-article"
    metadata = _metadata_from_record("0000000000000001", record)
    assert metadata["entry_type"] == "inproceedings"


def test_openalex_entry_type_preprint():
    record = _oa_record()
    record["raw"]["type"] = "preprint"
    metadata = _metadata_from_record("0000000000000001", record)
    assert metadata["entry_type"] == "preprint"


def test_openalex_unknown_type_warns_and_falls_back():
    record = _oa_record()
    record["raw"]["type"] = "paratext"
    metadata = _metadata_from_record("0000000000000001", record)
    # paratext is mapped to misc (a valid schema entry_type), not silently article.
    assert metadata["entry_type"] == "misc"


def test_openalex_truly_unknown_type_warns():
    record = _oa_record()
    record["raw"]["type"] = "some-future-type"
    canonical = canonicalize_network_record(record)
    assert canonical.entry_type == "misc"
    assert any("unknown provider type" in w for w in canonical.warnings)


def test_combine_page_range_edge_cases():
    assert combine_page_range("101", "110") == "101-110"
    assert combine_page_range("101", "") == "101"
    assert combine_page_range("", "110") == "110"
    assert combine_page_range("101", "101") == "101"
    assert combine_page_range("", "") == ""
    assert combine_page_range(None, None) == ""
    # Never produces malformed ranges.
    assert combine_page_range("101", None) == "101"
    assert combine_page_range(None, "110") == "110"


def test_crossref_resolution_takes_precedence_over_openalex_biblio():
    """Crossref resolution raw is bibliographically primary per-field."""
    record = _oa_record()
    record["doi_resolution"] = {
        "raw_record": {
            "type": "journal-article",
            "volume": "99",
            "issue": "7",
            "page": "201-210",
            "container-title": ["CR Journal"],
            "published-online": {"date-parts": [[2024, 3, 1]]},
            "author": [{"given": "A", "family": "Author"}],
        },
    }
    metadata = _metadata_from_record("0000000000000001", record)
    assert metadata["publication"]["volume"] == "99"
    assert metadata["publication"]["issue"] == "7"
    assert metadata["publication"]["pages"] == "201-210"
    assert metadata["container"]["journal"] == "CR Journal"

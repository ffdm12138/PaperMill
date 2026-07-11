from __future__ import annotations

import pytest

from src.catalog.schema import validate_catalog_document as validate_catalog_schema
from tests.factories.catalog_factory import make_minimal_catalog


pytestmark = pytest.mark.contract


def test_catalog_content_only_no_bibliographic_duplication():
    catalog = make_minimal_catalog()

    assert validate_catalog_schema(catalog) == []
    forbidden = {"doi", "authors", "year", "container", "journal", "metadata_match"}
    assert forbidden.isdisjoint(catalog.keys())


def test_catalog_contains_pending_screening_and_complete_content():
    catalog = make_minimal_catalog()

    assert catalog["screening"]["read_decision"] == "pending"
    assert catalog["mechanisms"]
    assert catalog["limitations"]
    assert catalog["abstract"]["summary_zh"]


def test_catalog_rejects_retired_content_block():
    catalog = make_minimal_catalog()
    catalog["research" + "_card"] = {}
    assert validate_catalog_schema(catalog)


def test_catalog_rejects_old_display_block():
    catalog = make_minimal_catalog()
    catalog["display"] = {"title": "legacy"}

    errors = validate_catalog_schema(catalog)

    assert any("display" in error for error in errors)


def test_catalog_requires_content_identity():
    catalog = make_minimal_catalog()
    catalog.pop("content_identity")

    errors = validate_catalog_schema(catalog)

    assert any("content_identity" in error for error in errors)

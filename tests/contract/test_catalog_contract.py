from __future__ import annotations

import pytest

from src.services.v2_library import validate_catalog_schema
from tests.factories.catalog_factory import make_minimal_catalog


pytestmark = pytest.mark.contract


def test_catalog_content_only_no_bibliographic_duplication():
    catalog = make_minimal_catalog()

    assert validate_catalog_schema(catalog) == []
    forbidden = {"doi", "authors", "year", "container", "journal", "metadata_match"}
    assert forbidden.isdisjoint(catalog.keys())


def test_catalog_contains_pending_screening_and_research_card():
    catalog = make_minimal_catalog()

    assert catalog["screening"]["read_decision"] == "pending"
    assert catalog["research_card"]["mechanisms"]
    assert catalog["research_card"]["limitations"]
    assert catalog["writing_value"]["short_summary"]


def test_catalog_rejects_old_display_block():
    catalog = make_minimal_catalog()
    catalog["display"] = {"title": "legacy"}

    errors = validate_catalog_schema(catalog)

    assert any("display" in error for error in errors)


def test_catalog_requires_asset_refs():
    catalog = make_minimal_catalog()
    catalog["library_locator"]["asset_refs"].pop("markdown")

    errors = validate_catalog_schema(catalog)

    assert any("asset_refs missing markdown" in error for error in errors)

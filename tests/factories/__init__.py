"""Small, contract-shaped fixtures for the layered test suite."""

from tests.factories.catalog_factory import make_minimal_catalog
from tests.factories.library_factory import make_minimal_paths, make_paper_raw_item
from tests.factories.metadata_factory import make_minimal_metadata
from tests.factories.pdf_factory import write_fake_pdf
from tests.factories.source_record_factory import write_metadata_source_record

__all__ = [
    "make_minimal_catalog",
    "make_minimal_metadata",
    "make_minimal_paths",
    "make_paper_raw_item",
    "write_fake_pdf",
    "write_metadata_source_record",
]

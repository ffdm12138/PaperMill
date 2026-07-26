"""Pure DOI index and registry refresh contracts."""
from pathlib import Path

import pytest

from src.ingest.duplicate_guard import DuplicateIndex, DuplicateRef


def _ref(number: str) -> DuplicateRef:
    return DuplicateRef(scope="paper_raw", paper_number=number, paper_name="",
                        folder=number, source="metadata", doi="10.1000/shared")


def test_duplicate_index_copy_is_independent():
    index = DuplicateIndex()
    index.add_doi_ref(_ref("0000000000000001"))
    copied = index.copy()
    copied.add_doi_ref(_ref("0000000000000002"))
    assert len(index.lookup_doi("10.1000/shared")) == 1
    assert len(copied.lookup_doi("10.1000/shared")) == 2


def test_duplicate_index_freeze_rejects_mutation():
    index = DuplicateIndex()
    index.add_doi_ref(_ref("0000000000000001"))
    frozen = index.freeze()
    with pytest.raises(TypeError):
        frozen.add_doi_ref(_ref("0000000000000002"))

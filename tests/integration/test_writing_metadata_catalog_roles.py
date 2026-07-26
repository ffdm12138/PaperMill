from __future__ import annotations
from pathlib import Path
import pytest

from src.ingest.commit import commit_paper_raw
from src.ingest.formalization import write_formalization_plan
from src.catalog_folders.paper_library import PaperLibrary
from src.metadata.citation import bibtex_from_metadata
from src.metadata.citation_readiness import citation_key_from_metadata, validate_citation_ready
from src.metadata.schema import empty_metadata
from tests.integration.test_frozen_v32_transaction_pipeline import NUMBER,_workspace


def test_writer_selection_is_decision_then_paper_number_and_needs_no_year():
    from scripts.prepare_write_article_workdir import _selection_sort_key

    rows = [
        {"paper_number": "0000000000000003", "read_decision": "pending"},
        {"paper_number": "0000000000000002", "read_decision": "read"},
        {"paper_number": "0000000000000004", "read_decision": "skip"},
        {"paper_number": "0000000000000005", "read_decision": "priority"},
        {"paper_number": "0000000000000001", "read_decision": "read"},
    ]
    expected = [
        "0000000000000005", "0000000000000001", "0000000000000002",
        "0000000000000003", "0000000000000004",
    ]
    assert [row["paper_number"] for row in sorted(rows, key=_selection_sort_key)] == expected
    assert [row["paper_number"] for row in sorted(reversed(rows), key=_selection_sort_key)] == expected

def test_writer_resolves_content_and_citation_by_paper_number(tmp_path: Path):
    workspace,papers,ledger,catalog_root=_workspace(tmp_path); write_formalization_plan(workspace,papers_dir=papers); commit_paper_raw(workspace,paper_raw_root=tmp_path/"paper_raw",papers_dir=papers,ledger_path=ledger,catalog_root=catalog_root,transactions_dir=tmp_path/"transactions")
    library=PaperLibrary(ledger_path=ledger,papers_dir=papers); metadata=library.load_metadata(NUMBER); catalog=library.load_catalog(NUMBER)
    assert metadata and catalog and catalog["paper_number"]==NUMBER; bib=bibtex_from_metadata(metadata); assert "10.1234/example" in bib and catalog["paper_name"] not in bib

def test_citation_key_is_independent_of_paper_name():
    metadata = empty_metadata(NUMBER)
    metadata.update({"entry_type": "article", "year": 2024})
    metadata["title"]["original"] = "Stable citation identity"
    metadata["authors"] = [{"full_name": "Jane Smith", "family": "Smith", "given": "Jane", "orcid": "", "affiliation": ""}]
    metadata["first_author"] = {"family": "Smith", "display": "Jane Smith"}
    metadata["container"]["journal"] = "Journal"
    metadata["identifiers"]["doi"] = "10.1234/stable"
    first = citation_key_from_metadata(metadata)
    metadata["paper_name"] = "forbidden_name_that_must_not_be_read"
    assert citation_key_from_metadata(metadata) == first


def test_non_journal_stable_identifier_is_citation_ready():
    metadata = empty_metadata(NUMBER)
    metadata.update({"entry_type": "thesis", "year": 2022})
    metadata["title"]["original"] = "A thesis"
    metadata["authors"] = [{"full_name": "Alex Doe", "family": "Doe", "given": "Alex", "orcid": "", "affiliation": ""}]
    metadata["first_author"] = {"family": "Doe", "display": "Alex Doe"}
    metadata["container"]["institution"] = "Example University"
    metadata["links"]["url"] = "https://example.edu/thesis/123"
    result = validate_citation_ready(metadata)
    assert result.ready, result.errors
    assert result.generated_csl and result.generated_bibtex

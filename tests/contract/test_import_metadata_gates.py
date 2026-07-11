import json
import runpy
import sys
from pathlib import Path

from scripts.validate_v2_library import validate_v2_library
from src.discovery.models import PaperCandidate
from src.services.asset_manifest import write_asset_manifest
from src.services.network_metadata_staging import _metadata_from_record
from src.services.metadata_resolver import patch_from_candidate
from src.metadata.schema import empty_metadata, validate_metadata_schema


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _run_script(script: str, argv: list[str]) -> int:
    saved = sys.argv
    sys.argv = argv
    try:
        runpy.run_path(str(_REPO_ROOT / "scripts" / script), run_name="__main__")
        return 0
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = saved


def test_network_metadata_requires_doi(tmp_path, monkeypatch):
    input_path = tmp_path / "candidates.jsonl"
    input_path.write_text(json.dumps({"title": "No DOI Paper", "year": 2024}) + "\n", encoding="utf-8")
    paper_raw = tmp_path / "paper_raw"
    ledger = tmp_path / "catalog" / "paper_number_ledger.json"
    report = tmp_path / "report.json"
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_script(
        "stage_network_metadata_to_paper_raw.py",
        [
            "stage_network_metadata_to_paper_raw.py",
            "--input", str(input_path),
            "--paper-raw-dir", str(paper_raw),
            "--ledger-path", str(ledger),
            "--report", str(report),
            "--apply",
        ],
    )

    assert rc == 1
    assert not paper_raw.exists() or not any(paper_raw.iterdir())
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["items"][0]["error"] == "network/search metadata import requires metadata.identifiers.doi"


def test_network_metadata_rejects_invalid_doi(tmp_path, monkeypatch):
    input_path = tmp_path / "candidates.jsonl"
    input_path.write_text(json.dumps({"title": "Bad DOI", "year": 2024, "doi": "not-a-doi"}) + "\n", encoding="utf-8")
    paper_raw = tmp_path / "paper_raw"
    ledger = tmp_path / "catalog" / "paper_number_ledger.json"
    report = tmp_path / "report.json"
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_script(
        "stage_network_metadata_to_paper_raw.py",
        [
            "stage_network_metadata_to_paper_raw.py",
            "--input", str(input_path),
            "--paper-raw-dir", str(paper_raw),
            "--ledger-path", str(ledger),
            "--report", str(report),
            "--apply",
        ],
    )

    assert rc == 1
    assert not paper_raw.exists() or not any(paper_raw.iterdir())
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["items"][0]["error"] == "network_metadata_requires_valid_doi"


def test_network_metadata_maps_publication_fields(tmp_path, monkeypatch):
    input_path = tmp_path / "candidates.jsonl"
    input_path.write_text(json.dumps({
        "title": "Network Paper",
        "year": 2024,
        "doi": "https://doi.org/10.1000/example",
        "provider": "openalex",
        "venue": "Test Journal",
        "volume": "12",
        "issue": "3",
        "page": "45-56",
        "authors": [{"family": "Wang", "given": "A", "full_name": "A Wang"}],
    }) + "\n", encoding="utf-8")
    paper_raw = tmp_path / "paper_raw"
    ledger = tmp_path / "catalog" / "paper_number_ledger.json"
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_script(
        "stage_network_metadata_to_paper_raw.py",
        [
            "stage_network_metadata_to_paper_raw.py",
            "--input", str(input_path),
            "--paper-raw-dir", str(paper_raw),
            "--ledger-path", str(ledger),
            "--apply",
        ],
    )

    assert rc == 0
    metadata = json.loads((paper_raw / "0000000000000001" / "0000000000000001.metadata.json").read_text(encoding="utf-8"))
    assert metadata["identifiers"]["doi"] == "10.1000/example"
    assert metadata["publication"]["volume"] == "12"
    assert metadata["publication"]["number"] == "3"
    assert metadata["publication"]["issue"] == "3"
    assert metadata["publication"]["pages"] == "45-56"
    assert "metadata_match" not in metadata
    assert validate_metadata_schema(metadata) == []
    status = json.loads((paper_raw / "0000000000000001" / ".import_status.json").read_text(encoding="utf-8"))
    assert status["metadata"]["state"] == "resolved"


def test_crossref_raw_canonicalizes_structured_metadata():
    metadata = _metadata_from_record("0000000000000001", {
        "title": "Flat title fallback",
        "doi": "10.1000/book",
        "provider": "crossref",
        "raw": {
            "DOI": "10.1000/book",
            "type": "edited-book",
            "title": ["Raw Book Title"],
            "author": [{"given": "A. P.", "family": "Van Ulden", "ORCID": "https://orcid.org/0000-0001"}],
            "publisher": "Oxford University Press",
            "container-title": ["Book Series"],
            "volume": "7",
            "issue": "2",
            "page": "11-22",
            "ISSN": ["1234-5678"],
            "ISBN": ["9780000000000"],
            "issued": {"date-parts": [[2024, 5, 6]]},
        },
    })
    assert metadata["entry_type"] == "book"
    assert metadata["title"]["original"] == "Raw Book Title"
    assert metadata["authors"][0]["family"] == "Van Ulden"
    assert metadata["first_author"]["family"] == "Van Ulden"
    assert metadata["container"]["publisher"] == "Oxford University Press"
    assert metadata["publication"]["volume"] == "7"
    assert metadata["publication"]["issue"] == "2"
    assert metadata["publication"]["pages"] == "11-22"
    assert metadata["identifiers"]["issn"] == "1234-5678"
    assert metadata["identifiers"]["isbn"] == "9780000000000"
    assert "metadata_match" not in metadata


def test_openalex_type_and_affiliation_are_canonicalized():
    metadata = _metadata_from_record("0000000000000001", {
        "title": "Fallback",
        "doi": "10.1000/oa",
        "provider": "openalex",
        "raw": {
            "doi": "https://doi.org/10.1000/oa",
            "type": "posted-content",
            "display_name": "OpenAlex Preprint",
            "publication_year": 2025,
            "authorships": [{
                "author": {"display_name": "Alice Smith", "orcid": "https://orcid.org/0000-0002"},
                "institutions": [{"display_name": "Example University"}],
            }],
            "primary_location": {
                "pdf_url": "https://example.test/paper.pdf",
                "source": {"display_name": "Preprint Server"},
            },
        },
    })
    assert metadata["entry_type"] == "preprint"
    assert metadata["authors"][0]["family"] == "Smith"
    assert metadata["authors"][0]["affiliation"] == "Example University"
    assert metadata["links"]["pdf_url"] == "https://example.test/paper.pdf"
    assert "metadata_match" not in metadata


def test_canonicalization_merges_resolution_and_candidate_raw_by_field():
    metadata = _metadata_from_record("0000000000000001", {
        "title": "Candidate Title",
        "doi": "10.1000/merge",
        "provider": "openalex",
        "raw": {
            "doi": "https://doi.org/10.1000/merge",
            "type": "journal-article",
            "display_name": "Candidate Title",
            "publication_year": 2024,
            "authorships": [{
                "author": {"display_name": "Alice Smith", "orcid": "https://orcid.org/0000-0002"},
                "institutions": [{"display_name": "Example University"}],
            }],
            "primary_location": {
                "pdf_url": "https://example.test/oa.pdf",
                "source": {"display_name": "Candidate Journal"},
            },
        },
        "doi_resolution": {
            "provider": "crossref",
            "confidence": 0.95,
            "raw_record": {
                "DOI": "10.1000/merge",
                "type": "journal-article",
                "title": ["Resolved Title"],
                "author": [{"given": "Alice", "family": "Smith"}],
                "container-title": ["Resolved Journal"],
                "publisher": "Resolved Publisher",
                "volume": "9",
                "issue": "4",
                "page": "100-110",
                "issued": {"date-parts": [[2024]]},
            },
        },
    })
    assert metadata["title"]["original"] == "Resolved Title"
    assert metadata["container"]["journal"] == "Resolved Journal"
    assert metadata["container"]["publisher"] == "Resolved Publisher"
    assert metadata["publication"]["volume"] == "9"
    assert metadata["publication"]["pages"] == "100-110"
    assert metadata["authors"][0]["affiliation"] == "Example University"
    assert metadata["links"]["pdf_url"] == "https://example.test/oa.pdf"


def test_network_metadata_with_doi_but_missing_authors_or_venue_is_not_matched(tmp_path, monkeypatch):
    input_path = tmp_path / "candidates.jsonl"
    input_path.write_text(json.dumps({
        "title": "Incomplete Network Paper",
        "year": 2024,
        "doi": "10.1000/incomplete",
        "provider": "openalex",
    }) + "\n", encoding="utf-8")
    paper_raw = tmp_path / "paper_raw"
    ledger = tmp_path / "catalog" / "paper_number_ledger.json"
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_script(
        "stage_network_metadata_to_paper_raw.py",
        [
            "stage_network_metadata_to_paper_raw.py",
            "--input", str(input_path),
            "--paper-raw-dir", str(paper_raw),
            "--ledger-path", str(ledger),
            "--apply",
        ],
    )

    assert rc == 0
    folder = paper_raw / "0000000000000001"
    metadata = json.loads((folder / "0000000000000001.metadata.json").read_text(encoding="utf-8"))
    assert "metadata_match" not in metadata
    status = json.loads((folder / ".import_status.json").read_text(encoding="utf-8"))
    assert status["metadata"]["state"] == "resolved"


def test_network_metadata_complete_record_can_be_matched(tmp_path, monkeypatch):
    input_path = tmp_path / "candidates.jsonl"
    input_path.write_text(json.dumps({
        "title": "Complete Network Paper",
        "year": 2024,
        "doi": "10.1000/complete",
        "provider": "openalex",
        "venue": "Test Journal",
        "authors": [{"family": "Wang", "given": "A", "full_name": "A Wang"}],
    }) + "\n", encoding="utf-8")
    paper_raw = tmp_path / "paper_raw"
    ledger = tmp_path / "catalog" / "paper_number_ledger.json"
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_script(
        "stage_network_metadata_to_paper_raw.py",
        [
            "stage_network_metadata_to_paper_raw.py",
            "--input", str(input_path),
            "--paper-raw-dir", str(paper_raw),
            "--ledger-path", str(ledger),
            "--apply",
        ],
    )

    assert rc == 0
    metadata = json.loads((paper_raw / "0000000000000001" / "0000000000000001.metadata.json").read_text(encoding="utf-8"))
    assert "metadata_match" not in metadata
    assert validate_metadata_schema(metadata) == []


def test_network_provider_list_stays_in_raw_record_sidecar_not_metadata(tmp_path, monkeypatch):
    input_path = tmp_path / "candidates.jsonl"
    input_path.write_text(json.dumps({
        "title": "Merged Network Paper",
        "year": 2024,
        "doi": "10.1000/merged",
        "provider": "openalex",
        "providers": ["openalex", "crossref"],
        "venue": "Test Journal",
        "authors": [{"family": "Wang", "given": "A", "full_name": "A Wang"}],
    }) + "\n", encoding="utf-8")
    paper_raw = tmp_path / "paper_raw"
    ledger = tmp_path / "catalog" / "paper_number_ledger.json"
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_script(
        "stage_network_metadata_to_paper_raw.py",
        [
            "stage_network_metadata_to_paper_raw.py",
            "--input", str(input_path),
            "--paper-raw-dir", str(paper_raw),
            "--ledger-path", str(ledger),
            "--apply",
        ],
    )

    assert rc == 0
    folder = paper_raw / "0000000000000001"
    metadata = json.loads((folder / "0000000000000001.metadata.json").read_text(encoding="utf-8"))
    assert metadata["source"]["provider"] == "openalex"
    assert "providers" not in metadata["source"]
    assert "raw_record" not in metadata["source"]
    sidecar = json.loads((folder / metadata["source"]["raw_record_path"]).read_text(encoding="utf-8"))
    assert sidecar["provider"] == "openalex"
    assert sidecar["providers"] == ["openalex", "crossref"]
    assert sidecar["record"]["doi"] == "10.1000/merged"


def test_network_and_resolver_metadata_share_shape():
    network = _metadata_from_record("0000000000000001", {
        "title": "Shape Paper",
        "year": 2024,
        "doi": "10.1000/shape",
        "provider": "openalex",
        "venue": "Shape Journal",
        "authors": [{"family": "Wang", "given": "A", "full_name": "A Wang"}],
    })
    resolver = patch_from_candidate(
        "0000000000000001",
        PaperCandidate(
            title="Shape Paper",
            authors=["A Wang"],
            year=2024,
            venue="Shape Journal",
            doi="10.1000/shape",
            source="openalex",
            confidence=0.9,
        ),
    )

    for key in ("title", "source", "identifiers", "container", "publication"):
        assert set(network[key]) == set(resolver[key])
    assert set(network) == set(resolver)


def test_fetch_rejects_invalid_doi_before_provider(tmp_path, monkeypatch):
    paper_raw = tmp_path / "paper_raw"
    folder = paper_raw / "0000000000000001"
    folder.mkdir(parents=True)
    metadata = empty_metadata("0000000000000001", source_type="network_search")
    metadata["identifiers"]["doi"] = "not-a-doi"
    (folder / "0000000000000001.metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

    def _boom(*args, **kwargs):
        raise AssertionError("fetch provider should not be called")

    import src.fetch.fetch_pipeline as fetch_pipeline
    monkeypatch.setattr(fetch_pipeline, "fetch_pdf", _boom)
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_script(
        "fetch_pdf_for_paper_raw.py",
        [
            "fetch_pdf_for_paper_raw.py",
            "--paper-number", "0000000000000001",
            "--paper-raw-dir", str(paper_raw),
            "--apply",
        ],
    )

    assert rc == 0
    assert not (folder / ".import_status.json").exists()


def test_validate_formal_library_requires_doi(tmp_path):
    from src.metadata.citation_readiness import validate_citation_ready
    metadata = empty_metadata("0000000000000001")
    metadata["title"]["original"] = "Test Paper"
    metadata["year"] = 2024
    metadata["authors"] = [{"full_name": "Wang A", "family": "Wang", "given": "A", "orcid": "", "affiliation": ""}]
    metadata["container"]["journal"] = "Test Journal"
    result = validate_citation_ready(metadata)
    assert not result.ready
    assert "journal article requires valid DOI" in result.errors

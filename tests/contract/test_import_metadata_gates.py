import json
import runpy
import sys
from pathlib import Path

from scripts.validate_v2_library import validate_v2_library
from src.discovery.models import PaperCandidate
from src.services.asset_manifest import write_asset_manifest
from src.services.network_metadata_staging import _metadata_from_record
from src.services.metadata_resolver import patch_from_candidate
from src.services.v2_library import empty_catalog, empty_metadata, validate_metadata_schema
from tests.helpers.paper_raw_factory import fill_valid_catalog_v31


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
    assert metadata["metadata_match"]["status"] == "matched"
    assert metadata["metadata_match"]["source"] == "openalex"
    assert metadata["metadata_match"]["confidence"] == 0.80
    assert validate_metadata_schema(metadata) == []
    status = json.loads((paper_raw / "0000000000000001" / ".import_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "metadata_matched"
    assert status["doi"] == "10.1000/example"
    assert status["source_provider"] == "openalex"


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
    assert metadata["metadata_match"]["status"] == "unmatched"
    warnings = metadata["metadata_match"]["warnings"]
    assert "missing authors" in warnings
    assert "missing first_author.family" in warnings
    assert "missing container journal/conference/booktitle" in warnings
    status = json.loads((folder / ".import_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "metadata_manual_review_required"
    assert "metadata_matched" != status["status"]


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
    assert metadata["metadata_match"]["status"] == "matched"
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
    pid = "2024_wang_测试论文"
    folder = tmp_path / "papers" / pid
    folder.mkdir(parents=True)
    metadata = empty_metadata(pid)
    metadata["title"]["original"] = "Test Paper"
    metadata["year"] = 2024
    metadata["authors"] = [{"full_name": "Wang A", "family": "Wang", "given": "A", "orcid": "", "affiliation": ""}]
    metadata["container"]["journal"] = "Test Journal"
    metadata["metadata_match"]["status"] = "matched"
    catalog = fill_valid_catalog_v31(
        empty_catalog(),
        paper_number="0000000000000001",
        title_zh="测试论文",
        title_original="Test Paper",
        domain="test",
    )
    catalog["library_locator"]["paper_id"] = pid
    (folder / f"{pid}.metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    (folder / f"{pid}.catalog.json").write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    (folder / f"{pid}.md").write_text("# Test", encoding="utf-8")
    (folder / f"{pid}.pdf").write_bytes(b"%PDF")
    (folder / "images").mkdir()
    marker_name = "0000000000000001" + ".paper.number"
    (folder / marker_name).write_text("0000000000000001", encoding="utf-8")
    write_asset_manifest(folder, prefix=pid, paper_number="0000000000000001", paper_id=pid, stage="papers")
    all_catalog = tmp_path / "catalog" / "all.catalog.json"
    all_catalog.parent.mkdir()
    all_catalog.write_text(json.dumps({"schema_version": "1.0", "papers": []}), encoding="utf-8")

    errors, _ = validate_v2_library(papers_dir=tmp_path / "papers", all_catalog_path=all_catalog, check_paths=False)

    assert any(f"{pid} metadata.identifiers.doi is required in formal library" in err for err in errors)

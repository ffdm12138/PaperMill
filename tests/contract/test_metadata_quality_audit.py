from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.audit_metadata_quality import main as audit_main
from scripts.pack_repo import _should_pack
from scripts.validate_v2_library import validate_v2_library
from src.services.metadata_quality import audit_metadata_library
from src.services.v2_library import PaperNumberLedger, empty_catalog, empty_metadata
from src.services.asset_manifest import write_asset_manifest
from tests.helpers.paper_raw_factory import fill_valid_catalog_v31


_ROOT = Path(__file__).resolve().parent.parent.parent


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _valid_metadata(paper_number: str, idx: int) -> dict:
    metadata = empty_metadata(paper_number)
    metadata["title"]["original"] = f"Metadata Audit Paper {idx}"
    metadata["year"] = 2024
    metadata["authors"] = [
        {"full_name": f"Author {idx}", "family": f"Author{idx}", "given": "A", "orcid": "", "affiliation": ""}
    ]
    metadata["first_author"]["family"] = f"Author{idx}"
    metadata["first_author"]["display"] = f"Author {idx}"
    metadata["container"]["journal"] = "Metadata Audit Journal"
    metadata["container"]["publisher"] = "Metadata Audit Press"
    metadata["publication"]["volume"] = "12"
    metadata["publication"]["number"] = "3"
    metadata["publication"]["issue"] = "3"
    metadata["publication"]["pages"] = "45-67"
    metadata["identifiers"]["doi"] = f"10.5555/metadata-audit.{idx}"
    metadata["links"]["url"] = f"https://example.org/paper/{idx}"
    metadata["source"]["raw_record_path"] = "source_records/metadata_source.test.json"
    metadata["metadata_match"] = {
        "status": "matched",
        "source": "test",
        "confidence": 1.0,
        "matched_at": "2026-01-01",
        "warnings": [],
    }
    return metadata


def _catalog(pid: str, number: str) -> dict:
    catalog = fill_valid_catalog_v31(
        empty_catalog(),
        paper_number=number,
        title_zh=f"元数据审计{number[-1]}",
        title_original="Metadata Audit Paper",
        domain="metadata_audit",
    )
    catalog["library_locator"]["paper_id"] = pid
    catalog["library_locator"]["asset_refs"].update({
        "markdown": f"{pid}.md",
        "pdf": f"{pid}.pdf",
        "metadata": f"{pid}.metadata.json",
        "catalog": f"{pid}.catalog.json",
        "asset_manifest": f"{pid}.asset_manifest.json",
        "images_dir": "images/",
    })
    catalog["provenance"]["markdown_path"] = f"{pid}.md"
    return catalog


def _all_catalog_entry(pid: str, number: str) -> dict:
    catalog = _catalog(pid, number)
    return {
        "paper_number": number,
        "paper_id": pid,
        "paper_dir": "",
        "asset_refs": {"markdown": "", "pdf": "", "images_dir": ""},
        "content_identity": catalog["content_identity"],
        "terminology": catalog["terminology"],
        "classification": catalog["classification"],
        "screening": catalog["screening"],
        "research_card": catalog["research_card"],
        "writing_value": catalog["writing_value"],
        "evidence_profile": catalog["evidence_profile"],
        "figure_inventory": catalog["figure_inventory"],
        "quality_control": catalog["quality_control"],
        "provenance": catalog["provenance"],
    }


def _make_library(tmp_path: Path, mutators=None) -> tuple[Path, Path]:
    mutators = mutators or {}
    papers_dir = tmp_path / "data" / "papers"
    catalog_dir = tmp_path / "data" / "catalog"
    all_catalog = catalog_dir / "all.catalog.json"
    all_entries = []
    index_entries = []
    ledger_data = PaperNumberLedger(catalog_dir / "paper_number_ledger.json").empty_data()
    for idx in range(1, len(mutators) + 2):
        number = f"{idx:016d}"
        pid = f"2024_Author{idx}_元数据审计{idx}"
        folder = papers_dir / pid
        images = folder / "images"
        images.mkdir(parents=True)
        metadata = _valid_metadata(number, idx)
        mutator = mutators.get(idx)
        if mutator:
            mutator(metadata)
        catalog = _catalog(pid, number)
        metadata_path = folder / f"{pid}.metadata.json"
        catalog_path = folder / f"{pid}.catalog.json"
        markdown_path = folder / f"{pid}.md"
        pdf_path = folder / f"{pid}.pdf"
        _write_json(metadata_path, metadata)
        _write_json(catalog_path, catalog)
        _write_json(folder / f"{number}.paper.number", {"paper_number": number, "folder_name": pid})
        ledger_data["max_number"] = number
        ledger_data["items"][number] = {
            "folder_name": pid,
            "folder_path": str(folder),
            "planned_paper_id": pid,
            "state": "active",
            "created_at": "2026-01-01T00:00:00",
            "activated_at": "2026-01-01T00:00:00",
        }
        markdown_path.write_text("# Metadata Audit\n", encoding="utf-8")
        pdf_path.write_bytes(f"%PDF-{idx}".encode("utf-8"))
        write_asset_manifest(folder, prefix=pid, paper_number=number, paper_id=pid, stage="papers")
        _write_json(folder / "source_records" / "metadata_source.test.json", {"id": idx})
        all_entries.append(_all_catalog_entry(pid, number))
        index_entries.append({
            "paper_number": number,
            "paper_id": pid,
            "metadata_path": str(metadata_path),
            "catalog_path": str(catalog_path),
            "markdown_path": str(markdown_path),
            "pdf_path": str(pdf_path),
            "images_dir": str(images),
        })
    _write_json(all_catalog, {"schema_version": "3.1", "updated_at": "", "papers": all_entries})
    _write_json(catalog_dir / "paper_index.json", {
        "schema_version": "2.0",
        "updated_at": "",
        "papers": index_entries,
    })
    _write_json(catalog_dir / "paper_number_ledger.json", ledger_data)
    return papers_dir, all_catalog


@pytest.mark.parametrize(
    ("mutator", "audit_text", "validate_text"),
    [
        (lambda m: m["identifiers"].update({"doi": ""}), "missing metadata.identifiers.doi", "metadata.identifiers.doi is required in formal library"),
        (lambda m: m["title"].update({"original": ""}), "missing metadata.title.original", "metadata.title.original is required"),
        (lambda m: m.update({"authors": []}), "missing metadata.authors", "metadata.authors must contain at least one author"),
        (lambda m: m.update({"year": None}), "missing metadata.year", "metadata.year is required"),
        (lambda m: m["container"].update({"journal": "", "conference": "", "booktitle": "", "venue": ""}), "missing metadata.container venue", "metadata.container.journal"),
        (lambda m: m["metadata_match"].update({"status": "unmatched"}), "metadata.metadata_match.status must be matched or manual_confirmed", "metadata.metadata_match.status must be matched or manual_confirmed"),
    ],
)
def test_metadata_quality_hard_errors_match_validate(tmp_path, mutator, audit_text, validate_text):
    papers_dir, all_catalog = _make_library(tmp_path, {1: mutator})

    report = audit_metadata_library(papers_dir)
    errors, _ = validate_v2_library(papers_dir=papers_dir, all_catalog_path=all_catalog, check_paths=False)

    assert any(audit_text in error for error in report["errors"])
    assert any(validate_text in error for error in errors)


def test_missing_pages_or_issue_are_audit_warnings_not_validate_errors(tmp_path):
    def mutate(metadata: dict) -> None:
        metadata["publication"]["pages"] = ""
        metadata["publication"]["article_number"] = ""
        metadata["publication"]["issue"] = ""
        metadata["publication"]["number"] = ""

    papers_dir, all_catalog = _make_library(tmp_path, {1: mutate})

    report = audit_metadata_library(papers_dir)
    errors, warnings = validate_v2_library(papers_dir=papers_dir, all_catalog_path=all_catalog, check_paths=False)

    assert any("missing publication.pages or publication.article_number" in warning for warning in report["warnings"])
    assert any("missing publication.issue or publication.number" in warning for warning in report["warnings"])
    assert errors == []
    assert any("metadata.publication.pages or metadata.publication.article_number is missing" in warning for warning in warnings)


def test_duplicate_doi_is_audit_and_validate_error(tmp_path):
    doi = "10.5555/metadata-audit.duplicate"
    papers_dir, all_catalog = _make_library(
        tmp_path,
        {
            1: lambda m: m["identifiers"].update({"doi": doi}),
            2: lambda m: m["identifiers"].update({"doi": f"https://doi.org/{doi}"}),
        },
    )

    report = audit_metadata_library(papers_dir)
    errors, _ = validate_v2_library(papers_dir=papers_dir, all_catalog_path=all_catalog, check_paths=False)

    assert any("duplicate metadata.identifiers.doi" in error for error in report["errors"])
    assert any("duplicate metadata.identifiers.doi in formal library" in error for error in errors)


def test_invalid_doi_is_audit_and_validate_error(tmp_path):
    papers_dir, all_catalog = _make_library(tmp_path, {1: lambda m: m["identifiers"].update({"doi": "not-a-doi"})})

    report = audit_metadata_library(papers_dir)
    errors, _ = validate_v2_library(papers_dir=papers_dir, all_catalog_path=all_catalog, check_paths=False)

    assert any("invalid metadata.identifiers.doi" in error for error in report["errors"])
    assert any("metadata.identifiers.doi must be a valid DOI for formal commit" in error for error in errors)


def test_audit_report_writes_stable_json(tmp_path):
    papers_dir, _ = _make_library(tmp_path)
    report_path = tmp_path / "data" / "catalog" / "metadata_quality_report.json"

    code = audit_main(["--papers-dir", str(papers_dir), "--report", "--report-path", str(report_path)])

    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert code == 0
    assert data["total"] == 1
    assert data["errors"] == []
    assert data["papers"][0]["hard_status"] == "ok"
    assert set(data) == {"total", "errors", "warnings", "papers"}


def test_metadata_quality_report_is_ignored_and_not_packed():
    gitignore = (_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "data/catalog/metadata_quality_report.json" in gitignore
    assert not _should_pack("data/catalog/metadata_quality_report.json")

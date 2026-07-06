from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.prepare_write_article_workdir import prepare_workdir
from scripts.validate_v2_library import validate_v2_library
from src.services.v2_library import (
    AllCatalogBuilder,
    PaperNumberLedger,
    empty_catalog,
    empty_metadata,
)
from src.services.asset_manifest import write_asset_manifest
from tests.helpers.paper_raw_factory import fill_valid_catalog_v31


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _metadata(pid: str, idx: int = 1) -> dict:
    metadata = empty_metadata(pid)
    metadata["title"]["original"] = f"Repository State Paper {idx}"
    metadata["year"] = 2024
    metadata["authors"] = [
        {"full_name": f"Author {idx}", "family": f"Author{idx}", "given": "A", "orcid": "", "affiliation": ""}
    ]
    metadata["first_author"]["family"] = f"Author{idx}"
    metadata["first_author"]["display"] = f"Author {idx}"
    metadata["container"]["journal"] = "Repository State Journal"
    metadata["publication"]["volume"] = "1"
    metadata["publication"]["number"] = "1"
    metadata["publication"]["issue"] = "1"
    metadata["publication"]["pages"] = "1-10"
    metadata["identifiers"]["doi"] = f"10.5555/repository-state.{idx}"
    metadata["metadata_match"] = {
        "status": "matched",
        "source": "test",
        "confidence": 1.0,
        "matched_at": "2026-01-01",
        "warnings": [],
    }
    return metadata


def _catalog(pid: str, number: str = "0000000000000001") -> dict:
    catalog = fill_valid_catalog_v31(
        empty_catalog(),
        paper_number=number,
        title_zh=f"仓库状态论文{number[-1]}",
        title_original="Repository State Paper",
        domain="repo_state",
    )
    catalog["library_locator"]["paper_id"] = pid
    return catalog


def _formal_paper(tmp_path: Path, idx: int = 1) -> tuple[Path, str, str]:
    number = f"{idx:016d}"
    pid = f"2024_author{idx}_repository_state_{idx}"
    folder = tmp_path / "data" / "papers" / pid
    (folder / "images").mkdir(parents=True)
    _write_json(folder / f"{pid}.metadata.json", _metadata(pid, idx))
    _write_json(folder / f"{pid}.catalog.json", _catalog(pid, number))
    (folder / f"{pid}.md").write_text("# Repository State Paper\n", encoding="utf-8")
    (folder / f"{pid}.pdf").write_bytes(b"%PDF")
    write_asset_manifest(folder, prefix=pid, paper_number=number, paper_id=pid, stage="papers")
    (folder / f"{number}.paper.number").write_text(number, encoding="utf-8")
    return folder, pid, number


def _content_only_all_catalog(pid: str, number: str) -> dict:
    catalog = _catalog(pid, number)
    return {
        "paper_number": number,
        "paper_id": pid,
        "paper_dir": "",
        "asset_refs": {
            "markdown": "",
            "pdf": "",
            "images_dir": "",
        },
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


def test_committed_all_catalog_template_is_content_only():
    template_path = _REPO_ROOT / "data" / "catalog" / "all.catalog.template.json"
    data = json.loads(template_path.read_text(encoding="utf-8"))

    assert data == {"schema_version": "3.1", "updated_at": "", "papers": []}


def test_validate_rejects_old_all_catalog_wrapper(tmp_path):
    papers_dir = tmp_path / "data" / "papers"
    papers_dir.mkdir(parents=True)
    all_catalog = tmp_path / "data" / "catalog" / "all.catalog.json"
    entry = _content_only_all_catalog("paper_a", "0000000000000001")
    entry["catalog"] = {"classification": {}}
    entry["metadata"] = {"identifiers": {"doi": "10.1/legacy"}}
    _write_json(all_catalog, {"schema_version": "3.1", "updated_at": "", "papers": [entry]})

    errors, _ = validate_v2_library(papers_dir=papers_dir, all_catalog_path=all_catalog, check_paths=False)

    assert any("legacy wrapper/path key: catalog" in error for error in errors)
    assert any("must not embed metadata" in error for error in errors)


def test_pack_repo_has_no_stale_head_catalog_logic():
    text = (_REPO_ROOT / "scripts" / "pack_repo.py").read_text(encoding="utf-8")

    assert "_GIT_CATALOG_FILES" not in text
    assert "git show" not in text
    assert "HEAD:" not in text


def test_clean_checkout_empty_library_validate_passes(tmp_path):
    papers_dir = tmp_path / "data" / "papers"
    papers_dir.mkdir(parents=True)
    all_catalog = tmp_path / "data" / "catalog" / "all.catalog.json"

    errors, warnings = validate_v2_library(papers_dir=papers_dir, all_catalog_path=all_catalog, check_paths=False)

    assert errors == []
    assert warnings == []


def test_all_catalog_schema_version_must_be_v2(tmp_path):
    papers_dir = tmp_path / "data" / "papers"
    papers_dir.mkdir(parents=True)
    all_catalog = tmp_path / "data" / "catalog" / "all.catalog.json"
    _write_json(all_catalog, {"schema_version": "1.0", "papers": []})

    errors, _ = validate_v2_library(papers_dir=papers_dir, all_catalog_path=all_catalog, check_paths=False)

    assert "all.catalog.schema_version must be 3.1" in errors


def test_validate_rejects_stale_formal_catalog_markdown_path(tmp_path):
    folder, pid, number = _formal_paper(tmp_path)
    catalog_path = folder / f"{pid}.catalog.json"
    catalog = _catalog(pid, number)
    catalog["library_locator"]["asset_refs"] = {
        "markdown": f"{pid}.md",
        "pdf": f"{pid}.pdf",
        "metadata": f"{pid}.metadata.json",
        "catalog": f"{pid}.catalog.json",
        "asset_manifest": f"{pid}.asset_manifest.json",
        "images_dir": "images/",
    }
    catalog["provenance"]["markdown_path"] = f"{number}.md"
    _write_json(catalog_path, catalog)
    all_catalog = tmp_path / "data" / "catalog" / "all.catalog.json"
    all_catalog.parent.mkdir(parents=True)
    all_catalog.write_text(json.dumps({"schema_version": "3.1", "updated_at": "", "papers": []}), encoding="utf-8")

    errors, _ = validate_v2_library(papers_dir=folder.parent, all_catalog_path=all_catalog, check_paths=False)

    assert any("catalog.provenance.markdown_path must be" in err for err in errors)


@pytest.mark.parametrize("legacy_key", ["folder_path", "main_md", "metadata_file", "catalog_file", "display"])
def test_validate_rejects_legacy_path_fields(tmp_path, legacy_key):
    papers_dir = tmp_path / "data" / "papers"
    papers_dir.mkdir(parents=True)
    all_catalog = tmp_path / "data" / "catalog" / "all.catalog.json"
    entry = _content_only_all_catalog("paper_a", "0000000000000001")
    entry[legacy_key] = "legacy"
    _write_json(all_catalog, {"schema_version": "3.1", "updated_at": "", "papers": [entry]})

    errors, _ = validate_v2_library(papers_dir=papers_dir, all_catalog_path=all_catalog, check_paths=False)

    assert any(f"legacy wrapper/path key: {legacy_key}" in error for error in errors)


def test_prepare_reads_metadata_from_papers_not_all_catalog(tmp_path):
    source, pid, number = _formal_paper(tmp_path)
    all_catalog = tmp_path / "data" / "catalog" / "all.catalog.json"
    entry = _content_only_all_catalog(pid, number)
    _write_json(all_catalog, {"schema_version": "3.1", "updated_at": "", "papers": [entry]})
    write_dir = tmp_path / "write"

    report = prepare_workdir(argparse.Namespace(
        job_id="repo_state_job",
        paper_numbers=[number],
        primary_domain=None,
        topic=None,
        read_decision=None,
        min_relevance_score=None,
        limit=None,
        apply=True,
        dry_run=False,
        overwrite=False,
        all_catalog=all_catalog,
        papers_dir=source.parent,
        write_dir=write_dir,
    ))

    selected_path = write_dir / "repo_state_job" / "selected_catalog.json"
    selected = json.loads(selected_path.read_text(encoding="utf-8"))
    assert report["selected_count"] == 1
    assert "metadata" not in entry
    # selected_catalog is content-only; DOI truth lives in the copied article metadata.
    assert "metadata" not in selected["papers"][0]
    article_meta = json.loads(
        (write_dir / "repo_state_job" / "article" / number / f"{pid}.metadata.json")
        .read_text(encoding="utf-8"))
    assert article_meta["identifiers"]["doi"] == "10.5555/repository-state.1"
    assert "catalog" not in selected["papers"][0]
    assert selected["papers"][0]["content_identity"]["content_title_zh"] == entry["content_identity"]["content_title_zh"]


def test_all_catalog_builder_skips_invalid_source_catalog(tmp_path):
    source, pid, _ = _formal_paper(tmp_path)
    catalog_path = source / f"{pid}.catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["schema_version"] = "1.0"
    _write_json(catalog_path, catalog)
    all_catalog = tmp_path / "data" / "catalog" / "all.catalog.json"
    builder = AllCatalogBuilder(
        tmp_path / "data" / "papers",
        all_catalog,
        PaperNumberLedger(tmp_path / "data" / "catalog" / "paper_number_ledger.json"),
    )

    data = builder.build(write=True)

    assert data["papers"] == []
    assert any("catalog.schema_version must be 3.1" in error for error in builder.last_errors)


def test_catalog_load_does_not_write_all_catalog_file(tmp_path):
    """Catalog.load() is read-only: a missing all.catalog must NOT create a file."""
    from src.catalog import Catalog

    all_catalog = tmp_path / "catalog" / "all.catalog.json"
    cat = Catalog(path=all_catalog, papers_dir=tmp_path / "papers")
    data = cat.load()
    # the read must not have created the file on disk (content may come from the
    # global papers_dir default, but the point is: no file is written)
    assert not all_catalog.exists(), "Catalog.load() wrote all.catalog.json (read must be side-effect free)"
    assert isinstance(data.get("papers"), list)


def test_catalog_load_fallback_uses_custom_ledger_path_without_writing_default(tmp_path):
    """Readonly fallback must honor injected ledger path and create no ledger files."""
    from config.settings import PAPER_NUMBER_LEDGER_PATH
    from src.catalog import Catalog

    all_catalog = tmp_path / "catalog" / "all.catalog.json"
    custom_ledger = tmp_path / "catalog" / "custom_ledger.json"
    default_ledger = Path(PAPER_NUMBER_LEDGER_PATH)
    before = default_ledger.read_text(encoding="utf-8") if default_ledger.exists() else None

    data = Catalog(path=all_catalog, papers_dir=tmp_path / "papers", ledger_path=custom_ledger).load()

    after = default_ledger.read_text(encoding="utf-8") if default_ledger.exists() else None
    assert after == before
    assert not all_catalog.exists()
    assert not custom_ledger.exists()
    assert isinstance(data.get("papers"), list)

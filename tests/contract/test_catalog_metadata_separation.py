"""Tests for catalog/metadata separation (metadata v2.0 / catalog v3.1)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.validate_v2_library import validate_v2_library
from src.services.v2_library import (
    AllCatalogBuilder,
    PaperCurationService,
    PaperNumberLedger,
    bibtex_from_metadata,
    find_forbidden_catalog_keys,
    validate_catalog_schema,
    validate_metadata_completeness_for_commit,
    validate_metadata_schema,
    empty_catalog,
    empty_metadata,
)
from src.services.asset_manifest import write_asset_manifest
from tests.helpers.paper_raw_factory import fill_valid_catalog_v31


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _valid_catalog() -> dict:
    return fill_valid_catalog_v31(
        empty_catalog(),
        paper_number="0000000000000001",
        title_zh="娴嬭瘯璁烘枃",
        title_original="A Test Paper",
        domain="snow",
    )


def test_catalog_rejects_doi():
    c = _valid_catalog()
    c["doi"] = "10.1/x"
    errors = validate_catalog_schema(c)
    assert any("forbidden bibliographic key: doi" in e for e in errors)


def test_catalog_rejects_authors():
    c = _valid_catalog()
    c["authors"] = [{"family": "Wang"}]
    errors = validate_catalog_schema(c)
    assert any("forbidden bibliographic key: authors" in e for e in errors)


def test_catalog_rejects_nested_identifiers():
    c = _valid_catalog()
    c["content_identity"]["identifiers"] = {"doi": "10.1/x"}
    errors = validate_catalog_schema(c)
    assert any("content_identity.identifiers" in e for e in errors)


def test_catalog_rejects_legacy_content_title():
    c = _valid_catalog()
    c["content_identity"]["content_title"] = "old"
    errors = validate_catalog_schema(c)
    assert any("content_identity.content_title is legacy" in e for e in errors)


def _build_formal_paper(tmp_path: Path, pid: str = "2024_wang_test", *, doi: str = "10.1/x") -> Path:
    folder = tmp_path / "papers" / pid
    folder.mkdir(parents=True)
    metadata = empty_metadata(pid)
    metadata["title"]["original"] = "A Test Paper"
    metadata["year"] = 2024
    metadata["authors"] = [{"full_name": "Wang A", "family": "Wang", "given": "A", "orcid": "", "affiliation": ""}]
    metadata["container"]["journal"] = "Test Journal"
    metadata["identifiers"]["doi"] = doi
    metadata["metadata_match"] = {"status": "matched", "source": "test", "confidence": 1.0,
                                  "matched_at": "2026-01-01", "warnings": []}
    catalog = _valid_catalog()
    catalog["library_locator"]["paper_id"] = pid
    catalog["library_locator"]["paper_dir"] = str(folder)
    (folder / f"{pid}.metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    (folder / f"{pid}.catalog.json").write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    (folder / f"{pid}.md").write_text("# A Test Paper", encoding="utf-8")
    (folder / f"{pid}.pdf").write_bytes(b"%PDF")
    (folder / "images").mkdir()
    write_asset_manifest(folder, prefix=pid, paper_number="0000000000000001", paper_id=pid, stage="papers")
    # catalog builder v2.2 requires a paper_number marker + ledger entry
    (folder / "0000000000000001.paper.number").write_text(
        json.dumps({"paper_number": "0000000000000001", "folder_name": pid,
                    "state": "active", "planned_paper_id": pid}), encoding="utf-8"
    )
    from src.services.v2_library import PaperNumberLedger
    ledger = PaperNumberLedger(tmp_path / "catalog" / "paper_number_ledger.json")
    ledger.save({"schema_version": "1.0", "max_number": "0000000000000001",
                  "items": {"0000000000000001": {"folder_name": pid,
                     "folder_path": str(folder), "state": "active",
                     "planned_paper_id": pid, "created_at": "2026-01-01"}}})
    return folder


def test_all_catalog_excludes_metadata_fields(tmp_path):
    _build_formal_paper(tmp_path)
    all_catalog = tmp_path / "catalog" / "all.catalog.json"
    ledger = tmp_path / "catalog" / "paper_number_ledger.json"
    AllCatalogBuilder(tmp_path / "papers", all_catalog, PaperNumberLedger(ledger)).build(write=True)
    data = json.loads(all_catalog.read_text(encoding="utf-8"))
    assert data["schema_version"] == "3.1"
    entry = data["papers"][0]
    # all.catalog must NOT carry bibliographic metadata
    assert "metadata" not in entry
    for forbidden in ("doi", "authors", "year", "journal", "venue", "first_author", "identifiers"):
        assert forbidden not in entry, f"all.catalog entry leaked {forbidden}"
        assert forbidden not in json.dumps(entry), f"all.catalog entry leaked {forbidden} anywhere"
    # content fields present
    assert "classification" in entry and "screening" in entry


def test_metadata_still_requires_doi(tmp_path):
    folder = _build_formal_paper(tmp_path)
    meta_path = folder / "2024_wang_test.metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["identifiers"]["doi"] = ""
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    all_catalog = tmp_path / "catalog" / "all.catalog.json"
    AllCatalogBuilder(tmp_path / "papers", all_catalog, PaperNumberLedger(tmp_path / "catalog" / "l.json")).build(write=True)
    errors, _ = validate_v2_library(papers_dir=tmp_path / "papers", all_catalog_path=all_catalog, check_paths=False)
    assert any("doi is required" in e for e in errors)


def test_paper_index_contains_paths_not_bibliography(tmp_path):
    _build_formal_paper(tmp_path)
    all_catalog = tmp_path / "catalog" / "all.catalog.json"
    AllCatalogBuilder(tmp_path / "papers", all_catalog, PaperNumberLedger(tmp_path / "catalog" / "l.json")).build(write=True)
    index = json.loads((all_catalog.parent / "paper_index.json").read_text(encoding="utf-8"))
    item = index["papers"][0]
    for key in ("paper_number", "paper_id", "metadata_path", "catalog_path", "markdown_path", "pdf_path", "images_dir"):
        assert key in item
    # no bibliographic fields
    for forbidden in ("doi", "authors", "year", "journal", "venue", "title"):
        assert forbidden not in item


def test_migrate_catalog_removes_v3_0_fields(tmp_path):
    from scripts.one_shot_migrations.migrate_catalog_v3_0_to_v3_1 import migrate_catalog

    old_catalog = {
        "schema_version": "3.0",
        "paper_number": "0000000000000001",
        "paper_id": "2024_wang_test",
        "asset_refs": {},
        "content_identity": {
            "content_title_zh": "测试标题",
            "content_title_original_candidates": ["0000000000000001", "A Test Paper"]
        },
        "classification": {"primary_domain": "snow"},
        "screening": {"read_decision": "pending", "reason": "测试夹具：旧结果"},
        "research_card": {},
        "evidence_profile": {},
        "content_notes": {"short_summary": "测试摘要。"},
        "terminology": [{"term_original": "snow", "term_zh": "雪", "abbr": "", "note_zh": "测试。"}],
        "provenance": {}
    }
    metadata = empty_metadata("0000000000000001")
    metadata["title"]["original"] = "A Test Paper"
    catalog_path = tmp_path / "2024_wang_test.catalog.json"
    (tmp_path / "2024_wang_test.metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "2024_wang_test.md").write_text("# A Test Paper\n", encoding="utf-8")
    catalog_path.write_text(json.dumps(old_catalog, ensure_ascii=False), encoding="utf-8")

    migrated, report = migrate_catalog(catalog_path)

    assert migrated["schema_version"] == "3.1"
    assert "asset_refs" not in migrated
    assert "naming" not in migrated
    assert "content_notes" not in migrated
    assert migrated["content_identity"]["content_title_original_candidates"] == ["A Test Paper"]
    assert migrated["screening"]["reason"] == ""
    assert report["warnings"]


def test_catalog_curator_skill_declares_content_only():
    text = (_REPO_ROOT / "skills" / "paper_raw_catalog_curator" / "SKILL.md").read_text(encoding="utf-8")
    tl = text.lower()
    assert "v3.1" in text
    assert "v3.0" in text
    assert "content" in tl
    assert "mineru" in tl
    assert "markdown" in tl
    assert "forbidden" in tl
    assert "doi" in tl and "never belong in catalog" in tl
    assert "bibtex" in tl and "never belong in catalog" in tl
    assert "metadata" in tl and "catalog" in tl
    assert "does not output final `paper_id`" in text


def test_metadata_resolver_skill_declares_metadata_only():
    text = (_REPO_ROOT / "skills" / "paper_raw_metadata_resolver" / "SKILL.md").read_text(encoding="utf-8")
    tl = text.lower()
    assert "metadata" in tl
    assert "catalog" in tl
    assert "classification" in tl or "research_card" in tl
    assert "all.catalog" in tl


def test_validate_rejects_all_catalog_with_embedded_metadata(tmp_path):
    _build_formal_paper(tmp_path)
    all_catalog = tmp_path / "catalog" / "all.catalog.json"
    AllCatalogBuilder(tmp_path / "papers", all_catalog, PaperNumberLedger(tmp_path / "catalog" / "l.json")).build(write=True)
    # tamper: inject a metadata key into an entry
    data = json.loads(all_catalog.read_text(encoding="utf-8"))
    data["papers"][0]["metadata"] = {"identifiers": {"doi": "10.1/x"}}
    all_catalog.write_text(json.dumps(data), encoding="utf-8")
    errors, _ = validate_v2_library(papers_dir=tmp_path / "papers", all_catalog_path=all_catalog, check_paths=False)
    assert any("must not embed metadata" in e for e in errors)


def test_catalog_rejects_container_and_publication():
    """Bibliographic wrappers container/publication must be forbidden in catalog."""
    c = _valid_catalog()
    c["container"] = {"journal": "Test Journal"}
    errors = validate_catalog_schema(c)
    assert any("forbidden bibliographic key: container" in e for e in errors)
    assert "container" in find_forbidden_catalog_keys(c)

    c2 = _valid_catalog()
    c2["publication"] = {"volume": "8", "pages": "1-2"}
    errors2 = validate_catalog_schema(c2)
    assert any("forbidden bibliographic key: publication" in e for e in errors2)
    assert "publication" in find_forbidden_catalog_keys(c2)


def test_curator_example_catalog_is_content_only():
    """The bundled curator example must be a valid v3.1 content-only catalog."""
    path = _REPO_ROOT / "skills" / "paper_raw_catalog_curator" / "examples" / "example_catalog.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    assert catalog["schema_version"] == "3.1"
    assert validate_catalog_schema(catalog) == []
    assert find_forbidden_catalog_keys(catalog) == []
    for forbidden in ("doi", "authors", "year", "journal", "venue", "container", "publication", "bibtex"):
        assert forbidden not in json.dumps(catalog), f"example catalog leaked {forbidden}"
    for value in (
        catalog["screening"]["reason"],
        catalog["research_card"]["research_problem"],
        catalog["research_card"]["method_summary"],
        catalog["writing_value"]["short_summary"],
    ):
        assert _has_cjk(value)


def test_catalog_curation_prompt_declares_chinese_values_and_english_keys(tmp_path):
    folder = _build_formal_paper(tmp_path, pid="0000000000000001")
    prompt = PaperCurationService().build_prompt(folder)

    assert "catalog v3.1" in prompt
    assert "content_identity.content_title_zh" in prompt
    assert "writing_value" in prompt
    assert "DOI" in prompt and "metadata" in prompt


def test_pdf_resolver_simplified_metadata_is_rejected():
    """A simplified {title, doi, authors} object must NOT pass the formal metadata
    path 鈥?guards against any future resolver emitting a simplified鏃佽矾 format."""
    simplified = {
        "title": "A bulk blowing-snow model",
        "doi": "10.1023/A:100052170",
        "authors": "D茅ry and Yau",
    }
    # (a) schema-shape validator rejects it (missing nested title/authors/identifiers/...)
    assert validate_metadata_schema(simplified) != []

    # (b) commit completeness gate does not accept it. The simplified shape is
    #     malformed (title is a string, not an object), so the gate either raises
    #     or returns errors 鈥?either way it is NOT an empty accept.
    def _completeness(meta):
        try:
            return validate_metadata_completeness_for_commit(meta)
        except (TypeError, AttributeError):
            return ["simplified metadata rejected by validator"]

    assert _completeness(simplified) != []

    # (c) bibtex cannot be produced correctly from the simplified shape: the doi
    #     lives at the wrong path so no doi line is emitted, and no year line
    bib = bibtex_from_metadata(simplified, key="dery1999")
    assert "doi = {" not in bib
    assert "year = {" not in bib
    assert bib.startswith("@article{")


# --- metadata v2.0 validator rejects legacy content fields ---

def _valid_metadata() -> dict:
    """A schema-valid metadata v2.0 dict used as the base for rejection tests."""
    m = empty_metadata("0000000000000001")
    m["title"]["original"] = "A Test Paper"
    m["year"] = 2024
    m["authors"] = [{"full_name": "Wang A", "family": "Wang", "given": "A", "orcid": "", "affiliation": ""}]
    m["first_author"]["family"] = "Wang"
    m["first_author"]["display"] = "Wang A"
    m["container"]["journal"] = "Test Journal"
    m["identifiers"]["doi"] = "10.1/test"
    m["links"]["url"] = "https://doi.org/10.1/test"
    m["metadata_match"]["status"] = "matched"
    m["metadata_match"]["confidence"] = 1.0
    return m


def test_metadata_valid_v2_base_passes():
    """Sanity: the base helper is accepted by validate_metadata_schema."""
    assert validate_metadata_schema(_valid_metadata()) == []


def test_metadata_rejects_title_short_zh():
    m = _valid_metadata()
    m["title"]["short_zh"] = "娴嬭瘯鐭"
    errors = validate_metadata_schema(m)
    assert any("metadata.title.short_zh is forbidden" in e for e in errors)


def test_metadata_rejects_title_translated_zh():
    m = _valid_metadata()
    m["title"]["translated_zh"] = "缈昏瘧鏍囬"
    errors = validate_metadata_schema(m)
    assert any("metadata.title.translated_zh is forbidden" in e for e in errors)


def test_metadata_rejects_source_raw_record():
    m = _valid_metadata()
    m["source"]["raw_record"] = {"some": "inline"}
    errors = validate_metadata_schema(m)
    assert any("metadata.source.raw_record is forbidden" in e for e in errors)


def test_metadata_rejects_forbidden_top_level_keys():
    for key in ("abstract", "keywords", "notes", "bibtex", "citation_key"):
        m = _valid_metadata()
        m[key] = "x" if key != "keywords" else ["x"]
        errors = validate_metadata_schema(m)
        assert any(f"metadata.{key} is forbidden" in e for e in errors), (key, errors)


def test_metadata_rejects_legacy_source_id():
    m = _valid_metadata()
    m["source_id"] = "legacy-source-id"
    errors = validate_metadata_schema(m)
    assert any("source-id is legacy only" in e for e in errors)


def test_metadata_accepts_source_raw_record_path():
    """raw_record_path (a path, not inline data) is the legitimate v2.0 field."""
    m = _valid_metadata()
    m["source"]["raw_record_path"] = "data/paper_raw/0000000000000001/0000000000000001.metadata.raw_record.json"
    assert validate_metadata_schema(m) == []

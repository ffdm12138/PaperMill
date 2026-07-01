"""Tests for catalog/metadata separation (catalog v2.0 content-only)."""
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
    migrate_catalog_to_v2_0,
    validate_catalog_schema,
    validate_metadata_completeness_for_commit,
    validate_metadata_schema,
    empty_catalog,
    empty_metadata,
)


_REPO_ROOT = Path(__file__).resolve().parent.parent


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _valid_catalog() -> dict:
    c = empty_catalog()
    c["paper_number"] = "0000000000000001"
    c["paper_id"] = "2024_wang_test"
    c["content_identity"]["content_title"] = "A Test Paper"
    c["classification"]["primary_domain"] = "snow"
    c["screening"]["read_decision"] = "must_read"
    return c


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


def test_catalog_accepts_content_title():
    c = _valid_catalog()
    errors = validate_catalog_schema(c)
    assert errors == []
    # content_title is allowed (not forbidden)
    assert "content_identity.content_title" not in find_forbidden_catalog_keys(c)


def _build_formal_paper(tmp_path: Path, pid: str = "2024_wang_test", *, doi: str = "10.1/x") -> Path:
    folder = tmp_path / "papers" / pid
    folder.mkdir(parents=True)
    metadata = empty_metadata(pid)
    metadata["title"]["original"] = "A Test Paper"
    metadata["title"]["short_zh"] = "测试"
    metadata["year"] = 2024
    metadata["authors"] = [{"full_name": "Wang A", "family": "Wang", "given": "A", "orcid": "", "affiliation": ""}]
    metadata["container"]["journal"] = "Test Journal"
    metadata["identifiers"]["doi"] = doi
    metadata["metadata_match"] = {"status": "matched", "source": "test", "confidence": 1.0,
                                  "matched_at": "2026-01-01", "warnings": [], "candidates": []}
    metadata["pdf"]["sha256"] = "abc"
    metadata["pdf"]["file_size"] = 4
    catalog = _valid_catalog()
    catalog["paper_id"] = pid
    (folder / f"{pid}.metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    (folder / f"{pid}.catalog.json").write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    (folder / f"{pid}.md").write_text("# A Test Paper", encoding="utf-8")
    (folder / f"{pid}.pdf").write_bytes(b"%PDF")
    (folder / "images").mkdir()
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
    assert data["schema_version"] == "2.0"
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


def test_migrate_catalog_removes_forbidden_fields():
    old = {
        "schema_version": "1.1",
        "display": {"title_original": "Keep", "doi": "10.1/x", "year": 2020, "authors_short": "Wang et al.", "venue": "J"},
        "classification": {"primary_domain": "snow", "domains": [], "topics": [], "keywords_en": [], "keywords_zh": []},
    }
    new, removed = migrate_catalog_to_v2_0(old)
    assert new["schema_version"] == "2.0"
    assert "display" not in new
    assert validate_catalog_schema(new) == []
    removed_keys = [r.split(".")[-1] for r in removed]
    assert "doi" in removed_keys and "year" in removed_keys and "venue" in removed_keys
    # the entire display group (incl. non-forbidden authors_short) is dropped by migration
    assert "display" not in new
    # content preserved
    assert new["content_identity"]["content_title"] == "Keep"


def test_catalog_curator_skill_declares_content_only():
    text = (_REPO_ROOT / "skills" / "paper_raw_catalog_curator" / "SKILL.md").read_text(encoding="utf-8")
    tl = text.lower()
    assert "v2.0" in text
    assert "content" in tl
    assert "converted" in tl or "转换完成" in text or "转换后" in text
    assert "markdown" in tl or "md" in tl
    # must declare it does NOT carry bibliographic fields
    assert "禁止" in text or "forbidden" in tl or "不得" in text
    assert "doi" in tl and ("不负责" in text or "禁止" in text or "不得" in text)
    assert "bibtex" in tl and ("不生成" in text or "不得" in text)
    assert "metadata" in tl and "catalog" in tl and ("都" in text or "both" in tl)
    # must declare it does not produce metadata patch
    assert "不生成 metadata patch" in text or "不负责" in text
    assert "使用中文" in text or "默认使用中文" in text or "默认中文" in text
    assert "JSON key" in text and ("保持英文" in text or "stay English" in text)


def test_metadata_resolver_skill_declares_metadata_only():
    text = (_REPO_ROOT / "skills" / "paper_raw_metadata_resolver" / "SKILL.md").read_text(encoding="utf-8")
    tl = text.lower()
    assert "不负责 catalog" in text or "不生成 classification" in text or "不生成 catalog" in text
    assert "research_card" in text or "classification" in text  # declares it won't produce these
    assert "all.catalog" in text or "all.catalog" in tl


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
    """The bundled curator example must be a valid v2.0 content-only catalog."""
    path = _REPO_ROOT / "skills" / "paper_raw_catalog_curator" / "examples" / "example_catalog.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    assert catalog["schema_version"] == "2.0"
    assert validate_catalog_schema(catalog) == []
    assert find_forbidden_catalog_keys(catalog) == []
    for forbidden in ("doi", "authors", "year", "journal", "venue", "container", "publication", "bibtex"):
        assert forbidden not in json.dumps(catalog), f"example catalog leaked {forbidden}"
    for value in (
        catalog["screening"]["reason"],
        catalog["research_card"]["research_problem"],
        catalog["research_card"]["method_summary"],
        catalog["content_notes"]["short_summary"],
    ):
        assert _has_cjk(value)


def test_catalog_curation_prompt_declares_chinese_values_and_english_keys(tmp_path):
    folder = _build_formal_paper(tmp_path, pid="000001")
    prompt = PaperCurationService().build_prompt(folder)

    assert "请使用中文生成 catalog 中的自然语言内容" in prompt
    assert "JSON key" in prompt and "保持英文" in prompt
    assert "不要重复 DOI、作者、期刊、年份" in prompt


def test_pdf_resolver_simplified_metadata_is_rejected():
    """A simplified {title, doi, authors} object must NOT pass the formal metadata
    path — guards against any future resolver emitting a simplified旁路 format."""
    simplified = {
        "title": "A bulk blowing-snow model",
        "doi": "10.1023/A:100052170",
        "authors": "Déry and Yau",
    }
    # (a) schema-shape validator rejects it (missing nested title/authors/identifiers/...)
    assert validate_metadata_schema(simplified) != []

    # (b) commit completeness gate does not accept it. The simplified shape is
    #     malformed (title is a string, not an object), so the gate either raises
    #     or returns errors — either way it is NOT an empty accept.
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

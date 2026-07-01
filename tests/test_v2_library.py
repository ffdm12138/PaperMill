import json
from pathlib import Path

import pytest

from src.services.v2_library import (
    AllCatalogBuilder,
    PaperCurationService,
    PaperNumberLedger,
    PaperRawAllocator,
    PaperRawConverter,
    V2PaperCommitService,
    empty_catalog,
    empty_metadata,
    migrate_catalog_to_v2_0,
    validate_catalog_schema,
)
from src.services.paper_library import PaperLibrary
from scripts.validate_v2_library import validate_v2_library


def _curated_raw(root: Path, pid: str = "2024_wang_测试论文", *, no_commit: bool = False) -> Path:
    """Build a real paper_number paper_raw source + formalize it against a tmp ledger.

    Returns the formalized ``<paper_id>`` folder (ready_for_commit, with a real
    reserved ledger entry on root/catalog/paper_number_ledger.json). Formalize's
    duplicate gate runs against an empty ``root/formalize_work`` dir so it never
    sees a previously-committed paper (if tests need a committed first paper, pass
    ``no_commit=False`` and commit the first before building the second). Tests
    must commit via V2PaperCommitService(papers_dir=root/'papers', ...) so the
    ledger matches.
    """
    from tests.helpers.paper_raw_factory import make_staged_source, formalize_for_test

    parts = pid.split("_")
    year = parts[0] if parts[0].isdigit() else "2024"
    family = parts[1] if len(parts) > 1 else "wang"
    title_zh = "_".join(parts[2:]) or "测试论文"
    source_id = PaperNumberLedger(root / "catalog" / "paper_number_ledger.json").peek_next_numbers(1)[0]
    # If a previous _curated_raw already created+formalized this source_id at
    # this root, the folder was renamed away — but if not yet committed, the
    # <pid> folder may still exist. Clean it to allow a fresh make_staged_source.
    source = make_staged_source(
        root,
        source_id,
        title_zh=title_zh,
        title_original="Test Paper",
        doi="10.1/test",
        family=family,
        year=int(year),
    )
    # formalize's duplicate gate MUST run against an empty papers dir (not the
    # commit target) — otherwise a second _curated_raw in a dedup test would be
    # rejected by formalize before commit ever gets to quarantine it.
    formalize_papers = root / "formalize_work"
    formalized = formalize_for_test(
        root,
        source,
        papers_dir=formalize_papers,
        ledger_path=root / "catalog" / "paper_number_ledger.json",
        all_catalog_path=root / "catalog" / "all.catalog.json",
    )
    assert formalized.get("success"), formalized
    return Path(formalized["folder"])


def test_paper_raw_allocator_reserves_monotonic_paper_numbers(tmp_path):
    raw = tmp_path / "data" / "raw"
    raw.mkdir(parents=True)
    pdf = raw / "a.pdf"
    pdf.write_bytes(b"%PDF")
    paper_raw = tmp_path / "data" / "paper_raw"
    ledger = tmp_path / "data" / "catalog" / "paper_number_ledger.json"

    result = PaperRawAllocator(paper_raw, ledger_path=ledger).allocate_from_pdf(pdf)

    assert result["paper_number"] == "0000000000000001"
    assert result["paper_raw_id"] == "0000000000000001"
    assert (paper_raw / "0000000000000001" / "0000000000000001.pdf").exists()
    assert (paper_raw / "0000000000000001" / "0000000000000001.paper.number").exists()
    metadata = json.loads((paper_raw / "0000000000000001" / "0000000000000001.metadata.json").read_text(encoding="utf-8"))
    assert metadata["paper_number"] == "0000000000000001"
    assert metadata["pdf"]["sha256"]
    manifest = json.loads((paper_raw / "0000000000000001" / "stage_manifest.json").read_text(encoding="utf-8"))
    assert manifest["original_path"] == str(pdf)
    assert manifest["original_sha256"] == manifest["staged_sha256"]


def test_v2_commit_assigns_number_and_builds_all_catalog(tmp_path):
    raw_folder = _curated_raw(tmp_path)
    papers = tmp_path / "papers"
    all_catalog = tmp_path / "catalog" / "all.catalog.json"
    ledger = tmp_path / "catalog" / "paper_number_ledger.json"

    result = V2PaperCommitService(
        papers_dir=papers,
        all_catalog_path=all_catalog,
        ledger_path=ledger,
    ).commit_paper_raw(raw_folder)

    pid = "2024_wang_测试论文"
    assert result["status"] == "imported"
    assert result["paper_number"] == "0000000000000001"
    assert (papers / pid / f"{pid}.pdf").exists()
    assert (papers / pid / f"{pid}.md").exists()
    assert not (papers / pid / "stage_manifest.json").exists()
    assert (papers / pid / "0000000000000001.paper.number").exists()
    formal_catalog = json.loads((papers / pid / f"{pid}.catalog.json").read_text(encoding="utf-8"))
    assert formal_catalog["paper_id"] == pid
    assert formal_catalog["paper_number"] == "0000000000000001"
    assert formal_catalog["asset_refs"]["markdown"] == f"{pid}.md"
    assert formal_catalog["asset_refs"]["pdf"] == f"{pid}.pdf"
    assert formal_catalog["asset_refs"]["metadata"] == f"{pid}.metadata.json"
    assert formal_catalog["asset_refs"]["catalog"] == f"{pid}.catalog.json"
    assert formal_catalog["asset_refs"]["images_dir"] == "images/"
    for forbidden in ("doi", "authors", "year", "venue", "journal", "metadata"):
        assert forbidden not in formal_catalog
    data = json.loads(all_catalog.read_text(encoding="utf-8"))
    assert data["papers"][0]["paper_id"] == pid
    assert data["papers"][0]["paper_number"] == "0000000000000001"
    assert data["papers"][0]["asset_refs"]["markdown"].endswith(f"{pid}.md")
    assert not raw_folder.exists()


def test_v2_commit_removes_resolver_side_files(tmp_path):
    raw_folder = _curated_raw(tmp_path)
    (raw_folder / "000001.metadata.candidates.json").write_text("{}", encoding="utf-8")
    (raw_folder / "000001.metadata.resolve_report.json").write_text("{}", encoding="utf-8")
    (raw_folder / "000001.metadata.patch.json").write_text("{}", encoding="utf-8")

    result = V2PaperCommitService(
        papers_dir=tmp_path / "papers",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
    ).commit_paper_raw(raw_folder)

    paper_dir = tmp_path / "papers" / "2024_wang_测试论文"
    assert result["status"] == "imported"
    assert not list(paper_dir.glob("*.metadata.candidates.json"))
    assert not list(paper_dir.glob("*.metadata.resolve_report.json"))
    assert not list(paper_dir.glob("*.metadata.patch.json"))


def test_validate_v2_library_rejects_formal_resolver_side_files(tmp_path):
    raw_folder = _curated_raw(tmp_path)
    V2PaperCommitService(
        papers_dir=tmp_path / "papers",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
    ).commit_paper_raw(raw_folder)
    paper_dir = tmp_path / "papers" / "2024_wang_测试论文"
    (paper_dir / "000001.metadata.candidates.json").write_text("{}", encoding="utf-8")

    errors, _ = validate_v2_library(
        papers_dir=tmp_path / "papers",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        check_paths=False,
    )

    assert any("paper_raw transient file must not enter formal library" in err for err in errors)


def test_formal_commit_requires_chinese_short_title(tmp_path):
    raw_folder = _curated_raw(tmp_path)
    meta_path = raw_folder / "2024_wang_测试论文.metadata.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    metadata["title"]["short_zh"] = "english title"
    metadata["title"]["translated_zh"] = "english title"
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")

    result = V2PaperCommitService(
        papers_dir=tmp_path / "papers",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
    ).commit_paper_raw(raw_folder)

    assert result["success"] is False
    assert result["status"] == "metadata_incomplete"
    assert any("must contain Chinese" in err for err in result["errors"])


def test_commit_activates_preserved_paper_number_from_formalize(tmp_path):
    """preserve_paper_number is a formalize concern then commit activates it."""
    from tests.helpers.paper_raw_factory import make_staged_source, formalize_for_test

    source = make_staged_source(tmp_path, "0000000000000007", title_zh="测试论文", family="wang")
    formalized = formalize_for_test(
        tmp_path,
        source,
        preserve_paper_number="0000000000000007",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
    )
    assert formalized["success"], formalized
    assert formalized["paper_number"] == "0000000000000007"
    reserved = json.loads((tmp_path / "catalog" / "paper_number_ledger.json").read_text(encoding="utf-8"))
    assert reserved["items"]["0000000000000007"]["state"] == "reserved"
    assert reserved["items"]["0000000000000007"]["planned_paper_id"] == formalized["paper_id"]

    result = V2PaperCommitService(
        papers_dir=tmp_path / "papers",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
    ).commit_paper_raw(formalized["folder"])

    paper_dir = tmp_path / "papers" / formalized["paper_id"]
    ledger = json.loads((tmp_path / "catalog" / "paper_number_ledger.json").read_text(encoding="utf-8"))
    assert result["paper_number"] == "0000000000000007"
    assert (paper_dir / "0000000000000007.paper.number").exists()
    assert ledger["items"]["0000000000000007"]["folder_name"] == formalized["paper_id"]
    assert ledger["max_number"] == "0000000000000007"


def test_commit_postcheck_failure_rolls_back_final(tmp_path, monkeypatch):
    raw_folder = _curated_raw(tmp_path, "2024_wang_后置检查")
    papers = tmp_path / "papers"

    def _boom(self, *, write=True):
        raise RuntimeError("catalog rebuild exploded")

    monkeypatch.setattr(AllCatalogBuilder, "build", _boom)
    result = V2PaperCommitService(
        papers_dir=papers,
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
    ).commit_paper_raw(raw_folder)

    final = papers / "2024_wang_后置检查"
    assert result["status"] == "commit_failed"
    assert raw_folder.exists()  # paper_raw NOT deleted
    assert not final.exists()  # formal library NOT polluted (rollback)
    status = json.loads((raw_folder / ".import_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "commit_failed"


def test_all_catalog_rebuild_drops_deleted_folders_without_reusing_number(tmp_path):
    raw_folder = _curated_raw(tmp_path)
    papers = tmp_path / "papers"
    all_catalog = tmp_path / "catalog" / "all.catalog.json"
    ledger = tmp_path / "catalog" / "paper_number_ledger.json"
    svc = V2PaperCommitService(papers_dir=papers, all_catalog_path=all_catalog, ledger_path=ledger)
    svc.commit_paper_raw(raw_folder)
    for child in papers.iterdir():
        if child.is_dir():
            import shutil
            shutil.rmtree(child)

    rebuilt = AllCatalogBuilder(papers, all_catalog, PaperNumberLedger(ledger)).build(write=True)

    assert rebuilt["papers"] == []
    ledger_data = json.loads(ledger.read_text(encoding="utf-8"))
    assert ledger_data["max_number"] == "0000000000000001"


def test_paper_library_resolves_by_paper_number(tmp_path):
    raw_folder = _curated_raw(tmp_path)
    papers = tmp_path / "papers"
    all_catalog = tmp_path / "catalog" / "all.catalog.json"
    ledger = tmp_path / "catalog" / "paper_number_ledger.json"
    V2PaperCommitService(papers_dir=papers, all_catalog_path=all_catalog, ledger_path=ledger).commit_paper_raw(raw_folder)

    library = PaperLibrary(all_catalog_path=all_catalog, papers_dir=papers)
    result = library.resolve("0000000000000001")

    assert result["paper_id"] == "2024_wang_测试论文"
    assert library.markdown_path("0000000000000001").exists()


def test_v2_commit_quarantines_duplicate_doi(tmp_path):
    first = _curated_raw(tmp_path, "2024_wang_测试论文")
    papers = tmp_path / "papers"
    all_catalog = tmp_path / "catalog" / "all.catalog.json"
    ledger = tmp_path / "catalog" / "paper_number_ledger.json"
    svc = V2PaperCommitService(papers_dir=papers, all_catalog_path=all_catalog, ledger_path=ledger)
    svc.commit_paper_raw(first)
    second = _curated_raw(tmp_path, "2024_li_重复论文")
    # overwrite the second folder's DOI to match the first
    second_pid = second.name
    meta_path = second / f"{second_pid}.metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["identifiers"]["doi"] = "10.1/test"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    result = svc.commit_paper_raw(second)

    assert result["status"] == "possible_duplicate"
    assert Path(result["quarantine_dir"]).exists()
    assert not (papers / second_pid).exists()


def test_ledgers_reports_marker_conflict(tmp_path):
    folder = tmp_path / "papers" / "pid"
    folder.mkdir(parents=True)
    (folder / "0000000000000002.paper.number").write_text("{}", encoding="utf-8")
    ledger = PaperNumberLedger(tmp_path / "catalog" / "paper_number_ledger.json")
    ledger.save({
        "schema_version": "1.0",
        "max_number": "0000000000000001",
        "items": {
            "0000000000000001": {
                "folder_name": "pid",
                "folder_path": str(folder),
                "created_at": "",
            }
        },
    })

    errors, _ = ledger.validate(tmp_path / "papers")

    assert any("conflict" in err for err in errors)


def test_v2_commit_blocks_unmatched_metadata(tmp_path):
    raw_folder = _curated_raw(tmp_path, "2024_wang_未匹配论文")
    meta_path = raw_folder / "2024_wang_未匹配论文.metadata.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    metadata["metadata_match"]["status"] = "unmatched"
    meta_path.write_text(json.dumps(metadata), encoding="utf-8")

    result = V2PaperCommitService(
        papers_dir=tmp_path / "papers",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
    ).commit_paper_raw(raw_folder)

    assert result["status"] == "metadata_unmatched"
    assert not (tmp_path / "papers" / "2024_wang_未匹配论文").exists()
    assert (raw_folder / ".import_status.json").exists()


def test_pdf_metadata_without_doi_cannot_commit(tmp_path):
    raw_folder = _curated_raw(tmp_path, "2024_wang_无doi论文")
    meta_path = raw_folder / "2024_wang_无doi论文.metadata.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    metadata["identifiers"]["doi"] = ""
    meta_path.write_text(json.dumps(metadata), encoding="utf-8")
    papers = tmp_path / "papers"
    all_catalog = tmp_path / "catalog" / "all.catalog.json"
    ledger = tmp_path / "catalog" / "paper_number_ledger.json"

    result = V2PaperCommitService(
        papers_dir=papers,
        all_catalog_path=all_catalog,
        ledger_path=ledger,
    ).commit_paper_raw(raw_folder)

    assert result == {
        "success": False,
        "status": "metadata_incomplete",
        "errors": ["metadata.identifiers.doi is required for formal commit"],
    }
    assert not (papers / "2024_wang_无doi论文").exists()
    # ledger already exists from formalize; commit failure must not activate the
    # reserved entry (it stays reserved, and no new max_number is allocated).
    ledger_data = json.loads(ledger.read_text(encoding="utf-8"))
    assert ledger_data["max_number"] == "0000000000000001"
    assert not all_catalog.exists()
    assert (raw_folder / ".import_status.json").exists()


def test_commit_normalizes_doi_into_formal_metadata(tmp_path):
    raw_folder = _curated_raw(tmp_path, "2024_wang_DOI标准化")
    meta_path = raw_folder / "2024_wang_DOI标准化.metadata.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    metadata["identifiers"]["doi"] = "https://doi.org/10.1038/s41586-023-06185-3"
    meta_path.write_text(json.dumps(metadata), encoding="utf-8")
    papers = tmp_path / "papers"

    result = V2PaperCommitService(
        papers_dir=papers,
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
    ).commit_paper_raw(raw_folder)

    assert result["status"] == "imported"
    formal = json.loads(
        (papers / "2024_wang_DOI标准化" / "2024_wang_DOI标准化.metadata.json").read_text(encoding="utf-8")
    )
    assert formal["identifiers"]["doi"] == "10.1038/s41586-023-06185-3"


class _FakeRawConverter:
    def convert(self, input_path, output_dir, **kwargs):
        source_id = Path(input_path).stem
        out = Path(output_dir) / source_id / "hybrid_auto"
        out.mkdir(parents=True)
        (out / f"{source_id}.md").write_text("![x](./images/a.png)\n\ntext", encoding="utf-8")
        (out / "images").mkdir()
        (out / "images" / "a.png").write_bytes(b"png")
        return {"success": True, "output_dir": str(Path(output_dir) / source_id), "runner": "cli"}


def test_paper_raw_converter_guards_input_and_extracts_images(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    paper_number = "0000000000000001"
    src = paper_raw / paper_number
    src.mkdir(parents=True)
    (src / f"{paper_number}.pdf").write_bytes(b"%PDF")
    metadata = empty_metadata(paper_number)
    (src / f"{paper_number}.metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

    converter = PaperRawConverter(paper_raw_dir=paper_raw, converter=_FakeRawConverter())
    result = converter.convert(paper_number)

    assert result["success"]
    assert (src / f"{paper_number}.md").read_text(encoding="utf-8").startswith("![x](images/a.png)")
    assert (src / "images" / "a.png").exists()
    with pytest.raises(ValueError):
        converter.convert(tmp_path / "raw" / paper_number)


def test_curation_merges_only_empty_metadata_and_keeps_source_folder(tmp_path):
    paper_number = "0000000000000001"
    folder = tmp_path / "paper_raw" / paper_number
    folder.mkdir(parents=True)
    metadata = empty_metadata(paper_number)
    metadata["title"]["original"] = "Trusted Original"
    metadata["title"]["short_zh"] = "可信论文"
    metadata["year"] = 2024
    metadata["authors"] = [{"full_name": "Wang A", "family": "Wang", "given": "A", "orcid": "", "affiliation": ""}]
    metadata["container"]["journal"] = "Test Journal"
    metadata["identifiers"]["doi"] = "10.1/test"
    metadata["metadata_match"]["status"] = "matched"
    metadata["metadata_match"]["confidence"] = 1.0
    catalog = empty_catalog()
    catalog["content_identity"]["content_title"] = "可信论文"
    catalog["classification"]["primary_domain"] = "blowing_snow"
    catalog["screening"]["reason"] = "该文献与中文综述主题相关。"
    catalog["research_card"].update({
        "research_problem": "研究可信论文的入库流程。",
        "core_question": "如何验证 curated 文件只补空字段？",
        "hypothesis_or_objective": "验证 metadata 合并边界。",
        "study_object": "测试论文",
        "method_summary": "使用本地 mock 资产测试。",
        "data_or_experiment": "临时目录中的 PDF 与 Markdown。",
        "main_findings": ["非空 metadata 字段不会被覆盖。"],
        "mechanisms": ["通过 merge_missing_metadata 控制补空行为。"],
        "limitations": ["仅覆盖结构性流程。"],
        "usefulness_for_user": "用于保障入库流程稳定。",
    })
    catalog["content_notes"]["short_summary"] = "测试 curated 文件合并与重命名。"
    (folder / f"{paper_number}.metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (folder / f"{paper_number}.catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    (folder / f"{paper_number}.md").write_text("# Trusted", encoding="utf-8")
    (folder / f"{paper_number}.pdf").write_bytes(b"%PDF")
    (folder / "images").mkdir()
    patch = empty_metadata(paper_number)
    patch["title"]["original"] = "Overwrite Attempt"
    patch["abstract"] = "new abstract"
    patch_path = tmp_path / "patch.metadata.json"
    patch_path.write_text(json.dumps(patch), encoding="utf-8")

    result = PaperCurationService().apply_curated_files(folder, curated_metadata_path=patch_path)

    assert result["success"]
    # curate does NOT rename; folder stays at the paper_number workspace.
    assert result["status"] == "catalog_ready"
    assert folder.exists()
    assert (folder / f"{paper_number}.metadata.json").exists()
    assert (folder / f"{paper_number}.catalog.json").exists()
    assert not (tmp_path / "paper_raw" / "2024_Wang_可信论文").exists()
    merged = json.loads((folder / f"{paper_number}.metadata.json").read_text(encoding="utf-8"))
    assert merged["title"]["original"] == "Trusted Original"
    assert merged["abstract"] == "new abstract"
    status = json.loads((folder / ".import_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "catalog_ready"
    assert status["paper_id"] == "2024_Wang_可信论文"


def test_v2_commit_does_not_write_pdf_mirror(tmp_path):
    raw_folder = _curated_raw(tmp_path)
    papers = tmp_path / "papers"
    mirror_dir = tmp_path / "pdf_mirror"
    result = V2PaperCommitService(
        papers_dir=papers,
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
    ).commit_paper_raw(raw_folder)

    assert result["status"] == "imported"
    assert not mirror_dir.exists()


def test_empty_catalog_is_v2_0_with_content_groups():
    cat = empty_catalog()
    assert cat["schema_version"] == "2.0"
    assert "display" not in cat  # display removed in v2.0
    for key in ("content_identity", "classification", "screening", "research_card", "evidence_profile", "content_notes", "provenance", "asset_refs"):
        assert key in cat
    for key in ("read_decision", "relevance_score", "reason"):
        assert key in cat["screening"]
    for key in ("research_problem", "main_findings", "usefulness_for_user"):
        assert key in cat["research_card"]


def test_validate_catalog_schema_rejects_missing_v2_0_groups():
    cat = empty_catalog()
    del cat["evidence_profile"]
    del cat["screening"]
    errors = validate_catalog_schema(cat)
    assert any("evidence_profile" in e for e in errors)
    assert any("screening" in e for e in errors)


def test_validate_catalog_schema_accepts_v2_0():
    assert validate_catalog_schema(empty_catalog()) == []


def test_validate_catalog_schema_rejects_forbidden_metadata_keys():
    cat = empty_catalog()
    cat["doi"] = "10.1/x"  # forbidden at top level
    errors = validate_catalog_schema(cat)
    assert any("forbidden bibliographic key: doi" in e for e in errors)
    cat2 = empty_catalog()
    cat2["content_identity"]["identifiers"] = {"doi": "10.1/x"}  # forbidden nested
    errors2 = validate_catalog_schema(cat2)
    assert any("forbidden bibliographic key: content_identity.identifiers" in e for e in errors2)


def test_validate_catalog_schema_rejects_non_list_evidence_fields():
    cat = empty_catalog()
    cat["evidence_profile"]["important_figures"] = {"图1": "not a list"}

    errors = validate_catalog_schema(cat)

    assert any("catalog.evidence_profile.important_figures must be a list" in e for e in errors)


def test_migrate_catalog_to_v2_0_strips_forbidden_and_preserves_content():
    old = {
        "schema_version": "1.1",
        "display": {"title_original": "Keep", "title_zh": "", "short_name_zh": "", "year": 2020, "first_author": "X", "doi": "10.1/x"},
        "classification": {"primary_domain": "snow", "domains": ["snow"], "topics": ["drift"], "keywords_en": [], "keywords_zh": []},
        "research_card": {"one_sentence_summary_zh": "kept summary", "research_question_zh": "", "object_zh": "",
                          "method_zh": "", "data_or_experiment_zh": "", "key_variables": [], "main_conclusion_zh": "",
                          "usefulness_for_project_zh": "", "recommended_use_cases_zh": []},
    }
    migrated, removed = migrate_catalog_to_v2_0(old)
    assert migrated["schema_version"] == "2.0"
    assert "display" not in migrated
    assert migrated["content_identity"]["content_title"] == "Keep"
    assert migrated["content_notes"]["short_summary"] == "kept summary"
    assert validate_catalog_schema(migrated) == []
    assert any("doi" in r for r in removed)
    assert any("year" in r for r in removed)


def test_paper_id_folds_accented_author_family_to_ascii():
    """Accented family names (Déry, Müller) must produce an ASCII-safe paper_id, not crash."""
    from src.services.v2_library import first_author_family, paper_id_from_metadata_catalog
    from src.naming import validate_paper_id
    m = empty_metadata("000001")
    m["year"] = 1999
    m["title"]["short_zh"] = "体相吹雪模型"
    m["authors"] = [{"full_name": "Stephen J. Déry", "family": "Déry", "given": "Stephen J.", "orcid": "", "affiliation": ""}]
    c = empty_catalog()
    assert first_author_family(m) == "Dery"
    pid = paper_id_from_metadata_catalog(m, c)
    validate_paper_id(pid)  # must not raise
    assert pid == "1999_Dery_体相吹雪模型"


def test_accented_author_apply_curated_files_does_not_rename(tmp_path):
    """apply_curated_files must produce the correct ASCII paper_id but must NOT rename (formalize does that)."""
    paper_number = "0000000000000001"
    folder = tmp_path / "paper_raw" / paper_number
    folder.mkdir(parents=True)
    metadata = empty_metadata(paper_number)
    metadata["title"]["original"] = "A Bulk Blowing-Snow Model"
    metadata["title"]["short_zh"] = "体相吹雪模型"
    metadata["year"] = 1999
    metadata["authors"] = [{"full_name": "Stephen J. Déry", "family": "Déry", "given": "Stephen J.", "orcid": "", "affiliation": ""}]
    metadata["container"]["journal"] = "Test Journal"
    metadata["identifiers"]["doi"] = "10.1/dery"
    metadata["metadata_match"]["status"] = "matched"
    metadata["metadata_match"]["confidence"] = 1.0
    catalog = empty_catalog()
    catalog["content_identity"]["content_title"] = "体相吹雪模型"
    catalog["classification"]["primary_domain"] = "blowing_snow"
    catalog["screening"]["reason"] = "该文献与风吹雪中文综述主题相关。"
    catalog["research_card"].update({
        "research_problem": "研究体相风吹雪模型。",
        "core_question": "如何模拟风吹雪过程？",
        "hypothesis_or_objective": "验证重命名支持重音作者姓氏。",
        "study_object": "风吹雪模型",
        "method_summary": "使用 mock catalog 测试 curation。",
        "data_or_experiment": "临时测试资产。",
        "main_findings": ["作者姓氏可折叠为 ASCII。"],
        "mechanisms": ["命名时只折叠作者姓氏。"],
        "limitations": ["仅测试命名边界。"],
        "usefulness_for_user": "保障中文 paper_id 生成。",
    })
    catalog["content_notes"]["short_summary"] = "体相风吹雪模型命名测试。"
    (folder / f"{paper_number}.metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (folder / f"{paper_number}.catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    (folder / f"{paper_number}.md").write_text("# test", encoding="utf-8")
    (folder / f"{paper_number}.pdf").write_bytes(b"%PDF")
    (folder / "images").mkdir()
    result = PaperCurationService().apply_curated_files(folder, curated_catalog_path=folder / f"{paper_number}.catalog.json")
    assert result["success"], f"apply_curated_files failed: {result.get('errors', [])}"
    assert result["paper_id"] == "1999_Dery_体相吹雪模型"
    # curate does NOT rename; folder stays at the paper_number workspace.
    assert folder.exists()
    assert not (tmp_path / "paper_raw" / "1999_Dery_体相吹雪模型").exists()
    status = json.loads((folder / ".import_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "catalog_ready"


def test_apply_rejects_catalog_missing_screening_group(tmp_path):
    """apply_curated_files must reject a curator catalog missing the critical screening group."""
    folder = tmp_path / "paper_raw" / "000001"
    folder.mkdir(parents=True)
    metadata = empty_metadata("000001")
    metadata["title"]["original"] = "T"
    metadata["year"] = 2024
    metadata["authors"] = [{"full_name": "Wang A", "family": "Wang", "given": "A", "orcid": "", "affiliation": ""}]
    metadata["container"]["journal"] = "Test Journal"
    metadata["identifiers"]["doi"] = "10.1/test"
    metadata["metadata_match"]["status"] = "matched"
    metadata["metadata_match"]["confidence"] = 1.0
    (folder / "000001.metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    catalog = empty_catalog()
    del catalog["screening"]
    catalog_path = folder / "000001.catalog.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    (folder / "000001.md").write_text("# T", encoding="utf-8")
    (folder / "000001.pdf").write_bytes(b"%PDF")
    (folder / "images").mkdir()
    result = PaperCurationService().apply_curated_files(folder, curated_catalog_path=catalog_path)
    assert not result["success"]
    assert any("screening" in e for e in result["errors"])
    assert (folder / ".import_status.json").exists(), ".import_status.json must be written on failure"
    assert folder.exists(), "folder must NOT be renamed on failure"


# --- PaperNumberLedger reserve/activate/deactivate state machine -------------

def _ledger(tmp_path: Path) -> PaperNumberLedger:
    return PaperNumberLedger(tmp_path / "catalog" / "paper_number_ledger.json")


def test_ledger_reserve_writes_marker_and_reserved_state(tmp_path: Path):
    ledger = _ledger(tmp_path)
    folder = tmp_path / "paper_raw" / "000001"
    folder.mkdir(parents=True)

    number = ledger.reserve_for_paper_raw(folder, planned_paper_id="2024_Wang_可信论文")

    assert number == "0000000000000001"
    assert (folder / "0000000000000001.paper.number").exists()
    data = ledger.load()
    assert data["max_number"] == "0000000000000001"
    item = data["items"][number]
    assert item["state"] == "reserved"
    assert item["planned_paper_id"] == "2024_Wang_可信论文"
    assert item["folder_name"] == "000001"
    assert ledger.paper_number_from_marker(folder) == number


def test_ledger_reserve_is_idempotent(tmp_path: Path):
    ledger = _ledger(tmp_path)
    folder = tmp_path / "paper_raw" / "000001"
    folder.mkdir(parents=True)
    first = ledger.reserve_for_paper_raw(folder)
    second = ledger.reserve_for_paper_raw(folder)
    assert first == second == "0000000000000001"
    data = ledger.load()
    assert len(data["items"]) == 1


def test_ledger_activate_reserved_flips_state_and_repoints(tmp_path: Path):
    ledger = _ledger(tmp_path)
    src = tmp_path / "paper_raw" / "000001"
    src.mkdir(parents=True)
    number = ledger.reserve_for_paper_raw(src, planned_paper_id="2024_Wang_可信论文")

    final = tmp_path / "papers" / "2024_Wang_可信论文"
    final.mkdir(parents=True)
    # copytree would bring the marker; simulate that here
    (final / f"{number}.paper.number").write_text("{}", encoding="utf-8")

    ledger.activate_reserved(number, final, paper_id="2024_Wang_可信论文")

    data = ledger.load()
    item = data["items"][number]
    assert item["state"] == "active"
    assert item["folder_name"] == "2024_Wang_可信论文"
    assert item["activated_at"]
    marker = json.loads((final / f"{number}.paper.number").read_text(encoding="utf-8"))
    assert marker["state"] == "active"


def test_ledger_activate_rejects_marker_only_folder(tmp_path: Path):
    ledger = _ledger(tmp_path)
    final = tmp_path / "papers" / "2024_Wang_x"
    final.mkdir(parents=True)
    (final / "0000000000000009.paper.number").write_text("{}", encoding="utf-8")

    with pytest.raises(KeyError, match="paper_number not in ledger"):
        ledger.activate_reserved("0000000000000009", final, paper_id="2024_Wang_x")

    assert "0000000000000009" not in ledger.load()["items"]


def test_ledger_deactivate_to_source_rolls_back(tmp_path: Path):
    ledger = _ledger(tmp_path)
    src = tmp_path / "paper_raw" / "000001"
    src.mkdir(parents=True)
    number = ledger.reserve_for_paper_raw(src)
    final = tmp_path / "papers" / "2024_Wang_x"
    final.mkdir(parents=True)
    (final / f"{number}.paper.number").write_text("{}", encoding="utf-8")
    ledger.activate_reserved(number, final)

    ledger.deactivate_to_source(number, src)

    item = ledger.load()["items"][number]
    assert item["state"] == "reserved"
    assert item["folder_name"] == "000001"
    assert item["deactivated_at"]


def test_ledger_validate_warns_on_reserved_orphan(tmp_path: Path):
    ledger = _ledger(tmp_path)
    # reserved number whose folder has been deleted (orphan)
    data = ledger.empty_data()
    data["max_number"] = "0000000000000001"
    data["items"]["0000000000000001"] = {
        "folder_name": "000001",
        "folder_path": str(tmp_path / "paper_raw" / "gone"),
        "state": "reserved",
        "created_at": "2026-01-01T00:00:00",
    }
    ledger.save(data)

    errors, warnings = ledger.validate(papers_dir=tmp_path / "papers")
    assert not errors
    assert any("ledger folder missing" in w for w in warnings)


def test_ledger_load_backfills_state_for_legacy_entries(tmp_path: Path):
    ledger = _ledger(tmp_path)
    data = ledger.empty_data()
    data["items"]["0000000000000001"] = {
        "folder_name": "2024_Wang_x",
        "folder_path": str(tmp_path / "papers" / "2024_Wang_x"),
        "created_at": "2026-01-01T00:00:00",
    }
    ledger.save(data)
    assert ledger.load()["items"]["0000000000000001"]["state"] == "active"

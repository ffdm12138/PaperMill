"""Shared paper_raw ingest test fixtures.

``make_staged_source`` builds a real 16-digit ``data/paper_raw/<paper_number>/`` workspace
with a genuine conversion manifest (computed sha256/file_size, current MINERU_*
settings), so formalize's conversion gate is exercised for real. Happy-path
tests must start from a staged paper_number folder and run formalize — not hand-write
formalization.json / .paper.number / ready_for_commit.

``formalize_for_test`` / ``commit_for_test`` are thin wrappers that wire a
PaperRawFormalizationService / V2PaperCommitService against tmp paths so tests
never touch the real ``data/catalog``.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.services.ingest_state import CATALOG_READY, METADATA_RESOLVE_FAILED, write_import_status
from src.services.ingest_ids import PAPER_NUMBER_RE
from src.services.v2_library import (
    PaperNumberLedger,
    V2PaperCommitService,
    empty_catalog,
    empty_metadata,
    write_conversion_manifest_for_existing_assets,
)


def make_staged_source(
    root: Path,
    source_id: str = "0000000000000001",
    *,
    title_zh: str = "可信论文",
    title_original: str = "Trusted Original",
    doi: str = "10.1/test",
    family: str = "Wang",
    given: str = "A",
    year: int = 2024,
    journal: str = "Test Journal",
    pdf_bytes: bytes = b"%PDF",
    md_text: str | None = None,
    metadata_status: str = "matched",
    catalog_ready: bool = True,
    catalog_domain: str = "blowing_snow",
) -> Path:
    folder = root / "paper_raw" / source_id
    folder.mkdir(parents=True)
    if PAPER_NUMBER_RE.match(source_id):
        PaperNumberLedger(root / "catalog" / "paper_number_ledger.json").reserve_specific_for_paper_raw(source_id, folder)
    metadata = empty_metadata(source_id)
    metadata["title"]["original"] = title_original
    metadata["title"]["translated_zh"] = title_zh
    metadata["title"]["short_zh"] = title_zh
    metadata["year"] = year
    metadata["authors"] = [{"full_name": f"{family} {given}", "family": family, "given": given, "orcid": "", "affiliation": ""}]
    metadata["container"]["journal"] = journal
    metadata["identifiers"]["doi"] = doi
    metadata["metadata_match"] = {
        "status": metadata_status,
        "source": "test",
        "confidence": 1.0,
        "matched_at": "2026-01-01T00:00:00",
        "warnings": [],
        "candidates": [],
    }
    catalog = empty_catalog()
    catalog["content_identity"]["content_title"] = title_zh
    catalog["classification"]["primary_domain"] = catalog_domain
    catalog["screening"]["read_decision"] = "pending"
    catalog["screening"]["relevance_score"] = 5
    catalog["screening"]["reason"] = f"该文献与{title_zh}主题相关。"
    catalog["research_card"].update({
        "research_problem": f"研究{title_zh}。",
        "core_question": "测试核心问题",
        "hypothesis_or_objective": "测试目标",
        "study_object": "测试对象",
        "method_summary": "测试方法摘要。",
        "data_or_experiment": "测试数据。",
        "main_findings": ["测试发现"],
        "mechanisms": ["测试机制"],
        "limitations": ["测试局限"],
        "usefulness_for_user": "测试用途",
    })
    catalog["content_notes"]["short_summary"] = f"{title_zh}测试摘要。"
    (folder / f"{source_id}.metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    (folder / f"{source_id}.catalog.json").write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    (folder / f"{source_id}.md").write_text(md_text or f"# {title_zh}\nbody", encoding="utf-8")
    (folder / f"{source_id}.pdf").write_bytes(pdf_bytes)
    (folder / "images").mkdir()
    write_conversion_manifest_for_existing_assets(folder, source_id)
    if catalog_ready:
        write_import_status(folder, CATALOG_READY, reason="curated")
    return folder


def make_legacy_workspace(
    root: Path,
    folder_name: str = "1979_sykest_untitled",
    *,
    paper_number: str = "0000000000000157",
    title_zh: str = "遗留论文",
    title_original: str = "Legacy Original",
    doi: str = "10.1/legacy",
    family: str = "Sykest",
    given: str = "A",
    year: int = 1979,
    journal: str = "Legacy Journal",
    pdf_bytes: bytes = b"%PDF",
    md_text: str | None = None,
    catalog_ready: bool = False,
    import_status: str = METADATA_RESOLVE_FAILED,
    marker: bool = True,
) -> Path:
    """Build a legacy/untitled paper_raw workspace mirroring on-disk reality.

    The folder is named by ``paper_id`` (NOT 16-digit), yet carries a
    ``*.paper.number`` marker and metadata ``paper_number``/``paper_raw_id``
    tying it to the numbered system. Unlike ``make_staged_source``, NO ledger
    entry is reserved for this marker paper_number (legacy folders were not
    reserved at staging time). The import_status defaults to
    ``metadata_resolve_failed`` — the most common state of real legacy folders,
    which outranks a freshly-restaged duplicate's ``ready_for_convert``.
    """
    folder = root / "paper_raw" / folder_name
    folder.mkdir(parents=True)
    metadata = empty_metadata(folder_name)
    # legacy folders resolve to a real 16-digit marker number regardless of name
    metadata["paper_number"] = paper_number
    metadata["paper_raw_id"] = paper_number
    metadata["title"]["original"] = title_original
    metadata["title"]["translated_zh"] = title_zh
    metadata["title"]["short_zh"] = title_zh
    metadata["year"] = year
    metadata["authors"] = [{"full_name": f"{family} {given}", "family": family, "given": given, "orcid": "", "affiliation": ""}]
    metadata["container"]["journal"] = journal
    metadata["identifiers"]["doi"] = doi
    catalog = empty_catalog()
    catalog["content_identity"]["content_title"] = title_zh
    catalog["screening"]["read_decision"] = "pending"
    (folder / f"{folder_name}.metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    (folder / f"{folder_name}.catalog.json").write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    (folder / f"{folder_name}.md").write_text(md_text or f"# {title_zh}\nbody", encoding="utf-8")
    (folder / f"{folder_name}.pdf").write_bytes(pdf_bytes)
    (folder / "images").mkdir()
    (folder / "stage_manifest.json").write_text("{}", encoding="utf-8")
    if marker:
        (folder / f"{paper_number}.paper.number").write_text(paper_number, encoding="utf-8")
    if catalog_ready:
        write_import_status(folder, CATALOG_READY, reason="curated", extra={"source_id": paper_number})
    else:
        write_import_status(folder, import_status, reason="legacy", extra={"source_id": paper_number})
    return folder


def formalize_for_test(
    tmp_path: Path,
    folder: Path,
    *,
    paper_raw_dir: Path | None = None,
    papers_dir: Path | None = None,
    ledger_path: Path | None = None,
    all_catalog_path: Path | None = None,
    **kw,
) -> dict:
    from src.services.paper_raw_formalizer import PaperRawFormalizationService

    svc = PaperRawFormalizationService(
        paper_raw_dir=paper_raw_dir or folder.parent,
        papers_dir=papers_dir or (tmp_path / "papers"),
        ledger_path=ledger_path or (tmp_path / "catalog" / "paper_number_ledger.json"),
        all_catalog_path=all_catalog_path or (tmp_path / "catalog" / "all.catalog.json"),
    )
    return svc.formalize(folder, **kw)


def commit_for_test(
    tmp_path: Path,
    folder: Path,
    *,
    papers_dir: Path | None = None,
    ledger_path: Path | None = None,
    all_catalog_path: Path | None = None,
) -> dict:
    svc = V2PaperCommitService(
        papers_dir=papers_dir or (tmp_path / "papers"),
        all_catalog_path=all_catalog_path or (tmp_path / "catalog" / "all.catalog.json"),
        ledger_path=ledger_path or (tmp_path / "catalog" / "paper_number_ledger.json"),
    )
    return svc.commit_paper_raw(folder)


def default_paths(tmp_path: Path) -> dict:
    return {
        "papers_dir": tmp_path / "papers",
        "ledger_path": tmp_path / "catalog" / "paper_number_ledger.json",
        "all_catalog_path": tmp_path / "catalog" / "all.catalog.json",
    }

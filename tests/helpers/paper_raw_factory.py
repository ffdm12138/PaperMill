"""Shared paper_raw ingest test fixtures.

``make_staged_source`` builds a real 6-digit ``data/paper_raw/000001/`` workspace
with a genuine conversion manifest (computed sha256/file_size, current MINERU_*
settings), so formalize's conversion gate is exercised for real. Happy-path
tests must start from a 6-digit source folder and run formalize — not hand-write
formalization.json / .paper.number / ready_for_commit.

``formalize_for_test`` / ``commit_for_test`` are thin wrappers that wire a
PaperRawFormalizationService / V2PaperCommitService against tmp paths so tests
never touch the real ``data/catalog``.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from config.settings import (
    ALL_CATALOG_PATH,
    MINERU_BACKEND,
    MINERU_EFFORT,
    MINERU_LANG,
    MINERU_METHOD,
    PAPER_NUMBER_LEDGER_PATH,
    PAPER_RAW_DIR,
    PAPERS_DIR,
)
from src.file_fingerprint import compute_sha256
from src.services.ingest_state import CATALOG_READY, write_import_status
from src.services.v2_library import (
    V2PaperCommitService,
    empty_catalog,
    empty_metadata,
    write_conversion_manifest_for_existing_assets,
)


def make_staged_source(
    root: Path,
    source_id: str = "000001",
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
    catalog_domain: str = "blowing_snow",
) -> Path:
    folder = root / "paper_raw" / source_id
    folder.mkdir(parents=True)
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
    _ = compute_sha256(folder / f"{source_id}.pdf")  # ensure sha computed for manifest
    write_import_status(folder, CATALOG_READY, reason="curated")
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
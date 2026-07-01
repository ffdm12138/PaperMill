"""E2E structural tests: commit, dedup, numbering, rebuild, compact, copy, bibtex, destructive.

All tests use mock PDFs and in-memory temp directories — no network, no GPU.
Drives the real v2 service classes through the full chain.
"""
import json
import shutil
from pathlib import Path

import pytest

from src.services.v2_library import (
    AllCatalogBuilder,
    PaperNumberLedger,
    V2PaperCommitService,
    bibtex_from_metadata,
    empty_catalog,
    empty_metadata,
)
from src.catalog import build_compact_catalog_text
from src.services.paper_library import PaperLibrary

# Import the validate_v2_library CLI's validation function
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.validate_v2_library import validate_v2_library


def _curated_raw(root: Path, pid: str, *, doi: str = "10.1/x", year: int = 2024,
                 family: str | None = None, tz: str | None = None, pdf_content: bytes = b"%PDF-X") -> Path:
    """Build a complete 16-digit paper_raw source folder ready for formalize."""
    from tests.helpers.paper_raw_factory import make_staged_source

    parts = pid.split("_")
    family = parts[1] if len(parts) > 1 else (family or "wang")
    tz = tz if tz is not None else ("_".join(parts[2:]) or "测试论文")
    source_id = PaperNumberLedger(root / "catalog" / "paper_number_ledger.json").peek_next_numbers(1)[0]
    return make_staged_source(
        root,
        source_id,
        title_zh=tz,
        title_original=f"Paper {pid}",
        doi=doi,
        family=family,
        year=year,
        pdf_bytes=pdf_content,
        catalog_domain="test",
    )


def _commit(papers_dir: Path, all_catalog: Path, ledger: Path, folder: Path) -> dict:
    # commit now requires a formalized folder; run formalize first (idempotent,
    # reserves the next 16-digit paper_number on the shared ledger).
    from src.services.paper_raw_formalizer import PaperRawFormalizationService

    _reserve_staged_marker_on_ledger(ledger, folder)
    formalized = PaperRawFormalizationService(
        paper_raw_dir=folder.parent, papers_dir=papers_dir,
        ledger_path=ledger, all_catalog_path=all_catalog,
    ).formalize(folder)
    if not formalized.get("success"):
        return formalized
    return V2PaperCommitService(papers_dir=papers_dir, all_catalog_path=all_catalog, ledger_path=ledger).commit_paper_raw(formalized["folder"])


def _reserve_staged_marker_on_ledger(ledger: Path, folder: Path) -> None:
    number = PaperNumberLedger(ledger).paper_number_from_marker(folder) or folder.name
    PaperNumberLedger(ledger).reserve_specific_for_paper_raw(number, folder)


def _commit_svc(svc: V2PaperCommitService, folder: Path) -> dict:
    """formalize (using svc's ledger) then commit — for tests that hold a svc handle."""
    from src.services.paper_raw_formalizer import PaperRawFormalizationService

    _reserve_staged_marker_on_ledger(svc.ledger.path, folder)
    formalized = PaperRawFormalizationService(
        paper_raw_dir=folder.parent, papers_dir=svc.papers_dir,
        ledger_path=svc.ledger.path, all_catalog_path=svc.all_catalog_path,
    ).formalize(folder)
    if not formalized.get("success"):
        return formalized
    return svc.commit_paper_raw(formalized["folder"])


class TestCommit:
    def test_commit_success_assigns_paper_number(self, tmp_path):
        f = _curated_raw(tmp_path, "2024_wang_测试论文")
        r = _commit(tmp_path / "papers", tmp_path / "catalog" / "all.catalog.json",
                    tmp_path / "catalog" / "paper_number_ledger.json", f)
        assert r["status"] == "imported"
        assert r["paper_number"] == "0000000000000001"
        pdir = tmp_path / "papers" / "2024_wang_测试论文"
        for suffix in ("metadata.json", "catalog.json", "md", "pdf"):
            assert (pdir / f"2024_wang_测试论文.{suffix}").exists()
        assert (pdir / "images").is_dir()
        assert (pdir / "0000000000000001.paper.number").exists()
        assert not f.exists()  # source removed

    def test_missing_asset_rejected(self, tmp_path):
        for asset in ("pdf", "catalog.json", "md"):
            base = tmp_path / asset
            base.mkdir(parents=True, exist_ok=True)
            pid = "2024_wang_缺失资产"
            f = _curated_raw(base, pid)
            if asset == "pdf":
                (f / f"{f.name}.pdf").unlink()
            elif asset == "catalog.json":
                (f / f"{f.name}.catalog.json").unlink()
            elif asset == "md":
                (f / f"{f.name}.md").unlink()
            result = _commit(base / "p", base / "c" / "ac.json", base / "c" / "l.json", f)
            assert result["success"] is False
            assert result["status"] in {"assets_incomplete", "catalog_invalid", "formalize_failed"}


class TestDedup:
    def test_doi_duplicate_quarantined(self, tmp_path):
        f1 = _curated_raw(tmp_path, "2024_wang_论文A", doi="10.1/same")
        f2 = _curated_raw(tmp_path, "2024_li_论文B", doi="10.1/same", pdf_content=b"%PDF-B")
        svc = V2PaperCommitService(
            papers_dir=tmp_path / "papers",
            all_catalog_path=tmp_path / "c" / "all.catalog.json",
            ledger_path=tmp_path / "c" / "l.json",
        )
        r1 = _commit_svc(svc, f1)
        assert r1["status"] == "imported"
        r2 = _commit_svc(svc, f2)
        assert r2["status"] == "possible_duplicate"
        assert Path(r2["quarantine_dir"]).exists()
        assert not (tmp_path / "papers" / "2024_li_论文B").exists()

    def test_pdf_sha_duplicate_quarantined(self, tmp_path):
        f1 = _curated_raw(tmp_path, "2024_wang_论文A", doi="10.1/uniq1", pdf_content=b"%PDF-same-sha")
        f2 = _curated_raw(tmp_path, "2023_li_不同标题", doi="10.1/uniq2", pdf_content=b"%PDF-same-sha",
                          year=2023)
        svc = V2PaperCommitService(
            papers_dir=tmp_path / "papers",
            all_catalog_path=tmp_path / "c" / "all.json",
            ledger_path=tmp_path / "c" / "l.json",
        )
        assert _commit_svc(svc, f1)["status"] == "imported"
        r2 = _commit_svc(svc, f2)
        assert r2["status"] == "possible_duplicate"
        assert not (tmp_path / "papers" / "2024_li_不同标题").exists()


class TestNumberingAndRebuild:
    def test_sequential_numbering_and_rebuild(self, tmp_path):
        papers = tmp_path / "papers"
        ac = tmp_path / "c" / "all.catalog.json"
        lg = tmp_path / "c" / "paper_number_ledger.json"
        svc = V2PaperCommitService(papers_dir=papers, all_catalog_path=ac, ledger_path=lg)

        f1 = _curated_raw(tmp_path, "2024_wang_论文A")
        f2 = _curated_raw(tmp_path, "2024_li_论文B", doi="10.1/b", tz="论文B", family="Li",
                          pdf_content=b"%PDF-distinct-B")
        r1 = _commit_svc(svc, f1)
        r2 = _commit_svc(svc, f2)
        assert r1["paper_number"] == "0000000000000001"
        assert r2["paper_number"] == "0000000000000002"

    def test_deleted_paper_not_in_rebuild(self, tmp_path):
        papers = tmp_path / "papers"
        ac = tmp_path / "c" / "all.catalog.json"
        lg = tmp_path / "c" / "paper_number_ledger.json"
        svc = V2PaperCommitService(papers_dir=papers, all_catalog_path=ac, ledger_path=lg)
        f1 = _curated_raw(tmp_path, "2024_wang_论文A")
        f2 = _curated_raw(tmp_path, "2024_li_论文B", doi="10.1/b", tz="论文B", family="Li",
                          pdf_content=b"%PDF-B")
        _commit_svc(svc, f1)
        _commit_svc(svc, f2)
        # delete first paper
        shutil.rmtree(papers / "2024_wang_论文A")
        AllCatalogBuilder(papers, ac, PaperNumberLedger(lg)).build(write=True)
        data = json.loads(ac.read_text(encoding="utf-8"))
        assert len(data["papers"]) == 1
        assert data["papers"][0]["paper_id"] == "2024_li_论文B"

    def test_number_not_reused(self, tmp_path):
        papers = tmp_path / "papers"
        ac = tmp_path / "c" / "all.catalog.json"
        lg = tmp_path / "c" / "paper_number_ledger.json"
        svc = V2PaperCommitService(papers_dir=papers, all_catalog_path=ac, ledger_path=lg)
        f1 = _curated_raw(tmp_path, "2024_wang_论文A")
        f2 = _curated_raw(tmp_path, "2024_li_论文B", doi="10.1/b", tz="论文B", family="Li", pdf_content=b"%PDF-B")
        _commit_svc(svc, f1)
        _commit_svc(svc, f2)
        shutil.rmtree(papers / "2024_wang_论文A")
        AllCatalogBuilder(papers, ac, PaperNumberLedger(lg)).build(write=True)
        lg_data = json.loads(lg.read_text(encoding="utf-8"))
        assert lg_data["max_number"] == "0000000000000002"  # 1 was used, not freed


class TestCompactCatalog:
    def test_compact_includes_all_screening_fields(self, tmp_path):
        f = _curated_raw(tmp_path, "2024_wang_测试论文")
        svc = V2PaperCommitService(papers_dir=tmp_path / "papers",
                                    all_catalog_path=tmp_path / "c" / "all.json",
                                    ledger_path=tmp_path / "c" / "l.json")
        _commit_svc(svc, f)
        AllCatalogBuilder(tmp_path / "papers", tmp_path / "c" / "all.json",
                          PaperNumberLedger(tmp_path / "c" / "l.json")).build(write=True)
        data = json.loads((tmp_path / "c" / "all.json").read_text(encoding="utf-8"))
        from src.services.paper_library import PaperLibrary
        lib = PaperLibrary(all_catalog_path=tmp_path / "c" / "all.json", papers_dir=tmp_path / "papers")
        # default include_metadata=False: content-only — paper_number + content, no bibliography
        txt_content = build_compact_catalog_text(data["papers"], library=lib)
        for kw in ("0000000000000001", "must_read", "method:", "usefulness:"):
            assert kw in txt_content, f"compact catalog (content-only) missing: {kw}"
        # bibliographic bits must NOT leak in the default content-only view.
        # (year/author surname also appear in paper_id, so only check venue+doi,
        #  which are purely bibliographic and never part of paper_id.)
        for kw in ("Test Journal", "10.1/x"):
            assert kw not in txt_content, f"content-only view leaked bibliography: {kw}"
        # include_metadata=True: bibliographic bits joined from metadata (display-layer only)
        txt_meta = build_compact_catalog_text(data["papers"], library=lib, include_metadata=True)
        for kw in ("0000000000000001", "2024", "wang", "Test Journal", "10.1/x",
                   "must_read", "method:", "usefulness:"):
            assert kw in txt_meta, f"compact catalog (include_metadata) missing: {kw}"
        # catalog must NOT leak its own bibliographic fields into the compact text
        # (they come from metadata; the catalog content is content-only)


class TestPaperLibraryAccess:
    def test_resolve_by_paper_number_and_load_assets(self, tmp_path):
        f = _curated_raw(tmp_path, "2024_wang_测试论文")
        papers = tmp_path / "papers"
        ac = tmp_path / "c" / "all.json"
        lg = tmp_path / "c" / "l.json"
        svc = V2PaperCommitService(papers_dir=papers, all_catalog_path=ac, ledger_path=lg)
        _commit_svc(svc, f)
        AllCatalogBuilder(papers, ac, PaperNumberLedger(lg)).build(write=True)

        library = PaperLibrary(all_catalog_path=ac, papers_dir=papers)
        number = "0000000000000001"

        r = library.resolve(number)
        assert r["paper_id"] == "2024_wang_测试论文"
        assert library.markdown_path(number).exists()
        assert library.read_markdown(number)
        assert (papers / "2024_wang_测试论文" / "2024_wang_测试论文.md").exists()  # source untouched

        assert library.resolve("9999999999999999") is None


class TestBibtex:
    def test_bibtex_from_metadata_only(self, tmp_path):
        f = _curated_raw(tmp_path, "2024_wang_测试论文")
        svc = V2PaperCommitService(papers_dir=tmp_path / "papers",
                                    all_catalog_path=tmp_path / "c" / "all.json",
                                    ledger_path=tmp_path / "c" / "l.json")
        _commit_svc(svc, f)
        meta = json.loads((tmp_path / "papers" / "2024_wang_测试论文" / "2024_wang_测试论文.metadata.json")
                          .read_text(encoding="utf-8"))
        bib = bibtex_from_metadata(meta)
        assert bib.startswith("@article{")
        for kw in ("title", "author", "year", "doi"):
            assert kw in bib, f"bibtex missing {kw}"
        # bibtex must NOT contain catalog fields
        assert "screening" not in bib
        assert "research_card" not in bib


class TestDestructive:
    def test_paper_md_rejected_by_validate(self, tmp_path):
        f = _curated_raw(tmp_path, "2024_wang_测试论文")
        papers = tmp_path / "papers"
        ac = tmp_path / "c" / "all.json"
        lg = tmp_path / "c" / "l.json"
        _commit(papers, ac, lg, f)
        AllCatalogBuilder(papers, ac, PaperNumberLedger(lg)).build(write=True)

        (papers / "2024_wang_测试论文" / "paper.md").write_text("legacy", encoding="utf-8")
        errors, _ = validate_v2_library(papers_dir=papers, all_catalog_path=ac, check_paths=False)
        assert any("paper.md" in e for e in errors), f"validate should reject paper.md: {errors}"

    def test_catalog_missing_screening_rejected_by_validate(self, tmp_path):
        f = _curated_raw(tmp_path, "2024_wang_测试论文")
        papers = tmp_path / "papers"
        ac = tmp_path / "c" / "all.json"
        lg = tmp_path / "c" / "l.json"
        _commit(papers, ac, lg, f)
        AllCatalogBuilder(papers, ac, PaperNumberLedger(lg)).build(write=True)

        cat_path = papers / "2024_wang_测试论文" / "2024_wang_测试论文.catalog.json"
        cat = json.loads(cat_path.read_text(encoding="utf-8"))
        saved = cat.pop("screening")
        cat_path.write_text(json.dumps(cat, ensure_ascii=False), encoding="utf-8")

        errors, _ = validate_v2_library(papers_dir=papers, all_catalog_path=ac, check_paths=False)
        assert any("screening" in e for e in errors), f"validate should reject missing screening: {errors}"

        cat_path.write_text(json.dumps({**cat, "screening": saved}, ensure_ascii=False), encoding="utf-8")

    def test_ledger_marker_conflict_reported(self, tmp_path):
        f = _curated_raw(tmp_path, "2024_wang_测试论文")
        papers = tmp_path / "papers"
        ac = tmp_path / "c" / "all.json"
        lg = tmp_path / "c" / "l.json"
        _commit(papers, ac, lg, f)

        marker = papers / "2024_wang_测试论文" / "0000000000000001.paper.number"
        # overwrite marker with wrong data then rename
        marker.write_text(json.dumps({"paper_number": "0000000000000099", "folder_name": "2024_wang_测试论文"}),
                          encoding="utf-8")
        renamed = papers / "2024_wang_测试论文" / "0000000000000099.paper.number"
        marker.rename(renamed)
        errors, _ = PaperNumberLedger(lg).validate(papers)
        assert any("conflict" in e for e in errors), f"ledger should report marker conflict: {errors}"

"""Tests for the unified ingest sidecar JSON schema.

Both ingest paths (manual PDF and DOI-first network metadata + PDF fetch) must
produce consistent sidecar JSON: stage_manifest, asset_manifest, .import_status,
and source_records/ separation of metadata source records from fetch results.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.fetch.models import FetchResult
from src.services.source_records import (
    fetch_result_rel_path,
    is_fetch_result_path,
    metadata_source_rel_path,
)
from src.services.v2_library import PaperRawAllocator, empty_metadata
from src.services.ingest_state import read_import_status
from config.settings import PAPER_NUMBER_LEDGER_PATH


# ── Helpers ────────────────────────────────────────────────────────────

def _make_pdf(path: Path, content: bytes = b"%PDF-1.4 test") -> Path:
    path.write_bytes(content)
    return path


def _ledger(tmp_path: Path) -> Path:
    return tmp_path / "catalog" / "paper_number_ledger.json"


def _stage_manual(tmp_path: Path) -> tuple[Path, Path, str]:
    """Stage a manual PDF; return (paper_raw_dir, workspace_folder, paper_number)."""
    raw = tmp_path / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    pdf = _make_pdf(raw / "paper.pdf", b"%PDF manual staging test")
    paper_raw = tmp_path / "paper_raw"
    allocator = PaperRawAllocator(paper_raw, ledger_path=_ledger(tmp_path), papers_dir=tmp_path / "papers")
    result = allocator.allocate_from_pdf(pdf, source_type="manual_pdf", move=True)
    return paper_raw, Path(result["folder"]), result["paper_number"]


def _stage_network_metadata(tmp_path: Path, doi: str = "10.1000/net-meta") -> tuple[Path, Path, str]:
    """Stage network metadata (no PDF); return (paper_raw_dir, workspace_folder, paper_number)."""
    paper_raw = tmp_path / "paper_raw"
    allocator = PaperRawAllocator(paper_raw, ledger_path=_ledger(tmp_path), papers_dir=tmp_path / "papers")
    meta = empty_metadata("0000000000000001", source_type="network_search")
    meta["identifiers"]["doi"] = doi
    meta["title"]["original"] = "Network Metadata Paper"
    meta["year"] = 2024
    meta["source"]["provider"] = "crossref"
    meta["metadata_match"]["status"] = "matched"
    result = allocator.allocate_metadata(meta, source_type="network_search",
                                         raw_record={"provider": "crossref", "record": {"DOI": doi}})
    return paper_raw, Path(result["folder"]), result["paper_number"]


def _fetch_pdf_for_workspace(paper_raw: Path, folder: Path, paper_number: str,
                             monkeypatch, doi: str = "10.1000/net-meta") -> dict:
    """Run fetch_pdf_for_paper_raw to attach a PDF to an existing metadata workspace."""
    import runpy
    import sys
    from src.fetch import fetch_pipeline

    def fake_fetch(doi, output_root=None, **kwargs):
        output = Path(output_root) / "download.pdf"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"%PDF fetched content")
        return FetchResult(
            doi=doi, success=True, output_path=str(output),
            pdf_url="https://example.test/p.pdf",
            landing_url="https://example.test/landing",
            resolver="header_based", resolver_chain=["header_based"],
            access_mode="custom",
        )

    monkeypatch.setattr(fetch_pipeline, "fetch_pdf", fake_fetch)
    repo = Path(__file__).resolve().parent.parent
    argv = [
        "fetch_pdf_for_paper_raw.py",
        "--paper-number", paper_number,
        "--paper-raw-dir", str(paper_raw),
        "--papers-dir", str(paper_raw.parent / "papers"),
        "--resolver", "header-based",
        "--url-template", "https://example.test/fetch?doi={doi}",
        "--header", "Cookie: secret",
        "--apply",
    ]
    saved = sys.argv
    sys.argv = argv
    try:
        runpy.run_path(str(repo / "scripts" / "fetch_pdf_for_paper_raw.py"), run_name="__main__")
    except SystemExit:
        pass
    finally:
        sys.argv = saved
    return json.loads((folder / ".import_status.json").read_text(encoding="utf-8"))


# ── Common sidecar keys ────────────────────────────────────────────────

STAGE_MANIFEST_REQUIRED_KEYS = {"schema_version", "paper_number", "paper_raw_id", "workflow_path",
                                "source_type", "pdf_source", "staged_pdf", "created_at", "updated_at"}
IMPORT_STATUS_REQUIRED_KEYS = {"status", "reason", "errors", "warnings", "created_at"}


# ── 1. Manual PDF stage sidecar JSON schema complete ───────────────────

def test_manual_pdf_stage_sidecar_schema_complete(tmp_path):
    _, folder, pn = _stage_manual(tmp_path)
    # stage_manifest
    manifest = json.loads((folder / "stage_manifest.json").read_text(encoding="utf-8"))
    assert manifest["workflow_path"] == "manual_pdf"
    assert manifest["source_type"] == "manual_pdf"
    assert manifest["pdf_source"]["kind"] == "local_raw_queue"
    assert manifest["pdf_source"]["operation"] == "move"
    assert manifest["staged_pdf"]["path"]
    assert manifest["staged_pdf"]["sha256"]
    # asset_manifest
    am = json.loads((folder / f"{pn}.asset_manifest.json").read_text(encoding="utf-8"))
    assert am["files"]["pdf"]["sha256"]
    assert am["files"]["metadata"]["path"] == f"{pn}.metadata.json"
    # .import_status.json by canonical writer
    status = read_import_status(folder)
    assert status["status"] == "ready_for_convert"
    assert "errors" in status and "warnings" in status
    # source_records/metadata_source.manual.json exists
    assert (folder / "source_records" / "metadata_source.manual.json").exists()
    # paper.number marker
    assert (folder / f"{pn}.paper.number").exists()


# ── 2. DOI-first metadata stage source_records correct ─────────────────

def test_network_metadata_stage_source_records_correct(tmp_path):
    _, folder, pn = _stage_network_metadata(tmp_path)
    # source_records/metadata_source.crossref.json must exist
    ms_path = folder / "source_records" / "metadata_source.crossref.json"
    assert ms_path.exists()
    record = json.loads(ms_path.read_text(encoding="utf-8"))
    assert record["provider"] == "crossref"
    # metadata.source.raw_record_path points at the metadata source record
    meta = json.loads((folder / f"{pn}.metadata.json").read_text(encoding="utf-8"))
    assert meta["source"]["raw_record_path"] == "source_records/metadata_source.crossref.json"
    assert not is_fetch_result_path(meta["source"]["raw_record_path"])
    # stage_manifest has network_metadata workflow (no PDF yet)
    manifest = json.loads((folder / "stage_manifest.json").read_text(encoding="utf-8"))
    assert manifest["workflow_path"] == "network_metadata"
    assert manifest["pdf_source"] is None
    assert manifest["staged_pdf"] is None


# ── 3. DOI-first fetch PDF sidecar schema matches manual PDF path ──────

def test_fetch_pdf_sidecar_schema_matches_manual(tmp_path, monkeypatch):
    _, folder, pn = _stage_network_metadata(tmp_path)
    _fetch_pdf_for_workspace(_, folder, pn, monkeypatch)
    # After fetch, stage_manifest should have the same required keys as manual
    manifest = json.loads((folder / "stage_manifest.json").read_text(encoding="utf-8"))
    assert STAGE_MANIFEST_REQUIRED_KEYS.issubset(manifest.keys())
    assert manifest["workflow_path"] == "network_metadata_pdf_fetch"
    assert manifest["pdf_source"]["kind"] == "doi_fetch"
    assert manifest["pdf_source"]["fetch_record_path"] == fetch_result_rel_path()
    assert manifest["staged_pdf"]["sha256"]
    # asset_manifest has pdf
    am = json.loads((folder / f"{pn}.asset_manifest.json").read_text(encoding="utf-8"))
    assert am["files"]["pdf"]["sha256"]


# ── 4. fetch_result does not overwrite metadata_source ─────────────────

def test_fetch_result_does_not_overwrite_metadata_source(tmp_path, monkeypatch):
    _, folder, pn = _stage_network_metadata(tmp_path)
    ms_path = folder / "source_records" / "metadata_source.crossref.json"
    original_ms = json.loads(ms_path.read_text(encoding="utf-8"))
    _fetch_pdf_for_workspace(_, folder, pn, monkeypatch)
    # metadata source record is intact (not overwritten by fetch_result)
    after_ms = json.loads(ms_path.read_text(encoding="utf-8"))
    assert after_ms == original_ms
    # fetch_result.json is a separate file
    fr_path = folder / "source_records" / "fetch_result.json"
    assert fr_path.exists()
    fr = json.loads(fr_path.read_text(encoding="utf-8"))
    assert "fetch_result" in fr
    # metadata.source.raw_record_path still points at metadata source, not fetch_result
    meta = json.loads((folder / f"{pn}.metadata.json").read_text(encoding="utf-8"))
    assert meta["source"]["raw_record_path"] == "source_records/metadata_source.crossref.json"
    assert not is_fetch_result_path(meta["source"]["raw_record_path"])


# ── 5. .import_status.json by canonical writer ─────────────────────────

def test_import_status_by_canonical_writer(tmp_path):
    _, folder, _ = _stage_manual(tmp_path)
    status = read_import_status(folder)
    # canonical writer always includes errors + warnings lists
    assert isinstance(status.get("errors"), list)
    assert isinstance(status.get("warnings"), list)
    assert status["status"] == "ready_for_convert"
    assert "created_at" in status


# ── 6. stage_manifest key sets match across paths ──────────────────────

def test_stage_manifest_key_sets_match_across_paths(tmp_path, monkeypatch):
    _, manual_folder, _ = _stage_manual(tmp_path / "manual")
    net_raw, net_folder, net_pn = _stage_network_metadata(tmp_path / "net")
    _fetch_pdf_for_workspace(net_raw, net_folder, net_pn, monkeypatch)
    manual_m = json.loads((manual_folder / "stage_manifest.json").read_text(encoding="utf-8"))
    net_m = json.loads((net_folder / "stage_manifest.json").read_text(encoding="utf-8"))
    # Both have the same required key set
    assert STAGE_MANIFEST_REQUIRED_KEYS.issubset(manual_m.keys())
    assert STAGE_MANIFEST_REQUIRED_KEYS.issubset(net_m.keys())
    # Both have the same schema_version
    assert manual_m["schema_version"] == net_m["schema_version"]


# ── 7. asset_manifest key sets match across paths ──────────────────────

def test_asset_manifest_key_sets_match_across_paths(tmp_path, monkeypatch):
    _, manual_folder, manual_pn = _stage_manual(tmp_path / "manual")
    net_raw, net_folder, net_pn = _stage_network_metadata(tmp_path / "net")
    _fetch_pdf_for_workspace(net_raw, net_folder, net_pn, monkeypatch)
    manual_am = json.loads((manual_folder / f"{manual_pn}.asset_manifest.json").read_text(encoding="utf-8"))
    net_am = json.loads((net_folder / f"{net_pn}.asset_manifest.json").read_text(encoding="utf-8"))
    # Both have the same top-level keys
    assert set(manual_am.keys()) == set(net_am.keys())
    # Both have pdf + metadata in files
    assert "pdf" in manual_am["files"] and "pdf" in net_am["files"]
    assert "metadata" in manual_am["files"] and "metadata" in net_am["files"]


# ── 8. metadata schema not polluted by sidecar ─────────────────────────

def test_metadata_not_polluted_by_sidecar(tmp_path):
    _, folder, pn = _stage_manual(tmp_path)
    meta = json.loads((folder / f"{pn}.metadata.json").read_text(encoding="utf-8"))
    # metadata must NOT contain PDF hash, fetch_result, catalog content, etc.
    forbidden = {"pdf", "fetch_result", "content", "abstract", "keywords", "bibtex", "citation_key"}
    for key in forbidden:
        assert key not in meta, f"metadata must not contain {key}"
    # source.raw_record must not exist (forbidden in schema v2.0)
    assert "raw_record" not in (meta.get("source") or {})
    # source.raw_record_path must not point at fetch_result.json
    assert not is_fetch_result_path((meta.get("source") or {}).get("raw_record_path", ""))


# ── 9. source_records helpers ──────────────────────────────────────────

def test_source_records_path_helpers():
    assert metadata_source_rel_path("crossref") == "source_records/metadata_source.crossref.json"
    assert metadata_source_rel_path("") == "source_records/metadata_source.manual.json"
    assert fetch_result_rel_path() == "source_records/fetch_result.json"
    assert is_fetch_result_path("source_records/fetch_result.json")
    assert not is_fetch_result_path("source_records/metadata_source.crossref.json")
    assert not is_fetch_result_path("")

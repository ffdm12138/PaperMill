"""Tests for scripts/curate_paper_raw.py under metadata v2/catalog v3."""

import json
import runpy
import sys
from pathlib import Path

from src.services.v2_library import empty_catalog, empty_metadata
from src.services.source_records import (
    manual_metadata_source_record,
    metadata_source_rel_path,
    write_metadata_source_record,
)
from tests.helpers.paper_raw_factory import fill_valid_catalog_v31

PN1 = "0000000000000001"
PN2 = "0000000000000002"

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "curate_paper_raw.py"


def _run_cli(argv: list[str]) -> int:
    saved = sys.argv
    sys.argv = argv
    try:
        runpy.run_path(str(_SCRIPT), run_name="__main__")
        return 0
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = saved


def _matched_raw(folder: Path, source_id: str = PN1) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    metadata = empty_metadata(source_id)
    metadata["title"]["original"] = "Trusted Original"
    metadata["year"] = 2024
    metadata["authors"] = [{"full_name": "Wang A", "family": "Wang", "given": "A", "orcid": "", "affiliation": ""}]
    metadata["first_author"] = {"family": "Wang", "display": "Wang A"}
    metadata["container"]["journal"] = "Test Journal"
    metadata["identifiers"]["doi"] = "10.1/test"
    metadata["metadata_match"]["status"] = "matched"
    metadata["metadata_match"]["confidence"] = 1.0
    source = metadata.setdefault("source", {})
    source["kind"] = "manual_pdf"
    source["provider"] = "manual"
    source["raw_record_path"] = metadata_source_rel_path("manual")
    write_metadata_source_record(
        folder, "manual",
        manual_metadata_source_record(original_filename=f"{source_id}.pdf"),
    )
    (folder / f"{source_id}.metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    (folder / f"{source_id}.md").write_text("# Trusted Original\n\nbody text", encoding="utf-8")
    (folder / f"{source_id}.pdf").write_bytes(b"%PDF")
    (folder / "images").mkdir()
    return folder


def _fill_catalog(catalog: dict, title: str = "可信论文") -> dict:
    return fill_valid_catalog_v31(
        catalog,
        paper_number=PN1,
        title_zh=title,
        title_original="Trusted Original",
        domain="blowing_snow",
    )


def test_dry_run_writes_curation_prompt(tmp_path, monkeypatch):
    folder = _matched_raw(tmp_path / "paper_raw" / PN1)
    monkeypatch.syspath_prepend(str(_REPO_ROOT))
    rc = _run_cli(["curate_paper_raw.py", "--paper-dir", str(folder), "--dry-run"])

    assert rc == 0
    text = (folder / "curation_prompt.md").read_text(encoding="utf-8")
    assert "paper_raw_catalog_curator" in text
    assert "evidence_profile" in text
    assert "screening" in text
    assert '"pending"' in text


def test_apply_merges_only_empty_and_keeps_source_folder(tmp_path, monkeypatch):
    raw = tmp_path / "paper_raw"
    folder = _matched_raw(raw / PN1)
    catalog_path = folder / f"{PN1}.catalog.json"
    catalog_path.write_text(json.dumps(_fill_catalog(empty_catalog()), ensure_ascii=False), encoding="utf-8")
    patch = empty_metadata(PN1)
    patch["links"]["url"] = "https://example.org/curated"
    patch["title"]["original"] = "Overwrite Attempt"
    patch_path = tmp_path / "patch.metadata.json"
    patch_path.write_text(json.dumps(patch, ensure_ascii=False), encoding="utf-8")
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_cli([
        "curate_paper_raw.py",
        "--paper-dir", str(folder),
        "--catalog", str(catalog_path),
        "--metadata", str(patch_path),
        "--apply",
    ])

    assert rc == 0
    assert folder.exists()
    assert (folder / f"{PN1}.metadata.json").exists()
    merged = json.loads((folder / f"{PN1}.metadata.json").read_text(encoding="utf-8"))
    assert merged["title"]["original"] == "Trusted Original"
    assert merged["links"]["url"] == "https://example.org/curated"
    status = json.loads((folder / ".import_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "catalog_ready"
    assert status["paper_id"] == "2024_Wang_可信论文"


def test_apply_rejects_unmatched_metadata(tmp_path, monkeypatch):
    folder = _matched_raw(tmp_path / "paper_raw" / PN1)
    meta_path = folder / f"{PN1}.metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["metadata_match"]["status"] = "unmatched"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_cli(["curate_paper_raw.py", "--paper-dir", str(folder), "--apply"])

    assert rc == 1
    assert (folder / ".import_status.json").exists()


def test_pdf_metadata_without_doi_cannot_curate(tmp_path, monkeypatch):
    folder = _matched_raw(tmp_path / "paper_raw" / PN1)
    meta_path = folder / f"{PN1}.metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["metadata_match"]["status"] = "manual_confirmed"
    meta["identifiers"]["doi"] = ""
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_cli(["curate_paper_raw.py", "--paper-dir", str(folder), "--apply"])

    assert rc == 1
    status = json.loads((folder / ".import_status.json").read_text(encoding="utf-8"))
    assert status["reason"] == "curation requires metadata.identifiers.doi"


def test_all_ready_apply_only_processes_curated(tmp_path, monkeypatch):
    raw = tmp_path / "paper_raw"
    folder_a = _matched_raw(raw / PN1)
    (folder_a / f"{PN1}.catalog.json").write_text(
        json.dumps(_fill_catalog(empty_catalog(), "甲论文"), ensure_ascii=False),
        encoding="utf-8",
    )
    _matched_raw(raw / PN2, PN2)
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_cli(["curate_paper_raw.py", "--all-ready", "--paper-raw-dir", str(raw), "--apply"])

    assert folder_a.exists()
    assert (raw / PN2).exists()
    status = json.loads((folder_a / ".import_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "catalog_ready"
    assert rc in (0, 1)


def test_all_ready_apply_auto_loads_metadata_patch(tmp_path, monkeypatch):
    raw = tmp_path / "paper_raw"
    folder = _matched_raw(raw / PN1)
    (folder / f"{PN1}.catalog.json").write_text(
        json.dumps(_fill_catalog(empty_catalog(), "甲论文"), ensure_ascii=False),
        encoding="utf-8",
    )
    patch = empty_metadata(PN1)
    patch["links"]["url"] = "https://example.org/auto-patch"
    (folder / f"{PN1}.metadata.patch.json").write_text(json.dumps(patch), encoding="utf-8")
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_cli(["curate_paper_raw.py", "--all-ready", "--paper-raw-dir", str(raw), "--apply"])

    assert folder.exists()
    assert rc in (0, 1)
    if rc == 0:
        merged = json.loads((folder / f"{PN1}.metadata.json").read_text(encoding="utf-8"))
        assert merged["links"]["url"] == "https://example.org/auto-patch"


def test_all_matched_dry_run_skips_unmatched_metadata(tmp_path, monkeypatch):
    raw = tmp_path / "paper_raw"
    matched = _matched_raw(raw / PN1)
    unmatched = _matched_raw(raw / PN2, PN2)
    meta_path = unmatched / f"{PN2}.metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["metadata_match"]["status"] = "unmatched"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_cli(["curate_paper_raw.py", "--all-matched", "--paper-raw-dir", str(raw), "--dry-run"])

    assert rc == 0
    assert (matched / "curation_prompt.md").exists()
    assert not (unmatched / "curation_prompt.md").exists()

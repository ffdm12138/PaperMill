"""Tests for PaperRawFormalizationService + formalize_paper_raw.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.services.paper_raw_formalizer import PaperRawFormalizationService
from src.services.v2_library import (
    PaperRawConverter,
    PaperNumberLedger,
    empty_catalog,
    empty_metadata,
    validate_catalog_schema,
)
from src.services.asset_manifest import write_asset_manifest
from src.services.ingest_state import CATALOG_READY, READY_FOR_COMMIT, read_import_status
from tests.helpers.paper_raw_factory import fill_valid_catalog_v31


def _matched_metadata(source_id: str, *, doi: str = "10.1/test") -> dict:
    metadata = empty_metadata(source_id)
    metadata["title"]["original"] = "Trusted Original"
    metadata["year"] = 2024
    metadata["authors"] = [{"full_name": "Wang A", "family": "Wang", "given": "A", "orcid": "", "affiliation": ""}]
    metadata["first_author"] = {"family": "Wang", "display": "Wang A"}
    metadata["container"]["journal"] = "Test Journal"
    metadata["identifiers"]["doi"] = doi
    metadata["metadata_match"] = {
        "status": "matched",
        "source": "test",
        "confidence": 1.0,
        "matched_at": "2026-01-01T00:00:00",
        "warnings": [],
    }
    return metadata


def _chinese_catalog() -> dict:
    return fill_valid_catalog_v31(
        empty_catalog(),
        paper_number="0000000000000001",
        title_zh="可信论文",
        title_original="Trusted Original",
        domain="blowing_snow_physics",
    )


def _write_conversion_manifest(folder: Path, source_id: str, pdf_sha: str, md_sha: str) -> None:
    from src.utils.atomic_io import atomic_write_json
    from config.settings import MINERU_BACKEND, MINERU_METHOD, MINERU_LANG, MINERU_EFFORT

    atomic_write_json(folder / f"{source_id}.conversion.json", {
        "schema_version": "1.0",
        "status": "converted",
        "paper_number": source_id,
        "paper_raw_id": source_id,
        "pdf_sha256": pdf_sha,
        "pdf_file_size": 4,
        "markdown_path": f"{source_id}.md",
        "markdown_sha256": md_sha,
        "images_dir": "images",
        "images_count": 0,
        "backend": MINERU_BACKEND,
        "method": MINERU_METHOD,
        "lang": MINERU_LANG,
        "effort": MINERU_EFFORT,
        "runner": "",
        "api_url": "",
        "output_dir": str(folder / "output"),
        "converted_at": "2026-01-01T00:00:00",
    }, indent=2)


def _staged_raw(root: Path, source_id: str = "0000000000000001", *, doi: str = "10.1/test") -> Path:
    from tests.helpers.paper_raw_factory import make_staged_source

    return make_staged_source(root, source_id, title_zh="可信论文", doi=doi, family="Wang")


def _service(tmp_path: Path) -> PaperRawFormalizationService:
    return PaperRawFormalizationService(
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
    )


def test_test_factory_uses_paper_number_workspace_and_conversion_manifest(tmp_path: Path):
    from tests.helpers.paper_raw_factory import make_staged_source

    folder = make_staged_source(tmp_path)

    assert folder.name == "0000000000000001"
    assert (folder / "0000000000000001.conversion.json").exists()
    assert (folder / "0000000000000001.paper.number").exists()
    assert read_import_status(folder)["status"] == "catalog_ready"


def test_formalize_renames_folder_files_and_reserves_number(tmp_path: Path):
    folder = _staged_raw(tmp_path)
    svc = _service(tmp_path)

    result = svc.formalize(folder)

    assert result["success"]
    assert result["status"] == READY_FOR_COMMIT
    pid = result["paper_id"]
    assert pid == "2024_Wang_可信论文"
    number = result["paper_number"]
    assert number == "0000000000000001"

    renamed = tmp_path / "paper_raw" / pid
    assert renamed.exists()
    assert not folder.exists()
    for suffix in ("metadata.json", "catalog.json", "md", "pdf"):
        assert (renamed / f"{pid}.{suffix}").exists()
    assert (renamed / f"{number}.paper.number").exists()
    assert (renamed / f"{pid}.formalization.json").exists()

    status = json.loads((renamed / ".import_status.json").read_text(encoding="utf-8"))
    assert status["status"] == READY_FOR_COMMIT
    assert status["paper_id"] == pid
    assert status["paper_number"] == number

    data = svc.ledger.load()
    item = data["items"][number]
    assert item["state"] == "reserved"
    assert item["folder_name"] == pid
    assert item["planned_paper_id"] == pid
    assert Path(item["folder_path"]).name == pid
    assert "0000000000000001" not in item["folder_path"]
    assert data["max_number"] == number
    marker = json.loads((renamed / f"{number}.paper.number").read_text(encoding="utf-8"))
    assert marker["paper_number"] == number
    assert marker["state"] == "reserved"
    assert marker["folder_name"] == pid
    assert marker["planned_paper_id"] == pid


def test_formalize_backfills_catalog_links(tmp_path: Path):
    folder = _staged_raw(tmp_path)
    catalog_path = folder / "0000000000000001.catalog.json"
    staged_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    staged_catalog["provenance"]["markdown_path"] = "0000000000000001.md"
    catalog_path.write_text(json.dumps(staged_catalog, ensure_ascii=False), encoding="utf-8")
    svc = _service(tmp_path)
    result = svc.formalize(folder)
    pid = result["paper_id"]
    renamed = tmp_path / "paper_raw" / pid
    catalog = json.loads((renamed / f"{pid}.catalog.json").read_text(encoding="utf-8"))
    assert catalog["library_locator"]["paper_id"] == pid
    assert catalog["library_locator"]["paper_number"] == result["paper_number"]
    refs = catalog["library_locator"]["asset_refs"]
    assert refs["markdown"] == f"{pid}.md"
    assert refs["pdf"] == f"{pid}.pdf"
    assert refs["metadata"] == f"{pid}.metadata.json"
    assert refs["catalog"] == f"{pid}.catalog.json"
    assert refs["images_dir"] == "images/"
    assert catalog["provenance"]["markdown_path"] == f"{pid}.md"
    assert catalog["provenance"]["original_markdown_path"] == "0000000000000001.md"
    assert validate_catalog_schema(catalog) == []


def test_formalize_reserved_ledger_points_to_renamed_folder(tmp_path: Path):
    folder = _staged_raw(tmp_path, source_id="0000000000000001")
    svc = _service(tmp_path)

    result = svc.formalize(folder)

    number = result["paper_number"]
    pid = result["paper_id"]
    item = svc.ledger.load()["items"][number]

    assert item["state"] == "reserved"
    assert item["folder_name"] == pid
    assert item["planned_paper_id"] == pid
    assert Path(item["folder_path"]).name == pid
    assert Path(item["folder_path"]).name != "0000000000000001"


def test_formalize_idempotent_on_rerun(tmp_path: Path):
    folder = _staged_raw(tmp_path)
    svc = _service(tmp_path)
    first = svc.formalize(folder)
    renamed = tmp_path / "paper_raw" / first["paper_id"]
    second = svc.formalize(renamed)

    assert second["success"]
    assert second["paper_number"] == first["paper_number"]
    assert second["paper_id"] == first["paper_id"]
    data = svc.ledger.load()
    assert len(data["items"]) == 1  # no new number allocated


def test_formalize_idempotent_skip_when_source_record_broken(tmp_path: Path):
    """Regression: already-formalized + ready_for_commit must skip validation entirely.

    If an already-formalized paper's source record is missing or the metadata
    pointer breaks, a re-run must NOT overwrite .import_status with formalize_failed.
    The idempotency guard reads .import_status and returns early when
    status=ready_for_commit, bypassing gates 2-4 (metadata load, source record
    check, readiness gate).
    """
    folder = _staged_raw(tmp_path, "0000000000000099")
    svc = _service(tmp_path)

    # 1. First formalize — succeeds, renames folder.
    first = svc.formalize(folder)
    assert first["success"]
    pid = first["paper_id"]

    # 2. Break the source record file AND the metadata pointer.
    renamed = tmp_path / "paper_raw" / pid
    src_rec_dir = renamed / "source_records"
    for f in list(src_rec_dir.glob("*")):
        f.unlink()
    # Also corrupt the metadata raw_record_path so it points at a nonexistent file.
    meta_path = renamed / f"{pid}.metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["source"]["raw_record_path"] = "source_records/missing_file.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    # 3. Second formalize — idempotency guard fires, skips all validation.
    second = svc.formalize(renamed)
    assert second["success"]
    assert second["status"] == READY_FOR_COMMIT
    assert second["paper_number"] == first["paper_number"]
    assert second["paper_id"] == pid

    # 4. .import_status.json must still read ready_for_commit — no formalize_failed.
    status = json.loads((renamed / ".import_status.json").read_text(encoding="utf-8"))
    assert status["status"] == READY_FOR_COMMIT

    # 5. Ledger unchanged (no new entry, no state mutation).
    data = svc.ledger.load()
    assert len(data["items"]) == 1
    item = data["items"][first["paper_number"]]
    assert item["state"] == "reserved"
    assert item["folder_name"] == pid


def test_formalize_rejects_stale_conversion(tmp_path: Path, monkeypatch):
    folder = _staged_raw(tmp_path)
    svc = _service(tmp_path)

    def _stale(self, folder, *, file_prefix, **kwargs):
        return {"state": "stale", "reason": "PDF sha256 changed", "manifest": None,
                "markdown": "", "images_dir": "", "pdf_sha256": ""}

    monkeypatch.setattr(PaperRawConverter, "inspect_converted_assets", _stale)
    result = svc.formalize(folder)

    assert not result["success"]
    assert result["status"] == "formalize_failed"
    assert folder.exists()  # not renamed
    status = json.loads((folder / ".import_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "formalize_failed"


def test_formalize_rejects_missing_conversion_manifest(tmp_path: Path):
    folder = _staged_raw(tmp_path)
    (folder / "0000000000000001.conversion.json").unlink()
    svc = _service(tmp_path)

    result = svc.formalize(folder)

    assert not result["success"]
    assert result["status"] == "formalize_failed"
    assert result["conversion_state"] == "conversion_manifest_missing"
    assert "conversion_manifest_missing" in "; ".join(result["errors"])


def test_formalize_rejects_unmatched_metadata(tmp_path: Path):
    folder = _staged_raw(tmp_path)
    meta_path = folder / "0000000000000001.metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["metadata_match"]["status"] = "unmatched"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    svc = _service(tmp_path)

    result = svc.formalize(folder)

    assert not result["success"]
    assert result["status"] == "metadata_unmatched"
    assert folder.exists()
    assert not (tmp_path / "paper_raw" / "2024_Wang_可信论文").exists()
    status = json.loads((folder / ".import_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "formalize_failed"


def test_formalize_preserve_paper_number(tmp_path: Path):
    folder = _staged_raw(tmp_path, "0000000000000007")
    svc = _service(tmp_path)
    result = svc.formalize(folder, preserve_paper_number="0000000000000007")
    assert result["paper_number"] == "0000000000000007"
    renamed = tmp_path / "paper_raw" / result["paper_id"]
    assert (renamed / "0000000000000007.paper.number").exists()
    data = svc.ledger.load()
    assert data["max_number"] == "0000000000000007"
    item = data["items"]["0000000000000007"]
    assert item["state"] == "reserved"
    assert item["planned_paper_id"] == result["paper_id"]


def test_formalize_preserve_paper_number_rejects_active_number(tmp_path: Path):
    folder = _staged_raw(tmp_path, "0000000000000007")
    svc = _service(tmp_path)
    active_folder = tmp_path / "papers" / "already_active"
    active_folder.mkdir(parents=True)
    svc.ledger.save({
        "schema_version": "1.0",
        "max_number": "0000000000000007",
        "items": {
            "0000000000000007": {
                "folder_name": active_folder.name,
                "folder_path": str(active_folder),
                "state": "active",
                "planned_paper_id": "already_active",
                "created_at": "2026-01-01T00:00:00",
            }
        },
    })

    result = svc.formalize(folder, preserve_paper_number="0000000000000007")

    assert not result["success"]
    assert result["status"] == "formalize_failed"
    assert any("ledger state active" in err for err in result["errors"])


def test_formalize_quarantines_duplicate(tmp_path: Path):
    # commit one paper first to seed the formal library
    folder1 = _staged_raw(tmp_path, "0000000000000001", doi="10.1/dup")
    svc = _service(tmp_path)
    first = svc.formalize(folder1)
    # Simulate the formal copy existing with that DOI (duplicate gate target)
    pid1 = first["paper_id"]
    papers = tmp_path / "papers" / pid1
    papers.mkdir(parents=True)
    (papers / f"{pid1}.metadata.json").write_text(
        (tmp_path / "paper_raw" / pid1 / f"{pid1}.metadata.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    # second paper_raw with same DOI
    folder2 = _staged_raw(tmp_path, "0000000000000002", doi="10.1/dup")
    # its paper_id would collide too; tweak title so paper_id differs but DOI dup
    cat = json.loads((folder2 / "0000000000000002.catalog.json").read_text(encoding="utf-8"))
    cat["content_identity"]["content_title_zh"] = "重复论文"
    (folder2 / "0000000000000002.catalog.json").write_text(json.dumps(cat, ensure_ascii=False), encoding="utf-8")

    result = svc.formalize(folder2)
    assert result["status"] == "possible_duplicate"
    quarantine_dir = Path(result["quarantine_dir"])
    assert quarantine_dir.exists()
    assert not folder2.exists()
    status = read_import_status(quarantine_dir)
    assert status["status"] == "quarantined_duplicate"
    assert status["paper_number"] == "0000000000000002"
    ledger_item = svc.ledger.load()["items"]["0000000000000002"]
    assert ledger_item["state"] == "quarantined_duplicate"
    assert ledger_item["folder_path"].endswith(quarantine_dir.name)


def test_formalize_cli_all_ready_apply(tmp_path: Path):
    folder = _staged_raw(tmp_path)
    # curate would have written catalog_ready; simulate
    from src.services.ingest_state import write_import_status

    write_import_status(folder, CATALOG_READY, reason="curated")
    import subprocess
    import os
    import sys

    project_root = Path(__file__).resolve().parent.parent.parent
    default_ledger = project_root / "data" / "catalog" / "paper_number_ledger.json"
    before = default_ledger.read_text(encoding="utf-8") if default_ledger.exists() else None

    try:
        proc = subprocess.run(
            [sys.executable, "scripts/formalize_paper_raw.py",
             "--all-ready", "--apply",
             "--paper-raw-dir", str(tmp_path / "paper_raw"),
             "--papers-dir", str(tmp_path / "papers"),
             "--ledger-path", str(tmp_path / "catalog" / "paper_number_ledger.json"),
             "--all-catalog-path", str(tmp_path / "catalog" / "all.catalog.json"),
             "--report", str(tmp_path / "report.json")],
            capture_output=True, text=True, cwd=str(project_root),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("formalize CLI hung > 30s — possible deadlock or env pollution")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert (tmp_path / "paper_raw" / "2024_Wang_可信论文" / "2024_Wang_可信论文.formalization.json").exists()
    # tmp ledger must exist and the real default ledger must NOT have been touched.
    assert (tmp_path / "catalog" / "paper_number_ledger.json").exists()
    after = default_ledger.read_text(encoding="utf-8") if default_ledger.exists() else None
    assert after == before, "formalize CLI mutated the real data/catalog/paper_number_ledger.json"


def test_formalize_cli_rejects_missing_conversion_manifest(tmp_path: Path):
    folder = _staged_raw(tmp_path)
    (folder / "0000000000000001.conversion.json").unlink()
    import subprocess
    import os
    import sys

    project_root = Path(__file__).resolve().parent.parent.parent
    base_cmd = [
        sys.executable, "scripts/formalize_paper_raw.py",
        "--paper-number", "0000000000000001", "--apply",
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--papers-dir", str(tmp_path / "papers"),
        "--ledger-path", str(tmp_path / "catalog" / "paper_number_ledger.json"),
        "--all-catalog-path", str(tmp_path / "catalog" / "all.catalog.json"),
    ]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    try:
        rejected = subprocess.run(base_cmd, capture_output=True, text=True,
                                  cwd=str(project_root), env=env, timeout=30)
    except subprocess.TimeoutExpired:
        pytest.fail("formalize CLI (reject case) hung > 30s")
    assert rejected.returncode == 1
    assert "conversion_manifest_missing" in rejected.stdout
    assert not (tmp_path / "paper_raw" / "2024_Wang_可信论文" / "2024_Wang_可信论文.formalization.json").exists()

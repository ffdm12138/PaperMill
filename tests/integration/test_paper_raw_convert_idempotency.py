from __future__ import annotations

import json
import sys
from pathlib import Path

import scripts.convert_paper_raw_batch as batch
from scripts.preflight_paper_raw_import import preflight_one
from src.services.v2_library import (
    PaperRawConverter,
    V2PaperCommitService,
    empty_catalog,
    empty_metadata,
    write_conversion_manifest_for_existing_assets,
)


PN1 = "0000000000000001"


class FakeMinerUConverter:
    def __init__(self):
        self.calls: list[str] = []

    def convert(self, pdf, output_root, backend, method, lang, effort, paper_id=""):
        self.calls.append(paper_id)
        out = Path(output_root) / paper_id / "hybrid_auto"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{paper_id}.md").write_text(f"# Converted {paper_id}", encoding="utf-8")
        images = out / "images"
        images.mkdir()
        (images / "new.png").write_bytes(b"new")
        return {
            "success": True,
            "output_dir": str(out),
            "runner": "test",
            "backend": backend,
            "method": method,
            "effort": effort,
        }


class FakeCleaner:
    def locate_markdown(self, source_dir, **kwargs):
        source_dir = Path(source_dir)
        return next(source_dir.glob("*.md"), None)

    def locate_images_dir(self, source_dir, md_path):
        images = Path(source_dir) / "images"
        return images if images.exists() else None


def _raw_folder(root: Path, source_id: str = PN1) -> Path:
    folder = root / source_id
    folder.mkdir(parents=True)
    metadata = empty_metadata(source_id)
    metadata["title"]["original"] = "Idempotent Paper"
    metadata["year"] = 2024
    metadata["authors"] = [{"full_name": "Wang A", "family": "Wang", "given": "A", "orcid": "", "affiliation": ""}]
    metadata["container"]["journal"] = "Test Journal"
    metadata["identifiers"]["doi"] = f"10.1000/{source_id}"
    metadata["metadata_match"]["status"] = "matched"
    (folder / f"{source_id}.metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (folder / f"{source_id}.pdf").write_bytes(b"%PDF-" + source_id.encode("ascii"))
    return folder


def _converter(root: Path, fake: FakeMinerUConverter | None = None) -> tuple[PaperRawConverter, FakeMinerUConverter]:
    fake = fake or FakeMinerUConverter()
    return PaperRawConverter(root, converter=fake, cleaner=FakeCleaner()), fake


def test_repeated_conversion_writes_manifest_then_skips(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    _raw_folder(paper_raw)
    converter, fake = _converter(paper_raw)

    first = converter.convert(PN1)
    second = converter.convert(PN1)

    folder = paper_raw / PN1
    manifest = json.loads((folder / f"{PN1}.conversion.json").read_text(encoding="utf-8"))
    status = json.loads((folder / ".import_status.json").read_text(encoding="utf-8"))
    assert first["success"] is True
    assert second["skipped"] is True
    assert second["status"] == "skipped_existing"
    assert fake.calls == [PN1]
    assert manifest["status"] == "converted"
    assert manifest["markdown_sha256"]
    assert status["status"] == "converted"


def test_converted_assets_without_manifest_are_reconverted(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    folder = _raw_folder(paper_raw)
    (folder / f"{PN1}.md").write_text("# Legacy", encoding="utf-8")
    (folder / "images").mkdir()
    converter, fake = _converter(paper_raw)

    result = converter.convert(PN1)

    assert result["success"] is True
    assert result["conversion_state"] == "converted_current"
    assert fake.calls == [PN1]


def test_stale_manifest_requires_force_and_force_cleans_old_images(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    folder = _raw_folder(paper_raw)
    (folder / f"{PN1}.md").write_text("# Old", encoding="utf-8")
    images = folder / "images"
    images.mkdir()
    (images / "old.png").write_bytes(b"old")
    (folder / f"{PN1}.conversion.json").write_text(json.dumps({
        "status": "converted",
        "paper_number": PN1,
        "paper_raw_id": PN1,
        "pdf_sha256": "stale",
        "backend": "hybrid-engine",
        "method": "auto",
        "lang": "ch",
        "effort": "medium",
    }), encoding="utf-8")
    converter, fake = _converter(paper_raw)

    stale = converter.convert(PN1)
    forced = converter.convert(PN1, force_reconvert=True)

    assert stale["success"] is False
    assert stale["status"] == "stale_conversion"
    assert forced["success"] is True
    assert fake.calls == [PN1]
    assert not (images / "old.png").exists()
    assert (images / "new.png").exists()


def test_partial_assets_require_force(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    folder = _raw_folder(paper_raw)
    (folder / f"{PN1}.md").write_text("# Partial", encoding="utf-8")
    converter, fake = _converter(paper_raw)

    partial = converter.convert(PN1)
    forced = converter.convert(PN1, force_reconvert=True)

    assert partial["success"] is False
    assert partial["status"] == "partial_conversion"
    assert forced["success"] is True
    assert fake.calls == [PN1]


def test_all_skipped_batch_does_not_run_runtime_preflight(monkeypatch, tmp_path):
    paper_raw = tmp_path / "paper_raw"
    folder = _raw_folder(paper_raw)
    (folder / f"{PN1}.md").write_text("# Done", encoding="utf-8")
    (folder / "images").mkdir()
    write_conversion_manifest_for_existing_assets(folder, PN1)
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "false")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("runtime preflight should not run when all sources are skipped")

    monkeypatch.setattr("src.mineru_runtime.preflight_gpu", fail_if_called)
    monkeypatch.setattr("src.mineru_runtime.preflight_torch_cuda", fail_if_called)
    saved = sys.argv
    sys.argv = ["convert_paper_raw_batch.py", "--paper-raw-dir", str(paper_raw), "--all", "--apply"]
    try:
        rc = batch.main()
    finally:
        sys.argv = saved

    assert rc == 0


def test_preflight_marks_existing_conversion_as_converted(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    folder = _raw_folder(paper_raw)
    (folder / f"{PN1}.md").write_text("# Done", encoding="utf-8")
    (folder / "images").mkdir()

    item = preflight_one(
        paper_raw,
        PN1,
        papers_dir=tmp_path / "papers",
    )

    assert item["status"] == "converted"
    assert item["has_markdown"] is True
    assert item["has_images_dir"] is True


def test_formal_commit_removes_conversion_manifest(tmp_path):
    from tests.helpers.paper_raw_factory import make_staged_source

    raw = make_staged_source(
        tmp_path,
        PN1,
        title_zh="转换清单",
        title_original="Manifest Paper",
        doi="10.1000/manifest",
        family="wang",
    )
    pid = "2024_wang_转换清单"
    assert (raw / f"{PN1}.conversion.json").exists()

    from src.services.paper_raw_formalizer import PaperRawFormalizationService

    formalized = PaperRawFormalizationService(
        paper_raw_dir=raw.parent, papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
    ).formalize(raw)
    assert formalized.get("success"), formalized

    result = V2PaperCommitService(
        papers_dir=tmp_path / "papers",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
    ).commit_paper_raw(formalized["folder"])

    assert result["success"] is True
    assert not (tmp_path / "papers" / pid / f"{pid}.conversion.json").exists()

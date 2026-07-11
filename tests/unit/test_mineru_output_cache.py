from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import scripts.convert_paper_raw_batch as batch
from config.settings import MINERU_BACKEND, MINERU_EFFORT, MINERU_LANG, MINERU_METHOD
from src.services.mineru_output_cache import MinerUOutputCache
from src.ingest.paper_raw import PaperRawConverter
from src.metadata.schema import empty_metadata


pytestmark = pytest.mark.unit

PN1 = "0000000000000001"
PN2 = "0000000000000002"


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


def _raw_folder(root: Path, source_id: str = PN1, *, pdf_bytes: bytes | None = None) -> Path:
    folder = root / source_id
    folder.mkdir(parents=True)
    metadata = empty_metadata(source_id)
    metadata["title"]["original"] = "Cache Paper"
    metadata["year"] = 2024
    metadata["authors"] = [{"full_name": "Wang A", "family": "Wang", "given": "A", "orcid": "", "affiliation": ""}]
    metadata["container"]["journal"] = "Test Journal"
    metadata["identifiers"]["doi"] = f"10.1000/{source_id}"
    (folder / f"{source_id}.metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (folder / f"{source_id}.pdf").write_bytes(pdf_bytes or (b"%PDF-cache-" + source_id.encode("ascii")))
    return folder


def _source_output(root: Path, stem: str, *, text: str = "# Cached") -> Path:
    out = root / stem / "hybrid_auto"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{stem}.md").write_text(text, encoding="utf-8")
    images = out / "images"
    images.mkdir()
    (images / "a.png").write_bytes(b"image")
    return out


def _register_cache(cache: MinerUOutputCache, pdf: Path, source_out: Path, source_id: str = PN1) -> dict:
    return cache.register(
        source_output_dir=source_out,
        pdf_path=pdf,
        source_paper_raw_id=source_id,
        backend=MINERU_BACKEND,
        method=MINERU_METHOD,
        lang=MINERU_LANG,
        effort=MINERU_EFFORT,
        runner="test",
        api_url="",
    )


def _converter(paper_raw: Path, cache_dir: Path, fake: FakeMinerUConverter | None = None):
    fake = fake or FakeMinerUConverter()
    cache = MinerUOutputCache(cache_dir, legacy_output_roots=[])
    return PaperRawConverter(paper_raw, converter=fake, output_cache=cache, reuse_output_cache=True), fake, cache


def test_cache_hit_restores_without_calling_mineru(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    folder = _raw_folder(paper_raw)
    converter, fake, cache = _converter(paper_raw, tmp_path / "output" / "mineru_cache")
    _register_cache(cache, folder / f"{PN1}.pdf", _source_output(tmp_path / "source", PN1), PN1)

    result = converter.convert(PN1)

    manifest = json.loads((folder / f"{PN1}.conversion.json").read_text(encoding="utf-8"))
    assert result["success"] is True
    assert result["restored_from_output_cache"] is True
    assert fake.calls == []
    assert (folder / f"{PN1}.md").exists()
    assert (folder / "images" / "a.png").exists()
    assert manifest["pdf_md5"]
    assert manifest["restored_from_output_cache"] is True
    assert manifest["output_cache_hit"] is True


def test_cache_miss_calls_mineru_and_registers_cache(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    folder = _raw_folder(paper_raw)
    converter, fake, cache = _converter(paper_raw, tmp_path / "output" / "mineru_cache")

    result = converter.convert(PN1)

    hit = cache.find(
        folder / f"{PN1}.pdf",
        backend=MINERU_BACKEND,
        method=MINERU_METHOD,
        lang=MINERU_LANG,
        effort=MINERU_EFFORT,
        stem=PN1,
    )
    assert result["success"] is True
    assert fake.calls == [PN1]
    assert hit.ok is True
    assert hit.manifest_path and hit.manifest_path.exists()


def test_md5_match_but_sha256_mismatch_is_rejected(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    folder = _raw_folder(paper_raw)
    _, _, cache = _converter(paper_raw, tmp_path / "output" / "mineru_cache")
    registered = _register_cache(cache, folder / f"{PN1}.pdf", _source_output(tmp_path / "source", PN1), PN1)
    manifest_path = Path(registered["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pdf_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    hit = cache.find(
        folder / f"{PN1}.pdf",
        backend=MINERU_BACKEND,
        method=MINERU_METHOD,
        lang=MINERU_LANG,
        effort=MINERU_EFFORT,
        stem=PN1,
    )

    assert hit.ok is False
    assert "pdf_sha256 mismatch" in hit.reason


def test_conversion_parameter_mismatch_is_rejected(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    folder = _raw_folder(paper_raw)
    _, _, cache = _converter(paper_raw, tmp_path / "output" / "mineru_cache")
    registered = _register_cache(cache, folder / f"{PN1}.pdf", _source_output(tmp_path / "source", PN1), PN1)
    manifest_path = Path(registered["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["method"] = "ocr"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    hit = cache.find(
        folder / f"{PN1}.pdf",
        backend=MINERU_BACKEND,
        method=MINERU_METHOD,
        lang=MINERU_LANG,
        effort=MINERU_EFFORT,
        stem=PN1,
    )

    assert hit.ok is False
    assert "method mismatch" in hit.reason


def test_legacy_output_without_manifest_and_pdf_is_not_reused(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    folder = _raw_folder(paper_raw)
    legacy = tmp_path / "output" / "legacy"
    _source_output(legacy, PN1)
    cache = MinerUOutputCache(tmp_path / "output" / "mineru_cache", legacy_output_roots=[legacy])

    hit = cache.find(
        folder / f"{PN1}.pdf",
        backend=MINERU_BACKEND,
        method=MINERU_METHOD,
        lang=MINERU_LANG,
        effort=MINERU_EFFORT,
        stem=PN1,
    )

    assert hit.ok is False
    assert "unverifiable legacy output" in hit.reason


def test_legacy_output_with_matching_pdf_hash_is_reused(tmp_path):
    import shutil as _shutil
    paper_raw = tmp_path / "paper_raw"
    folder = _raw_folder(paper_raw)
    legacy_root = tmp_path / "output" / "legacy_uuid"
    legacy_out = _source_output(legacy_root, "oldstem")
    shutil_pdf = legacy_out / "oldstem_origin.pdf"
    shutil_pdf.write_bytes((folder / f"{PN1}.pdf").read_bytes())
    cache = MinerUOutputCache(tmp_path / "output" / "mineru_cache", legacy_output_roots=[legacy_root])

    hit = cache.find(
        folder / f"{PN1}.pdf",
        backend=MINERU_BACKEND,
        method=MINERU_METHOD,
        lang=MINERU_LANG,
        effort=MINERU_EFFORT,
        stem=PN1,
    )

    assert hit.ok is True
    assert hit.markdown_path and hit.markdown_path.name == "oldstem.md"


def test_force_reconvert_bypasses_cache(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    folder = _raw_folder(paper_raw)
    converter, fake, cache = _converter(paper_raw, tmp_path / "output" / "mineru_cache")
    _register_cache(cache, folder / f"{PN1}.pdf", _source_output(tmp_path / "source", PN1), PN1)

    result = converter.convert(PN1, force_reconvert=True)

    assert result["success"] is True
    assert result["restored_from_output_cache"] is False
    assert fake.calls == [PN1]


def test_all_cache_hits_batch_does_not_run_runtime_preflight(monkeypatch, tmp_path):
    paper_raw = tmp_path / "paper_raw"
    folder = _raw_folder(paper_raw)
    cache = MinerUOutputCache(tmp_path / "output" / "mineru_cache", legacy_output_roots=[])
    _register_cache(cache, folder / f"{PN1}.pdf", _source_output(tmp_path / "source", PN1), PN1)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("runtime preflight should not run for all cache hits")

    monkeypatch.setattr("src.mineru_runtime.preflight_gpu", fail_if_called)
    monkeypatch.setattr("src.mineru_runtime.preflight_torch_cuda", fail_if_called)
    monkeypatch.setattr("src.mineru_runtime.preflight_mineru_api", fail_if_called)
    saved = sys.argv
    sys.argv = [
        "convert_paper_raw_batch.py",
        "--paper-raw-dir", str(paper_raw),
        "--all",
        "--apply",
        "--output-cache-dir", str(tmp_path / "output" / "mineru_cache"),
    ]
    try:
        rc = batch.main()
    finally:
        sys.argv = saved

    assert rc == 0


def test_cache_only_miss_never_runs_runtime_preflight(monkeypatch, tmp_path):
    paper_raw = tmp_path / "paper_raw"
    _raw_folder(paper_raw)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("runtime preflight should not run in cache-only mode")

    monkeypatch.setattr("src.mineru_runtime.preflight_gpu", fail_if_called)
    monkeypatch.setattr("src.mineru_runtime.preflight_torch_cuda", fail_if_called)
    monkeypatch.setattr("src.mineru_runtime.preflight_mineru_api", fail_if_called)
    saved = sys.argv
    sys.argv = [
        "convert_paper_raw_batch.py",
        "--paper-raw-dir", str(paper_raw),
        "--all",
        "--apply",
        "--cache-only",
        "--output-cache-dir", str(tmp_path / "output" / "mineru_cache"),
    ]
    try:
        rc = batch.main()
    finally:
        sys.argv = saved

    assert rc == 1


def test_cache_only_cli_conflicts_exit(monkeypatch):
    saved = sys.argv
    sys.argv = ["convert_paper_raw_batch.py", "--all", "--cache-only", "--ignore-output-cache"]
    try:
        with pytest.raises(SystemExit) as exc:
            batch.main()
    finally:
        sys.argv = saved
    assert exc.value.code == 2

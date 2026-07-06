import json
import runpy
import sys
from pathlib import Path

from src.services.v2_library import empty_catalog, empty_metadata


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PN1 = "0000000000000001"
PN2 = "0000000000000002"


def _run_script(script: str, argv: list[str]) -> int:
    saved = sys.argv
    sys.argv = argv
    try:
        runpy.run_path(str(_REPO_ROOT / "scripts" / script), run_name="__main__")
        return 0
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = saved


def _raw_folder(root: Path, source_id: str = PN1, *, doi: str = "10.1000/ok",
                matched: bool = True, pdf_bytes: bytes = b"%PDF") -> Path:
    folder = root / source_id
    folder.mkdir(parents=True)
    metadata = empty_metadata(source_id)
    metadata["title"]["original"] = "Preflight Paper"
    metadata["year"] = 2024
    metadata["authors"] = [{"full_name": "Wang A", "family": "Wang", "given": "A", "orcid": "", "affiliation": ""}]
    metadata["first_author"] = {"family": "Wang", "display": "Wang A"}
    metadata["container"]["journal"] = "Test Journal"
    metadata["identifiers"]["doi"] = doi
    metadata["metadata_match"]["status"] = "matched" if matched else "unmatched"
    (folder / f"{source_id}.metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (folder / f"{source_id}.pdf").write_bytes(pdf_bytes)
    return folder


def _formal_metadata(root: Path, pid: str, *, doi: str, sha: str = "") -> None:
    folder = root / pid
    folder.mkdir(parents=True)
    metadata = empty_metadata(pid)
    metadata["identifiers"]["doi"] = doi
    (folder / f"{pid}.metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (folder / f"{pid}.pdf").write_bytes(b"%PDF-same")


def test_preflight_ready_and_invalid_doi(tmp_path, monkeypatch):
    paper_raw = tmp_path / "paper_raw"
    ready = _raw_folder(paper_raw, PN1, pdf_bytes=b"%PDF-ready")
    bad = _raw_folder(paper_raw, PN2, doi="not-a-doi", pdf_bytes=b"%PDF-bad")
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_script(
        "preflight_paper_raw_import.py",
        ["preflight_paper_raw_import.py", "--all", "--paper-raw-dir", str(paper_raw), "--strict"],
    )

    assert rc == 1
    ready_status = json.loads((ready / ".import_status.json").read_text(encoding="utf-8"))
    bad_status = json.loads((bad / ".import_status.json").read_text(encoding="utf-8"))
    assert ready_status["status"] == "ready_for_convert"
    assert bad_status["status"] == "doi_invalid"


def test_preflight_detects_formal_and_internal_duplicates(tmp_path, monkeypatch):
    paper_raw = tmp_path / "paper_raw"
    first = _raw_folder(paper_raw, PN1, doi="10.1000/dup", pdf_bytes=b"%PDF-same")
    second = _raw_folder(paper_raw, PN2, doi="10.1000/dup", pdf_bytes=b"%PDF-same")
    formal = tmp_path / "papers"
    _formal_metadata(formal, "2024_wang_existing", doi="10.1000/dup")
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_script(
        "preflight_paper_raw_import.py",
        [
            "preflight_paper_raw_import.py",
            "--all",
            "--paper-raw-dir", str(paper_raw),
            "--papers-dir", str(formal),
            "--strict",
        ],
    )

    assert rc == 1
    first_status = json.loads((first / ".import_status.json").read_text(encoding="utf-8"))
    second_status = json.loads((second / ".import_status.json").read_text(encoding="utf-8"))
    assert "doi_duplicate" in first_status["errors"]
    assert "pdf_sha_duplicate" in first_status["errors"]
    assert "pdf_md5_duplicate" in first_status["errors"]
    assert "doi_duplicate" in second_status["errors"]
    assert "pdf_sha_duplicate" in second_status["errors"]
    assert "pdf_md5_duplicate" in second_status["errors"]


def test_convert_only_preflight_ready_skips_nonready(tmp_path, monkeypatch):
    paper_raw = tmp_path / "paper_raw"
    ready = _raw_folder(paper_raw, PN1, pdf_bytes=b"%PDF-ready")
    skipped = _raw_folder(paper_raw, PN2, doi="", matched=False, pdf_bytes=b"%PDF-skip")
    (ready / ".import_status.json").write_text(json.dumps({"status": "ready_for_convert"}), encoding="utf-8")
    (skipped / ".import_status.json").write_text(json.dumps({"status": "doi_invalid"}), encoding="utf-8")
    calls: list[str] = []

    class FakeConverter:
        def __init__(self, paper_raw_dir):
            self.paper_raw_dir = paper_raw_dir

        def inspect_conversion(self, source_id, **kwargs):
            return {
                "state": "not_converted",
                "reason": "not converted",
                "manifest": None,
                "markdown": "",
                "images_dir": "",
                "pdf_sha256": "",
            }

        def convert(self, source_id, **kwargs):
            calls.append(source_id)
            return {"success": True, "paper_number": source_id, "paper_raw_id": source_id}

    import src.services.v2_library as v2_library
    monkeypatch.setattr(v2_library, "PaperRawConverter", FakeConverter)
    monkeypatch.setenv("MINERU_RUNNER", "cli")
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_script(
        "convert_paper_raw_batch.py",
        [
            "convert_paper_raw_batch.py",
            "--all",
            "--paper-raw-dir", str(paper_raw),
            "--only-preflight-ready",
            "--apply",
            "--allow-cpu",
            "--allow-cold-cli-batch",
        ],
    )

    assert rc == 0
    assert calls == [PN1]


def _status_folder(root: Path, source_id: str, status: str, *, doi: str = "") -> Path:
    folder = root / source_id
    folder.mkdir(parents=True)
    metadata = empty_metadata(source_id)
    metadata["identifiers"]["doi"] = doi
    if doi and status == "metadata_matched":
        metadata["metadata_match"]["status"] = "matched"
    (folder / f"{source_id}.metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (folder / f"{source_id}.pdf").write_bytes(b"%PDF")
    (folder / ".import_status.json").write_text(json.dumps({"status": status}), encoding="utf-8")
    return folder


def _run_batch_dryrun(paper_raw: Path, monkeypatch, *extra: str) -> tuple[int, list]:
    import src.services.v2_library as v2_library

    class FakeConverter:
        def __init__(self, paper_raw_dir):
            self.paper_raw_dir = paper_raw_dir

        def inspect_conversion(self, source_id, **kwargs):
            return {
                "state": "not_converted",
                "reason": "not converted",
                "manifest": None,
                "markdown": "",
                "images_dir": "",
                "pdf_sha256": "",
            }

        def convert(self, source_id, **kwargs):
            raise AssertionError("convert should not run in dry-run")

    monkeypatch.setattr(v2_library, "PaperRawConverter", FakeConverter)
    monkeypatch.setenv("MINERU_RUNNER", "cli")
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    argv = ["convert_paper_raw_batch.py", "--all", "--paper-raw-dir", str(paper_raw), "--dry-run"]
    argv.extend(extra)
    rc = _run_script("convert_paper_raw_batch.py", argv)
    return rc


def test_convert_report_metadata_fields(tmp_path, monkeypatch, capsys):
    paper_raw = tmp_path / "paper_raw"
    _status_folder(paper_raw, PN1, "doi_invalid", doi="")
    _status_folder(paper_raw, PN2, "metadata_matched", doi="10.1000/ok")

    rc = _run_batch_dryrun(paper_raw, monkeypatch)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    items = {i["paper_number"]: i for i in payload["items"]}

    # doi_invalid / empty-DOI workspace is still planned for conversion when
    # the staging-created metadata shell exists.
    assert items[PN1]["status"] == "planned"
    assert items[PN1]["metadata_required_for_conversion"] is True
    assert items[PN1]["metadata_ready_for_commit"] is False
    assert items[PN1]["post_conversion_metadata_resolution_recommended"] is True
    assert items[PN1]["import_status"] == "doi_invalid"

    # metadata_matched workspace is also planned, but no post-convert resolve needed.
    assert items[PN2]["status"] == "planned"
    assert items[PN2]["metadata_required_for_conversion"] is True
    assert items[PN2]["metadata_ready_for_commit"] is True
    assert items[PN2]["post_conversion_metadata_resolution_recommended"] is False


def test_only_convertible_skips_commit_stage_workspace(tmp_path, monkeypatch, capsys):
    paper_raw = tmp_path / "paper_raw"
    _status_folder(paper_raw, PN1, "ready_for_commit", doi="10.1000/ok")
    _status_folder(paper_raw, PN2, "doi_invalid", doi="")

    rc = _run_batch_dryrun(paper_raw, monkeypatch, "--only-convertible")
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    items = {i["paper_number"]: i for i in payload["items"]}

    # ready_for_commit is past conversion stage -> skipped, not convertible.
    assert items[PN1]["status"] == "skipped"
    assert "not convertible" in items[PN1]["reason"]
    # doi_invalid bootstrap workspace is still convertible under --only-convertible.
    assert items[PN2]["status"] == "planned"


def _status_folder_no_pdf(root: Path, source_id: str, status: str) -> Path:
    """A workspace with import_status + metadata but NO PDF."""
    folder = root / source_id
    folder.mkdir(parents=True)
    metadata = empty_metadata(source_id)
    (folder / f"{source_id}.metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (folder / ".import_status.json").write_text(json.dumps({"status": status}), encoding="utf-8")
    return folder


def test_only_convertible_metadata_missing_statuses_still_planned(tmp_path, monkeypatch, capsys):
    """Missing/unmatched metadata must NOT block conversion (only PDF absence does)."""
    paper_raw = tmp_path / "paper_raw"
    _status_folder(paper_raw, "0000000000000101", "doi_invalid", doi="")
    _status_folder(paper_raw, "0000000000000102", "metadata_resolve_failed", doi="")
    _status_folder(paper_raw, "0000000000000103", "metadata_manual_review_required", doi="")

    rc = _run_batch_dryrun(paper_raw, monkeypatch, "--only-convertible")
    assert rc == 0
    items = {i["paper_number"]: i for i in json.loads(capsys.readouterr().out)["items"]}
    for sid in ("0000000000000101", "0000000000000102", "0000000000000103"):
        assert items[sid]["status"] == "planned", f"{sid} should be planned"
        assert items[sid]["has_pdf"] is True


def test_only_convertible_skips_missing_pdf(tmp_path, monkeypatch, capsys):
    """A workspace without a PDF must be skipped, regardless of metadata state."""
    paper_raw = tmp_path / "paper_raw"
    _status_folder_no_pdf(paper_raw, PN1, "doi_invalid")

    rc = _run_batch_dryrun(paper_raw, monkeypatch, "--only-convertible")
    assert rc == 0
    items = {i["paper_number"]: i for i in json.loads(capsys.readouterr().out)["items"]}
    assert items[PN1]["status"] == "skipped"
    assert items[PN1]["has_pdf"] is False
    assert "missing paper_raw PDF" in items[PN1]["reason"]


def test_readiness_blocks_converted_but_unmatched_workspace(tmp_path):
    """Even when the PDF is already converted to Markdown, formalize/commit
    stays blocked until metadata is matched/manual_confirmed. Conversion-without-
    metadata is allowed; formal ingestion is not."""
    from src.services.v2_library import assess_paper_raw_commit_readiness

    paper_raw = tmp_path / "paper_raw"
    folder = _raw_folder(paper_raw, PN1, doi="10.1000/ok", matched=False)
    # Simulate already-converted assets (md + images present).
    (folder / f"{PN1}.md").write_text("# Converted", encoding="utf-8")
    (folder / "images").mkdir()
    # A minimal content-only catalog so the gate reaches the metadata check.
    catalog = empty_catalog()
    catalog["library_locator"].update({"paper_number": PN1, "paper_id": PN1})
    catalog["content_identity"].update({
        "content_title_zh": "测试标题",
        "content_title_original": "Converted",
        "content_title_original_candidates": ["Converted"],
        "content_language": "en",
        "document_type": "article",
    })
    catalog["classification"]["primary_domain"] = "test"
    catalog["terminology"] = [{"term_original": "converted", "term_zh": "转换"}]
    catalog["screening"].update({"read_decision": "pending", "reason_zh": "用于预检"})
    catalog["writing_value"]["short_summary"] = "测试摘要"
    catalog["provenance"].update({"generated_at": "2026-07-03T00:00:00", "generator": "test"})
    (folder / f"{PN1}.catalog.json").write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")

    readiness = assess_paper_raw_commit_readiness(
        folder,
        file_prefix=PN1,
        papers_dir=tmp_path / "papers",
        check_duplicates=True,
    )

    assert readiness["ready"] is False
    assert any("metadata_match.status" in err for err in readiness["errors"])
    assert readiness["metadata_layered_hint"] is not None
    assert "formalize/commit is blocked" in readiness["metadata_layered_hint"]

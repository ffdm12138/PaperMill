import json
import runpy
import sys
from pathlib import Path

from src.services.v2_library import empty_metadata


_REPO_ROOT = Path(__file__).resolve().parent.parent
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
    metadata["pdf"]["sha256"] = sha
    (folder / f"{pid}.metadata.json").write_text(json.dumps(metadata), encoding="utf-8")


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
    assert "doi_duplicate" in second_status["errors"]
    assert "pdf_sha_duplicate" in second_status["errors"]


def test_convert_only_preflight_ready_skips_nonready(tmp_path, monkeypatch):
    paper_raw = tmp_path / "paper_raw"
    ready = paper_raw / PN1
    skipped = paper_raw / PN2
    ready.mkdir(parents=True)
    skipped.mkdir(parents=True)
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

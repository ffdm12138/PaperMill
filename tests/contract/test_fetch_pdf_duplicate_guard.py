import json
import runpy
import sys
from pathlib import Path

from src.fetch.models import FetchResult
from src.services.v2_library import empty_metadata


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PN1 = "0000000000000001"
PN2 = "0000000000000002"


def _run_fetch(argv: list[str]) -> int:
    saved = sys.argv
    sys.argv = argv
    try:
        runpy.run_path(str(_REPO_ROOT / "scripts" / "fetch_pdf_for_paper_raw.py"), run_name="__main__")
        return 0
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = saved


def _raw_folder(root: Path, paper_number: str, *, doi: str, pdf_bytes: bytes | None = None) -> Path:
    folder = root / paper_number
    folder.mkdir(parents=True)
    meta = empty_metadata(paper_number, source_type="network_search")
    meta["identifiers"]["doi"] = doi
    meta["title"]["original"] = "Fetch Test"
    meta["year"] = 2024
    (folder / f"{paper_number}.metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    if pdf_bytes is not None:
        (folder / f"{paper_number}.pdf").write_bytes(pdf_bytes)
    return folder


def test_fetch_duplicate_pdf_blocks_attach_and_cleans_fetch_dir(tmp_path, monkeypatch):
    paper_raw = tmp_path / "paper_raw"
    folder = _raw_folder(paper_raw, PN1, doi="10.1000/fetch")
    _raw_folder(paper_raw, PN2, doi="10.1000/other", pdf_bytes=b"%PDF fetched duplicate")

    def fake_fetch_pdf(doi, domain_id=None, output_root=None, **kwargs):
        output = Path(output_root) / "download.pdf"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"%PDF fetched duplicate")
        return FetchResult(doi=doi, success=True, output_path=str(output), pdf_url="https://example.test/p.pdf")

    import src.fetch.fetch_pipeline as fetch_pipeline
    monkeypatch.setattr(fetch_pipeline, "fetch_pdf", fake_fetch_pdf)
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_fetch([
        "fetch_pdf_for_paper_raw.py",
        "--paper-number", PN1,
        "--paper-raw-dir", str(paper_raw),
        "--papers-dir", str(tmp_path / "papers"),
        "--apply",
    ])

    assert rc == 1
    assert not (folder / ".fetch").exists()
    assert not (folder / f"{PN1}.pdf").exists()
    status = json.loads((folder / ".import_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "duplicate"
    metadata = json.loads((folder / f"{PN1}.metadata.json").read_text(encoding="utf-8"))
    assert metadata.get("pdf", {}).get("sha256", "") == ""
    assert metadata.get("pdf", {}).get("md5", "") == ""

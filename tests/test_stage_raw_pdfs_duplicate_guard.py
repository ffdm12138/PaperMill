import json
import runpy
import sys
from pathlib import Path

from src.services.v2_library import empty_metadata


_REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_stage(argv: list[str]) -> int:
    saved = sys.argv
    sys.argv = argv
    try:
        runpy.run_path(str(_REPO_ROOT / "scripts" / "stage_raw_pdfs_to_paper_raw.py"), run_name="__main__")
        return 0
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = saved


def _formal_pdf(root: Path, pdf_bytes: bytes) -> None:
    folder = root / "2024_wang_existing"
    folder.mkdir(parents=True)
    (folder / "2024_wang_existing.pdf").write_bytes(pdf_bytes)
    meta = empty_metadata("2024_wang_existing")
    meta["identifiers"]["doi"] = "10.1000/existing"
    (folder / "2024_wang_existing.metadata.json").write_text(json.dumps(meta), encoding="utf-8")


def test_stage_blocks_pdf_duplicate_in_formal_and_keeps_raw_on_move(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    pdf = raw / "paper.pdf"
    pdf.write_bytes(b"%PDF same")
    papers = tmp_path / "papers"
    _formal_pdf(papers, b"%PDF same")
    report = tmp_path / "report.json"
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_stage([
        "stage_raw_pdfs_to_paper_raw.py",
        "--raw-dir", str(raw),
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--papers-dir", str(papers),
        "--ledger-path", str(tmp_path / "catalog" / "ledger.json"),
        "--report", str(report),
        "--move",
        "--apply",
    ])

    assert rc == 1
    assert pdf.exists()
    assert not (tmp_path / "paper_raw" / "0000000000000001").exists()
    item = json.loads(report.read_text(encoding="utf-8"))[0]
    assert item["status"] == "duplicate"
    assert "pdf_sha256_duplicate" in item["duplicate_reasons"]


def test_stage_blocks_batch_pdf_duplicate_and_stages_only_first(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "a.pdf").write_bytes(b"%PDF batch")
    (raw / "b.pdf").write_bytes(b"%PDF batch")
    paper_raw = tmp_path / "paper_raw"
    report = tmp_path / "report.json"
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_stage([
        "stage_raw_pdfs_to_paper_raw.py",
        "--raw-dir", str(raw),
        "--paper-raw-dir", str(paper_raw),
        "--papers-dir", str(tmp_path / "papers"),
        "--ledger-path", str(tmp_path / "catalog" / "ledger.json"),
        "--report", str(report),
        "--apply",
    ])

    assert rc == 1
    assert (paper_raw / "0000000000000001" / "0000000000000001.pdf").exists()
    assert not (paper_raw / "0000000000000002").exists()
    items = json.loads(report.read_text(encoding="utf-8"))
    assert items[0]["status"] == "staged"
    assert items[1]["status"] == "duplicate"
    assert "batch_pdf_duplicate" in items[1]["duplicate_reasons"]


def test_legacy_paper_raw_workspace_blocks_restage(tmp_path, monkeypatch):
    """A legacy/untitled paper_raw workspace must block re-staging the same PDF
    instead of allowing a new 16-digit numbered workspace to be created."""
    from tests.helpers.paper_raw_factory import make_legacy_workspace

    raw = tmp_path / "raw"
    raw.mkdir()
    pdf = raw / "paper.pdf"
    legacy_pdf_bytes = b"%PDF legacy bytes"
    pdf.write_bytes(legacy_pdf_bytes)
    # pre-existing legacy workspace containing the same PDF
    make_legacy_workspace(tmp_path, folder_name="1979_sykest_untitled",
                          paper_number="0000000000000157", pdf_bytes=legacy_pdf_bytes, doi="10.1/legacy")
    papers = tmp_path / "papers"
    report = tmp_path / "report.json"
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_stage([
        "stage_raw_pdfs_to_paper_raw.py",
        "--raw-dir", str(raw),
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--papers-dir", str(papers),
        "--ledger-path", str(tmp_path / "catalog" / "ledger.json"),
        "--report", str(report),
        "--move",
        "--apply",
    ])

    assert rc == 1
    assert pdf.exists(), "raw PDF must NOT be moved since staging was blocked"
    # no new numbered workspace should have been created
    paper_raw = tmp_path / "paper_raw"
    assert not any(p.name.isdigit() and len(p.name) == 16 for p in paper_raw.iterdir() if p.is_dir())
    item = json.loads(report.read_text(encoding="utf-8"))[0]
    assert item["status"] == "duplicate"
    assert "pdf_sha256_duplicate" in item["duplicate_reasons"]

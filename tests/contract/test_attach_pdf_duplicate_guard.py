import json
import runpy
import sys
from pathlib import Path

from src.ingest.paper_raw import PaperRawAllocator
from src.metadata.schema import empty_metadata


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PN1 = "0000000000000001"
PN2 = "0000000000000002"


def _run_attach(argv: list[str]) -> int:
    saved = sys.argv
    sys.argv = argv
    try:
        runpy.run_path(str(_REPO_ROOT / "scripts" / "attach_pdf_to_paper_raw.py"), run_name="__main__")
        return 0
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = saved


def _raw_folder(root: Path, paper_number: str, pdf_bytes: bytes | None = None) -> Path:
    folder = root / paper_number
    folder.mkdir(parents=True)
    meta = empty_metadata(paper_number)
    (folder / f"{paper_number}.metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    if pdf_bytes is not None:
        (folder / f"{paper_number}.pdf").write_bytes(pdf_bytes)
    return folder


def test_attach_blocks_duplicate_from_other_paper_raw_and_keeps_source_on_move(tmp_path, monkeypatch, capsys):
    paper_raw = tmp_path / "paper_raw"
    _raw_folder(paper_raw, PN1)
    _raw_folder(paper_raw, PN2, b"%PDF duplicate")
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF duplicate")
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_attach([
        "attach_pdf_to_paper_raw.py",
        str(source),
        "--paper-number", PN1,
        "--paper-raw-dir", str(paper_raw),
        "--papers-dir", str(tmp_path / "papers"),
        "--move",
        "--apply",
    ])

    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["status"] == "duplicate"
    assert source.exists()
    assert not (paper_raw / PN1 / f"{PN1}.pdf").exists()


def test_attach_allows_same_content_from_current_paper_raw_self(tmp_path, monkeypatch):
    paper_raw = tmp_path / "paper_raw"
    _raw_folder(paper_raw, PN1, b"%PDF self")
    source = tmp_path / "same.pdf"
    source.write_bytes(b"%PDF self")
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_attach([
        "attach_pdf_to_paper_raw.py",
        str(source),
        "--paper-number", PN1,
        "--paper-raw-dir", str(paper_raw),
        "--papers-dir", str(tmp_path / "papers"),
        "--apply",
    ])

    assert rc == 0


def test_attach_existing_pdf_without_replace_fails_and_keeps_move_source(tmp_path, monkeypatch, capsys):
    paper_raw = tmp_path / "paper_raw"
    folder = _raw_folder(paper_raw, PN1, b"%PDF existing")
    source = tmp_path / "new.pdf"
    source.write_bytes(b"%PDF new")
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_attach([
        "attach_pdf_to_paper_raw.py",
        str(source),
        "--paper-number", PN1,
        "--paper-raw-dir", str(paper_raw),
        "--papers-dir", str(tmp_path / "papers"),
        "--move",
        "--apply",
    ])

    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["status"] == "failed"
    assert "PDF already exists" in out["error"]
    assert source.exists()
    assert (folder / f"{PN1}.pdf").read_bytes() == b"%PDF existing"


def test_attach_replace_rolls_back_existing_pdf_if_copy_fails(tmp_path, monkeypatch):
    paper_raw = tmp_path / "paper_raw"
    folder = _raw_folder(paper_raw, PN1, b"%PDF existing")
    source = tmp_path / "new.pdf"
    source.write_bytes(b"%PDF new")

    def fail_copy(*args, **kwargs):
        raise RuntimeError("copy failed")

    monkeypatch.setattr("src.ingest.paper_raw.shutil.copy2", fail_copy)

    try:
        PaperRawAllocator(paper_raw, papers_dir=tmp_path / "papers").attach_pdf(
            PN1,
            source,
            replace=True,
        )
    except RuntimeError as exc:
        assert "copy failed" in str(exc)
    else:
        raise AssertionError("replace copy failure should be surfaced")

    assert source.exists()
    assert (folder / f"{PN1}.pdf").read_bytes() == b"%PDF existing"
    assert not (folder / f"{PN1}.pdf.replace.tmp").exists()

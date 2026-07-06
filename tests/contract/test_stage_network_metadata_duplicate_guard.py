import json
import runpy
import sys
from pathlib import Path

from src.services.v2_library import empty_metadata


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _run_script(argv: list[str]) -> int:
    saved = sys.argv
    sys.argv = argv
    try:
        runpy.run_path(str(_REPO_ROOT / "scripts" / "stage_network_metadata_to_paper_raw.py"), run_name="__main__")
        return 0
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = saved


def _formal_doi(root: Path, doi: str) -> None:
    folder = root / "2024_wang_existing"
    folder.mkdir(parents=True)
    meta = empty_metadata("2024_wang_existing")
    meta["identifiers"]["doi"] = doi
    (folder / "2024_wang_existing.metadata.json").write_text(json.dumps(meta), encoding="utf-8")


def test_network_metadata_duplicate_formal_doi_does_not_allocate(tmp_path, monkeypatch):
    input_path = tmp_path / "items.jsonl"
    input_path.write_text(json.dumps({"title": "Dup", "year": 2024, "doi": "10.1000/dup"}) + "\n", encoding="utf-8")
    papers = tmp_path / "papers"
    _formal_doi(papers, "10.1000/dup")
    report = tmp_path / "report.json"
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_script([
        "stage_network_metadata_to_paper_raw.py",
        "--input", str(input_path),
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--papers-dir", str(papers),
        "--ledger-path", str(tmp_path / "catalog" / "ledger.json"),
        "--report", str(report),
        "--apply",
    ])

    assert rc == 1
    assert not (tmp_path / "paper_raw" / "0000000000000001").exists()
    report_obj = json.loads(report.read_text(encoding="utf-8"))
    assert report_obj["applied"] is True
    item = report_obj["items"][0]
    assert item["status"] == "duplicate"
    assert item["error"] == "doi_duplicate"


def test_network_metadata_skip_duplicates_stages_unique_record(tmp_path, monkeypatch):
    input_path = tmp_path / "items.jsonl"
    input_path.write_text(
        json.dumps({"title": "Dup", "year": 2024, "doi": "10.1000/dup"}) + "\n"
        + json.dumps({"title": "Unique", "year": 2024, "doi": "10.1000/unique"}) + "\n",
        encoding="utf-8",
    )
    papers = tmp_path / "papers"
    _formal_doi(papers, "10.1000/dup")
    report = tmp_path / "report.json"
    paper_raw = tmp_path / "paper_raw"
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_script([
        "stage_network_metadata_to_paper_raw.py",
        "--input", str(input_path),
        "--paper-raw-dir", str(paper_raw),
        "--papers-dir", str(papers),
        "--ledger-path", str(tmp_path / "catalog" / "ledger.json"),
        "--report", str(report),
        "--skip-duplicates",
        "--apply",
    ])

    assert rc == 0
    report_obj = json.loads(report.read_text(encoding="utf-8"))
    assert [item["status"] for item in report_obj["items"]] == ["duplicate", "staged"]
    assert (paper_raw / "0000000000000001" / "0000000000000001.metadata.json").exists()


def test_network_metadata_batch_duplicate_doi(tmp_path, monkeypatch):
    input_path = tmp_path / "items.jsonl"
    input_path.write_text(
        json.dumps({"title": "A", "year": 2024, "doi": "10.1000/same"}) + "\n"
        + json.dumps({"title": "B", "year": 2024, "doi": "10.1000/same"}) + "\n",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_script([
        "stage_network_metadata_to_paper_raw.py",
        "--input", str(input_path),
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--papers-dir", str(tmp_path / "papers"),
        "--ledger-path", str(tmp_path / "catalog" / "ledger.json"),
        "--report", str(report),
        "--apply",
    ])

    assert rc == 1
    report_obj = json.loads(report.read_text(encoding="utf-8"))
    items = report_obj["items"]
    assert items[0]["status"] == "staged"
    assert items[1]["status"] == "duplicate"
    assert "batch_doi_duplicate" in items[1]["duplicate_reasons"]

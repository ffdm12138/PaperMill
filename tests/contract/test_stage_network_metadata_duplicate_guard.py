import json
import runpy
import sys
from pathlib import Path

from src.metadata.schema import empty_metadata
from src.staging.network_metadata_staging import stage_network_metadata_records


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
    from tests.factories.paper_raw_factory import create_active_formal_workspace
    create_active_formal_workspace(root, doi=doi)


def _paper_raw_doi(root: Path, number: str, doi: str) -> None:
    folder = root / number
    folder.mkdir(parents=True)
    meta = empty_metadata(number, source_type="network_search")
    meta["identifiers"]["doi"] = doi
    (folder / f"{number}.metadata.json").write_text(json.dumps(meta), encoding="utf-8")


def test_network_metadata_duplicate_formal_doi_does_not_allocate(tmp_path, monkeypatch):
    input_path = tmp_path / "items.jsonl"
    input_path.write_text(json.dumps({"title": "Dup", "year": 2024, "doi": "10.1000/dup"}) + "\n", encoding="utf-8")
    papers = tmp_path / "papers"
    _formal_doi(tmp_path, "10.1000/dup")
    report = tmp_path / "report.json"
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_script([
        "stage_network_metadata_to_paper_raw.py",
        "--input", str(input_path),
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--papers-dir", str(papers),
        "--ledger-path", str(tmp_path / "ledger.json"),
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
    _formal_doi(tmp_path, "10.1000/dup")
    report = tmp_path / "report.json"
    paper_raw = tmp_path / "paper_raw"
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    rc = _run_script([
        "stage_network_metadata_to_paper_raw.py",
        "--input", str(input_path),
        "--paper-raw-dir", str(paper_raw),
        "--papers-dir", str(papers),
        "--ledger-path", str(tmp_path / "ledger.json"),
        "--report", str(report),
        "--skip-duplicates",
        "--apply",
    ])

    assert rc == 0
    report_obj = json.loads(report.read_text(encoding="utf-8"))
    assert [item["status"] for item in report_obj["items"]] == ["duplicate", "staged"]
    assert (paper_raw / "0000000000000002" / "0000000000000002.metadata.json").exists()


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


def test_stage_network_metadata_rejects_existing_paper_raw_doi(tmp_path):
    from tests.factories.paper_raw_factory import create_network_metadata_workspace
    create_network_metadata_workspace(tmp_path, doi="10.1000/existing")
    paper_raw = tmp_path / "paper_raw"

    report = stage_network_metadata_records(
        [{"title": "Dup", "year": 2024, "doi": "10.1000/existing"}],
        paper_raw_dir=paper_raw,
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
        apply=True,
    )

    assert report["items"][0]["status"] == "duplicate"
    assert not (paper_raw / "0000000000000002").exists()
    assert not (tmp_path / "catalog" / "ledger.json").exists()


def test_apply_mode_never_outputs_stale_planned_number(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("peek_next_numbers must not run in apply mode")

    monkeypatch.setattr("src.staging.network_metadata_staging.PaperNumberLedger.peek_next_numbers", boom)

    report = stage_network_metadata_records(
        [{"title": "Unique", "year": 2024, "doi": "10.1000/unique"}],
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "catalog" / "ledger.json",
        apply=True,
    )

    item = report["items"][0]
    assert item["status"] == "staged"
    assert item["paper_number"] == "0000000000000001"
    assert "planned_paper_number" not in item
    assert "dry_run_planned_paper_number" not in item
    assert item["actual_allocated"] is True

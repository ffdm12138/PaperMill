from __future__ import annotations
import pytest
pytestmark = pytest.mark.legacy

import json
import runpy
import sys
from pathlib import Path

from src.services.v2_library import empty_catalog, empty_metadata, write_conversion_manifest_for_existing_assets


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(argv: list[str]) -> int:
    saved = sys.argv
    sys.argv = argv
    try:
        runpy.run_path(str(_REPO_ROOT / "scripts" / "legacy" / "migrate_paper_raw_6digit_to_paper_number.py"), run_name="__main__")
        return 0
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = saved


def _legacy_folder(root: Path) -> Path:
    folder = root / "paper_raw" / "000001"
    folder.mkdir(parents=True)
    metadata = empty_metadata("000001")
    metadata["identifiers"]["doi"] = "10.1/migrate"
    (folder / "000001.metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    catalog = empty_catalog()
    (folder / "000001.catalog.json").write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    (folder / "000001.pdf").write_bytes(b"%PDF")
    (folder / "000001.md").write_text("# migrate", encoding="utf-8")
    (folder / "images").mkdir()
    write_conversion_manifest_for_existing_assets(folder, "000001")
    (folder / "stage_manifest.json").write_text(json.dumps({"source_id": "000001"}), encoding="utf-8")
    return folder


def test_migrate_legacy_6digit_paper_raw_to_paper_number(tmp_path, monkeypatch):
    legacy = _legacy_folder(tmp_path)
    paper_raw = tmp_path / "paper_raw"
    ledger = tmp_path / "catalog" / "paper_number_ledger.json"
    report = tmp_path / "report.json"
    monkeypatch.syspath_prepend(str(_REPO_ROOT))

    dry = _run([
        "legacy/migrate_paper_raw_6digit_to_paper_number.py",
        "--paper-raw-dir", str(paper_raw),
        "--ledger-path", str(ledger),
        "--report", str(report),
    ])

    assert dry == 0
    assert legacy.exists()
    assert not ledger.exists()
    dry_report = json.loads(report.read_text(encoding="utf-8"))
    assert dry_report[0]["paper_number"] == "0000000000000001"

    rc = _run([
        "legacy/migrate_paper_raw_6digit_to_paper_number.py",
        "--paper-raw-dir", str(paper_raw),
        "--ledger-path", str(ledger),
        "--apply",
    ])

    assert rc == 0
    target = paper_raw / "0000000000000001"
    assert target.exists()
    assert not legacy.exists()
    for suffix in ("metadata.json", "catalog.json", "md", "pdf", "conversion.json"):
        assert (target / f"0000000000000001.{suffix}").exists()
    metadata = json.loads((target / "0000000000000001.metadata.json").read_text(encoding="utf-8"))
    assert metadata["source_id"] == "0000000000000001"
    assert metadata["paper_number"] == "0000000000000001"
    assert metadata["source"]["legacy_source_id"] == "000001"
    stage = json.loads((target / "stage_manifest.json").read_text(encoding="utf-8"))
    assert stage["legacy_source_id"] == "000001"
    ledger_data = json.loads(ledger.read_text(encoding="utf-8"))
    item = ledger_data["items"]["0000000000000001"]
    assert item["state"] == "reserved"
    assert item["folder_name"] == "0000000000000001"

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

from src.services.v2_library import empty_catalog


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _run(argv: list[str]) -> int:
    saved = sys.argv
    sys.argv = argv
    try:
        runpy.run_path(str(_REPO_ROOT / "scripts" / "repair_catalog_asset_refs.py"), run_name="__main__")
        return 0
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = saved


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _catalog(number: str, pid: str, *, markdown_path: str) -> dict:
    catalog = empty_catalog()
    catalog["library_locator"]["paper_number"] = number
    catalog["library_locator"]["paper_id"] = pid
    catalog["library_locator"]["asset_refs"] = {
        "markdown": markdown_path,
        "pdf": markdown_path.replace(".md", ".pdf"),
        "metadata": markdown_path.replace(".md", ".metadata.json"),
        "catalog": markdown_path.replace(".md", ".catalog.json"),
        "asset_manifest": markdown_path.replace(".md", ".asset_manifest.json"),
        "images_dir": "images/",
    }
    catalog["provenance"]["markdown_path"] = markdown_path
    return catalog


def test_repair_fixes_stale_papers_catalog(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(_REPO_ROOT))
    number = "0000000000000027"
    pid = "2024_wang_repair"
    folder = tmp_path / "papers" / pid
    (folder / "images").mkdir(parents=True)
    for suffix in ("md", "pdf", "metadata.json"):
        (folder / f"{pid}.{suffix}").write_text("x", encoding="utf-8")
    marker_name = number + ".paper.number"
    (folder / marker_name).write_text(number, encoding="utf-8")
    _write_json(folder / f"{pid}.catalog.json", _catalog(number, pid, markdown_path=f"{number}.md"))
    report = tmp_path / "report.json"

    rc = _run([
        "repair_catalog_asset_refs.py",
        "--papers-dir", str(tmp_path / "papers"),
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--apply",
        "--report", str(report),
    ])

    assert rc == 0
    repaired = json.loads((folder / f"{pid}.catalog.json").read_text(encoding="utf-8"))
    assert repaired["library_locator"]["asset_refs"]["markdown"] == f"{pid}.md"
    assert repaired["provenance"]["markdown_path"] == f"{pid}.md"
    assert repaired["provenance"]["original_markdown_path"] == f"{number}.md"
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["summary"]["repaired"] == 1


def test_repair_leaves_valid_numbered_paper_raw_staging(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(_REPO_ROOT))
    number = "0000000000000027"
    folder = tmp_path / "paper_raw" / number
    (folder / "images").mkdir(parents=True)
    _write_json(folder / f"{number}.catalog.json", _catalog(number, "", markdown_path=f"{number}.md"))

    rc = _run([
        "repair_catalog_asset_refs.py",
        "--papers-dir", str(tmp_path / "papers"),
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--apply",
    ])

    assert rc == 0
    catalog = json.loads((folder / f"{number}.catalog.json").read_text(encoding="utf-8"))
    assert catalog["library_locator"]["asset_refs"]["markdown"] == f"{number}.md"
    assert catalog["provenance"]["markdown_path"] == f"{number}.md"


def test_repair_fixes_formalized_paper_raw(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(_REPO_ROOT))
    number = "0000000000000027"
    pid = "2024_wang_formalized"
    folder = tmp_path / "paper_raw" / pid
    (folder / "images").mkdir(parents=True)
    _write_json(folder / f"{pid}.catalog.json", _catalog(number, pid, markdown_path=f"{number}.md"))

    rc = _run([
        "repair_catalog_asset_refs.py",
        "--papers-dir", str(tmp_path / "papers"),
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--apply",
    ])

    assert rc == 0
    catalog = json.loads((folder / f"{pid}.catalog.json").read_text(encoding="utf-8"))
    assert catalog["library_locator"]["asset_refs"]["markdown"] == f"{pid}.md"
    assert catalog["provenance"]["markdown_path"] == f"{pid}.md"

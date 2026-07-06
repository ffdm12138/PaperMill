from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.services.v2_library import V2PaperCommitService, empty_catalog, empty_metadata
from scripts.commit_paper_raw_to_papers import _ready_dirs


def _raw_folder(tmp_path: Path, pid: str = "2024_wang_鎵嬪姩瀵煎叆") -> Path:
    from tests.helpers.paper_raw_factory import make_staged_source

    parts = pid.split("_")
    short_name = "_".join(parts[2:]) or "鎵嬪姩瀵煎叆"
    family = parts[1] if len(parts) > 1 else "wang"
    paper_raw = tmp_path / "paper_raw"
    used = {p.name for p in paper_raw.iterdir()} if paper_raw.exists() else set()
    source_id = next(f"{i:016d}" for i in range(1, 1000) if f"{i:016d}" not in used)
    return make_staged_source(
        tmp_path,
        source_id,
        title_zh=short_name,
        title_original="Manual Import Paper",
        doi="10.1000/manual-import",
        family=family,
        journal="Manual Journal",
        metadata_status="manual_confirmed",
        catalog_domain="snow",
    )


def _commit(tmp_path: Path, folder: Path) -> dict:
    # commit now requires a formalized folder; formalize is the readiness gate.
    from src.services.paper_raw_formalizer import PaperRawFormalizationService

    formalized = PaperRawFormalizationService(
        paper_raw_dir=folder.parent, papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
    ).formalize(folder)
    if not formalized.get("success"):
        return formalized
    return V2PaperCommitService(
        papers_dir=tmp_path / "papers",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
    ).commit_paper_raw(formalized["folder"])


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda m: m["identifiers"].update({"doi": ""}), "metadata.identifiers.doi is required"),
        (lambda m: m["title"].update({"original": ""}), "metadata.title.original is required"),
        (lambda m: m.update({"authors": []}), "metadata.authors must contain at least one author"),
        (lambda m: m.update({"year": ""}), "metadata.year is required"),
        (lambda m: m["container"].update({"journal": "", "conference": "", "booktitle": ""}), "metadata.container.journal"),
    ],
)
def test_incomplete_manual_import_metadata_cannot_commit(tmp_path, mutate, expected):
    folder = _raw_folder(tmp_path)
    meta_path = folder / f"{folder.name}.metadata.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    mutate(metadata)
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")

    result = _commit(tmp_path, folder)

    assert result["success"] is False
    assert result["status"] == "metadata_incomplete"
    assert any(expected in err for err in result["errors"])
    assert not (tmp_path / "papers" / folder.name).exists()


def test_catalog_draft_does_not_bypass_metadata_gate(tmp_path):
    folder = _raw_folder(tmp_path)
    meta_path = folder / f"{folder.name}.metadata.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    metadata["identifiers"]["doi"] = ""
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    assert (folder / f"{folder.name}.catalog.json").exists()

    result = _commit(tmp_path, folder)

    assert result["success"] is False
    assert result["status"] == "metadata_incomplete"


def test_commit_rejects_paper_id_mismatch(tmp_path):
    folder = _raw_folder(tmp_path, "2024_wang_手动导入")
    catalog_path = folder / f"{folder.name}.catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["content_identity"]["content_title_zh"] = "不同名称"
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")

    from src.services.paper_raw_formalizer import PaperRawFormalizationService

    result = PaperRawFormalizationService(
        paper_raw_dir=folder.parent,
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
    ).formalize(folder, paper_id="2024_wang_手动导入")

    assert result["success"] is False
    assert result["status"] == "paper_id_mismatch"
    assert any("paper_id mismatch" in err for err in result["errors"])
    assert not (tmp_path / "papers" / folder.name).exists()


def test_catalog_invalid_type_cannot_commit(tmp_path):
    folder = _raw_folder(tmp_path)
    catalog_path = folder / f"{folder.name}.catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["evidence_profile"]["important_tables"] = {"table": "not a list"}
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")

    result = _commit(tmp_path, folder)

    assert result["success"] is False
    assert result["status"] == "catalog_invalid"
    assert any("important_tables must be a list" in err for err in result["errors"])


def test_all_ready_requires_ready_for_commit_status(tmp_path):
    raw = tmp_path / "paper_raw"
    ledger_path = tmp_path / "catalog" / "paper_number_ledger.json"
    folder = raw / "2024_wang_状态门禁"
    folder.mkdir(parents=True)
    for suffix in ("metadata.json", "catalog.json", "md", "pdf"):
        (folder / f"{folder.name}.{suffix}").write_text("{}", encoding="utf-8")
    (folder / "images").mkdir()
    (folder / ".import_status.json").write_text(json.dumps({"status": "metadata_incomplete"}), encoding="utf-8")

    assert folder not in _ready_dirs(raw, ledger_path)

    # commit also requires formalize outputs (formalization.json + paper.number marker)
    (folder / ".import_status.json").write_text(json.dumps({"status": "ready_for_commit"}), encoding="utf-8")
    assert folder not in _ready_dirs(raw, ledger_path)  # still missing formalization.json + marker

    (folder / f"{folder.name}.formalization.json").write_text("{}", encoding="utf-8")
    (folder / "0000000000000001.paper.number").write_text("{}", encoding="utf-8")
    assert folder in _ready_dirs(raw, ledger_path)


def test_manual_confirmed_complete_metadata_can_commit(tmp_path):
    folder = _raw_folder(tmp_path)

    result = _commit(tmp_path, folder)

    assert result["success"] is True
    assert result["status"] == "imported"
    assert (tmp_path / "papers" / "2024_wang_鎵嬪姩瀵煎叆").exists()


def test_duplicate_doi_cannot_commit(tmp_path):
    first = _raw_folder(tmp_path, "2024_wang_第一篇")
    second = _raw_folder(tmp_path, "2024_wang_第二篇")
    first_result = _commit(tmp_path, first)
    result = first_result if first_result.get("status") == "possible_duplicate" else _commit(tmp_path, second)

    assert result["success"] is False
    assert result["status"] == "possible_duplicate"
    assert any("duplicate DOI" in err for err in result["errors"])

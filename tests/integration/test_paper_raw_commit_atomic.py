"""Tests for the transactional commit_paper_raw (rollback on postcheck failure)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.services.paper_raw_formalizer import PaperRawFormalizationService
from src.services.v2_library import (
    AllCatalogBuilder,
    PaperNumberLedger,
    V2PaperCommitService,
    empty_catalog,
    empty_metadata,
)


def _staged_raw(root: Path, source_id: str = "0000000000000001", *, doi: str = "10.1/atomic") -> Path:
    from tests.helpers.paper_raw_factory import make_staged_source

    return make_staged_source(
        root,
        source_id,
        title_zh="原子提交",
        title_original="Atomic Paper",
        doi=doi,
        family="Wang",
        pdf_bytes=b"%PDF-atomic",
        md_text="# Atomic",
        catalog_domain="test",
    )


def _svc(tmp_path: Path):
    return V2PaperCommitService(
        papers_dir=tmp_path / "papers",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
    )


def _formalize(tmp_path: Path, folder: Path, **kw) -> dict:
    return PaperRawFormalizationService(
        paper_raw_dir=folder.parent, papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "catalog" / "paper_number_ledger.json",
        all_catalog_path=tmp_path / "catalog" / "all.catalog.json",
    ).formalize(folder, **kw)


def test_commit_success_activates_reserved_number_and_deletes_source(tmp_path: Path):
    folder = _staged_raw(tmp_path)
    svc = _svc(tmp_path)
    formalized = _formalize(tmp_path, folder)
    assert formalized["success"], formalized

    result = svc.commit_paper_raw(Path(formalized["folder"]))

    assert result["status"] == "imported"
    assert result["paper_number"] == "0000000000000001"
    final = tmp_path / "papers" / formalized["paper_id"]
    assert final.exists()
    assert (final / "0000000000000001.paper.number").exists()
    assert not Path(formalized["folder"]).exists()  # paper_raw source deleted
    ledger = json.loads((tmp_path / "catalog" / "paper_number_ledger.json").read_text(encoding="utf-8"))
    item = ledger["items"]["0000000000000001"]
    assert item["state"] == "active"
    assert item["folder_name"] == formalized["paper_id"]


def test_commit_postcheck_failure_rolls_back_final(tmp_path: Path, monkeypatch):
    folder = _staged_raw(tmp_path)
    svc = _svc(tmp_path)
    formalized = _formalize(tmp_path, folder)
    final = tmp_path / "papers" / formalized["paper_id"]

    def _boom(self, *, write=True):
        raise RuntimeError("catalog rebuild exploded")

    monkeypatch.setattr(AllCatalogBuilder, "build", _boom)
    result = svc.commit_paper_raw(Path(formalized["folder"]))

    assert result["status"] == "commit_failed"
    assert Path(formalized["folder"]).exists()  # paper_raw NOT deleted
    assert not final.exists()  # formal library NOT polluted (rollback)
    # ledger number deactivated back to reserved, pointing at paper_raw
    ledger = json.loads((tmp_path / "catalog" / "paper_number_ledger.json").read_text(encoding="utf-8"))
    item = ledger["items"]["0000000000000001"]
    assert item["state"] == "reserved"
    status = json.loads((Path(formalized["folder"]) / ".import_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "commit_failed"


def test_commit_reports_ledger_rollback_failure(tmp_path: Path, monkeypatch):
    folder = _staged_raw(tmp_path)
    svc = _svc(tmp_path)
    formalized = _formalize(tmp_path, folder)

    def _boom_build(self, *, write=True):
        raise RuntimeError("catalog rebuild exploded")

    def _boom_deactivate(self, number, source_folder):
        raise RuntimeError("ledger write refused")

    monkeypatch.setattr(AllCatalogBuilder, "build", _boom_build)
    monkeypatch.setattr(PaperNumberLedger, "deactivate_to_source", _boom_deactivate)

    result = svc.commit_paper_raw(Path(formalized["folder"]))

    assert result["status"] == "commit_failed"
    assert any("catalog rebuild exploded" in err for err in result["errors"])
    assert result["rollback_errors"] == ["ledger rollback failed: ledger write refused"]
    assert result["rollback_errors"][0] in result["errors"]
    status = json.loads((Path(formalized["folder"]) / ".import_status.json").read_text(encoding="utf-8"))
    assert status["errors"] == result["errors"]


def test_commit_rejects_missing_formalization_json(tmp_path: Path):
    folder = _staged_raw(tmp_path)
    svc = _svc(tmp_path)
    formalized = _formalize(tmp_path, folder)
    # remove formalization.json — commit must reject
    (Path(formalized["folder"]) / f"{formalized['paper_id']}.formalization.json").unlink()

    result = svc.commit_paper_raw(Path(formalized["folder"]))

    assert result["status"] == "commit_failed"
    assert not (tmp_path / "papers" / formalized["paper_id"]).exists()


def test_commit_rejects_missing_marker(tmp_path: Path):
    folder = _staged_raw(tmp_path)
    svc = _svc(tmp_path)
    formalized = _formalize(tmp_path, folder)
    for m in Path(formalized["folder"]).glob("*.paper.number"):
        m.unlink()

    result = svc.commit_paper_raw(Path(formalized["folder"]))

    assert result["status"] == "commit_failed"
    assert not (tmp_path / "papers" / formalized["paper_id"]).exists()


def test_commit_rejects_unformalized_paper_number_workspace(tmp_path: Path):
    folder = _staged_raw(tmp_path)  # 16-digit workspace, not formalized
    svc = _svc(tmp_path)
    with pytest.raises(ValueError):
        svc.commit_paper_raw(folder)


def test_commit_retry_after_failure(tmp_path: Path, monkeypatch):
    folder = _staged_raw(tmp_path)
    svc = _svc(tmp_path)
    formalized = _formalize(tmp_path, folder)

    real_build = AllCatalogBuilder.build
    state = {"failed": False}

    def _flaky(self, *, write=True):
        if not state["failed"]:
            state["failed"] = True
            raise RuntimeError("transient catalog rebuild failure")
        monkeypatch.setattr(AllCatalogBuilder, "build", real_build)
        return real_build(self, write=write)

    monkeypatch.setattr(AllCatalogBuilder, "build", _flaky)
    r1 = svc.commit_paper_raw(Path(formalized["folder"]))
    assert r1["status"] == "commit_failed"
    # re-formalize resets ready_for_commit on the still-present paper_raw folder
    re_formalized = _formalize(tmp_path, Path(formalized["folder"]))
    # formalize is idempotent on the already-renamed folder
    assert re_formalized["success"]
    r2 = svc.commit_paper_raw(Path(re_formalized["folder"]))
    assert r2["status"] == "imported", r2
    assert (tmp_path / "papers" / formalized["paper_id"]).exists()


def test_commit_cli_isolation_uses_tmp_paths_and_does_not_touch_default_ledger(tmp_path: Path):
    import os
    import subprocess
    import sys

    folder = _staged_raw(tmp_path)
    formalized = _formalize(tmp_path, folder)
    assert formalized["success"], formalized

    project_root = Path(__file__).resolve().parent.parent.parent
    default_ledger = project_root / "data" / "catalog" / "paper_number_ledger.json"
    before = default_ledger.read_text(encoding="utf-8") if default_ledger.exists() else None

    try:
        proc = subprocess.run(
            [sys.executable, "scripts/commit_paper_raw_to_papers.py",
             "--all-ready", "--apply",
             "--paper-raw-dir", str(tmp_path / "paper_raw"),
             "--papers-dir", str(tmp_path / "papers"),
             "--ledger-path", str(tmp_path / "catalog" / "paper_number_ledger.json"),
             "--all-catalog-path", str(tmp_path / "catalog" / "all.catalog.json"),
             "--report", str(tmp_path / "reports" / "commit.json")],
            capture_output=True, text=True, cwd=str(project_root),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("commit CLI hung > 30s — possible deadlock or env pollution")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    # formalized folder is committed into the tmp papers dir
    assert (tmp_path / "papers" / formalized["paper_id"]).exists()
    assert (tmp_path / "catalog" / "paper_number_ledger.json").exists()
    assert (tmp_path / "catalog" / "all.catalog.json").exists()
    # the real default ledger must NOT have been mutated
    after = default_ledger.read_text(encoding="utf-8") if default_ledger.exists() else None
    assert after == before, "commit CLI mutated the real data/catalog/paper_number_ledger.json"

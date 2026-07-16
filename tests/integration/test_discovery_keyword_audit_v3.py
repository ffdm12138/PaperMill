from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.helpers.relevance_profiles import bind_test_relevance_profile

import scripts.audit_discovery_keyword_index_sources as audit
from src.catalog_folders.registry import definition_hash
from src.discovery.keyword_notebook import KeywordNotebookStore, notebook_filename


pytestmark = pytest.mark.integration


def _configure(monkeypatch: pytest.MonkeyPatch, root: Path) -> tuple[Path, Path]:
    notebooks = root / "notebooks"
    discovery = root / "discovery"
    state = root / "state"
    for path in (notebooks, discovery / "exports", discovery / "doi_candidates", root / "pages", root / "locks", root / "catalog", state):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(audit, "DISCOVERY_KEYWORD_NOTEBOOK_DIR", notebooks)
    monkeypatch.setattr(audit, "DISCOVERY_DIR", discovery)
    monkeypatch.setattr(audit, "DISCOVERY_EXPORTS_DIR", discovery / "exports")
    monkeypatch.setattr(audit, "DISCOVERY_PENDING_PAGES_DIR", root / "pages")
    monkeypatch.setattr(audit, "DISCOVERY_LOCKS_DIR", root / "locks")
    monkeypatch.setattr(audit, "CATALOG_FOLDER_ROOT", root / "catalog")
    monkeypatch.setattr(audit, "CATALOG_STATE_ROOT", state)
    return notebooks, state


def test_audit_cli_emits_machine_report_and_explicit_report_pair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
):
    notebooks, state = _configure(monkeypatch, tmp_path)
    store = KeywordNotebookStore(notebooks)
    store.ensure_notebook("风吹雪")
    store.sync_search_queries("风吹雪", add=[
        {"query": "风吹雪", "language": "zh"},
        {"query": "blowing snow", "language": "en"},
    ])
    bind_test_relevance_profile(store, "风吹雪")
    store.set_enabled("风吹雪", True)
    notebook = store.require_v3("风吹雪")
    row = {
        "category_id": notebook["keyword_id"],
        "keyword_zh": notebook["keyword_zh"],
        "normalized_keyword_zh": notebook["normalized_keyword_zh"],
        "directory_name": notebook["keyword_zh"],
        "source_notebook": notebook_filename(notebook["keyword_zh"]),
        "guidance_zh": None,
        "aliases_zh": [],
        "exclusions_zh": [],
        "classification_enabled": True,
    }
    row["definition_sha256"] = definition_hash(row)
    (state / "category_registry.json").write_text(
        json.dumps({"schema_version": "1.0", "categories": [row]}, ensure_ascii=False),
        encoding="utf-8",
    )

    before = (notebooks / notebook_filename("风吹雪")).read_bytes()
    assert audit.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["migration_safe"] is True
    assert payload["errors"] == []
    assert (notebooks / notebook_filename("风吹雪")).read_bytes() == before

    report_dir = tmp_path / "reports"
    assert audit.main(["--output-dir", str(report_dir)]) == 0
    assert len(list(report_dir.glob("*.json"))) == 1
    assert len(list(report_dir.glob("*.md"))) == 1


def test_pristine_unbound_lanes_in_cli_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
):
    notebooks, state = _configure(monkeypatch, tmp_path)
    store = KeywordNotebookStore(notebooks)
    store.ensure_notebook("风吹雪")
    store.sync_search_queries("风吹雪", add=[
        {"query": "风吹雪", "language": "zh"},
        {"query": "blowing snow", "language": "en"},
    ])
    bind_test_relevance_profile(store, "风吹雪")
    store.set_enabled("风吹雪", True)
    notebook = store.require_v3("风吹雪")
    row = {
        "category_id": notebook["keyword_id"],
        "keyword_zh": notebook["keyword_zh"],
        "normalized_keyword_zh": notebook["normalized_keyword_zh"],
        "directory_name": notebook["keyword_zh"],
        "source_notebook": notebook_filename(notebook["keyword_zh"]),
        "guidance_zh": None,
        "aliases_zh": [],
        "exclusions_zh": [],
        "classification_enabled": True,
    }
    row["definition_sha256"] = definition_hash(row)
    (state / "category_registry.json").write_text(
        json.dumps({"schema_version": "1.0", "categories": [row]}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert audit.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["errors"] == []
    assert payload["summary"]["pristine_unbound_lanes"] == 4

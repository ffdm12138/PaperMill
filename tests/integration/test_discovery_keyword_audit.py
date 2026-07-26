from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.helpers.relevance_profiles import bind_test_relevance_profile

import scripts.audit_discovery_keyword_index_sources as audit
from tests.helpers.discovery_workspace import make_test_workspace
from src.catalog_folders.identity import definition_hash
from src.discovery.contracts.notebook import notebook_filename
from src.discovery.stores.notebook_store import NotebookStoreV4 as KeywordNotebookStore


pytestmark = pytest.mark.integration


def _configure(monkeypatch: pytest.MonkeyPatch, root: Path) -> tuple[Path, Path, Path]:
    """Build an isolated v4 workspace plus catalog roots; return (ws_root, notebooks, state)."""
    state = root / "state"
    for path in (root / "catalog", state):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(audit, "CATALOG_FOLDER_ROOT", root / "catalog")
    monkeypatch.setattr(audit, "CATALOG_STATE_ROOT", state)
    workspace = make_test_workspace(root / "ws")
    return workspace.root, workspace.keyword_notebook_dir, state


def test_audit_cli_emits_machine_report_and_explicit_report_pair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
):
    ws_root, notebooks, state = _configure(monkeypatch, tmp_path)
    store = KeywordNotebookStore(notebooks)
    store.ensure_notebook("风吹雪")
    store.sync_search_queries("风吹雪", add=[
        {"query": "风吹雪", "language": "zh"},
        {"query": "blowing snow", "language": "en"},
    ])
    bind_test_relevance_profile(store, "风吹雪")
    store.set_enabled("风吹雪", True)
    notebook = store.require_v4("风吹雪")
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
    assert audit.main(["--json", "--workspace-root", str(ws_root)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["migration_safe"] is True
    assert payload["errors"] == []
    assert (notebooks / notebook_filename("风吹雪")).read_bytes() == before

    report_dir = tmp_path / "reports"
    assert audit.main(["--output-dir", str(report_dir), "--workspace-root", str(ws_root)]) == 0
    assert len(list(report_dir.glob("*.json"))) == 1
    assert len(list(report_dir.glob("*.md"))) == 1


def test_pristine_unbound_lanes_in_cli_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
):
    ws_root, notebooks, state = _configure(monkeypatch, tmp_path)
    store = KeywordNotebookStore(notebooks)
    store.ensure_notebook("风吹雪")
    store.sync_search_queries("风吹雪", add=[
        {"query": "风吹雪", "language": "zh"},
        {"query": "blowing snow", "language": "en"},
    ])
    bind_test_relevance_profile(store, "风吹雪")
    store.set_enabled("风吹雪", True)
    notebook = store.require_v4("风吹雪")
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
    assert audit.main(["--json", "--workspace-root", str(ws_root)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["errors"] == []
    assert payload["summary"]["pristine_unbound_lanes"] == 4

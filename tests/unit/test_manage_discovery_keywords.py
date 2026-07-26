from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.manage_discovery_keywords as manage
from src.discovery.contracts.notebook import notebook_filename
from src.discovery.stores.notebook_store import NotebookStoreV4 as KeywordNotebookStore


pytestmark = pytest.mark.unit


def _workspace(tmp_path: Path) -> Path:
    """Build an isolated v4 workspace root containing keyword_notebooks/."""
    root = tmp_path / "workspace"
    (root / "keyword_notebooks").mkdir(parents=True)
    return root


def test_create_disabled_writes_and_returns_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    workspace = _workspace(tmp_path)
    rc = manage.main([
        "--create", "--create-disabled", "--apply",
        "--workspace-root", str(workspace), "--keyword-zh", "风吹雪",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "created_disabled_draft"
    assert payload["enabled"] is False
    assert payload["ready"] is False
    notebook = KeywordNotebookStore(workspace / "keyword_notebooks").require_v4("风吹雪")
    assert notebook["enabled"] is False


def test_create_enabled_not_ready_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    workspace = _workspace(tmp_path)
    rc = manage.main([
        "--create", "--apply", "--workspace-root", str(workspace),
        "--keyword-zh", "风吹雪", "--query-zh", "风吹雪",
    ])
    assert rc == 2
    assert not (workspace / "keyword_notebooks" / notebook_filename("风吹雪")).exists()
    assert "requires at least one" in capsys.readouterr().err


def test_create_bilingual_writes_disabled_profile_draft_atomically(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    workspace = _workspace(tmp_path)
    rc = manage.main([
        "--create", "--apply", "--workspace-root", str(workspace),
        "--keyword-zh", "风吹雪", "--query-zh", "风吹雪",
        "--query-en", "blowing snow",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "created_disabled_profile_draft"
    assert payload["enabled"] is False
    assert payload["ready"] is False
    notebook = KeywordNotebookStore(workspace / "keyword_notebooks").require_v4("风吹雪")
    assert notebook["enabled"] is False
    assert payload["requires_relevance_profile"] is True


def test_create_failure_leaves_no_partial_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    workspace = _workspace(tmp_path)
    rc = manage.main([
        "--create", "--apply", "--workspace-root", str(workspace),
        "--keyword-zh", "风吹雪", "--query-zh", "风吹雪", "--query-en", "123",
    ])
    assert rc == 2
    assert not list((workspace / "keyword_notebooks").glob("*.json"))
    assert "not 'en'" in capsys.readouterr().err


def test_cli_cannot_enable_not_ready_draft(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    workspace = _workspace(tmp_path)
    notebooks = workspace / "keyword_notebooks"
    KeywordNotebookStore(notebooks).ensure_notebook("风吹雪")
    rc = manage.main([
        "--enable", "--apply", "--workspace-root", str(workspace),
        "--keyword-zh", "风吹雪",
    ])
    assert rc == 2
    assert KeywordNotebookStore(notebooks).show("风吹雪")["enabled"] is False
    assert "unready" in capsys.readouterr().err


def test_add_query_actions_are_language_specific(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    workspace = _workspace(tmp_path)
    store = KeywordNotebookStore(workspace / "keyword_notebooks")
    store.ensure_notebook("风吹雪")
    assert manage.main([
        "--add-query-en", "--workspace-root", str(workspace),
        "--keyword-zh", "风吹雪", "--query-en", "blowing snow", "--apply",
    ]) == 0
    with pytest.raises(SystemExit) as exc:
        manage.main([
            "--add-queries", "--workspace-root", str(workspace),
            "--keyword-zh", "风吹雪", "--query-en", "drifting snow",
        ])
    assert exc.value.code == 2
    assert "one of the arguments" in capsys.readouterr().err


def test_workspace_root_without_keyword_notebooks_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    rc = manage.main(["--list", "--workspace-root", str(tmp_path)])
    assert rc == 2
    assert "keyword_notebooks" in capsys.readouterr().err

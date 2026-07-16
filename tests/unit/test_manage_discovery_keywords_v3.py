from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.manage_discovery_keywords as manage
from src.discovery.keyword_notebook import KeywordNotebookStore, notebook_filename


pytestmark = pytest.mark.unit


def test_create_disabled_writes_and_returns_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    rc = manage.main([
        "--create", "--create-disabled", "--apply",
        "--keyword-notebook-dir", str(tmp_path), "--keyword-zh", "风吹雪",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "created_disabled_draft"
    assert payload["enabled"] is False
    assert payload["ready"] is False
    notebook = KeywordNotebookStore(tmp_path).require_v3("风吹雪")
    assert notebook["enabled"] is False


def test_create_enabled_not_ready_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    rc = manage.main([
        "--create", "--apply", "--keyword-notebook-dir", str(tmp_path),
        "--keyword-zh", "风吹雪", "--query-zh", "风吹雪",
    ])
    assert rc == 2
    assert not (tmp_path / notebook_filename("风吹雪")).exists()
    assert "requires at least one" in capsys.readouterr().err


def test_create_bilingual_writes_disabled_profile_draft_atomically(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    rc = manage.main([
        "--create", "--apply", "--keyword-notebook-dir", str(tmp_path),
        "--keyword-zh", "风吹雪", "--query-zh", "风吹雪",
        "--query-en", "blowing snow",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "created_disabled_profile_draft"
    assert payload["enabled"] is False
    assert payload["ready"] is False
    notebook = KeywordNotebookStore(tmp_path).require_v3("风吹雪")
    assert notebook["enabled"] is False
    assert payload["requires_relevance_profile"] is True


def test_create_failure_leaves_no_partial_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    rc = manage.main([
        "--create", "--apply", "--keyword-notebook-dir", str(tmp_path),
        "--keyword-zh", "风吹雪", "--query-zh", "风吹雪", "--query-en", "123",
    ])
    assert rc == 2
    assert not list(tmp_path.glob("*.json"))
    assert "not 'en'" in capsys.readouterr().err


def test_cli_cannot_enable_not_ready_draft(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    KeywordNotebookStore(tmp_path).ensure_notebook("风吹雪")
    rc = manage.main([
        "--enable", "--apply", "--keyword-notebook-dir", str(tmp_path),
        "--keyword-zh", "风吹雪",
    ])
    assert rc == 2
    assert KeywordNotebookStore(tmp_path).show("风吹雪")["enabled"] is False
    assert "unready" in capsys.readouterr().err


def test_add_query_actions_are_language_specific(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    store = KeywordNotebookStore(tmp_path)
    store.ensure_notebook("风吹雪")
    assert manage.main([
        "--add-query-en", "--keyword-notebook-dir", str(tmp_path),
        "--keyword-zh", "风吹雪", "--query-en", "blowing snow", "--apply",
    ]) == 0
    with pytest.raises(SystemExit) as exc:
        manage.main([
            "--add-queries", "--keyword-notebook-dir", str(tmp_path),
            "--keyword-zh", "风吹雪", "--query-en", "drifting snow",
        ])
    assert exc.value.code == 2
    assert "one of the arguments" in capsys.readouterr().err

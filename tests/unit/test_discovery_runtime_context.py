"""Unit tests for the active discovery runtime context composition root."""
from __future__ import annotations

from pathlib import Path

import pytest

import scripts.manage_discovery_keywords as manage
from config.settings import DISCOVERY_GENERATIONS_DIR, TRANSACTION_ROOT
from src.discovery.relevance_runtime import RelevanceRuntimePaths
from src.discovery.runtime_context import (
    DiscoveryRuntimeUnavailableError,
    resolve_active_runtime,
    runtime_context_from_workspace,
)
from src.discovery.stores.notebook_store import NotebookStoreV4 as KeywordNotebookStore
from src.discovery.workspace import WorkspaceResolver
from tests.helpers.discovery_workspace import make_test_workspace


pytestmark = pytest.mark.unit


def _workspace_root(tmp_path: Path) -> Path:
    """A complete, resolvable v4 workspace root (strict contract)."""
    return make_test_workspace(tmp_path / "gen-test").root


def test_resolve_active_runtime_with_explicit_workspace_root(tmp_path: Path):
    root = _workspace_root(tmp_path)
    ctx = resolve_active_runtime(workspace_root=root)
    assert ctx.notebook_root == root / "keyword_notebooks"
    assert ctx.page_journal_root == root / "page_journals"
    assert ctx.reports_root == root / "reports"
    assert ctx.locks_root == root / "locks"
    assert ctx.workspace.root == root


def test_resolve_active_runtime_rejects_root_without_keyword_notebooks(tmp_path: Path):
    with pytest.raises(
        DiscoveryRuntimeUnavailableError,
        match="not a complete v4 workspace",
    ):
        resolve_active_runtime(workspace_root=tmp_path)


def test_runtime_context_from_workspace_tracks_workspace_dirs(tmp_path: Path):
    workspace = make_test_workspace(tmp_path / "ws")
    ctx = runtime_context_from_workspace(workspace)
    assert ctx.notebook_root == workspace.keyword_notebook_dir
    assert ctx.page_journal_root == workspace.page_journals_dir
    assert ctx.reports_root == workspace.reports_dir
    assert ctx.locks_root == workspace.locks_dir


def test_production_tool_observes_notebooks_through_context(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    root = _workspace_root(tmp_path)
    ctx = resolve_active_runtime(workspace_root=root)
    store = KeywordNotebookStore(ctx.notebook_root)
    store.ensure_notebook("风吹雪")

    rc = manage.main(["--list", "--workspace-root", str(root)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "风吹雪" in out

    listed = KeywordNotebookStore(ctx.notebook_root).list_keywords()
    assert [item["keyword_zh"] for item in listed] == ["风吹雪"]


def test_resolve_active_runtime_fails_closed_without_active_workspace(monkeypatch):
    def _no_active(self):
        raise FileNotFoundError("no active generation pointer")

    monkeypatch.setattr(WorkspaceResolver, "resolve_active", _no_active)
    with pytest.raises(DiscoveryRuntimeUnavailableError, match="no active discovery v4 workspace"):
        resolve_active_runtime()


def test_manage_cli_fails_closed_without_active_workspace(monkeypatch, capsys: pytest.CaptureFixture[str]):
    def _no_active(self):
        raise FileNotFoundError("no active generation pointer")

    monkeypatch.setattr(WorkspaceResolver, "resolve_active", _no_active)
    rc = manage.main(["--list"])
    assert rc == 2
    assert "[ERROR]" in capsys.readouterr().err


def test_resolve_default_maps_active_generation_notebooks_to_repository_transactions():
    notebook_root = DISCOVERY_GENERATIONS_DIR / "gen-x" / "keyword_notebooks"
    paths = RelevanceRuntimePaths.resolve_default(
        notebook_root=notebook_root,
        journal_root=DISCOVERY_GENERATIONS_DIR / "gen-x" / "page_journals",
    )
    assert paths.transaction_root == (Path(TRANSACTION_ROOT) / "relevance_profiles").resolve()


def test_resolve_default_keeps_isolated_roots_on_sibling_layout(tmp_path: Path):
    paths = RelevanceRuntimePaths.resolve_default(
        notebook_root=tmp_path / "keyword_notebooks",
        journal_root=tmp_path / "page_journals",
    )
    assert paths.transaction_root == (tmp_path / "transactions" / "relevance_profiles").resolve()


def test_resolve_default_keeps_migration_staging_on_sibling_layout():
    staging = DISCOVERY_GENERATIONS_DIR / ".staging" / "gen-x"
    paths = RelevanceRuntimePaths.resolve_default(
        notebook_root=staging / "keyword_notebooks",
        journal_root=staging / "page_journals",
    )
    assert paths.transaction_root == (staging / "transactions" / "relevance_profiles").resolve()

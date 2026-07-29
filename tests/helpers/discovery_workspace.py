"""Explicit v4 discovery workspace helper for tests.

Tests build an isolated ``DiscoveryWorkspace`` under ``tmp_path`` and pass
it as ``options.workspace``.  All stores live under the workspace root;
there are no flat-path overrides.

Every workspace built here is a complete, resolvable v4 workspace: its
``generation_id`` equals the root directory name and the strict
``workspace.json`` manifest is bound to the same id, so
``WorkspaceResolver.resolve_explicit_workspace`` accepts it.  The helper
asserts that resolution itself, so a violating fixture fails at
construction.  Test fixtures meet the production contract; the contract
is never loosened for tests.
"""
from __future__ import annotations

from pathlib import Path

from src.discovery.workspace import (
    DiscoveryWorkspace,
    WorkspaceResolver,
    build_workspace_manifest,
    write_workspace_manifest,
)


def make_test_workspace(root: Path) -> DiscoveryWorkspace:
    """Build (and create) an isolated v4 workspace rooted at *root*.

    Every store lives under *root* (``keyword_notebooks``,
    ``page_journals``, ``exports``, ``reports``, ``locks``) and
    ``generation_id == root.name``.  A strict ``workspace.json`` bound to
    the same generation id is written, and the result is verified against
    ``WorkspaceResolver.resolve_explicit_workspace`` before returning.
    """
    root = Path(root)
    workspace = DiscoveryWorkspace(
        generation_id=root.name,
        root=root,
        keyword_notebook_dir=root / "keyword_notebooks",
        page_journals_dir=root / "page_journals",
        exports_dir=root / "exports",
        reports_dir=root / "reports",
        locks_dir=root / "locks",
    )
    workspace.ensure_dirs()
    manifest = build_workspace_manifest(
        root.name, root, migration_id="test-bootstrap"
    )
    write_workspace_manifest(root, manifest)
    resolved = WorkspaceResolver.resolve_explicit_workspace(root)
    assert resolved.generation_id == root.name, (
        f"test workspace {root} failed explicit resolution: "
        f"generation_id {resolved.generation_id!r} != {root.name!r}"
    )
    return workspace

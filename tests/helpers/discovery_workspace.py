"""Explicit v4 discovery workspace helper for tests.

Tests that previously relied on the removed ``DiscoveryOptions`` flat-path
fallback (``notebook_dir`` / ``pending_pages_dir`` / ``locks_dir`` /
``exports_dir``) now build an isolated ``DiscoveryWorkspace`` under
``tmp_path`` and pass it as ``options.workspace``.
"""
from __future__ import annotations

from pathlib import Path

from src.discovery.workspace import DiscoveryWorkspace


def make_test_workspace(
    root: Path,
    *,
    notebook_dir: Path | None = None,
    page_journals_dir: Path | None = None,
    locks_dir: Path | None = None,
    exports_dir: Path | None = None,
) -> DiscoveryWorkspace:
    """Build (and create) an isolated v4 workspace rooted at *root*.

    The four directory arguments map the retired flat-path knobs onto the
    workspace; every other v4 sub-directory defaults to a standard child of
    *root*.
    """
    root = Path(root)
    workspace = DiscoveryWorkspace(
        generation_id=f"test-{root.name}",
        root=root,
        keyword_notebook_dir=Path(notebook_dir) if notebook_dir is not None else root / "keyword_notebooks",
        lane_states_dir=root / "lane_states",
        page_journals_dir=Path(page_journals_dir) if page_journals_dir is not None else root / "page_journals",
        pending_candidates_dir=root / "pending_candidates",
        indexes_dir=root / "indexes",
        exports_dir=Path(exports_dir) if exports_dir is not None else root / "exports",
        reports_dir=root / "reports",
        locks_dir=Path(locks_dir) if locks_dir is not None else root / "locks",
    )
    workspace.ensure_dirs()
    return workspace

"""Active discovery runtime context — the single composition root for tools.

Every production tool that reads keyword notebooks, page journals, reports,
or locks MUST resolve them through :func:`resolve_active_runtime` (or an
explicit test-only ``workspace_root``) instead of the legacy flat
``config.settings`` discovery directories.  Resolution is fail-closed via
:class:`~src.discovery.workspace.WorkspaceResolver`: no active generation
means a hard error, never a silent fallback to retired flat paths.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.discovery.workspace import DiscoveryWorkspace, WorkspaceResolver


class DiscoveryRuntimeUnavailableError(RuntimeError):
    """The active v4 workspace cannot be resolved for a production tool."""


@dataclass(frozen=True)
class DiscoveryRuntimeContext:
    """Resolved active workspace plus its primary consumer roots."""

    workspace: DiscoveryWorkspace
    notebook_root: Path
    page_journal_root: Path
    reports_root: Path
    locks_root: Path


def runtime_context_from_workspace(
    workspace: DiscoveryWorkspace,
) -> DiscoveryRuntimeContext:
    """Build a context from an already-resolved workspace."""
    return DiscoveryRuntimeContext(
        workspace=workspace,
        notebook_root=workspace.keyword_notebook_dir,
        page_journal_root=workspace.page_journals_dir,
        reports_root=workspace.reports_dir,
        locks_root=workspace.locks_dir,
    )


def resolve_active_runtime(
    *,
    workspace_root: Path | None = None,
) -> DiscoveryRuntimeContext:
    """Resolve the active v4 runtime context, fail closed.

    ``workspace_root`` is the explicit test/staging override used by test
    suites and the migration smoke; production callers omit it and always
    land on the active generation.  When given, the directory must contain
    ``keyword_notebooks`` — this prevents accidentally pointing a tool at
    the retired legacy flat root.
    """
    if workspace_root is not None:
        root = Path(workspace_root)
        notebook_dir = root / "keyword_notebooks"
        if not notebook_dir.is_dir():
            raise DiscoveryRuntimeUnavailableError(
                f"explicit workspace root lacks keyword_notebooks/: {root}. "
                "Legacy flat directories are retired; pass an isolated v4 "
                "workspace root."
            )
        return runtime_context_from_workspace(
            DiscoveryWorkspace(
                generation_id=root.name,
                root=root,
                keyword_notebook_dir=notebook_dir,
                lane_states_dir=root / "lane_states",
                page_journals_dir=root / "page_journals",
                pending_candidates_dir=root / "pending_candidates",
                indexes_dir=root / "indexes",
                exports_dir=root / "exports",
                reports_dir=root / "reports",
                locks_dir=root / "locks",
            )
        )
    try:
        workspace = WorkspaceResolver().resolve_active()
    except Exception as exc:
        raise DiscoveryRuntimeUnavailableError(
            f"no active discovery v4 workspace: {exc}"
        ) from exc
    return runtime_context_from_workspace(workspace)

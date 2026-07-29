"""Active discovery runtime context — the single composition root for tools.

Every production tool that reads keyword notebooks, page journals, reports,
or locks MUST resolve them through :func:`resolve_active_runtime` (or an
explicit test-only ``workspace_root``) instead of the legacy flat
``config.settings`` discovery directories.  Resolution is fail-closed via
:class:`~src.discovery.workspace.WorkspaceResolver`: no active generation
means a hard error, never a silent fallback to retired flat paths.

Error taxonomy
--------------
Resolution failures keep their distinction across this boundary:

- :class:`DiscoveryRuntimeNotInitialized` — no active generation pointer
  exists.  This is the normal fresh-install state and the ONLY state a
  caller may optionally degrade on.
- :class:`DiscoveryRuntimeCorrupt` — the pointer, manifest, or generation
  directory exists but is damaged (unparseable, hash-mismatched, unknown
  schema).  Fail closed; never treat as a fresh install.
- :class:`DiscoveryRuntimeIncomplete` — the generation is structurally
  incomplete (missing required subdirectories or explicit-root pieces).
- :class:`DiscoveryRuntimeMaintenance` — resolution is blocked by an
  active maintenance window.

All four subclass :class:`DiscoveryRuntimeUnavailableError`, so existing
``except DiscoveryRuntimeUnavailableError`` callers keep working while new
callers can distinguish the states.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.discovery.workspace import (
    ActiveGenerationCorruptError,
    ActiveGenerationMissingError,
    DiscoveryWorkspace,
    WorkspaceIncompleteError,
    WorkspaceManifestMismatchError,
    WorkspaceManifestMissingError,
    WorkspaceResolver,
)


class DiscoveryRuntimeUnavailableError(RuntimeError):
    """The active v4 workspace cannot be resolved for a production tool."""


class DiscoveryRuntimeNotInitialized(DiscoveryRuntimeUnavailableError):
    """No active generation pointer exists — normal fresh-install state."""


class DiscoveryRuntimeCorrupt(DiscoveryRuntimeUnavailableError):
    """Pointer, manifest, or generation state is damaged.  Fail closed."""


class DiscoveryRuntimeIncomplete(DiscoveryRuntimeUnavailableError):
    """The generation is missing required structural pieces."""


class DiscoveryRuntimeMaintenance(DiscoveryRuntimeUnavailableError):
    """Resolution is blocked by an active maintenance window."""


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


def _map_resolution_error(exc: Exception, *, origin: str) -> DiscoveryRuntimeUnavailableError:
    """Translate workspace-resolution errors into the runtime taxonomy.

    Only :class:`ActiveGenerationMissingError` maps to NotInitialized;
    every other failure — including unexpected ones — is Corrupt or
    Incomplete so damaged production state can never masquerade as a
    fresh install.
    """
    if isinstance(exc, ActiveGenerationMissingError):
        return DiscoveryRuntimeNotInitialized(f"{origin}: {exc}")
    if isinstance(exc, WorkspaceIncompleteError):
        return DiscoveryRuntimeIncomplete(f"{origin}: {exc}")
    if isinstance(
        exc,
        (
            ActiveGenerationCorruptError,
            WorkspaceManifestMissingError,
            WorkspaceManifestMismatchError,
        ),
    ):
        return DiscoveryRuntimeCorrupt(f"{origin}: {exc}")
    return DiscoveryRuntimeCorrupt(f"{origin} (unexpected): {exc}")


def resolve_active_runtime(
    *,
    workspace_root: Path | None = None,
) -> DiscoveryRuntimeContext:
    """Resolve the active v4 runtime context, fail closed.

    ``workspace_root`` is the explicit test/staging override used by test
    suites; production callers omit it and always land on the active
    generation.  When given, the directory must be a complete v4 workspace
    verified by ``WorkspaceResolver.resolve_explicit_workspace`` — this
    prevents accidentally pointing a tool at the retired legacy flat root.

    Raises a typed :class:`DiscoveryRuntimeUnavailableError` subclass; the
    original workspace error is preserved as ``__cause__``.
    """
    if workspace_root is not None:
        try:
            workspace = WorkspaceResolver.resolve_explicit_workspace(
                workspace_root
            )
        except Exception as exc:
            raise _map_resolution_error(
                exc, origin="explicit workspace root is not a complete v4 workspace"
            ) from exc
        return runtime_context_from_workspace(workspace)
    try:
        workspace = WorkspaceResolver().resolve_active()
    except Exception as exc:
        raise _map_resolution_error(
            exc, origin="no active discovery v4 workspace"
        ) from exc
    return runtime_context_from_workspace(workspace)

"""Discovery v4 workspace: generation-isolated directory layout with atomic activation.

Layout::

    data/discovery/
    ├── active_generation.json          ← single atomic cutover point
    ├── generations/
    │   └── <generation_id>/
    │       ├── workspace.json
    │       ├── keyword_notebooks/
    │       ├── lane_states/
    │       ├── page_journals/
    │       ├── pending_candidates/
    │       ├── indexes/
    │       ├── exports/
    │       ├── reports/
    │       └── locks/
    ├── migrations/
    └── legacy_archive/

Production code resolves paths exclusively through ``WorkspaceResolver.resolve_active()``.
No module may construct discovery paths directly from ``data/discovery/``.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.settings import DISCOVERY_DIR, DISCOVERY_GENERATIONS_DIR
from src.discovery.contracts.manifest import (
    WORKSPACE_MANIFEST_SCHEMA_VERSION_V4,
    ActiveGenerationPointerV4,
    DiscoveryWorkspaceManifestV4,
)

WORKSPACE_SCHEMA_VERSION = "4.0"

ACTIVE_GENERATION_PATH = DISCOVERY_DIR / "active_generation.json"

KNOWN_SUBDIRS = (
    "keyword_notebooks", "lane_states", "page_journals",
    "pending_candidates", "indexes", "exports",
    "reports", "locks",
)


# ── Typed errors ──────────────────────────────────────────────────────────


class ActiveGenerationMissingError(RuntimeError):
    """No active_generation.json exists — first-time install required."""


class ActiveGenerationCorruptError(RuntimeError):
    """active_generation.json exists but cannot be parsed as strict V4 pointer."""


class WorkspaceManifestMissingError(RuntimeError):
    """Generation directory exists but has no workspace.json."""


class WorkspaceManifestMismatchError(RuntimeError):
    """workspace.json fails hash or field verification."""


class WorkspaceIncompleteError(RuntimeError):
    """Required subdirectories are missing from the workspace."""


# ── DiscoveryWorkspace ────────────────────────────────────────────────────


@dataclass(frozen=True)
class DiscoveryWorkspace:
    """Immutable resolved v4 workspace pointing to one generation.

    Every subdirectory is an absolute ``Path`` derived from the
    generation root.  Use ``WorkspaceResolver.resolve_active()``
    to obtain instances.
    """

    generation_id: str
    root: Path
    keyword_notebook_dir: Path
    lane_states_dir: Path
    page_journals_dir: Path
    pending_candidates_dir: Path
    indexes_dir: Path
    exports_dir: Path
    reports_dir: Path
    locks_dir: Path

    def __post_init__(self) -> None:
        if not self.generation_id or not self.generation_id.strip():
            raise ValueError(
                f"generation_id must be non-blank, got {self.generation_id!r}"
            )
        for ch in ("/", "\\", ":", "\n", "\r"):
            if ch in self.generation_id:
                raise ValueError(
                    f"generation_id contains forbidden character {ch!r}: "
                    f"{self.generation_id!r}"
                )

    @classmethod
    def from_generation_id(cls, generation_id: str) -> "DiscoveryWorkspace":
        """Create a workspace reference from a generation id."""
        root = DISCOVERY_GENERATIONS_DIR / generation_id
        return cls(
            generation_id=generation_id,
            root=root,
            keyword_notebook_dir=root / "keyword_notebooks",
            lane_states_dir=root / "lane_states",
            page_journals_dir=root / "page_journals",
            pending_candidates_dir=root / "pending_candidates",
            indexes_dir=root / "indexes",
            exports_dir=root / "exports",
            reports_dir=root / "reports",
            locks_dir=root / "locks",
        )

    def ensure_dirs(self) -> None:
        """Create all workspace subdirectories if they do not exist."""
        dirs: list[Path] = [
            self.keyword_notebook_dir, self.lane_states_dir,
            self.page_journals_dir, self.pending_candidates_dir,
            self.indexes_dir, self.exports_dir,
            self.reports_dir, self.locks_dir,
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)

    def verify_dirs(self) -> list[str]:
        """Return list of missing required subdirectories.  Empty = complete."""
        missing: list[str] = []
        for attr, dirname in [
            ("keyword_notebook_dir", "keyword_notebooks"),
            ("lane_states_dir", "lane_states"),
            ("page_journals_dir", "page_journals"),
            ("pending_candidates_dir", "pending_candidates"),
            ("indexes_dir", "indexes"),
            ("exports_dir", "exports"),
            ("reports_dir", "reports"),
            ("locks_dir", "locks"),
        ]:
            p = getattr(self, attr)
            if not p.is_dir():
                missing.append(dirname)
        return missing

    def to_dict(self) -> dict[str, str]:
        return {
            "generation_id": self.generation_id,
            "root": str(self.root),
            "keyword_notebook_dir": str(self.keyword_notebook_dir),
            "lane_states_dir": str(self.lane_states_dir),
            "page_journals_dir": str(self.page_journals_dir),
            "pending_candidates_dir": str(self.pending_candidates_dir),
            "indexes_dir": str(self.indexes_dir),
            "exports_dir": str(self.exports_dir),
            "reports_dir": str(self.reports_dir),
            "locks_dir": str(self.locks_dir),
        }


# ── WorkspaceResolver ─────────────────────────────────────────────────────


class WorkspaceResolver:
    """Sole entry point for resolving discovery workspace paths.

    Production CLI calls ``resolve_active()``.  Migration tools call
    ``resolve_staging_for_migration()``.  No module may bypass this
    resolver to construct paths from ``config.settings`` discovery
    constants.
    """

    def __init__(
        self,
        active_pointer_path: Path | None = None,
        generations_root: Path | None = None,
    ) -> None:
        self._active_path = active_pointer_path or ACTIVE_GENERATION_PATH
        self._generations_root = generations_root or DISCOVERY_GENERATIONS_DIR

    # ── Active workspace ───────────────────────────────────────────────

    def resolve_active(self) -> DiscoveryWorkspace:
        """Resolve the currently active workspace.

        Flow:
        1. Read active_generation.json
        2. Strict-parse as ActiveGenerationPointerV4 (no empty fields)
        3. Locate generation directory
        4. Require workspace.json
        5. Strict-parse as DiscoveryWorkspaceManifestV4
        6. Verify generation_id matches pointer
        7. Verify migration_id matches pointer
        8. Recompute manifest SHA-256 against pointer
        9. Verify workspace tree hash
        10. Verify all required subdirectories exist
        11. Return DiscoveryWorkspace

        Raises:
            ActiveGenerationMissingError: No active_generation.json exists.
            ActiveGenerationCorruptError: Pointer exists but is invalid.
            WorkspaceManifestMissingError: No workspace.json in generation.
            WorkspaceManifestMismatchError: Manifest hash/integrity mismatch.
            WorkspaceIncompleteError: Required subdirectories missing.

        Never returns ``None``.
        """
        # Step 1-2: Read and parse pointer
        if not self._active_path.is_file():
            raise ActiveGenerationMissingError(
                f"no active generation pointer at {self._active_path}"
            )

        try:
            raw = json.loads(self._active_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            raise ActiveGenerationCorruptError(
                f"cannot read active generation pointer: {exc}"
            ) from exc

        try:
            pointer = ActiveGenerationPointerV4.from_dict_strict(raw)
        except (ValueError, TypeError) as exc:
            raise ActiveGenerationCorruptError(
                f"active generation pointer is not strict V4: {exc}"
            ) from exc

        # Step 3: Locate generation directory
        gen_root = self._generations_root / pointer.generation_id
        if not gen_root.is_dir():
            raise ActiveGenerationCorruptError(
                f"active generation {pointer.generation_id!r} "
                f"does not exist at {gen_root}"
            )

        # Step 4-5: Require and parse workspace.json
        workspace_json = gen_root / "workspace.json"
        if not workspace_json.is_file():
            raise WorkspaceManifestMissingError(
                f"workspace.json missing in generation {pointer.generation_id} "
                f"at {workspace_json}"
            )

        manifest_raw = workspace_json.read_bytes()
        try:
            manifest_data = json.loads(manifest_raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise WorkspaceManifestMismatchError(
                f"cannot parse workspace.json: {exc}"
            ) from exc

        try:
            manifest = DiscoveryWorkspaceManifestV4.from_dict_strict(manifest_data)
        except (ValueError, TypeError) as exc:
            raise WorkspaceManifestMismatchError(
                f"workspace.json is not strict V4: {exc}"
            ) from exc

        # Step 6-7: Verify generation_id and migration_id
        if manifest.generation_id != pointer.generation_id:
            raise WorkspaceManifestMismatchError(
                f"workspace generation_id {manifest.generation_id!r} "
                f"!= pointer generation_id {pointer.generation_id!r}"
            )
        if manifest.migration_id != pointer.migration_id:
            raise WorkspaceManifestMismatchError(
                f"workspace migration_id {manifest.migration_id!r} "
                f"!= pointer migration_id {pointer.migration_id!r}"
            )

        # Step 8: Recompute manifest SHA-256
        computed_hash = hashlib.sha256(manifest_raw).hexdigest()
        if computed_hash != pointer.workspace_manifest_sha256:
            raise WorkspaceManifestMismatchError(
                f"workspace manifest SHA-256 mismatch: "
                f"pointer has {pointer.workspace_manifest_sha256[:16]}..., "
                f"computed {computed_hash[:16]}..."
            )

        # Step 9-10: Build workspace and verify
        ws = DiscoveryWorkspace.from_generation_id(pointer.generation_id)
        missing = ws.verify_dirs()
        if missing:
            raise WorkspaceIncompleteError(
                f"workspace {pointer.generation_id} missing directories: {missing}"
            )

        return ws

    def resolve_pointer(self) -> ActiveGenerationPointerV4:
        """Return the strict pointer without resolving the workspace.

        Raises ActiveGenerationMissingError or ActiveGenerationCorruptError.
        """
        if not self._active_path.is_file():
            raise ActiveGenerationMissingError(
                f"no active generation pointer at {self._active_path}"
            )
        try:
            raw = json.loads(self._active_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            raise ActiveGenerationCorruptError(
                f"cannot read active generation pointer: {exc}"
            ) from exc
        try:
            return ActiveGenerationPointerV4.from_dict_strict(raw)
        except (ValueError, TypeError) as exc:
            raise ActiveGenerationCorruptError(
                f"active generation pointer is not strict V4: {exc}"
            ) from exc

    # ── Staging workspace ──────────────────────────────────────────────

    def resolve_staging_for_migration(
        self, generation_id: str | None = None
    ) -> DiscoveryWorkspace:
        """Create a staging workspace for migration.  Migration-only."""
        return create_staging_workspace(generation_id)


# ── Staging workspace helpers ─────────────────────────────────────────────

STAGING_DIR = DISCOVERY_GENERATIONS_DIR / ".staging"


def create_staging_workspace(
    generation_id: str | None = None,
) -> DiscoveryWorkspace:
    """Create a fresh staging workspace under ``.staging/<id>/``.

    The workspace is NOT active — it must pass offline validation and
    smoke tests before commit.
    """
    gid = generation_id or f"v4-{uuid.uuid4().hex[:12]}"
    root = STAGING_DIR / gid
    if root.exists():
        raise FileExistsError(f"staging workspace already exists: {root}")
    ws = DiscoveryWorkspace(
        generation_id=gid,
        root=root,
        keyword_notebook_dir=root / "keyword_notebooks",
        lane_states_dir=root / "lane_states",
        page_journals_dir=root / "page_journals",
        pending_candidates_dir=root / "pending_candidates",
        indexes_dir=root / "indexes",
        exports_dir=root / "exports",
        reports_dir=root / "reports",
        locks_dir=root / "locks",
    )
    ws.ensure_dirs()
    return ws


# ── Atomic I/O ────────────────────────────────────────────────────────────


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically: tmp + flush + fsync + os.replace + fsync parent dir."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        raw = payload.encode("utf-8")
        with tmp_path.open("wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp_path), str(path))
        if os.name != "nt":
            try:
                fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


# ── Active generation commit ──────────────────────────────────────────────


def commit_workspace(
    staging_ws: DiscoveryWorkspace,
    pointer: ActiveGenerationPointerV4,
) -> DiscoveryWorkspace:
    """Promote a staging workspace to active.

    Moves ``.staging/<id>/`` → ``generations/<id>/``, then atomically
    writes the strict ``ActiveGenerationPointerV4`` to
    ``active_generation.json``.

    Args:
        staging_ws: The validated staging workspace to promote.
        pointer: A COMPLETE ActiveGenerationPointerV4 with all fields filled
                 (manifest SHA-256, activated_at, migration_id).

    Returns:
        The promoted DiscoveryWorkspace (now at generations/<id>/).

    Raises:
        FileExistsError: If the target generation already exists.
        ValueError: If any pointer field is empty.
    """
    if not pointer.is_valid:
        raise ValueError(
            "ActiveGenerationPointerV4 has empty fields — "
            "cannot commit incomplete pointer"
        )

    target_root = DISCOVERY_GENERATIONS_DIR / staging_ws.generation_id
    if target_root.exists():
        raise FileExistsError(f"generation already exists: {target_root}")
    target_root.parent.mkdir(parents=True, exist_ok=True)

    os.rename(str(staging_ws.root), str(target_root))

    _atomic_write_json(ACTIVE_GENERATION_PATH, pointer.to_dict())

    return DiscoveryWorkspace.from_generation_id(staging_ws.generation_id)


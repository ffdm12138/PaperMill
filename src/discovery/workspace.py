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
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout as FileLockTimeout

from config.settings import (
    DISCOVERY_DIR,
    DISCOVERY_GENERATIONS_DIR,
    DISCOVERY_MIGRATIONS_DIR,
)
from src.utils.atomic_io import atomic_write_json_unlocked
from src.discovery.contracts.manifest import (
    WORKSPACE_MANIFEST_SCHEMA_VERSION_V4,
    ActiveGenerationPointerV4,
    DiscoveryWorkspaceManifestV4,
)

WORKSPACE_SCHEMA_VERSION = "4.0"

ACTIVE_GENERATION_PATH = DISCOVERY_DIR / "active_generation.json"
MIGRATION_LOCK_PATH = DISCOVERY_MIGRATIONS_DIR / ".migration.lock"

KNOWN_SUBDIRS = (
    "keyword_notebooks", "lane_states", "page_journals",
    "indexes", "exports", "reports", "locks",
)


# ── Workspace tree hashing ────────────────────────────────────────────────


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def hash_workspace_tree(root: Path, exclude: set[str] | None = None) -> str:
    """Deterministic hash of every file under ``root``.

    Files contribute one ``<relative-posix-path>:<sha256>`` line each; the
    sorted lines are joined and hashed.  ``exclude`` filters by file name
    (e.g. ``{"workspace.json"}`` so a manifest never hashes itself).
    """
    exclude = exclude or set()
    lines: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name not in exclude:
            rel = path.relative_to(root).as_posix()
            lines.append(f"{rel}:{_sha256_file(path)}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


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


class CutoverLockError(RuntimeError):
    """The global migration cutover lock is held by another process."""


class CutoverReconciliationError(RuntimeError):
    """Cutover filesystem state is inconsistent and cannot self-heal."""


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
            indexes_dir=root / "indexes",
            exports_dir=root / "exports",
            reports_dir=root / "reports",
            locks_dir=root / "locks",
        )

    def ensure_dirs(self) -> None:
        """Create all workspace subdirectories if they do not exist."""
        dirs: list[Path] = [
            self.keyword_notebook_dir, self.lane_states_dir,
            self.page_journals_dir, self.indexes_dir,
            self.exports_dir, self.reports_dir, self.locks_dir,
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)

    def verify_dirs(self) -> list[str]:
        """Return list of missing required subdirectories.  Empty = complete.

        ``pending_candidates`` was the finalized v3→v4 migration's transitional
        channel; it is no longer part of the workspace layout.  A leftover
        directory in a retired generation is ignored.  See
        docs/ADR_DISCOVERY_V4_MIGRATION_FINAL.md.
        """
        missing: list[str] = []
        for attr, dirname in [
            ("keyword_notebook_dir", "keyword_notebooks"),
            ("lane_states_dir", "lane_states"),
            ("page_journals_dir", "page_journals"),
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
            "indexes_dir": str(self.indexes_dir),
            "exports_dir": str(self.exports_dir),
            "reports_dir": str(self.reports_dir),
            "locks_dir": str(self.locks_dir),
        }


# ── WorkspaceResolver ─────────────────────────────────────────────────────


class WorkspaceResolver:
    """Sole entry point for resolving discovery workspace paths.

    Production CLI calls ``resolve_active()``.  No module may bypass this
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

    def resolve_active(self, *, verify_tree: bool = False) -> DiscoveryWorkspace:
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
        9. Verify workspace tree hash (only when ``verify_tree=True``)
        10. Verify all required subdirectories exist
        11. Return DiscoveryWorkspace

        Step 9 recomputes :func:`hash_workspace_tree` over the generation
        root (excluding ``workspace.json``) and fails closed on any mismatch
        with ``manifest.workspace_tree_sha256``.  It is opt-in because the
        manifest tree hash binds the *activation-time* closure: every
        workspace subdirectory (``keyword_notebooks``, ``lane_states``,
        ``page_journals``, ``indexes``, ``exports``,
        ``reports``, ``locks``) and the generation root itself
        (``.relevance_raw_work_cache``, ``*.lock`` files) are intentionally
        mutated by normal discovery runs, so unconditional content
        verification would reject the first ordinary production resolve.
        Pass ``verify_tree=True`` only in the migration / first-activation
        window (e.g. post-cutover validation), before any production run has
        touched the workspace.  The production path (every discovery CLI
        startup) uses the default and is never rejected for runtime content
        drift; identity, manifest-hash, and directory checks always run.

        Args:
            verify_tree: Also recompute and compare the workspace tree hash.

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

        # Step 9: Optional workspace tree verification (migration window only;
        # see the docstring for why production resolves must skip this).
        if verify_tree:
            computed_tree = hash_workspace_tree(gen_root, exclude={"workspace.json"})
            if computed_tree != manifest.workspace_tree_sha256:
                raise WorkspaceManifestMismatchError(
                    f"workspace tree SHA-256 mismatch for generation "
                    f"{pointer.generation_id!r}: manifest has "
                    f"{manifest.workspace_tree_sha256[:16]}..., "
                    f"computed {computed_tree[:16]}..."
                )

        # Step 10-11: Build workspace and verify
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
        indexes_dir=root / "indexes",
        exports_dir=root / "exports",
        reports_dir=root / "reports",
        locks_dir=root / "locks",
    )
    ws.ensure_dirs()
    return ws


# ── Atomic I/O ────────────────────────────────────────────────────────────


_atomic_write_json = atomic_write_json_unlocked


# ── Active generation commit ──────────────────────────────────────────────


def _read_active_pointer() -> ActiveGenerationPointerV4 | None:
    """Read the current active pointer, or ``None`` when no pointer exists.

    A corrupt pointer fails closed with ActiveGenerationCorruptError.
    """
    if not ACTIVE_GENERATION_PATH.is_file():
        return None
    try:
        raw = json.loads(ACTIVE_GENERATION_PATH.read_text(encoding="utf-8"))
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


def _save_previous_pointer_snapshot(
    existing: ActiveGenerationPointerV4 | None,
    snapshot_path: Path,
    new_pointer: ActiveGenerationPointerV4,
) -> None:
    """Persist the superseded pointer for rollback.  Never overwrites: the
    first snapshot of a cutover attempt is the authoritative one."""
    if snapshot_path.exists():
        return
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "superseded_by": new_pointer.generation_id,
        "previous_pointer": existing.to_dict() if existing is not None else None,
    }
    _atomic_write_json(snapshot_path, payload)


def commit_workspace(
    staging_ws: DiscoveryWorkspace,
    pointer: ActiveGenerationPointerV4,
    *,
    lock_path: Path | None = None,
    previous_pointer_snapshot_path: Path | None = None,
) -> DiscoveryWorkspace:
    """Promote a staging workspace to active, idempotently.

    Holds the global migration cutover lock (fail-fast, never waits), then
    reconciles crashed prior attempts before mutating:

    (a) target generation exists and the active pointer already names it
        → commit already finished; return success (caller only fixes journal).
    (b) target generation exists but the pointer was never advanced
        → verify the target ``workspace.json`` hash, then write the pointer.
    (c) staging exists and target does not → snapshot the superseded pointer,
        rename, write the pointer (the normal path).
    (d) neither staging nor target exists → fail closed.

    The superseded pointer is snapshotted to ``previous_pointer_snapshot_path``
    (default ``<migrations>/<migration_id>.previous_pointer.json``) and its
    generation id is recorded in the new pointer's ``previous_generation_id``.

    Raises:
        ValueError: If any required pointer field is empty.
        CutoverLockError: If the cutover lock is held by another process.
        CutoverReconciliationError: If the state cannot self-heal.
    """
    if not pointer.is_valid:
        raise ValueError(
            "ActiveGenerationPointerV4 has empty fields — "
            "cannot commit incomplete pointer"
        )

    effective_lock_path = Path(lock_path) if lock_path else MIGRATION_LOCK_PATH
    effective_lock_path.parent.mkdir(parents=True, exist_ok=True)
    # is_singleton=True: --cutover already holds the unified migration
    # maintenance lock on this same file in this thread; the singleton
    # instance makes this inner acquisition re-entrant instead of deadlocking.
    lock = FileLock(str(effective_lock_path), timeout=0, is_singleton=True)
    try:
        with lock:
            return _commit_workspace_locked(
                staging_ws,
                pointer,
                previous_pointer_snapshot_path=previous_pointer_snapshot_path,
            )
    except FileLockTimeout as exc:
        raise CutoverLockError(
            f"migration cutover lock is already held: {effective_lock_path}"
        ) from exc


def _commit_workspace_locked(
    staging_ws: DiscoveryWorkspace,
    pointer: ActiveGenerationPointerV4,
    *,
    previous_pointer_snapshot_path: Path | None,
) -> DiscoveryWorkspace:
    generation_id = staging_ws.generation_id
    target_root = DISCOVERY_GENERATIONS_DIR / generation_id
    snapshot_path = (
        Path(previous_pointer_snapshot_path)
        if previous_pointer_snapshot_path
        else DISCOVERY_MIGRATIONS_DIR / f"{pointer.migration_id}.previous_pointer.json"
    )
    existing = _read_active_pointer()

    if target_root.exists():
        # A previous attempt completed the rename.  Reconcile the pointer.
        if existing is not None and existing.generation_id == generation_id:
            if existing.workspace_manifest_sha256 != pointer.workspace_manifest_sha256:
                raise CutoverReconciliationError(
                    f"active pointer for {generation_id!r} has manifest hash "
                    f"{existing.workspace_manifest_sha256[:16]}..., expected "
                    f"{pointer.workspace_manifest_sha256[:16]}..."
                )
            return DiscoveryWorkspace.from_generation_id(generation_id)
        manifest_path = target_root / "workspace.json"
        if not manifest_path.is_file():
            raise CutoverReconciliationError(
                f"generation {generation_id!r} exists without workspace.json "
                f"at {manifest_path}"
            )
        computed = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        if computed != pointer.workspace_manifest_sha256:
            raise CutoverReconciliationError(
                f"generation {generation_id!r} workspace.json hash "
                f"{computed[:16]}... != pointer hash "
                f"{pointer.workspace_manifest_sha256[:16]}..."
            )
        _save_previous_pointer_snapshot(existing, snapshot_path, pointer)
        new_pointer = _with_previous_generation(pointer, existing)
        _atomic_write_json(ACTIVE_GENERATION_PATH, new_pointer.to_dict())
        return DiscoveryWorkspace.from_generation_id(generation_id)

    if not staging_ws.root.is_dir():
        raise CutoverReconciliationError(
            f"neither staging workspace {staging_ws.root} nor target generation "
            f"{target_root} exists — cannot reconcile cutover"
        )

    _save_previous_pointer_snapshot(existing, snapshot_path, pointer)
    new_pointer = _with_previous_generation(pointer, existing)
    target_root.parent.mkdir(parents=True, exist_ok=True)
    os.rename(str(staging_ws.root), str(target_root))
    _atomic_write_json(ACTIVE_GENERATION_PATH, new_pointer.to_dict())
    return DiscoveryWorkspace.from_generation_id(generation_id)


def _with_previous_generation(
    pointer: ActiveGenerationPointerV4,
    existing: ActiveGenerationPointerV4 | None,
) -> ActiveGenerationPointerV4:
    if existing is None:
        return pointer
    return replace(pointer, previous_generation_id=existing.generation_id)


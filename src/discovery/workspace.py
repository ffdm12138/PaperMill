"""Discovery v4 workspace: generation-isolated directory layout with atomic activation.

Layout::

    data/discovery/
    ├── active_generation.json          ← single atomic cutover point
    ├── generations/
    │   └── <generation_id>/
    │       ├── workspace.json
    │       ├── keyword_notebooks/
    │       ├── page_journals/
    │       ├── exports/
    │       ├── reports/
    │       └── locks/
    └── migrations/
        ├── .maintenance.lock           ← exclusive maintenance lock
        └── writer_leases/              ← shared writer lease files

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
from src.utils.atomic_io import (
    atomic_replace_bytes_unlocked,
    atomic_write_json_unlocked,
)
from src.discovery.contracts.manifest import (
    EMPTY_SET_SHA256,
    STORE_SCHEMA_VERSIONS_V4,
    WORKSPACE_MANIFEST_SCHEMA_VERSION_V4,
    ActiveGenerationPointerV4,
    DiscoveryWorkspaceManifestV4,
    validate_generation_id,
)

WORKSPACE_SCHEMA_VERSION = "4.0"

ACTIVE_GENERATION_PATH = DISCOVERY_DIR / "active_generation.json"
DISCOVERY_MAINTENANCE_LOCK_PATH = DISCOVERY_MIGRATIONS_DIR / ".maintenance.lock"

KNOWN_SUBDIRS = (
    "keyword_notebooks", "page_journals",
    "exports", "reports", "locks",
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


def _is_reparse_point(path: Path) -> bool:
    """True for symlinks, junctions, and other reparse points."""
    isjunction = getattr(os.path, "isjunction", None)  # Python 3.12+
    if path.is_symlink() or (isjunction is not None and isjunction(path)):
        return True
    try:
        # FILE_ATTRIBUTE_REPARSE_POINT; only present on Windows stat results.
        return bool(os.lstat(path).st_file_attributes & 0x400)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        # Non-Windows hosts have no st_file_attributes; is_symlink above
        # already covers POSIX links.
        return False


def _is_safe_workspace_subdir(path: Path, root: Path) -> bool:
    """A required workspace subdirectory must be a real directory directly
    inside the canonical root — never a symlink/junction/reparse point and
    never resolving outside the root."""
    if _is_reparse_point(path):
        return False
    if not path.is_dir():
        return False
    if path.parent != root:
        return False
    canonical_root = root.resolve()
    resolved = path.resolve()
    return resolved == canonical_root or canonical_root in resolved.parents


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


class CommitLockError(RuntimeError):
    """The global discovery maintenance lock is held by another process."""


class CommitReconciliationError(RuntimeError):
    """Workspace commit filesystem state is inconsistent and cannot self-heal."""


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
    page_journals_dir: Path
    exports_dir: Path
    reports_dir: Path
    locks_dir: Path

    def __post_init__(self) -> None:
        validate_generation_id(self.generation_id, "generation_id")

    @classmethod
    def from_generation_id(cls, generation_id: str) -> "DiscoveryWorkspace":
        """Create a workspace reference from a generation id."""
        root = DISCOVERY_GENERATIONS_DIR / generation_id
        return cls(
            generation_id=generation_id,
            root=root,
            keyword_notebook_dir=root / "keyword_notebooks",
            page_journals_dir=root / "page_journals",
            exports_dir=root / "exports",
            reports_dir=root / "reports",
            locks_dir=root / "locks",
        )

    def ensure_dirs(self) -> None:
        """Create all workspace subdirectories if they do not exist."""
        dirs: list[Path] = [
            self.keyword_notebook_dir,
            self.page_journals_dir,
            self.exports_dir, self.reports_dir, self.locks_dir,
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)

    def verify_dirs(self) -> list[str]:
        """Return list of missing or unsafe required subdirectories.  Empty = OK.

        A required subdirectory is reported when it is absent, is a
        symlink/junction/reparse point, or resolves outside the canonical
        workspace root — directory links must never escape the generation.

        ``pending_candidates`` was the finalized v3→v4 migration's transitional
        channel and ``lane_states`` / ``indexes`` belonged to the deleted dead
        v4 store stack; none are part of the workspace layout anymore.  A
        leftover directory in a retired generation is ignored.  See
        docs/ADR_DISCOVERY_V4_MIGRATION_FINAL.md.
        """
        problems: list[str] = []
        for attr, dirname in [
            ("keyword_notebook_dir", "keyword_notebooks"),
            ("page_journals_dir", "page_journals"),
            ("exports_dir", "exports"),
            ("reports_dir", "reports"),
            ("locks_dir", "locks"),
        ]:
            p = getattr(self, attr)
            if not _is_safe_workspace_subdir(p, self.root):
                problems.append(dirname)
        return problems

    def to_dict(self) -> dict[str, str]:
        return {
            "generation_id": self.generation_id,
            "root": str(self.root),
            "keyword_notebook_dir": str(self.keyword_notebook_dir),
            "page_journals_dir": str(self.page_journals_dir),
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
        workspace subdirectory (``keyword_notebooks``,
        ``page_journals``, ``exports``,
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
        if _is_reparse_point(gen_root) or not gen_root.is_dir():
            raise ActiveGenerationCorruptError(
                f"active generation {pointer.generation_id!r} "
                f"does not exist as a real directory at {gen_root}"
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
        ws = DiscoveryWorkspace(
            generation_id=pointer.generation_id,
            root=gen_root,
            keyword_notebook_dir=gen_root / "keyword_notebooks",
            page_journals_dir=gen_root / "page_journals",
            exports_dir=gen_root / "exports",
            reports_dir=gen_root / "reports",
            locks_dir=gen_root / "locks",
        )
        missing = ws.verify_dirs()
        if missing:
            raise WorkspaceIncompleteError(
                f"workspace {pointer.generation_id} missing or unsafe "
                f"directories: {missing}"
            )

        return ws

    # ── Explicit workspace root (test/staging) ───────────────────────────

    @staticmethod
    def resolve_explicit_workspace(
        root: Path,
        *,
        verify_tree: bool = False,
    ) -> DiscoveryWorkspace:
        """Resolve an explicit v4 workspace root, fail closed.

        This is the sole entry point for the ``--workspace-root`` /
        ``workspace_root=`` overrides used by test suites and staging runs.
        Unlike the retired hand-rolled helpers, it enforces the same v4
        closure as production, with no way to skip verification:

        1. ``root`` must be an existing real directory (never a
           symlink/junction); its name becomes the generation id.
        2. ``workspace.json`` must exist, strict-parse as
           ``DiscoveryWorkspaceManifestV4``, and its ``generation_id`` must
           equal the root directory name.
        3. All required v4 subdirectories must exist as real in-root
           directories (``verify_dirs``).

        ``verify_tree`` recomputes the workspace tree hash against the
        manifest and, exactly like :meth:`resolve_active`, stays opt-in:
        workspace content drifts intentionally at runtime.

        Raises:
            WorkspaceIncompleteError: Root or required subdirectories missing
                or unsafe.
            WorkspaceManifestMissingError: No workspace.json in root.
            WorkspaceManifestMismatchError: Manifest invalid or not bound to
                the root directory name.

        Never returns ``None``.
        """
        raw_root = Path(root)
        if _is_reparse_point(raw_root):
            raise WorkspaceIncompleteError(
                f"explicit workspace root must not be a symlink/junction: "
                f"{raw_root}"
            )
        root = raw_root.resolve()
        if not root.is_dir():
            raise WorkspaceIncompleteError(
                f"explicit workspace root does not exist: {root}"
            )
        generation_id = root.name

        workspace_json = root / "workspace.json"
        if not workspace_json.is_file():
            raise WorkspaceManifestMissingError(
                f"workspace.json missing in explicit workspace root "
                f"{root}; legacy flat directories are retired — pass a "
                f"complete v4 workspace (see init_discovery_workspace.py)"
            )
        try:
            manifest_data = json.loads(
                workspace_json.read_bytes().decode("utf-8")
            )
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise WorkspaceManifestMismatchError(
                f"cannot parse workspace.json: {exc}"
            ) from exc
        try:
            manifest = DiscoveryWorkspaceManifestV4.from_dict_strict(
                manifest_data
            )
        except (ValueError, TypeError) as exc:
            raise WorkspaceManifestMismatchError(
                f"workspace.json is not strict V4: {exc}"
            ) from exc
        if manifest.generation_id != generation_id:
            raise WorkspaceManifestMismatchError(
                f"workspace generation_id {manifest.generation_id!r} "
                f"!= explicit root name {generation_id!r}"
            )
        if verify_tree:
            computed_tree = hash_workspace_tree(
                root, exclude={"workspace.json"}
            )
            if computed_tree != manifest.workspace_tree_sha256:
                raise WorkspaceManifestMismatchError(
                    f"workspace tree SHA-256 mismatch for explicit root "
                    f"{root}: manifest has "
                    f"{manifest.workspace_tree_sha256[:16]}..., "
                    f"computed {computed_tree[:16]}..."
                )

        ws = DiscoveryWorkspace(
            generation_id=generation_id,
            root=root,
            keyword_notebook_dir=root / "keyword_notebooks",
            page_journals_dir=root / "page_journals",
            exports_dir=root / "exports",
            reports_dir=root / "reports",
            locks_dir=root / "locks",
        )
        missing = ws.verify_dirs()
        if missing:
            raise WorkspaceIncompleteError(
                f"explicit workspace {root} missing or unsafe directories: "
                f"{missing}"
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
        page_journals_dir=root / "page_journals",
        exports_dir=root / "exports",
        reports_dir=root / "reports",
        locks_dir=root / "locks",
    )
    ws.ensure_dirs()
    return ws


# ── Fresh-install bootstrap ───────────────────────────────────────────────

BOOTSTRAP_MIGRATION_ID = "bootstrap-v4-init"


def build_workspace_manifest(
    generation_id: str,
    workspace_root: Path,
    *,
    migration_id: str,
) -> DiscoveryWorkspaceManifestV4:
    """Build the creation-time manifest for a complete, empty v4 workspace.

    All counts are zero and every store-set hash is
    :data:`~src.discovery.contracts.manifest.EMPTY_SET_SHA256`: a fresh
    generation owns no notebooks or journals, and the empty closure is
    hash-explicit rather than an empty string.  ``store_schema_versions``
    binds the live v4 store set.  ``workspace_tree_sha256`` binds the
    current tree (``workspace.json`` excluded, since the manifest never
    hashes itself).
    """
    now = datetime.now(timezone.utc).isoformat()
    return DiscoveryWorkspaceManifestV4(
        schema_version=WORKSPACE_MANIFEST_SCHEMA_VERSION_V4,
        generation_id=generation_id,
        migration_id=migration_id,
        created_at=now,
        completed_at=now,
        store_schema_versions=dict(STORE_SCHEMA_VERSIONS_V4),
        workspace_tree_sha256=hash_workspace_tree(
            Path(workspace_root), exclude={"workspace.json"}
        ),
    )


def write_workspace_manifest(
    workspace_root: Path,
    manifest: DiscoveryWorkspaceManifestV4,
) -> str:
    """Atomically write ``workspace.json`` and return its SHA-256 hex digest.

    The digest is computed over the exact bytes written, which is what an
    ``ActiveGenerationPointerV4.workspace_manifest_sha256`` binds.
    """
    raw = (
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    atomic_replace_bytes_unlocked(Path(workspace_root) / "workspace.json", raw)
    return hashlib.sha256(raw).hexdigest()


def bootstrap_initial_workspace(
    generation_id: str | None = None,
) -> tuple[DiscoveryWorkspace, bool]:
    """Create and activate the first v4 generation on a fresh install.

    Idempotent: when a valid active generation already resolves, it is
    returned unchanged with ``created=False``.  Any other resolution
    failure (corrupt pointer, damaged manifest) propagates — bootstrap
    never stomps an existing but broken closure.

    Crash recovery: before creating anything, the previous attempt's
    state is inspected and resumed deterministically:

    - crash after staging creation → the staging tree is reused and the
      manifest is (re)written into it;
    - crash after manifest write → the existing staging manifest is
      validated and committed as-is;
    - crash after the rename, before the pointer write → the existing
      generation is strictly re-validated and the pointer is rebuilt
      from ITS original manifest hash (never a new manifest);
    - crash after the pointer write → plain idempotent return above.

    Ambiguous states (multiple unpointed generations or staging dirs)
    fail closed with :class:`CommitReconciliationError` instead of
    guessing.  Returns ``(workspace, created)``.
    """
    try:
        return WorkspaceResolver().resolve_active(), False
    except ActiveGenerationMissingError:
        pass

    recovered = _recover_unpointed_generation(generation_id)
    if recovered is not None:
        return recovered, False

    staging = _resume_or_create_staging(generation_id)
    manifest_path = staging.root / "workspace.json"
    if manifest_path.is_file():
        # Resume a staged-but-never-committed attempt: validate the
        # existing manifest and bind the pointer to its exact bytes.
        manifest_sha256 = _validate_staged_manifest(staging)
        migration_id = DiscoveryWorkspaceManifestV4.from_dict_strict(
            json.loads(manifest_path.read_bytes().decode("utf-8"))
        ).migration_id
    else:
        manifest = build_workspace_manifest(
            staging.generation_id,
            staging.root,
            migration_id=BOOTSTRAP_MIGRATION_ID,
        )
        manifest_sha256 = write_workspace_manifest(staging.root, manifest)
        migration_id = BOOTSTRAP_MIGRATION_ID
    pointer = ActiveGenerationPointerV4(
        generation_id=staging.generation_id,
        workspace_manifest_sha256=manifest_sha256,
        activated_at=datetime.now(timezone.utc).isoformat(),
        migration_id=migration_id,
    )
    return commit_workspace(staging, pointer), True


# ── Active generation reseal ──────────────────────────────────────────────


def reseal_active_generation_manifest(*, apply: bool = False) -> dict[str, Any]:
    """Re-seal the active generation's ``workspace.json`` under the CURRENT
    strict manifest contract, preserving the generation.

    Needed exactly once after the final-freeze contract tightening: a
    generation sealed by the pre-freeze lenient writer carries empty set
    hashes, ``{}`` store schema versions, and retired fields, which the
    strict resolver now rejects as corrupt.  The reseal recomputes real
    counts and store-set hashes from the actual workspace content, writes
    a new manifest, and rebinds the active pointer to its hash.

    ``created_at`` is preserved from the old manifest when it parses as a
    valid timestamp; ``completed_at`` is the reseal time.  Everything is
    fail closed: an unreadable old manifest, an unparseable notebook, or a
    post-write verification failure raises and (for ``apply=True``) the
    pointer write is the LAST step, so a failure leaves the previous state
    untouched.

    With ``apply=False`` nothing is written; the returned report shows the
    planned new manifest.  The caller MUST hold the discovery maintenance
    lock for ``apply=True``.
    """
    pointer = _read_active_pointer()  # strict; corrupt pointer fails closed
    gen_root = DISCOVERY_GENERATIONS_DIR / pointer.generation_id
    if _is_reparse_point(gen_root) or not gen_root.is_dir():
        raise ActiveGenerationCorruptError(
            f"active generation {pointer.generation_id!r} is not a real "
            f"directory at {gen_root}"
        )
    manifest_path = gen_root / "workspace.json"
    if not manifest_path.is_file():
        raise WorkspaceManifestMissingError(
            f"workspace.json missing in generation {pointer.generation_id}"
        )
    old_raw = manifest_path.read_bytes()
    try:
        old_data = json.loads(old_raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WorkspaceManifestMismatchError(
            f"cannot parse existing workspace.json for reseal: {exc}"
        ) from exc
    if not isinstance(old_data, dict):
        raise WorkspaceManifestMismatchError(
            "existing workspace.json is not a JSON object"
        )

    notebooks_dir = gen_root / "keyword_notebooks"
    notebook_files = sorted(notebooks_dir.glob("*.json"))
    query_count = 0
    for notebook_file in notebook_files:
        try:
            notebook_data = json.loads(
                notebook_file.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            raise WorkspaceManifestMismatchError(
                f"cannot parse notebook {notebook_file.name} during reseal: "
                f"{exc}"
            ) from exc
        queries = notebook_data.get("search_queries") or {}
        if not isinstance(queries, dict):
            raise WorkspaceManifestMismatchError(
                f"notebook {notebook_file.name} has non-dict search_queries"
            )
        query_count += len(queries)

    page_journals_dir = gen_root / "page_journals"
    page_journal_files = [
        p for p in page_journals_dir.rglob("*") if p.is_file()
    ]

    now = datetime.now(timezone.utc).isoformat()
    created_at = now
    old_created = old_data.get("created_at")
    if isinstance(old_created, str) and old_created.strip():
        try:
            candidate = old_created[:-1] + "+00:00" if old_created.endswith(
                ("Z", "z")
            ) else old_created
            datetime.fromisoformat(candidate)
            created_at = old_created
        except ValueError:
            created_at = now

    manifest = DiscoveryWorkspaceManifestV4(
        schema_version=WORKSPACE_MANIFEST_SCHEMA_VERSION_V4,
        generation_id=pointer.generation_id,
        migration_id=pointer.migration_id,
        created_at=created_at,
        completed_at=now,
        notebook_count=len(notebook_files),
        query_count=query_count,
        page_journal_count=len(page_journal_files),
        notebook_set_hash=hash_workspace_tree(notebooks_dir),
        page_journal_set_hash=hash_workspace_tree(page_journals_dir),
        relevance_profile_hash=EMPTY_SET_SHA256,
        store_schema_versions=dict(STORE_SCHEMA_VERSIONS_V4),
        workspace_tree_sha256=hash_workspace_tree(
            gen_root, exclude={"workspace.json"}
        ),
        migration_inventory_sha256=EMPTY_SET_SHA256,
    )
    new_hash = hashlib.sha256(
        (
            json.dumps(
                manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()
    report: dict[str, Any] = {
        "applied": apply,
        "generation_id": pointer.generation_id,
        "old_manifest_sha256": hashlib.sha256(old_raw).hexdigest(),
        "new_manifest_sha256": new_hash,
        "new_manifest": manifest.to_dict(),
    }
    if apply:
        written_hash = write_workspace_manifest(gen_root, manifest)
        new_pointer = ActiveGenerationPointerV4(
            generation_id=pointer.generation_id,
            workspace_manifest_sha256=written_hash,
            activated_at=now,
            migration_id=pointer.migration_id,
            previous_generation_id=pointer.previous_generation_id,
        )
        _atomic_write_json(ACTIVE_GENERATION_PATH, new_pointer.to_dict())
        # Post-write verification: the resealed closure must resolve under
        # the strict contract, including the tree hash.
        WorkspaceResolver().resolve_active(verify_tree=True)
        report["verified"] = True
    return report





def _workspace_for_root(generation_id: str, root: Path) -> DiscoveryWorkspace:
    return DiscoveryWorkspace(
        generation_id=generation_id,
        root=root,
        keyword_notebook_dir=root / "keyword_notebooks",
        page_journals_dir=root / "page_journals",
        exports_dir=root / "exports",
        reports_dir=root / "reports",
        locks_dir=root / "locks",
    )


def _strict_validate_generation_root(gen_root: Path) -> DiscoveryWorkspaceManifestV4:
    """Strictly validate an on-disk generation and return its manifest.

    Every failure raises :class:`CommitReconciliationError` — recovery
    never repairs damaged content, it only resumes well-formed state.
    """
    def _fail(reason: str) -> None:
        raise CommitReconciliationError(
            f"cannot recover generation at {gen_root}: {reason}"
        )

    if _is_reparse_point(gen_root) or not gen_root.is_dir():
        _fail("not a real directory")
    manifest_path = gen_root / "workspace.json"
    if not manifest_path.is_file():
        _fail("workspace.json missing")
    try:
        manifest_data = json.loads(manifest_path.read_bytes().decode("utf-8"))
        manifest = DiscoveryWorkspaceManifestV4.from_dict_strict(manifest_data)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError) as exc:
        _fail(f"workspace.json is not strict V4: {exc}")
    if manifest.generation_id != gen_root.name:
        _fail(
            f"manifest generation_id {manifest.generation_id!r} != "
            f"directory name {gen_root.name!r}"
        )
    computed_tree = hash_workspace_tree(gen_root, exclude={"workspace.json"})
    if computed_tree != manifest.workspace_tree_sha256:
        _fail("workspace tree hash mismatch — content changed after sealing")
    ws = _workspace_for_root(gen_root.name, gen_root)
    problems = ws.verify_dirs()
    if problems:
        _fail(f"missing or unsafe directories: {problems}")
    return manifest


def _recover_unpointed_generation(
    requested_id: str | None,
) -> DiscoveryWorkspace | None:
    """Recover from a crash between the staging rename and pointer write.

    The renamed generation already exists but no active pointer does.  The
    existing generation is strictly re-validated and the pointer is rebuilt
    from its ORIGINAL manifest hash — no new manifest is ever generated.
    Returns the recovered workspace, or ``None`` when no unpointed
    generation exists.
    """
    candidates: list[Path] = []
    if requested_id is not None:
        gen_root = DISCOVERY_GENERATIONS_DIR / requested_id
        if gen_root.is_dir() or _is_reparse_point(gen_root):
            candidates.append(gen_root)
    elif DISCOVERY_GENERATIONS_DIR.is_dir():
        candidates = [
            child
            for child in sorted(DISCOVERY_GENERATIONS_DIR.iterdir())
            if child.is_dir() and not child.name.startswith(".")
        ]
    if not candidates:
        return None
    if len(candidates) > 1:
        raise CommitReconciliationError(
            f"multiple generation directories exist but no active pointer: "
            f"{[c.name for c in candidates]} — refusing to guess; inspect "
            f"and remove the stale one(s) manually"
        )
    gen_root = candidates[0]
    manifest = _strict_validate_generation_root(gen_root)
    manifest_raw = (gen_root / "workspace.json").read_bytes()
    pointer = ActiveGenerationPointerV4(
        generation_id=gen_root.name,
        workspace_manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
        activated_at=datetime.now(timezone.utc).isoformat(),
        migration_id=manifest.migration_id,
    )
    return commit_workspace(_workspace_for_root(gen_root.name, gen_root), pointer)


def _resume_or_create_staging(requested_id: str | None) -> DiscoveryWorkspace:
    """Resume a leftover staging workspace or create a fresh one.

    A staging tree left by a crashed attempt is reused as-is (the manifest
    step is idempotent); multiple leftover stagings fail closed.
    """
    existing: list[Path] = []
    if requested_id is not None:
        root = STAGING_DIR / requested_id
        if root.is_dir():
            existing.append(root)
    elif STAGING_DIR.is_dir():
        existing = [
            child for child in sorted(STAGING_DIR.iterdir()) if child.is_dir()
        ]
    if not existing:
        return create_staging_workspace(requested_id)
    if len(existing) > 1:
        raise CommitReconciliationError(
            f"multiple staging workspaces exist: "
            f"{[c.name for c in existing]} — refusing to guess; inspect "
            f"and remove the stale one(s) manually"
        )
    root = existing[0]
    if _is_reparse_point(root):
        raise CommitReconciliationError(
            f"staging workspace is a symlink/junction: {root}"
        )
    ws = _workspace_for_root(root.name, root)
    ws.ensure_dirs()
    return ws


def _validate_staged_manifest(staging: DiscoveryWorkspace) -> str:
    """Validate an existing staged manifest and return its SHA-256.

    The pointer must bind the exact staged bytes, so the manifest is
    strict-parsed and the staging tree hash is recomputed.
    """
    def _fail(reason: str) -> None:
        raise CommitReconciliationError(
            f"cannot resume staging at {staging.root}: {reason}"
        )

    manifest_path = staging.root / "workspace.json"
    try:
        manifest_data = json.loads(manifest_path.read_bytes().decode("utf-8"))
        manifest = DiscoveryWorkspaceManifestV4.from_dict_strict(manifest_data)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError) as exc:
        _fail(f"workspace.json is not strict V4: {exc}")
    if manifest.generation_id != staging.generation_id:
        _fail(
            f"manifest generation_id {manifest.generation_id!r} != staging "
            f"id {staging.generation_id!r}"
        )
    computed_tree = hash_workspace_tree(staging.root, exclude={"workspace.json"})
    if computed_tree != manifest.workspace_tree_sha256:
        _fail("staging tree hash mismatch — content changed after sealing")
    problems = staging.verify_dirs()
    if problems:
        _fail(f"missing or unsafe directories: {problems}")
    return hashlib.sha256(manifest_path.read_bytes()).hexdigest()


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

    Holds the global discovery maintenance lock (fail-fast, never waits),
    then reconciles crashed prior attempts before mutating:

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
        CommitLockError: If the maintenance lock is held by another process.
        CommitReconciliationError: If the state cannot self-heal.

    A constructed ``ActiveGenerationPointerV4`` is fully validated by its
    own ``__post_init__``; no incomplete pointer can reach this function.
    """
    effective_lock_path = Path(lock_path) if lock_path else DISCOVERY_MAINTENANCE_LOCK_PATH
    effective_lock_path.parent.mkdir(parents=True, exist_ok=True)
    # is_singleton=True: a maintenance command already holds the unified
    # discovery maintenance lock on this same file in this thread; the
    # singleton instance makes this inner acquisition re-entrant instead of
    # deadlocking.
    lock = FileLock(str(effective_lock_path), timeout=0, is_singleton=True)
    try:
        with lock:
            return _commit_workspace_locked(
                staging_ws,
                pointer,
                previous_pointer_snapshot_path=previous_pointer_snapshot_path,
            )
    except FileLockTimeout as exc:
        raise CommitLockError(
            f"discovery maintenance lock is already held: {effective_lock_path}"
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
                raise CommitReconciliationError(
                    f"active pointer for {generation_id!r} has manifest hash "
                    f"{existing.workspace_manifest_sha256[:16]}..., expected "
                    f"{pointer.workspace_manifest_sha256[:16]}..."
                )
            return DiscoveryWorkspace.from_generation_id(generation_id)
        manifest_path = target_root / "workspace.json"
        if not manifest_path.is_file():
            raise CommitReconciliationError(
                f"generation {generation_id!r} exists without workspace.json "
                f"at {manifest_path}"
            )
        computed = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        if computed != pointer.workspace_manifest_sha256:
            raise CommitReconciliationError(
                f"generation {generation_id!r} workspace.json hash "
                f"{computed[:16]}... != pointer hash "
                f"{pointer.workspace_manifest_sha256[:16]}..."
            )
        _save_previous_pointer_snapshot(existing, snapshot_path, pointer)
        new_pointer = _with_previous_generation(pointer, existing)
        _atomic_write_json(ACTIVE_GENERATION_PATH, new_pointer.to_dict())
        return DiscoveryWorkspace.from_generation_id(generation_id)

    if not staging_ws.root.is_dir():
        raise CommitReconciliationError(
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


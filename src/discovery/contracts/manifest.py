"""Discovery v4 strict manifest contracts.

``ActiveGenerationPointerV4`` — the single atomic cutover point in
``data/discovery/active_generation.json``.  All fields must be non-empty
and type-exact.

``DiscoveryWorkspaceManifestV4`` — the ``workspace.json`` inside each
generation directory.  Captures the complete hash-bound state of one
generation at creation time.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Mapping

from src.discovery.contracts.page_journal import PAGE_SCHEMA_VERSION_V4

# ── Schema version ───────────────────────────────────────────────────────

WORKSPACE_MANIFEST_SCHEMA_VERSION_V4 = "4.0"

# ── Validation ───────────────────────────────────────────────────────────

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_GEN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _check_non_blank(value: str, field_name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be non-blank str, got {value!r}")


# ── Active generation pointer ────────────────────────────────────────────


@dataclass(frozen=True)
class ActiveGenerationPointerV4:
    """The single atomic cutover point at ``data/discovery/active_generation.json``.

    ALL fields must be non-empty and type-exact.  An empty field means the
    pointer is corrupt or was produced by the v103 pseudo-v4 migration.
    ``previous_generation_id`` is the only optional field: it records the
    generation this pointer superseded at cutover (absent on first install).
    """

    schema_version: Literal["4.0"] = "4.0"
    generation_id: str = ""
    workspace_manifest_sha256: str = ""
    activated_at: str = ""
    migration_id: str = ""
    previous_generation_id: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != "4.0":
            raise ValueError(
                f"schema_version must be '4.0', got {self.schema_version!r}"
            )

        self._check_generation_id(self.generation_id, "generation_id")
        if self.previous_generation_id is not None:
            self._check_generation_id(
                self.previous_generation_id, "previous_generation_id"
            )

        _check_non_blank(self.workspace_manifest_sha256, "workspace_manifest_sha256")
        if not _HEX64_RE.match(self.workspace_manifest_sha256):
            raise ValueError(
                f"workspace_manifest_sha256 must be 64 lowercase hex chars, "
                f"got {self.workspace_manifest_sha256!r}"
            )

        _check_non_blank(self.activated_at, "activated_at")
        # Basic timezone-aware ISO-8601 check
        if "T" not in self.activated_at or (
            "+" not in self.activated_at and "Z" not in self.activated_at
        ):
            raise ValueError(
                f"activated_at must be timezone-aware ISO-8601, "
                f"got {self.activated_at!r}"
            )

        _check_non_blank(self.migration_id, "migration_id")

    @staticmethod
    def _check_generation_id(value: str, field_name: str) -> None:
        _check_non_blank(value, field_name)
        if not _GEN_ID_RE.match(value):
            raise ValueError(
                f"{field_name} contains forbidden characters: {value!r}"
            )
        if "/" in value or "\\" in value:
            raise ValueError(
                f"{field_name} contains path traversal: {value!r}"
            )

    @property
    def is_valid(self) -> bool:
        """True if all fields are non-empty (not a v103 pseudo-v4 marker)."""
        return bool(
            self.generation_id
            and self.workspace_manifest_sha256
            and self.activated_at
            and self.migration_id
        )

    @classmethod
    def from_dict_strict(cls, data: Mapping[str, Any]) -> "ActiveGenerationPointerV4":
        """Parse from JSON dict.  Rejects unknown fields and empty values."""
        if not isinstance(data, (dict, Mapping)):
            raise TypeError(f"expected dict, got {type(data).__name__}")

        allowed = {
            "schema_version", "generation_id",
            "workspace_manifest_sha256", "activated_at", "migration_id",
            "previous_generation_id",
        }
        extra = set(data) - allowed
        if extra:
            raise ValueError(
                f"ActiveGenerationPointerV4 unknown fields: {sorted(extra)}"
            )

        previous = data.get("previous_generation_id")
        return cls(
            schema_version="4.0",
            generation_id=str(data.get("generation_id", "")),
            workspace_manifest_sha256=str(data.get("workspace_manifest_sha256", "")),
            activated_at=str(data.get("activated_at", "")),
            migration_id=str(data.get("migration_id", "")),
            previous_generation_id=str(previous) if previous is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "generation_id": self.generation_id,
            "workspace_manifest_sha256": self.workspace_manifest_sha256,
            "activated_at": self.activated_at,
            "migration_id": self.migration_id,
        }
        if self.previous_generation_id is not None:
            payload["previous_generation_id"] = self.previous_generation_id
        return payload


# ── Workspace manifest ───────────────────────────────────────────────────


@dataclass(frozen=True)
class DiscoveryWorkspaceManifestV4:
    """Immutable workspace manifest written to ``workspace.json`` inside
    each generation directory.

    Contains the hash-bound closure of all workspace state at creation time.
    """

    schema_version: str = WORKSPACE_MANIFEST_SCHEMA_VERSION_V4
    generation_id: str = ""
    migration_id: str = ""
    created_at: str = ""
    completed_at: str = ""
    notebook_count: int = 0
    query_count: int = 0
    lane_count: int = 0
    page_journal_count: int = 0
    pending_candidate_count: int = 0
    notebook_set_hash: str = ""
    lane_state_set_hash: str = ""
    page_journal_set_hash: str = ""
    pending_set_hash: str = ""
    relevance_profile_hash: str = ""
    store_schema_versions: dict[str, str] | None = None
    workspace_tree_sha256: str = ""
    migration_inventory_sha256: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != WORKSPACE_MANIFEST_SCHEMA_VERSION_V4:
            raise ValueError(
                f"schema_version must be {WORKSPACE_MANIFEST_SCHEMA_VERSION_V4!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generation_id": self.generation_id,
            "migration_id": self.migration_id,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "notebook_count": self.notebook_count,
            "query_count": self.query_count,
            "lane_count": self.lane_count,
            "page_journal_count": self.page_journal_count,
            "pending_candidate_count": self.pending_candidate_count,
            "notebook_set_hash": self.notebook_set_hash,
            "lane_state_set_hash": self.lane_state_set_hash,
            "page_journal_set_hash": self.page_journal_set_hash,
            "pending_set_hash": self.pending_set_hash,
            "relevance_profile_hash": self.relevance_profile_hash,
            "store_schema_versions": self.store_schema_versions or {},
            "workspace_tree_sha256": self.workspace_tree_sha256,
            "migration_inventory_sha256": self.migration_inventory_sha256,
        }

    @classmethod
    def from_dict_strict(
        cls, data: Mapping[str, Any]
    ) -> "DiscoveryWorkspaceManifestV4":
        allowed = {
            "schema_version", "generation_id", "migration_id",
            "created_at", "completed_at",
            "notebook_count", "query_count", "lane_count",
            "page_journal_count", "pending_candidate_count",
            "notebook_set_hash", "lane_state_set_hash",
            "page_journal_set_hash", "pending_set_hash",
            "relevance_profile_hash", "store_schema_versions",
            "workspace_tree_sha256", "migration_inventory_sha256",
        }
        extra = set(data) - allowed
        if extra:
            raise ValueError(
                f"DiscoveryWorkspaceManifestV4 unknown fields: {sorted(extra)}"
            )
        return cls(
            schema_version=str(data.get("schema_version", WORKSPACE_MANIFEST_SCHEMA_VERSION_V4)),
            generation_id=str(data.get("generation_id", "")),
            migration_id=str(data.get("migration_id", "")),
            created_at=str(data.get("created_at", "")),
            completed_at=str(data.get("completed_at", "")),
            notebook_count=int(data.get("notebook_count", 0)) if not isinstance(data.get("notebook_count"), bool) else 0,
            query_count=int(data.get("query_count", 0)) if not isinstance(data.get("query_count"), bool) else 0,
            lane_count=int(data.get("lane_count", 0)) if not isinstance(data.get("lane_count"), bool) else 0,
            page_journal_count=int(data.get("page_journal_count", 0)) if not isinstance(data.get("page_journal_count"), bool) else 0,
            pending_candidate_count=int(data.get("pending_candidate_count", 0)) if not isinstance(data.get("pending_candidate_count"), bool) else 0,
            notebook_set_hash=str(data.get("notebook_set_hash", "")),
            lane_state_set_hash=str(data.get("lane_state_set_hash", "")),
            page_journal_set_hash=str(data.get("page_journal_set_hash", "")),
            pending_set_hash=str(data.get("pending_set_hash", "")),
            relevance_profile_hash=str(data.get("relevance_profile_hash", "")),
            store_schema_versions=dict(data["store_schema_versions"]) if isinstance(data.get("store_schema_versions"), dict) else None,
            workspace_tree_sha256=str(data.get("workspace_tree_sha256", "")),
            migration_inventory_sha256=str(data.get("migration_inventory_sha256", "")),
        )

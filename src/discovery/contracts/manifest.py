"""Discovery v4 strict manifest contracts.

``ActiveGenerationPointerV4`` — the single atomic cutover point in
``data/discovery/active_generation.json``.  All fields must be present,
type-exact, and semantically valid; the parser never coerces, defaults,
or repairs protocol input.

``DiscoveryWorkspaceManifestV4`` — the ``workspace.json`` inside each
generation directory.  Captures the complete hash-bound state of one
generation at creation time.  Bootstrap, activation, and the resolver
share the single validator in this module; an empty store set is
represented by :data:`EMPTY_SET_SHA256`, never by an empty string.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Mapping

from src.discovery.contracts.notebook import NOTEBOOK_SCHEMA_VERSION_V4
from src.discovery.contracts.page_journal import PAGE_SCHEMA_VERSION_V4

# ── Schema version ───────────────────────────────────────────────────────

WORKSPACE_MANIFEST_SCHEMA_VERSION_V4 = "4.0"

# Canonical hash of an empty store set: SHA-256 of the canonical JSON
# serialization of an empty (name, digest) list.  Bootstrap uses this for
# every store set hash so an empty closure is distinguishable from a
# missing/corrupt one.
EMPTY_SET_SHA256 = hashlib.sha256(b"[]").hexdigest()

# The exact live store set of a v4 workspace and its schema versions.
# ``store_schema_versions`` in a manifest must equal this mapping.
STORE_SCHEMA_VERSIONS_V4: dict[str, str] = {
    "notebooks": NOTEBOOK_SCHEMA_VERSION_V4,
    "page_journals": PAGE_SCHEMA_VERSION_V4,
}

# ── Validation helpers ───────────────────────────────────────────────────

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_GEN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def _check_exact_str(value: Any, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be non-blank str, got {value!r}")
    if value != value.strip():
        raise ValueError(
            f"{field_name} must not have surrounding whitespace: {value!r}"
        )
    return value


def _check_generation_id(value: Any, field_name: str) -> str:
    _check_exact_str(value, field_name)
    if value in (".", ".."):
        raise ValueError(
            f"{field_name} must not be a dot path segment: {value!r}"
        )
    if not _GEN_ID_RE.match(value):
        raise ValueError(
            f"{field_name} contains forbidden characters: {value!r}"
        )
    if "/" in value or "\\" in value:
        raise ValueError(f"{field_name} contains path traversal: {value!r}")
    if value.split(".")[0].lower() in _WINDOWS_RESERVED_NAMES:
        raise ValueError(
            f"{field_name} is a reserved device name: {value!r}"
        )
    return value


def validate_generation_id(value: Any, field_name: str = "generation_id") -> str:
    """Public generation/migration id validator shared with workspace code."""
    return _check_generation_id(value, field_name)


def _check_hex64(value: Any, field_name: str) -> str:
    if type(value) is not str or not _HEX64_RE.match(value):
        raise ValueError(
            f"{field_name} must be 64 lowercase hex chars, got {value!r}"
        )
    return value


def _check_iso8601_tz(value: Any, field_name: str) -> datetime:
    """Strict ISO-8601 parse; the timestamp must be timezone-aware."""
    _check_exact_str(value, field_name)
    candidate = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must be a valid ISO-8601 timestamp, "
            f"got {value!r}: {exc}"
        ) from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError(
            f"{field_name} must be timezone-aware ISO-8601, got {value!r}"
        )
    return parsed


def _check_count(value: Any, field_name: str) -> int:
    # bool is a subclass of int; protocol input must be a real int.
    if type(value) is not int or value < 0:
        raise ValueError(
            f"{field_name} must be a non-negative int, got {value!r}"
        )
    return value


def _check_key_set(
    data: Mapping[str, Any],
    *,
    required: frozenset[str],
    optional: frozenset[str],
    owner: str,
) -> None:
    missing = required - set(data)
    if missing:
        raise ValueError(f"{owner} missing fields: {sorted(missing)}")
    extra = set(data) - required - optional
    if extra:
        raise ValueError(f"{owner} unknown fields: {sorted(extra)}")


# ── Active generation pointer ────────────────────────────────────────────

_POINTER_REQUIRED = frozenset(
    {
        "schema_version",
        "generation_id",
        "workspace_manifest_sha256",
        "activated_at",
        "migration_id",
    }
)
_POINTER_OPTIONAL = frozenset({"previous_generation_id"})


@dataclass(frozen=True)
class ActiveGenerationPointerV4:
    """The single atomic cutover point at ``data/discovery/active_generation.json``.

    ALL fields must be present, non-empty, and type-exact.  An empty field
    means the pointer is corrupt or was produced by the v103 pseudo-v4
    migration.  ``previous_generation_id`` is the only optional field: it
    records the generation this pointer superseded at cutover (absent on
    first install).
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

        _check_generation_id(self.generation_id, "generation_id")
        if self.previous_generation_id is not None:
            _check_generation_id(
                self.previous_generation_id, "previous_generation_id"
            )

        _check_hex64(self.workspace_manifest_sha256, "workspace_manifest_sha256")
        _check_iso8601_tz(self.activated_at, "activated_at")
        _check_generation_id(self.migration_id, "migration_id")

    @classmethod
    def from_dict_strict(cls, data: Mapping[str, Any]) -> "ActiveGenerationPointerV4":
        """Parse from JSON dict.  Exact key set, exact types, no coercion."""
        if not isinstance(data, (dict, Mapping)):
            raise TypeError(f"expected dict, got {type(data).__name__}")
        _check_key_set(
            data,
            required=_POINTER_REQUIRED,
            optional=_POINTER_OPTIONAL,
            owner="ActiveGenerationPointerV4",
        )
        previous = data["previous_generation_id"] if "previous_generation_id" in data else None
        if previous is not None and type(previous) is not str:
            raise ValueError(
                f"previous_generation_id must be str or null, got {previous!r}"
            )
        return cls(
            schema_version=data["schema_version"],
            generation_id=data["generation_id"],
            workspace_manifest_sha256=data["workspace_manifest_sha256"],
            activated_at=data["activated_at"],
            migration_id=data["migration_id"],
            previous_generation_id=previous,
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

_MANIFEST_COUNT_FIELDS = (
    "notebook_count",
    "query_count",
    "page_journal_count",
)
_MANIFEST_HASH_FIELDS = (
    "notebook_set_hash",
    "page_journal_set_hash",
    "relevance_profile_hash",
    "workspace_tree_sha256",
    "migration_inventory_sha256",
)
_MANIFEST_REQUIRED = frozenset(
    {
        "schema_version",
        "generation_id",
        "migration_id",
        "created_at",
        "completed_at",
        "store_schema_versions",
    }
    | set(_MANIFEST_COUNT_FIELDS)
    | set(_MANIFEST_HASH_FIELDS)
)


@dataclass(frozen=True)
class DiscoveryWorkspaceManifestV4:
    """Immutable workspace manifest written to ``workspace.json`` inside
    each generation directory.

    Contains the hash-bound closure of all workspace state at creation
    time.  Every field is validated in ``__post_init__``; there is no
    construction path that yields a partially-valid manifest.
    """

    schema_version: str = WORKSPACE_MANIFEST_SCHEMA_VERSION_V4
    generation_id: str = ""
    migration_id: str = ""
    created_at: str = ""
    completed_at: str = ""
    notebook_count: int = 0
    query_count: int = 0
    page_journal_count: int = 0
    notebook_set_hash: str = EMPTY_SET_SHA256
    page_journal_set_hash: str = EMPTY_SET_SHA256
    relevance_profile_hash: str = EMPTY_SET_SHA256
    store_schema_versions: dict[str, str] | None = None
    workspace_tree_sha256: str = ""
    migration_inventory_sha256: str = EMPTY_SET_SHA256

    def __post_init__(self) -> None:
        if self.schema_version != WORKSPACE_MANIFEST_SCHEMA_VERSION_V4:
            raise ValueError(
                f"schema_version must be {WORKSPACE_MANIFEST_SCHEMA_VERSION_V4!r}"
            )
        _check_generation_id(self.generation_id, "generation_id")
        _check_generation_id(self.migration_id, "migration_id")
        created = _check_iso8601_tz(self.created_at, "created_at")
        completed = _check_iso8601_tz(self.completed_at, "completed_at")
        if completed < created:
            raise ValueError(
                f"completed_at {self.completed_at!r} is earlier than "
                f"created_at {self.created_at!r}"
            )
        for field_name in _MANIFEST_COUNT_FIELDS:
            _check_count(getattr(self, field_name), field_name)
        for field_name in _MANIFEST_HASH_FIELDS:
            _check_hex64(getattr(self, field_name), field_name)
        versions = self.store_schema_versions
        if not isinstance(versions, dict) or any(
            type(k) is not str or type(v) is not str
            for k, v in versions.items()
        ):
            raise ValueError(
                f"store_schema_versions must be a str->str mapping, "
                f"got {versions!r}"
            )
        if versions != STORE_SCHEMA_VERSIONS_V4:
            raise ValueError(
                f"store_schema_versions must equal the live v4 store set "
                f"{STORE_SCHEMA_VERSIONS_V4!r}, got {versions!r}"
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
            "page_journal_count": self.page_journal_count,
            "notebook_set_hash": self.notebook_set_hash,
            "page_journal_set_hash": self.page_journal_set_hash,
            "relevance_profile_hash": self.relevance_profile_hash,
            "store_schema_versions": dict(self.store_schema_versions or {}),
            "workspace_tree_sha256": self.workspace_tree_sha256,
            "migration_inventory_sha256": self.migration_inventory_sha256,
        }

    @classmethod
    def from_dict_strict(
        cls, data: Mapping[str, Any]
    ) -> "DiscoveryWorkspaceManifestV4":
        """Parse from JSON dict.  Exact key set, exact types, no coercion."""
        if not isinstance(data, (dict, Mapping)):
            raise TypeError(f"expected dict, got {type(data).__name__}")
        _check_key_set(
            data,
            required=_MANIFEST_REQUIRED,
            optional=frozenset(),
            owner="DiscoveryWorkspaceManifestV4",
        )
        for field_name in _MANIFEST_COUNT_FIELDS:
            _check_count(data[field_name], field_name)
        versions = data["store_schema_versions"]
        if not isinstance(versions, dict):
            raise ValueError(
                f"store_schema_versions must be a mapping, got {versions!r}"
            )
        return cls(
            schema_version=data["schema_version"],
            generation_id=data["generation_id"],
            migration_id=data["migration_id"],
            created_at=data["created_at"],
            completed_at=data["completed_at"],
            notebook_count=data["notebook_count"],
            query_count=data["query_count"],
            page_journal_count=data["page_journal_count"],
            notebook_set_hash=data["notebook_set_hash"],
            page_journal_set_hash=data["page_journal_set_hash"],
            relevance_profile_hash=data["relevance_profile_hash"],
            store_schema_versions=dict(versions),
            workspace_tree_sha256=data["workspace_tree_sha256"],
            migration_inventory_sha256=data["migration_inventory_sha256"],
        )

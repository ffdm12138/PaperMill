"""Strict Registry loader with schema validation and path-safety enforcement.

The normal loader (``load_registry`` / ``load_categories``) rejects any
registry that fails schema, identity, or path-safety checks.  The legacy
loader (``load_legacy_registry_for_migration``) is the ONLY entry point
that reads old-format registries, and its result MUST NOT enter the
normal Reader / Writer / Reconcile paths.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from src.catalog_folders.exceptions import (
    FilesystemNameCollision,
)
from src.catalog_folders.registry import definition_hash, validate_catalog_keyword
from src.discovery.keyword_notebook import keyword_id, normalize_keyword


_UNSAFE_PATH = re.compile(r'[<>:"|?*\x00-\x1f]|\.\.|^/|^[A-Z]:', re.IGNORECASE)
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def _validate_directory_name(name: str) -> None:
    """Reject path traversal, absolute paths, and unsafe characters."""
    if not name:
        raise FilesystemNameCollision("directory_name must not be empty")
    if _UNSAFE_PATH.search(name):
        raise FilesystemNameCollision(
            f"directory_name contains unsafe characters or path traversal: {name!r}"
        )
    if name.upper() in _WIN_RESERVED:
        raise FilesystemNameCollision(
            f"directory_name is a Windows reserved name: {name!r}"
        )
    if name in (".", ".."):
        raise FilesystemNameCollision(
            f"directory_name must not be '.' or '..': {name!r}"
        )


def _validate_source_notebook(name: str) -> None:
    """Reject unsafe source_notebook values."""
    if not name:
        raise ValueError("source_notebook must not be empty")
    if _UNSAFE_PATH.search(name):
        raise FilesystemNameCollision(
            f"source_notebook contains unsafe characters or path traversal: {name!r}"
        )
    if "/" in name or "\\" in name:
        raise FilesystemNameCollision(
            f"source_notebook must be a filename, not a path: {name!r}"
        )
    if not name.endswith(".json"):
        raise ValueError(
            f"source_notebook must end with .json: {name!r}"
        )


def validate_registry_entry(row: dict, *, require_sha256_match: bool = True) -> dict:
    """Validate a single registry category entry.

    Returns a cleaned dict suitable for constructing a ``Category``.
    Raises ``ValueError`` or an appropriate exception subclass on failure.
    """
    errors: list[str] = []

    # ── Required fields ──────────────────────────────────────────────────
    for field in ("category_id", "keyword_zh", "directory_name",
                  "source_notebook", "definition_sha256"):
        if not row.get(field):
            errors.append(f"missing required field: {field}")

    # ── schema_version propagated from registry level ────────────────────
    cid = str(row.get("category_id") or "")
    kw = str(row.get("keyword_zh") or "")
    dirname = str(row.get("directory_name") or "")
    src = str(row.get("source_notebook") or "")
    def_hash = str(row.get("definition_sha256") or "")
    nkw = str(row.get("normalized_keyword_zh") or "")

    # ── category_id format ───────────────────────────────────────────────
    if cid and not re.fullmatch(r"[0-9a-f]{16}", cid):
        errors.append(f"invalid category_id format: {cid!r}")

    # ── keyword_zh validation ────────────────────────────────────────────
    if kw:
        try:
            validate_catalog_keyword(kw)
        except Exception as exc:
            errors.append(f"invalid keyword_zh: {exc}")
        if cid and cid != keyword_id(kw):
            errors.append(
                f"category_id {cid!r} is not derived from keyword_zh {kw!r}"
            )

    # ── directory_name safety ────────────────────────────────────────────
    if dirname:
        try:
            _validate_directory_name(dirname)
        except FilesystemNameCollision as exc:
            errors.append(f"unsafe directory_name: {exc}")

    # ── directory_name == keyword_zh contract ────────────────────────────
    if dirname and kw and dirname != kw:
        errors.append(
            f"directory_name ({dirname!r}) != keyword_zh ({kw!r})"
        )

    # ── source_notebook safety ───────────────────────────────────────────
    if src:
        try:
            _validate_source_notebook(src)
        except (ValueError, FilesystemNameCollision) as exc:
            errors.append(f"unsafe source_notebook: {exc}")

    # ── definition_sha256 re-computation ─────────────────────────────────
    if require_sha256_match and def_hash and kw:
        recomputed = definition_hash({
            "category_id": cid,
            "keyword_zh": kw,
            "guidance_zh": row.get("guidance_zh"),
            "aliases_zh": row.get("aliases_zh"),
            "exclusions_zh": row.get("exclusions_zh"),
        })
        if recomputed != def_hash:
            errors.append(
                f"definition_sha256 mismatch: stored={def_hash[:16]}..., "
                f"computed={recomputed[:16]}..."
            )

    # ── classification_enabled ───────────────────────────────────────────
    enabled = row.get("classification_enabled")
    if enabled is not None and not isinstance(enabled, bool):
        errors.append(
            f"classification_enabled must be boolean, got {type(enabled).__name__}"
        )

    # ── normalized_keyword_zh safety ─────────────────────────────────────
    if nkw:
        if _UNSAFE_PATH.search(nkw):
            errors.append(f"normalized_keyword_zh contains unsafe characters: {nkw!r}")
        if kw and nkw != normalize_keyword(kw):
            errors.append(
                f"normalized_keyword_zh {nkw!r} does not match keyword_zh {kw!r}"
            )
    elif kw:
        errors.append("missing required field: normalized_keyword_zh")

    for field in ("aliases_zh", "exclusions_zh"):
        value = row.get(field, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            errors.append(f"{field} must be a list of strings")

    if errors:
        raise ValueError(
            f"Registry entry {cid!r}: " + "; ".join(errors)
        )

    return {
        "category_id": cid,
        "keyword_zh": kw,
        "normalized_keyword_zh": nkw,
        "directory_name": dirname,
        "source_notebook": src,
        "definition_sha256": def_hash,
        "classification_enabled": bool(row.get("classification_enabled", True)),
        "retired_at": row.get("retired_at"),
        "guidance_zh": row.get("guidance_zh"),
        "aliases_zh": tuple(row.get("aliases_zh") or ()),
        "exclusions_zh": tuple(row.get("exclusions_zh") or ()),
    }


def validate_registry_schema(
    data: dict, *, require_sha256_match: bool = True,
) -> list[str]:
    """Validate the top-level registry schema and detect duplicates.

    Returns a list of error strings (empty = valid).
    """
    errors: list[str] = []

    if data.get("schema_version") != "1.0":
        errors.append(
            f"unsupported schema_version: {data.get('schema_version')!r}"
        )

    categories = data.get("categories")
    if not isinstance(categories, list):
        errors.append("categories must be a list")
        return errors

    seen_ids: dict[str, int] = {}    # category_id → index
    seen_dirs: dict[str, int] = {}   # directory_name → index
    seen_keywords: dict[str, int] = {}  # keyword_zh → index

    for idx, row in enumerate(categories):
        if not isinstance(row, dict):
            errors.append(f"categories[{idx}] is not a dict")
            continue

        cid = str(row.get("category_id") or "")
        dirname = str(row.get("directory_name") or "")
        kw = str(row.get("keyword_zh") or "")

        # Duplicate category_id
        if cid and cid in seen_ids:
            errors.append(
                f"duplicate category_id {cid!r} at indices "
                f"{seen_ids[cid]} and {idx}"
            )
        if cid:
            seen_ids[cid] = idx

        # Duplicate directory_name
        if dirname and dirname in seen_dirs:
            errors.append(
                f"duplicate directory_name {dirname!r} at indices "
                f"{seen_dirs[dirname]} and {idx}"
            )
        if dirname:
            seen_dirs[dirname] = idx

        # Duplicate keyword_zh
        if kw and kw in seen_keywords:
            errors.append(
                f"duplicate keyword_zh {kw!r} at indices "
                f"{seen_keywords[kw]} and {idx}"
            )
        if kw:
            seen_keywords[kw] = idx

        # Per-entry validation
        try:
            validate_registry_entry(
                row, require_sha256_match=require_sha256_match,
            )
        except ValueError as exc:
            errors.append(str(exc))

    return errors


def load_strict_registry(registry_path: Path) -> dict:
    """Load and strictly validate a registry file.

    Raises ``ValueError`` on any schema, identity, or integrity violation.
    This is the ONLY loader for normal Reader / Writer / Reconcile paths.
    """
    data = json.loads(Path(registry_path).read_text(encoding="utf-8"))
    errors = validate_registry_schema(data)
    if errors:
        raise ValueError(
            f"Registry validation failed ({len(errors)} errors): "
            + "; ".join(errors[:10])
            + ("..." if len(errors) > 10 else "")
        )
    return data


def load_registry_for_sync(registry_path: Path) -> dict:
    """Load a structurally valid registry while rebuilding it from notebooks.

    A registry can be structurally sound while its stored definition hashes
    were produced by an earlier classifier contract.  The sync writer must be
    able to replace those hashes from the authoritative current notebooks, but
    it must not accept any other identity, path, or schema drift.  The result
    is historical input for ``sync_registry`` only; normal readers continue to
    use :func:`load_strict_registry`.
    """
    data = json.loads(Path(registry_path).read_text(encoding="utf-8"))
    errors = validate_registry_schema(data, require_sha256_match=False)
    if errors:
        raise ValueError(
            f"Registry sync input validation failed ({len(errors)} errors): "
            + "; ".join(errors[:10])
            + ("..." if len(errors) > 10 else "")
        )
    return data


def load_legacy_registry_for_migration(registry_path: Path) -> dict:
    """Load a legacy registry for migration purposes ONLY.

    This bypasses strict ``definition_sha256`` validation (legacy entries
    may have been computed with an older ``definition_hash``).  The result
    MUST NOT be passed to normal Reader / Writer / Reconcile paths.
    Callers must validate entries themselves post-migration.
    """
    data = json.loads(Path(registry_path).read_text(encoding="utf-8"))

    if not isinstance(data.get("categories"), list):
        raise ValueError("legacy registry: categories must be a list")

    # Basic structural checks only — no sha256 or keyword_id checks
    for idx, row in enumerate(data["categories"]):
        if not isinstance(row, dict):
            raise ValueError(f"legacy registry: categories[{idx}] is not a dict")
        for field in ("category_id", "keyword_zh", "directory_name"):
            if not row.get(field):
                raise ValueError(
                    f"legacy registry: categories[{idx}] missing {field}"
                )
        # Validate directory safety even for legacy entries
        _validate_directory_name(str(row.get("directory_name") or ""))

    return data

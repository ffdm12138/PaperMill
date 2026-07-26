from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from src.catalog_folders.exceptions import (
    DuplicateKeyword,
    FilesystemNameCollision,
    InvalidChineseKeyword,
    InvalidKeywordId,
    NotebookSchemaError,
)
from src.catalog_folders.models import CLASSIFIER_SKILL_VERSION, Category
from src.discovery.contracts.notebook import keyword_id as derive_keyword_id
from src.utils.atomic_io import atomic_write_json
from src.utils.timestamps import utc_now_iso_z as now_iso


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = {"all", "_pending", ".state", ".", ".."}
_RESERVED_CASEFOLD = {"all", "_pending", ".state"}
_HAS_CJK = re.compile(r"[一-鿿㐀-䶿豈-﫿]")
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}



def validate_catalog_keyword(keyword: str) -> str:
    """Validate a Chinese keyword is safe for use as a category directory name.

    Returns the NFC-normalized keyword.  Raises an appropriate exception if
    the keyword is invalid.  The original input is checked first against
    strict formatting rules before any normalization is applied.
    """
    original = str(keyword)

    # ── strict pre-normalization checks on original input ──────────────
    if original != original.strip():
        raise InvalidChineseKeyword(
            f"category keyword has leading/trailing whitespace: {original!r}"
        )
    if original != original.rstrip("."):
        raise InvalidChineseKeyword(
            f"category keyword has trailing dots: {original!r}"
        )
    if "/" in original or "\\" in original:
        raise FilesystemNameCollision(
            f"category keyword contains path separators: {original!r}"
        )
    if any(ord(c) < 0x20 for c in original):
        raise InvalidChineseKeyword(
            f"category keyword contains control characters: {original!r}"
        )
    if original.upper() in _WIN_RESERVED:
        raise FilesystemNameCollision(
            f"category keyword is a Windows reserved name: {original!r}"
        )
    if unicodedata.normalize("NFC", original) != original:
        raise InvalidChineseKeyword(
            f"category keyword must be NFC-normalized Unicode: {original!r}"
        )
    if original.casefold() in _RESERVED_CASEFOLD:
        raise FilesystemNameCollision(
            f"category keyword collides with reserved name: {original!r}"
        )

    # ── normalize and validate content ─────────────────────────────────
    text = original.strip()
    if not text:
        raise InvalidChineseKeyword("category keyword must not be empty")
    if not _HAS_CJK.search(text):
        raise InvalidChineseKeyword(
            f"category keyword must contain at least one Chinese character: {keyword!r}"
        )
    if _UNSAFE.search(text):
        raise FilesystemNameCollision(
            f"category keyword contains unsafe filesystem characters: {keyword!r}"
        )
    if text.casefold() in _RESERVED:
        raise FilesystemNameCollision(
            f"category keyword is a reserved name: {keyword!r}"
        )
    if len(text) > 64:
        raise InvalidChineseKeyword(
            f"category keyword too long (max 64): {len(text)}"
        )
    return text


def _is_empty(val: object) -> bool:
    """Return True for None, empty string, empty list, or empty tuple."""
    return val is None or val in ("", [], ())


def definition_hash(value: dict) -> str:
    """Hash only Chinese-relevant definition fields plus the classifier
    contract version.

    Chinese and English search queries, provider cursors, and file paths are
    explicitly excluded — changing them must not invalidate decisions.
    """
    keys = ("category_id", "keyword_zh", "guidance_zh", "aliases_zh", "exclusions_zh")
    payload = {key: value.get(key) for key in keys if not _is_empty(value.get(key))}
    payload["classifier_skill_version"] = CLASSIFIER_SKILL_VERSION
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def category_from_notebook(path: Path) -> Category:
    """Build a Category from a DOI keyword notebook.

    Active Catalog code accepts only a strictly valid discovery notebook v3.
    The directory name and category identity come solely from ``keyword_zh``;
    ``search_queries`` are deliberately not inspected here.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        from src.discovery.contracts.notebook import validate_notebook

        data = validate_notebook(raw)
    except (json.JSONDecodeError, OSError, ValueError, RuntimeError) as exc:
        raise NotebookSchemaError(f"invalid v3 notebook {path}: {exc}") from exc
    category_id = str(data.get("keyword_id") or "")
    if not re.fullmatch(r"[0-9a-f]{16}", category_id):
        raise InvalidKeywordId(f"invalid keyword_id in {path}")
    # Do NOT pre-strip — the validator rejects leading/trailing whitespace.
    raw_keyword = str(data.get("keyword_zh") or "")
    validated = validate_catalog_keyword(raw_keyword)
    # ── Enforce keyword_id derivation ───────────────────────────────────
    # The category_id MUST equal keyword_id(raw_keyword).  A mismatched ID
    # means the notebook identity is corrupt or has been tampered with.
    expected_id = derive_keyword_id(raw_keyword)
    if category_id != expected_id:
        raise InvalidKeywordId(
            f"keyword_id mismatch in {path}: "
            f"got {category_id!r}, expected {expected_id!r} "
            f"(derived from keyword {raw_keyword!r})"
        )
    classification = data.get("classification")
    guidance = classification.get("guidance_zh")
    aliases = tuple(classification.get("aliases_zh") or ())
    exclusions = tuple(classification.get("exclusions_zh") or ())
    base = {
        "category_id": category_id,
        "keyword_zh": validated,
        "guidance_zh": guidance,
        "aliases_zh": aliases,
        "exclusions_zh": exclusions,
    }
    norm = str(data.get("normalized_keyword_zh") or "")
    return Category(
        category_id=category_id,
        keyword_zh=validated,
        directory_name=validated,  # exactly the Chinese keyword
        source_notebook=Path(path).name,
        definition_sha256=definition_hash(base),
        classification_enabled=bool(data["enabled"]),
        guidance_zh=guidance,
        aliases_zh=aliases,
        exclusions_zh=exclusions,
        normalized_keyword_zh=norm,
    )


def load_registry(path: Path) -> dict:
    from src.catalog_folders.registry_schema import load_strict_registry

    return load_strict_registry(Path(path))


def load_categories(registry_path: Path) -> list[Category]:
    """Load active (enabled, non-retired) categories from the registry."""
    rows = load_registry(registry_path)["categories"] if registry_path.is_file() else []
    return [Category(
        category_id=row["category_id"], keyword_zh=row["keyword_zh"],
        normalized_keyword_zh=row.get("normalized_keyword_zh", ""),
        directory_name=row["directory_name"],
        source_notebook=row["source_notebook"], definition_sha256=row["definition_sha256"],
        classification_enabled=bool(row.get("classification_enabled", True)),
        retired_at=row.get("retired_at"),
        guidance_zh=row.get("guidance_zh"), aliases_zh=tuple(row.get("aliases_zh") or ()),
        exclusions_zh=tuple(row.get("exclusions_zh") or ()),
    ) for row in rows if row.get("classification_enabled", True) and not row.get("retired_at")]


def sync_registry(*, notebook_dir: Path, registry_path: Path, apply: bool) -> dict:
    """Sync the category registry from DOI keyword notebooks.

    The registry is fully derived from current notebooks — old registry entries
    are never preserved as active categories.  Only Chinese-keyword,
    enabled=true notebooks become active categories.

    Parse errors, schema errors, invalid Chinese keywords, and collisions **block**
    ``apply`` — a damaged notebook must never produce a partial registry.
    """
    if registry_path.is_file():
        try:
            old_registry = load_registry(registry_path)
        except ValueError as strict_error:
            # A prior classifier-contract version can leave only the stored
            # definition hashes stale.  Rebuild current values from the
            # authoritative notebooks, while still validating every other
            # registry identity and path field before using history metadata.
            from src.catalog_folders.registry_schema import load_registry_for_sync

            try:
                old_registry = load_registry_for_sync(registry_path)
            except ValueError:
                raise strict_error
    else:
        old_registry = {"categories": []}
    old_by_id: dict[str, dict] = {
        str(row.get("category_id")): dict(row)
        for row in old_registry.get("categories", []) if isinstance(row, dict)
    }
    added: list[str] = []
    changed: list[str] = []
    retired: list[str] = []
    collisions: list[dict] = []
    notebook_parse_errors: list[dict] = []
    invalid_keywords: list[dict] = []

    # ── First pass: validate every notebook, build new_categories ──────
    # active_notebooks: keyword_zh → category dict (enabled=true only)
    active_notebooks: dict[str, dict] = {}
    # disabled_entries: category_id → category dict (enabled=false, stored but not active)
    disabled_entries: dict[str, dict] = {}
    # all_seen_ids tracks every keyword_id seen (for collision detection)
    all_seen_ids: dict[str, str] = {}  # keyword_id → keyword_zh
    # all_seen_keywords tracks every keyword_zh → keyword_id (for cross-notebook collision)
    all_seen_keywords: dict[str, str] = {}  # keyword_zh → keyword_id
    # Track source filenames to detect duplicates and mismatches
    seen_filenames: dict[str, str] = {}  # source_notebook → keyword_id
    # Track disabled keywords and IDs for cross-detection with active
    disabled_keywords: dict[str, str] = {}  # keyword_zh → keyword_id
    disabled_ids: set[str] = set()
    # Track which IDs and keywords belong to active notebooks
    active_ids: set[str] = set()
    active_keywords: set[str] = set()

    for notebook in sorted(Path(notebook_dir).glob("*.json")):
        # ── Parse JSON ─────────────────────────────────────────────────
        try:
            data = json.loads(notebook.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            notebook_parse_errors.append({
                "path": str(notebook), "error": str(exc),
            })
            continue

        # ── Build Category from notebook ────────────────────────────────
        try:
            category = category_from_notebook(notebook).to_dict()
        except InvalidChineseKeyword as exc:
            invalid_keywords.append({
                "path": str(notebook),
                "keyword": str(data.get("keyword_zh") or ""),
                "keyword_id": data.get("keyword_id", ""),
                "error": str(exc),
            })
            continue
        except (NotebookSchemaError, InvalidKeywordId, FilesystemNameCollision) as exc:
            notebook_parse_errors.append({
                "path": str(notebook), "error": str(exc),
            })
            continue

        cid = category["category_id"]
        kw = category["keyword_zh"]
        src = category.get("source_notebook", "") or notebook.name

        # ── Detect same-id-same-keyword duplicate files ─────────────────
        if cid in all_seen_ids and all_seen_ids[cid] == kw:
            collisions.append({
                "type": "duplicate_notebook_same_identity",
                "keyword_id": cid,
                "keyword": kw,
                "files": [seen_filenames.get(cid, "?"), notebook.name],
            })

        # ── Detect same-id-different-keyword ────────────────────────────
        if cid in all_seen_ids and all_seen_ids[cid] != kw:
            collisions.append({
                "type": "same_id_different_keyword",
                "keyword_id": cid,
                "keywords": [all_seen_ids[cid], kw],
            })
        all_seen_ids[cid] = kw

        # ── Detect source filename duplicate or mismatch ────────────────
        if src in seen_filenames and seen_filenames[src] != cid:
            collisions.append({
                "type": "duplicate_source_filename",
                "filename": src,
                "ids": [seen_filenames[src], cid],
            })
        seen_filenames.setdefault(src, cid)

        # ── Disabled notebooks: track for cross-detection, store ────────
        if not category.get("classification_enabled", True):
            disabled_entries[cid] = category
            disabled_ids.add(cid)
            disabled_keywords[kw] = cid
            continue

        # ── Detect same-keyword-different-id ────────────────────────────
        if kw in all_seen_keywords and all_seen_keywords[kw] != cid:
            collisions.append({
                "type": "same_keyword_different_id",
                "keyword": kw,
                "ids": [all_seen_keywords[kw], cid],
            })
        all_seen_keywords[kw] = cid

        active_notebooks[kw] = category
        active_ids.add(cid)
        active_keywords.add(kw)

    # ── Cross-detection: active vs disabled collisions ─────────────────
    for kw, did in disabled_keywords.items():
        if kw in active_keywords:
            collisions.append({
                "type": "active_disabled_same_keyword",
                "keyword": kw,
                "active_id": did,  # same keyword → same derived keyword_id
                "disabled_id": did,
            })
    for did in disabled_ids:
        if did in active_ids:
            collisions.append({
                "type": "active_disabled_same_id",
                "keyword_id": did,
                "disabled_keyword": disabled_entries[did].get("keyword_zh", ""),
            })

    # ── Compute retired (old registry IDs no longer in any notebook) ──
    # active_notebooks is keyed by keyword_zh, disabled_entries by keyword_id.
    # Build ID-keyed views so the retirement computation operates on a single
    # key space (keyword_id); mixing keyword_zh and keyword_id in a set
    # operation would produce incorrect retirement decisions.
    active_by_id: dict[str, dict] = {
        cat["category_id"]: cat for cat in active_notebooks.values()
    }
    disabled_by_id: dict[str, dict] = dict(disabled_entries)
    new_ids = set(active_by_id) | set(disabled_by_id)
    retired = sorted(set(old_by_id) - new_ids)

    # ── Block apply on collisions ─────────────────────────────────────
    if collisions and apply:
        details: list[str] = []
        for c in collisions:
            ctype = c.get("type", "?")
            if ctype in ("same_id_different_keyword",):
                details.append(
                    f"id={c.get('keyword_id','?')} keywords={c.get('keywords',[])}"
                )
            elif ctype in ("same_keyword_different_id",):
                details.append(
                    f"keyword={c.get('keyword','?')} ids={c.get('ids',[])}"
                )
            elif ctype in ("active_disabled_same_keyword",):
                details.append(
                    f"keyword={c.get('keyword','?')} active+disabled conflict"
                )
            elif ctype in ("active_disabled_same_id",):
                details.append(
                    f"id={c.get('keyword_id','?')} active+disabled conflict"
                )
            elif ctype in ("duplicate_notebook_same_identity",):
                details.append(
                    f"id={c.get('keyword_id','?')} kw={c.get('keyword','?')} files={c.get('files',[])}"
                )
            elif ctype in ("duplicate_source_filename",):
                details.append(
                    f"filename={c.get('filename','?')} ids={c.get('ids',[])}"
                )
            else:
                details.append(f"{ctype}: {c}")
        raise DuplicateKeyword(
            f"Chinese keyword collisions detected: " + "; ".join(details)
        )

    # ── Block apply on parse/keyword errors ───────────────────────────
    has_errors = bool(notebook_parse_errors or invalid_keywords)
    if has_errors and apply:
        error_details: list[str] = []
        if notebook_parse_errors:
            error_details.append(f"{len(notebook_parse_errors)} parse error(s)")
        if invalid_keywords:
            error_details.append(f"{len(invalid_keywords)} invalid keyword(s)")
        raise ValueError(
            f"Registry sync blocked: {'; '.join(error_details)}. "
            f"Fix all notebook errors before retrying --apply."
        )

    # ── Build new registry: active + disabled ─────────────────────────
    new_categories: dict[str, dict] = {}

    # Active categories from current notebooks
    # enabled=true → active, NEVER carries retired_at from old registry
    for category in active_notebooks.values():
        cid = category["category_id"]
        old = old_by_id.get(cid)
        # Strip any old retired_at — an enabled notebook is definitively active
        category.pop("retired_at", None)
        if old is None:
            new_categories[cid] = category
            added.append(cid)
        else:
            enriched = {**category}
            enriched["definition_sha256"] = definition_hash(enriched)
            if enriched != old:
                changed.append(cid)
            new_categories[cid] = enriched

    # Disabled categories (stored in registry but not active)
    # disabled entries may preserve old retired_at only for historical tracking
    for cid, category in disabled_entries.items():
        old = old_by_id.get(cid)
        if old is not None and "retired_at" in old:
            category["retired_at"] = old["retired_at"]
        new_categories[cid] = category

    # ── Write retirement history for deleted categories ───────────────
    # Idempotent: if a history record already exists for this category_id,
    # do NOT write a duplicate.  Repeated syncs of the same deleted state
    # must not produce multiple retirement events.
    if apply and retired:
        history_dir = Path(registry_path).parent / "category_history"
        for cid in retired:
            cid_history_dir = history_dir / cid
            # Check if ANY history record already exists — if so, skip
            if cid_history_dir.is_dir() and any(cid_history_dir.glob("*.json")):
                continue
            old_entry = old_by_id.get(cid, {})
            retire_record = {
                "category_id": cid,
                "keyword_zh": old_entry.get("keyword_zh", ""),
                "retired_at": now_iso(),
                "reason": "notebook_deleted",
                "last_known_state": {
                    k: v for k, v in old_entry.items()
                    if k not in ("definition_sha256", "updated_at")
                },
            }
            record_path = cid_history_dir / f"{now_iso().replace(':', '-')}.json"
            record_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(record_path, retire_record, indent=2)

    # ── Assemble and publish ──────────────────────────────────────────
    result = {
        "schema_version": "1.0", "updated_at": now_iso(),
        "categories": [new_categories[key] for key in sorted(new_categories)],
    }
    if apply:
        atomic_write_json(registry_path, result, indent=2)
    return {
        "registry": result, "added": added, "changed": changed,
        "retired": retired,
        "collisions": collisions,
        "notebook_parse_errors": notebook_parse_errors,
        "invalid_keywords": invalid_keywords,
    }

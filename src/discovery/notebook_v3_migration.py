"""Transactional migration of discovery keyword notebooks to schema v3.

This is the only module allowed to read legacy ``keyword`` / ``expansions``
notebook fields.  Active discovery, audit, recovery, and Catalog code consume
strict v3 notebooks only.

Two explicit manifests are required:

* a source-controlled query manifest defines Chinese topics and curated
  Chinese/English search queries;
* a runtime mapping manifest maps every source notebook filename and SHA-256
  to exactly one Chinese topic.

The migration never guesses a topic from filenames, Catalog directories, or
DOI exports.  It preserves trusted provider cursor state and rewrites durable
page journals to their v2 keyword/query identity.  Any unmapped input,
ambiguous query, untrusted cursor signature, or output collision blocks before
the first mutation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid
from typing import Any, Iterable

from filelock import FileLock

from src.discovery.keyword_notebook import (
    INITIAL_CURSOR,
    PROVIDERS,
    SCHEMA_VERSION,
    detect_query_language,
    empty_notebook,
    keyword_id,
    normalize_keyword,
    query_identity,
)
from src.utils.atomic_io import atomic_write_json


QUERY_MANIFEST_SCHEMA_VERSION = "1.0"
MAPPING_MANIFEST_SCHEMA_VERSION = "1.0"
JOURNAL_SCHEMA_VERSION = "1.0"
PAGE_SCHEMA_VERSION = "2.0"
TRANSACTION_KIND = "discovery_keyword_v3"

PHASES = (
    "planned",
    "backed_up",
    "targets_staged",
    "targets_installed",
    "sources_archived",
    "verified",
    "committed",
)
TERMINAL_STATES = {"committed", "rolled_back"}
_HAS_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_SAFE_FILENAME = re.compile(r"^[^/\\]+\.json$")


class NotebookV3MigrationError(RuntimeError):
    """Base migration error."""


class MigrationBlocked(NotebookV3MigrationError):
    """Raised when preflight cannot prove a lossless deterministic mapping."""

    def __init__(self, message: str, *, findings: dict[str, list[dict]] | None = None):
        super().__init__(message)
        self.findings = findings or {}


class JournalConflict(NotebookV3MigrationError):
    """Raised when a journal conflicts with explicitly trusted inputs."""


class CursorPreservationError(MigrationBlocked):
    """Raised when a progressed legacy cursor lacks a trusted signature."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_bytes(raw.encode("utf-8"))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MigrationBlocked(f"invalid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MigrationBlocked(f"JSON root must be an object: {path}")
    return value


def _validate_v3_notebook(data: dict[str, Any]) -> dict[str, Any]:
    # Core exposes validate_notebook after the strict-v3 change.  The private
    # alias keeps this migration usable while that change and this file land.
    from src.discovery import keyword_notebook as notebook_module

    validator = getattr(notebook_module, "validate_notebook", None)
    if validator is None:
        validator = getattr(notebook_module, "_validate_notebook")
    return validator(data)


def _validate_page_v2(data: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    from src.discovery.page_journal import PAGE_ALL_V2_FIELDS, PAGE_REQUIRED_V2_FIELDS
    if data.get("schema_version") != PAGE_SCHEMA_VERSION:
        raise MigrationBlocked(f"page journal is not schema v2: {path or ''}")
    known = set(data)
    missing = sorted(PAGE_REQUIRED_V2_FIELDS - known)
    if missing:
        raise MigrationBlocked(f"page journal missing required fields {missing}: {path or ''}")
    extra = sorted(known - PAGE_ALL_V2_FIELDS)
    if extra:
        raise MigrationBlocked(f"page journal unknown fields {extra}: {path or ''}")
    if data["provider"] not in PROVIDERS:
        raise MigrationBlocked(f"invalid page provider: {path or ''}")
    if data["query_language"] not in ("zh", "en", "mixed"):
        raise MigrationBlocked(f"invalid page query language: {path or ''}")
    if not isinstance(data["candidates"], list):
        raise MigrationBlocked(f"page candidates must be a list: {path or ''}")
    gen = data.get("generation")
    if type(gen) is not int or gen < 1:
        raise MigrationBlocked(
            f"page generation must be int >= 1, got {gen!r} for {path or ''}"
        )
    return data


def _safe_filename(value: object, *, field: str) -> str:
    name = str(value or "")
    if not _SAFE_FILENAME.fullmatch(name) or name in (".", ".."):
        raise MigrationBlocked(f"{field} must be a plain .json filename: {name!r}")
    return name


def _resolve_roots(
    *, notebook_dir: Path, retired_dir: Path, pending_pages_dir: Path,
    locks_dir: Path, transaction_root: Path, query_manifest_path: Path,
    mapping_manifest_path: Path,
) -> dict[str, Path]:
    roots = {
        "notebook_dir": Path(notebook_dir).resolve(),
        "retired_dir": Path(retired_dir).resolve(),
        "pending_pages_dir": Path(pending_pages_dir).resolve(),
        "locks_dir": Path(locks_dir).resolve(),
        "transaction_root": Path(transaction_root).resolve(),
        "query_manifest_path": Path(query_manifest_path).resolve(),
        "mapping_manifest_path": Path(mapping_manifest_path).resolve(),
    }
    mutable = [roots[key] for key in (
        "notebook_dir", "retired_dir", "pending_pages_dir", "locks_dir",
        "transaction_root",
    )]
    if len({str(path).casefold() for path in mutable}) != len(mutable):
        raise MigrationBlocked("trusted migration roots must be distinct")
    for manifest_key in ("query_manifest_path", "mapping_manifest_path"):
        manifest = roots[manifest_key]
        if not manifest.is_file():
            raise MigrationBlocked(f"required manifest missing: {manifest}")
        for root in mutable:
            if manifest == root or root in manifest.parents:
                raise MigrationBlocked(
                    f"manifest must live outside mutable runtime roots: {manifest}"
                )
    tx_root = roots["transaction_root"]
    for other in mutable[:-1]:
        if tx_root in other.parents or other in tx_root.parents:
            raise MigrationBlocked("transaction_root must not nest inside another mutable root")
    return roots


def _topic_keyword(value: object) -> str:
    text = normalize_keyword(str(value or ""))
    if not text or not _HAS_CJK.search(text):
        raise MigrationBlocked(f"keyword_zh must contain Chinese text: {value!r}")
    if text != str(value or ""):
        raise MigrationBlocked(f"keyword_zh must already be normalized: {value!r}")
    if any(ch in text for ch in '<>:"/\\|?*') or text in (".", ".."):
        raise MigrationBlocked(f"unsafe keyword_zh: {text!r}")
    return text


def load_query_manifest(path: Path) -> dict[str, dict[str, Any]]:
    data = _read_json(path)
    if data.get("schema_version") != QUERY_MANIFEST_SCHEMA_VERSION:
        raise MigrationBlocked("unsupported discovery query manifest schema")
    if not isinstance(data.get("source"), str) or not data["source"].strip():
        raise MigrationBlocked("query manifest requires a non-empty source")
    topics = data.get("topics")
    if not isinstance(topics, list) or not topics:
        raise MigrationBlocked("query manifest topics must be a non-empty list")
    result: dict[str, dict[str, Any]] = {}
    seen_ids: set[str] = set()
    for index, raw in enumerate(topics):
        if not isinstance(raw, dict):
            raise MigrationBlocked(f"query manifest topics[{index}] must be an object")
        kw = _topic_keyword(raw.get("keyword_zh"))
        kid = keyword_id(kw)
        if kw in result or kid in seen_ids:
            raise MigrationBlocked(f"duplicate query-manifest topic: {kw}")
        enabled = raw.get("enabled")
        if not isinstance(enabled, bool):
            raise MigrationBlocked(f"topic {kw!r} requires boolean enabled")
        classification = raw.get("classification")
        if not isinstance(classification, dict):
            raise MigrationBlocked(f"topic {kw!r} requires classification object")
        for field in ("aliases_zh", "exclusions_zh"):
            if not isinstance(classification.get(field, []), list):
                raise MigrationBlocked(f"topic {kw!r} classification.{field} must be a list")
        queries = raw.get("search_queries")
        if not isinstance(queries, list) or not queries:
            raise MigrationBlocked(f"topic {kw!r} requires search_queries")
        normalized_queries: list[dict[str, Any]] = []
        seen_queries: set[str] = set()
        active_languages: set[str] = set()
        for qindex, query_row in enumerate(queries):
            if not isinstance(query_row, dict):
                raise MigrationBlocked(f"topic {kw!r} query[{qindex}] must be an object")
            query = normalize_keyword(str(query_row.get("query") or ""))
            detected = detect_query_language(query)
            language = str(query_row.get("language") or detected)
            if language not in ("zh", "en", "mixed") or detected != language:
                raise MigrationBlocked(
                    f"topic {kw!r} query language mismatch: {query!r}/{language!r}"
                )
            qid = query_identity(language, query)
            if qid in seen_queries:
                raise MigrationBlocked(f"duplicate normalized query in topic {kw!r}: {query!r}")
            seen_queries.add(qid)
            active = query_row.get("active", True)
            if not isinstance(active, bool):
                raise MigrationBlocked(f"query active must be boolean: {query!r}")
            if active:
                active_languages.add(language)
            normalized_queries.append({
                "query": query,
                "language": language,
                "active": active,
                "source": str(query_row.get("source") or data["source"]),
            })
        if enabled and active_languages != {"zh", "en"}:
            raise MigrationBlocked(
                f"enabled topic {kw!r} requires at least one active zh and en query"
            )
        result[kw] = {
            "keyword_zh": kw,
            "keyword_id": kid,
            "enabled": enabled,
            "classification": {
                "guidance_zh": classification.get("guidance_zh"),
                "aliases_zh": list(classification.get("aliases_zh") or []),
                "exclusions_zh": list(classification.get("exclusions_zh") or []),
            },
            "search_queries": normalized_queries,
            "source": data["source"],
        }
        seen_ids.add(kid)
    return result


def load_mapping_manifest(
    path: Path, *, topics: dict[str, dict[str, Any]], source_dir: Path,
) -> dict[str, dict[str, str]]:
    data = _read_json(path)
    if data.get("schema_version") != MAPPING_MANIFEST_SCHEMA_VERSION:
        raise MigrationBlocked("unsupported notebook mapping manifest schema")
    mappings = data.get("mappings")
    if not isinstance(mappings, list):
        raise MigrationBlocked("mapping manifest mappings must be a list")
    by_filename: dict[str, dict[str, str]] = {}
    for index, row in enumerate(mappings):
        if not isinstance(row, dict):
            raise MigrationBlocked(f"mappings[{index}] must be an object")
        filename = _safe_filename(row.get("source_notebook"), field="source_notebook")
        if row.get("status") != "confirmed":
            raise MigrationBlocked(f"mapping {filename!r} is not confirmed")
        kw = _topic_keyword(row.get("keyword_zh"))
        if kw not in topics:
            raise MigrationBlocked(f"mapping references unknown query-manifest topic: {kw}")
        expected = str(row.get("source_sha256") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise MigrationBlocked(f"mapping {filename!r} requires full source_sha256")
        if filename in by_filename:
            raise MigrationBlocked(f"source notebook mapped more than once: {filename}")
        source_path = source_dir / filename
        if not source_path.is_file():
            raise MigrationBlocked(f"mapped source notebook missing: {source_path}")
        actual = sha256_file(source_path)
        if actual != expected:
            raise MigrationBlocked(
                f"source notebook hash mismatch: {filename}: expected {expected}, got {actual}"
            )
        by_filename[filename] = {
            "source_notebook": filename,
            "source_sha256": expected,
            "keyword_zh": kw,
        }
    actual_sources = {path.name for path in source_dir.glob("*.json") if path.is_file()}
    mapped_sources = set(by_filename)
    if actual_sources != mapped_sources:
        missing = sorted(actual_sources - mapped_sources)
        absent = sorted(mapped_sources - actual_sources)
        raise MigrationBlocked(
            f"mapping must cover the exact source set: unmapped={missing}, missing={absent}",
            findings={"unmapped_sources": [
                {"filename": name} for name in missing
            ]},
        )
    return by_filename


def inventory_notebooks(notebook_dir: Path) -> dict[str, Any]:
    """Read legacy/v3 notebook identity and cursor summaries without mutation."""
    rows: list[dict[str, Any]] = []
    for path in sorted(Path(notebook_dir).glob("*.json")):
        raw = path.read_bytes()
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            rows.append({"path": str(path), "source_sha256": sha256_bytes(raw),
                         "corrupt": str(exc), "unmapped": True, "ambiguous": False})
            continue
        if data.get("schema_version") == "3.0":
            query_values = data.get("search_queries")
            if isinstance(query_values, dict):
                iterable = query_values.items()
            elif isinstance(query_values, list):
                iterable = ((str(index), value) for index, value in enumerate(query_values))
            else:
                iterable = ()
            queries: list[dict[str, Any]] = []
            provider_summary: dict[str, list[dict[str, Any]]] = {}
            page_root = Path(notebook_dir).parent / "pending_pages"
            for fallback_query_id, item in iterable:
                if not isinstance(item, dict):
                    continue
                query_id = str(item.get("query_id") or fallback_query_id)
                query = str(item.get("query") or "")
                queries.append({
                    "query_id": query_id,
                    "query": query,
                    "language": item.get("language") or detect_query_language(query),
                    "active": bool(item.get("active", True)),
                })
                providers = item.get("providers") if isinstance(item.get("providers"), dict) else {}
                for provider, state in providers.items():
                    if not isinstance(state, dict):
                        continue
                    backfill = state.get("backfill") if isinstance(state.get("backfill"), dict) else {}
                    provider_pages = page_root / str(data.get("keyword_id") or "") / query_id / str(provider)
                    page_count = sum(1 for page in provider_pages.rglob("*.json") if page.is_file())
                    provider_summary.setdefault(str(provider), []).append({
                        "query_id": query_id,
                        "generation": backfill.get("generation"),
                        "generation_history": list(backfill.get("generation_history") or []),
                        "request_signature": backfill.get("request_signature") or "",
                        "cursor": backfill.get("cursor"),
                        "exhausted": bool(backfill.get("exhausted", False)),
                        "page_journal_count": page_count,
                    })
            keyword_zh = data.get("keyword_zh")
            rows.append({
                "path": str(path),
                "source_notebook": path.name,
                "source_sha256": sha256_bytes(raw),
                "schema_version": data.get("schema_version"),
                "keyword_id": data.get("keyword_id"),
                "recognized_keyword": keyword_zh,
                "enabled": bool(data.get("enabled", False)),
                "queries": queries,
                "providers": provider_summary,
                "suggested_keyword_zh": keyword_zh,
                "unmapped": not bool(keyword_zh),
                "ambiguous": False,
            })
            continue
        legacy_queries = data.get("expansions") if isinstance(data, dict) else None
        queries: list[dict[str, Any]] = []
        if isinstance(legacy_queries, dict):
            iterable = legacy_queries.values()
        elif isinstance(legacy_queries, list):
            iterable = legacy_queries
        else:
            iterable = (data.get("search_queries") or {}).values() if isinstance(data, dict) else []
        for item in iterable:
            if not isinstance(item, dict):
                continue
            query = str(item.get("query") or item.get("expanded_query") or "")
            queries.append({"query": query, "language": item.get("language") or detect_query_language(query)})
        provider_summary: dict[str, Any] = {}
        state = data.get("provider_state") if isinstance(data, dict) else None
        if isinstance(state, dict):
            for provider in PROVIDERS:
                value = state.get(provider) or {}
                provider_summary[provider] = {
                    "cursor": value.get("cursor") or value.get("backfill_cursor"),
                    "generation": value.get("generation"),
                }
        rows.append({
            "path": str(path), "source_notebook": path.name,
            "source_sha256": sha256_bytes(raw),
            "schema_version": data.get("schema_version"),
            "recognized_keyword": data.get("keyword_zh") or data.get("keyword"),
            "queries": queries, "providers": provider_summary,
            "suggested_keyword_zh": None, "unmapped": True, "ambiguous": False,
        })
    return {"notebooks": rows, "unmapped": sum(bool(r.get("unmapped")) for r in rows),
            "ambiguous": sum(bool(r.get("ambiguous")) for r in rows)}


def _empty_refresh() -> dict[str, Any]:
    return {
        "last_started_at": None,
        "last_success_at": None,
        "last_status": None,
        "pages_scanned_last_run": 0,
        "items_returned_last_run": 0,
        "last_error": None,
    }


def _empty_backfill() -> dict[str, Any]:
    return {
        "cursor": INITIAL_CURSOR,
        "exhausted": False,
        "pages_succeeded": 0,
        "pages_committed": 0,
        "items_returned_total": 0,
        "last_page_count": 0,
        "last_committed_page_id": "",
        "cursor_conflicts": 0,
        "last_success_at": None,
        "last_error": None,
        "request_signature": "",
        "generation": 1,
        "generation_history": [],
        "consecutive_failures": 0,
        "last_failure_at": None,
        "last_error_type": None,
        "next_retry_at": None,
        "terminal_failure": False,
        "terminal_failure_at": None,
    }


def _flat_provider_to_nested(value: dict[str, Any]) -> tuple[dict, dict]:
    refresh = value.get("refresh") if isinstance(value.get("refresh"), dict) else {
        "last_started_at": value.get("refresh_last_started_at"),
        "last_success_at": value.get("refresh_last_success_at"),
        "last_status": value.get("refresh_status") or value.get("last_status"),
        "pages_scanned_last_run": value.get("refresh_pages_scanned", 0),
        "items_returned_last_run": value.get("refresh_items_returned", 0),
        "last_error": value.get("refresh_last_error"),
    }
    backfill = value.get("backfill") if isinstance(value.get("backfill"), dict) else {
        "cursor": value.get("backfill_cursor") or value.get("cursor"),
        "exhausted": value.get("backfill_exhausted", value.get("exhausted", False)),
        "pages_succeeded": value.get("pages_succeeded", 0),
        "pages_committed": value.get("pages_committed", 0),
        "items_returned_total": value.get("items_returned_total", 0),
        "last_page_count": value.get("last_page_count", 0),
        "last_committed_page_id": value.get("last_committed_page_id", ""),
        "last_success_at": value.get("last_success_at"),
        "last_error": value.get("last_error"),
        "request_signature": value.get("request_signature") or value.get("pagination_signature"),
        "generation": value.get("generation") or value.get("backfill_generation"),
        "generation_history": value.get("generation_history", []),
    }
    return refresh, backfill


def _translate_legacy_provider_state(value: object, *, context: str) -> dict[str, Any]:
    """Translate a legacy (v1/v2) provider state into canonical v3 backfill.

    This function handles flat field layouts (e.g. ``cursor`` / ``pages_succeeded``
    at the query level), optional ``pagination_signature`` aliases, and missing
    fields by filling defaults from ``_empty_backfill()``.

    Type coercion (``int(... or 0)``, ``bool(...)``) is deliberate here because
    legacy sources do not guarantee Python-typed JSON and cannot be exact-validated.
    """
    raw = value if isinstance(value, dict) else {}
    raw_refresh, raw_backfill = _flat_provider_to_nested(raw)
    refresh = _empty_refresh()
    for key in refresh:
        if key in raw_refresh:
            refresh[key] = raw_refresh[key]
    backfill = _empty_backfill()
    for key in backfill:
        if key in raw_backfill and key != "generation_history":
            backfill[key] = raw_backfill[key]
    history = raw_backfill.get("generation_history", [])
    if not isinstance(history, list):
        raise MigrationBlocked(f"generation_history must be a list: {context}")
    backfill["generation_history"] = history
    for key in (
        "pages_succeeded", "pages_committed", "items_returned_total",
        "last_page_count", "cursor_conflicts", "consecutive_failures", "generation",
    ):
        try:
            backfill[key] = int(backfill.get(key) or 0)
        except (TypeError, ValueError) as exc:
            raise MigrationBlocked(f"invalid integer {context}.{key}") from exc
        if backfill[key] < 0:
            raise MigrationBlocked(f"negative counter {context}.{key}")
    for key in ("exhausted", "terminal_failure"):
        backfill[key] = bool(backfill.get(key, False))
    cursor = str(backfill.get("cursor") or INITIAL_CURSOR)
    signature = str(
        raw_backfill.get("request_signature")
        or raw_backfill.get("pagination_signature")
        or ""
    )
    progressed = (
        cursor != INITIAL_CURSOR
        or backfill["exhausted"]
        or backfill["pages_succeeded"] > 0
        or backfill["pages_committed"] > 0
        or backfill["items_returned_total"] > 0
    )
    if progressed and not signature:
        raise CursorPreservationError(
            f"cannot preserve progressed cursor without trusted request signature: {context}",
            findings={"cursor_conflicts": [{"context": context, "cursor": cursor}]},
        )
    backfill["cursor"] = cursor
    backfill["request_signature"] = signature
    if progressed:
        backfill["generation"] = max(1, backfill["generation"])
    else:
        backfill["generation"] = 1
        backfill["request_signature"] = ""
    return {"refresh": refresh, "backfill": backfill}


def _copy_validated_v3_provider_state(
    value: object, *, context: str,
) -> dict[str, Any]:
    """Deep-copy a v3 provider state that has already passed ``validate_notebook``.

    Unlike ``_translate_legacy_provider_state``, this function performs **zero**
    type coercion.  It expects the input to be a fully-formed v3 backfill dict
    with correct ``BACKFILL_STATE_FIELDS``, types, and ranges.  Invalid input
    raises ``MigrationBlocked`` immediately.
    """
    raw = value if isinstance(value, dict) else {}
    if "backfill" not in raw:
        raise MigrationBlocked(f"v3 provider state missing 'backfill': {context}")
    backfill = raw.get("backfill", {})
    if not isinstance(backfill, dict):
        raise MigrationBlocked(f"v3 backfill is not a dict: {context}")
    # The caller (``_legacy_queries``) must have already validated the
    # notebook via ``validate_notebook``, so the backfill state is trusted.
    # We still verify exact fields for defence-in-depth.
    from src.discovery.constants import BACKFILL_STATE_FIELDS
    known = set(backfill)
    missing = sorted(BACKFILL_STATE_FIELDS - known)
    if missing:
        raise MigrationBlocked(
            f"v3 backfill missing fields {missing}: {context}"
        )
    extra = sorted(known - BACKFILL_STATE_FIELDS)
    if extra:
        raise MigrationBlocked(
            f"v3 backfill has unknown fields {extra}: {context}"
        )
    from copy import deepcopy
    refresh = raw.get("refresh")
    if not isinstance(refresh, dict):
        refresh = {}
    return {"refresh": deepcopy(refresh), "backfill": deepcopy(backfill)}


def _empty_query(
    query: str,
    language: str,
    source: str,
    *,
    active: bool,
    migration_at: str,
) -> dict[str, Any]:
    normalized = normalize_keyword(query)
    qid = query_identity(language, normalized)
    stamp = migration_at
    return {
        "query_id": qid,
        "query": normalized,
        "normalized_query": normalized,
        "language": language,
        "active": bool(active),
        "source": source,
        "created_at": stamp,
        "updated_at": stamp,
        "providers": {
            provider: {"refresh": _empty_refresh(), "backfill": _empty_backfill()}
            for provider in PROVIDERS
        },
    }


@dataclass(frozen=True)
class LegacyQuery:
    source_filename: str
    old_keyword_id: str
    old_query_ids: tuple[str, ...]
    query: str
    language: str
    entry: dict[str, Any]


def _legacy_queries(
    data: dict[str, Any], *, filename: str, migration_at: str,
    page_records: dict[str, dict[str, Any]] | None = None,
) -> list[LegacyQuery]:
    version = str(data.get("schema_version") or "")
    old_keyword_id = str(data.get("keyword_id") or "")
    if not old_keyword_id:
        raise MigrationBlocked(f"legacy notebook missing keyword_id: {filename}")
    rows: list[tuple[str, dict[str, Any]]] = []
    if version == SCHEMA_VERSION:
        from src.discovery.keyword_notebook import validate_notebook
        try:
            validate_notebook(data)
        except Exception as exc:
            raise MigrationBlocked(
                f"v3 source fails validation: {filename}: {exc}"
            ) from exc
        search_queries = data.get("search_queries")
        if not isinstance(search_queries, dict):
            raise MigrationBlocked(f"v3 notebook search_queries invalid: {filename}")
        rows = [(str(key), value) for key, value in search_queries.items() if isinstance(value, dict)]
    elif version in ("1.0", "2.0"):
        expansions = data.get("expansions")
        if isinstance(expansions, dict):
            rows = [(str(key), value) for key, value in expansions.items() if isinstance(value, dict)]
        elif isinstance(expansions, list):
            rows = [(str(index), value) for index, value in enumerate(expansions) if isinstance(value, dict)]
        elif expansions not in (None, {}):
            raise MigrationBlocked(f"legacy expansions invalid: {filename}")
        top_query = normalize_keyword(str(data.get("keyword") or ""))
        represented = {
            normalize_keyword(str(value.get("query") or "")) for _, value in rows
        }
        if top_query and top_query not in represented:
            rows.append(("__keyword__", {
                "query": top_query,
                "active": True,
                "providers": data.get("provider_state") or {},
                "source": "legacy_keyword",
            }))
    else:
        raise MigrationBlocked(f"unsupported notebook schema {version!r}: {filename}")
    result: list[LegacyQuery] = []
    for old_id, raw in rows:
        query = normalize_keyword(str(raw.get("query") or ""))
        language = str(raw.get("language") or detect_query_language(query))
        if language not in ("zh", "en", "mixed") or detect_query_language(query) != language:
            raise MigrationBlocked(f"invalid legacy query/language in {filename}: {query!r}")
        new_entry = _empty_query(
            query, language, str(raw.get("source") or "migration"),
            active=bool(raw.get("active", True)),
            migration_at=migration_at,
        )
        providers = raw.get("providers") if isinstance(raw.get("providers"), dict) else {}
        for provider in PROVIDERS:
            if version == SCHEMA_VERSION:
                normalized_provider = _copy_validated_v3_provider_state(
                    providers.get(provider, {}),
                    context=f"{filename}/{old_id}/{provider}",
                )
            else:
                normalized_provider = _translate_legacy_provider_state(
                    providers.get(provider, {}),
                    context=f"{filename}/{old_id}/{provider}",
                )
            if page_records is not None:
                _bind_current_page_signature(
                    normalized_provider["backfill"],
                    page_records=page_records,
                    old_keyword_id=old_keyword_id,
                    old_query_ids={old_id, str(raw.get("query_id") or "")},
                    provider=provider,
                    context=f"{filename}/{old_id}/{provider}",
                )
            new_entry["providers"][provider] = normalized_provider
        for field in ("created_at", "updated_at"):
            if raw.get(field):
                new_entry[field] = raw[field]
        old_ids = {old_id, str(raw.get("query_id") or "")}
        old_ids.discard("")
        result.append(LegacyQuery(
            source_filename=filename,
            old_keyword_id=old_keyword_id,
            old_query_ids=tuple(sorted(old_ids)),
            query=query,
            language=language,
            entry=new_entry,
        ))
    return result


def _is_pristine_backfill(state: dict[str, Any]) -> bool:
    """Return True when *state* is a strictly pristine unbound backfill.

    Delegates to the shared predicate so that every consumer — schema
    validator, Store, Audit, Migration — uses the same definition.
    """
    from src.discovery.backfill_state import (
        is_strictly_pristine_unbound_backfill,
    )
    return is_strictly_pristine_unbound_backfill(state)


def _page_signature(page: dict[str, Any]) -> str:
    value = page.get("request_signature")
    if isinstance(value, dict):
        return str(value.get("hash") or "")
    return str(value or "")


def _bind_current_page_signature(
    backfill: dict[str, Any], *, page_records: dict[str, dict[str, Any]],
    old_keyword_id: str, old_query_ids: set[str], provider: str, context: str,
) -> None:
    """Bind migrated state to the page-journal signature for its generation.

    Legacy v2 notebooks stored an expansion-level composite signature, while
    active page journals store the provider-specific request-signature hash.
    A progressed state must use the latter after migration or Audit will quite
    correctly reject the resulting notebook/page closure.  This is a format
    translation only: cursor, counters, generation, and history remain intact.
    """
    query_ids = {value for value in old_query_ids if value}
    current_generation = int(backfill.get("generation") or 1)
    observed: set[str] = set()
    for page in page_records.values():
        if str(page.get("keyword_id") or "") != old_keyword_id:
            continue
        page_query_id = str(
            page.get("query_id") or page.get("expansion_id") or ""
        )
        if page_query_id not in query_ids:
            continue
        if str(page.get("provider") or "") != provider \
                or str(page.get("lane") or "") != "backfill":
            continue
        try:
            page_generation = int(page.get("generation") or 1)
        except (TypeError, ValueError) as exc:
            raise MigrationBlocked(
                f"invalid page generation while binding signature: {context}",
                findings={"cursor_conflicts": [{"context": context}]},
            ) from exc
        if page_generation != current_generation:
            continue
        signature = _page_signature(page)
        if signature:
            observed.add(signature)
    if len(observed) > 1:
        raise MigrationBlocked(
            f"multiple page signatures for current generation: {context}",
            findings={"cursor_conflicts": [{
                "context": context,
                "generation": current_generation,
                "signatures": sorted(observed),
            }]},
        )
    if observed:
        backfill["request_signature"] = next(iter(observed))


def _cursor_path(
    edges: dict[str, list[tuple[str, dict[str, Any]]]],
    start: str,
    target: str,
) -> list[dict[str, Any]] | None:
    if start == target:
        return []
    cursor = start
    path: list[dict[str, Any]] = []
    visited: set[str] = set()
    while cursor in edges:
        if cursor in visited or len(edges[cursor]) != 1:
            return None
        visited.add(cursor)
        next_cursor, page = edges[cursor][0]
        path.append(page)
        if next_cursor == target:
            return path
        cursor = next_cursor
    return None


def _prove_cursor_merge(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    left_source: LegacyQuery,
    right_source: LegacyQuery,
    provider: str,
    query_id: str,
    page_records: dict[str, dict[str, Any]],
    context: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    left_bf = left["backfill"]
    right_bf = right["backfill"]
    if left_bf["generation"] != right_bf["generation"]:
        raise MigrationBlocked(
            f"generation mismatch for duplicate query: {context}/{provider}",
            findings={"cursor_conflicts": [{
                "context": context, "query_id": left.get("query_id"),
                "provider": provider,
                "left_generation": left_bf["generation"],
                "right_generation": right_bf["generation"],
            }]},
        )
    if left_bf["request_signature"] != right_bf["request_signature"]:
        raise MigrationBlocked(
            f"request signature mismatch for duplicate query: {context}/{provider}",
            findings={"cursor_conflicts": [{
                "context": context, "query_id": left.get("query_id"),
                "provider": provider,
                "left_signature": left_bf["request_signature"],
                "right_signature": right_bf["request_signature"],
            }]},
        )
    generation = int(left_bf["generation"])
    signature = str(left_bf["request_signature"])
    if not signature:
        raise MigrationBlocked(
            f"duplicate progressed query has no request signature: {context}/{provider}",
            findings={"cursor_conflicts": [{"context": context, "provider": provider}]},
        )
    source_ids = [
        (left_source.old_keyword_id, set(left_source.old_query_ids)),
        (right_source.old_keyword_id, set(right_source.old_query_ids)),
    ]
    matching_pages: list[dict[str, Any]] = []
    for page in page_records.values():
        page_keyword_id = str(page.get("keyword_id") or "")
        page_query_id = str(page.get("query_id") or page.get("expansion_id") or "")
        if not any(
            page_keyword_id == source_keyword_id and page_query_id in source_query_ids
            for source_keyword_id, source_query_ids in source_ids
        ):
            continue
        if str(page.get("provider") or "") != provider or str(page.get("lane") or "") != "backfill":
            continue
        if "generation" not in page or "request_signature" not in page:
            continue
        try:
            page_generation = int(page["generation"])
        except (TypeError, ValueError):
            continue
        if page_generation != generation or _page_signature(page) != signature:
            continue
        matching_pages.append(page)
    edges: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for page in matching_pages:
        request_cursor = str(page.get("request_cursor") or INITIAL_CURSOR)
        next_cursor = str(page.get("next_cursor") or "")
        if not next_cursor:
            continue
        edges.setdefault(request_cursor, []).append((next_cursor, page))
    left_cursor = str(left_bf["cursor"] or INITIAL_CURSOR)
    right_cursor = str(right_bf["cursor"] or INITIAL_CURSOR)
    direction = "same_cursor"
    proof_pages: list[dict[str, Any]] = []
    selected = left_bf
    selected_cursor = left_cursor
    if left_cursor != right_cursor:
        left_to_right = _cursor_path(edges, left_cursor, right_cursor)
        right_to_left = _cursor_path(edges, right_cursor, left_cursor)
        if (left_to_right is None) == (right_to_left is None):
            raise MigrationBlocked(
                f"unproven cursor divergence for duplicate query: {context}/{provider}",
                findings={"cursor_conflicts": [{
                    "context": context, "provider": provider,
                    "left_cursor": left_cursor, "right_cursor": right_cursor,
                }]},
            )
        if left_to_right is not None:
            direction = "left_to_right"
            proof_pages = left_to_right
            selected = right_bf
            selected_cursor = right_cursor
            if left_bf.get("exhausted") and not right_bf.get("exhausted"):
                raise MigrationBlocked(
                    f"exhausted state conflicts with forward cursor proof: {context}/{provider}",
                    findings={"cursor_conflicts": [{"context": context, "provider": provider}]},
                )
        else:
            direction = "right_to_left"
            proof_pages = right_to_left or []
            selected = left_bf
            selected_cursor = left_cursor
            if right_bf.get("exhausted") and not left_bf.get("exhausted"):
                raise MigrationBlocked(
                    f"exhausted state conflicts with forward cursor proof: {context}/{provider}",
                    findings={"cursor_conflicts": [{"context": context, "provider": provider}]},
                )
    else:
        proof_pages = _cursor_path(edges, INITIAL_CURSOR, left_cursor) or []
        if left_cursor != INITIAL_CURSOR and not proof_pages:
            raise MigrationBlocked(
                f"same cursor lacks a journal-chain proof: {context}/{provider}",
                findings={"cursor_conflicts": [{"context": context, "provider": provider, "cursor": left_cursor}]},
            )
        if left_bf.get("exhausted") or right_bf.get("exhausted"):
            selected = left_bf if left_bf.get("exhausted") else right_bf

    merged = deepcopy(selected)
    for key in (
        "pages_succeeded", "pages_committed", "items_returned_total",
        "last_page_count", "cursor_conflicts", "consecutive_failures",
    ):
        merged[key] = max(int(left_bf.get(key) or 0), int(right_bf.get(key) or 0))
    merged["cursor"] = selected_cursor
    merged["exhausted"] = bool(left_bf.get("exhausted") or right_bf.get("exhausted")) if direction == "same_cursor" else bool(selected.get("exhausted"))
    merged["request_signature"] = signature
    merged["generation"] = generation
    merged["last_committed_page_id"] = str(selected.get("last_committed_page_id") or left_bf.get("last_committed_page_id") or right_bf.get("last_committed_page_id") or "")
    for key in (
        "last_success_at", "last_error", "last_failure_at", "last_error_type",
        "next_retry_at", "terminal_failure_at",
    ):
        merged[key] = selected.get(key) or left_bf.get(key) or right_bf.get(key)
    merged["terminal_failure"] = bool(selected.get("terminal_failure") or left_bf.get("terminal_failure") or right_bf.get("terminal_failure"))
    history_by_generation = {
        int(row["generation"]): row
        for row in [*(left_bf.get("generation_history") or []), *(right_bf.get("generation_history") or [])]
    }
    merged["generation_history"] = [history_by_generation[key] for key in sorted(history_by_generation)]
    record = {
        "query_id": query_id,
        "provider": provider,
        "generation": generation,
        "request_signature": signature,
        "left_cursor": left_cursor,
        "right_cursor": right_cursor,
        "selected_cursor": selected_cursor,
        "proof": {
            "type": "page_journal_chain",
            "direction": direction,
            "journal_ids": [str(page.get("page_id") or "") for page in proof_pages],
        },
    }
    return merged, record


def _merge_query(
    target: dict[str, dict[str, Any]], incoming: dict[str, Any], *, context: str,
    incoming_source: LegacyQuery | None = None,
    source_by_query: dict[tuple[str, str], LegacyQuery] | None = None,
    page_records: dict[str, dict[str, Any]] | None = None,
    cursor_merges: list[dict[str, Any]] | None = None,
) -> None:
    qid = incoming["query_id"]
    current = target.get(qid)
    if current is None:
        target[qid] = incoming
        return
    if current["query"].casefold() != incoming["query"].casefold() \
            or current["language"] != incoming["language"]:
        raise MigrationBlocked(f"query identity collision: {context}")
    for provider in PROVIDERS:
        left = current["providers"][provider]
        right = incoming["providers"][provider]
        if left == right:
            continue
        left_bf, right_bf = left["backfill"], right["backfill"]

        if _is_pristine_backfill(left_bf) and not _is_pristine_backfill(right_bf):
            current["providers"][provider] = right
            if source_by_query is not None and incoming_source is not None:
                source_by_query[(qid, provider)] = incoming_source
            continue
        if _is_pristine_backfill(right_bf) and not _is_pristine_backfill(left_bf):
            continue
        left_source = (source_by_query or {}).get((qid, provider))
        if left_source is None or incoming_source is None or page_records is None:
            raise MigrationBlocked(
                f"duplicate progressed query lacks source journal proof: {context}/{provider}",
                findings={"cursor_conflicts": [{"context": context, "query_id": qid, "provider": provider}]},
            )
        merged, proof = _prove_cursor_merge(
            left, right,
            left_source=left_source,
            right_source=incoming_source,
            provider=provider,
            query_id=qid,
            page_records=page_records,
            context=context,
        )
        current["providers"][provider]["backfill"] = merged
        if cursor_merges is not None:
            cursor_merges.append(proof)
    current["active"] = bool(current.get("active") or incoming.get("active"))


_STAT_KEYS = (
    "keyword_runs", "refresh_lane_runs", "backfill_lane_runs",
    "provider_page_attempts", "provider_page_successes", "provider_page_failures",
    "provider_items_returned", "doi_observations", "candidates_staged",
    "candidates_existing",
)
_STAT_ALIASES = {
    "refresh_lane_runs": "refresh_runs",
    "backfill_lane_runs": "backfill_runs",
    "doi_observations": "unique_dois_seen",
    "candidates_staged": "new_dois_staged",
    "candidates_existing": "existing_dois_skipped",
}


def _add_statistics(total: dict[str, int], data: dict[str, Any], *, filename: str) -> None:
    raw = data.get("lifetime_statistics") if isinstance(data.get("lifetime_statistics"), dict) else {}
    for key in _STAT_KEYS:
        value = raw.get(key, raw.get(_STAT_ALIASES.get(key, ""), 0))
        try:
            number = int(value or 0)
        except (TypeError, ValueError) as exc:
            raise MigrationBlocked(f"invalid statistic {filename}.{key}") from exc
        if number < 0:
            raise MigrationBlocked(f"negative statistic {filename}.{key}")
        total[key] += number


def _notebook_output_name(keyword_zh: str) -> str:
    from src.discovery.keyword_notebook import notebook_filename
    return notebook_filename(keyword_zh)


def _build_notebooks(
    *, topics: dict[str, dict[str, Any]], mappings: dict[str, dict[str, str]],
    source_records: dict[str, dict[str, Any]], tx_id: str, migration_at: str,
    page_records: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, str]], dict, list[dict[str, Any]]]:
    by_topic: dict[str, list[tuple[str, dict[str, Any]]]] = {key: [] for key in topics}
    for filename, mapping in mappings.items():
        by_topic[mapping["keyword_zh"]].append((filename, source_records[filename]))
    outputs: dict[str, dict[str, Any]] = {}
    identity_map: dict[tuple[str, str], dict[str, str]] = {}
    text_map: dict[tuple[str, str], list[dict[str, str]]] = {}
    source_summary: dict[str, dict[str, Any]] = {}
    cursor_merges: list[dict[str, Any]] = []
    for kw, topic in topics.items():
        output = empty_notebook(kw)
        output["enabled"] = topic["enabled"]
        output["classification"] = topic["classification"]
        output["search_queries"] = {}
        output["lifetime_statistics"] = {key: 0 for key in _STAT_KEYS}
        created_values: list[str] = []
        reset_history: list[Any] = []
        definition_history: list[Any] = []
        migration_history: list[Any] = []
        source_by_query: dict[tuple[str, str], LegacyQuery] = {}
        source_rows = by_topic[kw]
        for filename, data in source_rows:
            _add_statistics(output["lifetime_statistics"], data, filename=filename)
            if data.get("created_at"):
                created_values.append(str(data["created_at"]))
            if isinstance(data.get("reset_history"), list):
                reset_history.extend(data["reset_history"])
            if isinstance(data.get("definition_history"), list):
                definition_history.extend(data["definition_history"])
            if isinstance(data.get("migration_history"), list):
                migration_history.extend(data["migration_history"])
            legacy = _legacy_queries(
                data, filename=filename, migration_at=migration_at,
                page_records=page_records,
            )
            source_summary[filename] = {
                "schema_version": str(data.get("schema_version") or ""),
                "keyword_id": str(data.get("keyword_id") or ""),
                "query_count": len(legacy),
                "target_keyword_zh": kw,
            }
            for item in legacy:
                _merge_query(
                    output["search_queries"], item.entry, context=filename,
                    incoming_source=item,
                    source_by_query=source_by_query,
                    page_records=page_records,
                    cursor_merges=cursor_merges,
                )
                for provider in PROVIDERS:
                    source_by_query.setdefault((item.entry["query_id"], provider), item)
                new_qid = item.entry["query_id"]
                mapping_value = {
                    "keyword_id": topic["keyword_id"],
                    "keyword_zh": kw,
                    "query_id": new_qid,
                    "query": item.query,
                    "query_language": item.language,
                }
                for old_qid in item.old_query_ids:
                    key = (item.old_keyword_id, old_qid)
                    existing = identity_map.get(key)
                    if existing is not None and existing != mapping_value:
                        raise MigrationBlocked(f"ambiguous legacy query identity: {key}")
                    identity_map[key] = mapping_value
                text_map.setdefault(
                    (item.old_keyword_id, normalize_keyword(item.query).casefold()), []
                ).append(mapping_value)
        for query_row in topic["search_queries"]:
            entry = _empty_query(
                query_row["query"], query_row["language"], query_row["source"],
                active=query_row["active"],
                migration_at=migration_at,
            )
            current = output["search_queries"].get(entry["query_id"])
            if current is None:
                output["search_queries"][entry["query_id"]] = entry
            else:
                # The curated manifest owns query activation and wording, while
                # the migrated source owns durable provider progress.  A
                # manifest confirmation must therefore never replace or
                # conflict with preserved cursor state.
                if current["language"] != entry["language"] \
                        or current["normalized_query"] != entry["normalized_query"]:
                    raise MigrationBlocked(f"query identity collision: query manifest/{kw}")
                current["active"] = entry["active"]
                current["query"] = entry["query"]
                current["source"] = entry["source"]
                current["updated_at"] = migration_at
        active_languages = {
            row["language"] for row in output["search_queries"].values()
            if row.get("active")
        }
        if topic["enabled"] and not {"zh", "en"}.issubset(active_languages):
            raise MigrationBlocked(f"migrated topic {kw!r} is not bilingual-ready")
        output["created_at"] = min(created_values) if created_values else migration_at
        output["updated_at"] = migration_at
        output["reset_history"] = reset_history
        output["definition_history"] = definition_history
        output["migration_history"] = [*migration_history, {
            "at": migration_at,
            "transaction_id": tx_id,
            "kind": TRANSACTION_KIND,
            "source_notebooks": [name for name, _ in source_rows],
        }]
        output["pending"] = {
            "pages": 0, "candidates": 0, "last_drained_at": migration_at,
        }
        output["backpressure"] = {
            "active": False, "entered_at": None, "last_pending_count": 0,
            "max_threshold": 1000, "resume_threshold": 700,
        }
        _validate_v3_notebook(output)
        outputs[_notebook_output_name(kw)] = output
    # Text fallback is allowed only when it resolves to one explicit mapping.
    for key, values in text_map.items():
        unique = {canonical_hash(value): value for value in values}
        if len(unique) == 1:
            identity_map[(key[0], f"__query_text__:{key[1]}")] = next(iter(unique.values()))
    return outputs, identity_map, source_summary, cursor_merges


def _transform_page(
    data: dict[str, Any], *, relative_path: str,
    identity_map: dict[tuple[str, str], dict[str, str]],
) -> tuple[str, dict[str, Any]]:
    version = str(data.get("schema_version") or "")
    old_keyword_id = str(data.get("keyword_id") or "")
    if version == PAGE_SCHEMA_VERSION:
        old_query_id = str(data.get("query_id") or "")
        old_query_text = normalize_keyword(str(data.get("query") or ""))
    elif version in ("1.0", ""):
        old_query_id = str(data.get("expansion_id") or "")
        old_query_text = normalize_keyword(str(data.get("expanded_query") or ""))
    else:
        raise MigrationBlocked(f"unsupported page journal schema {version!r}: {relative_path}")
    mapped = identity_map.get((old_keyword_id, old_query_id))
    if mapped is None and old_query_text:
        mapped = identity_map.get((
            old_keyword_id, f"__query_text__:{old_query_text.casefold()}"
        ))
    if mapped is None:
        raise MigrationBlocked(
            f"pending page has unmapped keyword/query identity: {relative_path}",
            findings={"page_conflicts": [{
                "path": relative_path, "keyword_id": old_keyword_id,
                "query_id": old_query_id,
            }]},
        )
    transformed = dict(data)
    for legacy_key in ("keyword", "expansion_id", "expanded_query"):
        transformed.pop(legacy_key, None)
    if version == PAGE_SCHEMA_VERSION:
        # v2 pages — keep generation as-is (already validated by PageJournalStore).
        transformed["generation"] = data.get("generation")
    else:
        # Legacy page journals — apply safe translation.
        transformed["generation"] = max(1, int(transformed.get("generation") or 1))
    transformed.update({
        "schema_version": PAGE_SCHEMA_VERSION,
        "keyword_id": mapped["keyword_id"],
        "keyword_zh": mapped["keyword_zh"],
        "query_id": mapped["query_id"],
        "query": mapped["query"],
        "query_language": mapped["query_language"],
    })
    _validate_page_v2(transformed, Path(relative_path))
    page_id = str(transformed.get("page_id") or "")
    provider = str(transformed.get("provider") or "")
    lane = str(transformed.get("lane") or "")
    if not page_id or "/" in page_id or "\\" in page_id:
        raise MigrationBlocked(f"unsafe page_id: {relative_path}")
    if lane not in ("refresh", "backfill"):
        raise MigrationBlocked(f"invalid page lane: {relative_path}")
    target_rel = Path(
        mapped["keyword_id"], mapped["query_id"], provider, lane, f"{page_id}.json"
    ).as_posix()
    return target_rel, transformed


def _reconcile_page_state(
    data: dict[str, Any], *, target_rel: str,
    notebooks: dict[str, dict[str, Any]], migration_at: str,
) -> dict[str, Any] | None:
    """Complete a provably committed legacy backfill page.

    A legacy writer could advance notebook cursor counters before persisting
    the page's ``cursor_committed`` transition.  When the page's ``next_cursor``
    and ``page_id`` exactly match the notebook's current committed state, the
    durable notebook state is sufficient proof to finish that idempotent page
    transition during migration.  Any weaker evidence is left untouched and
    remains an audit failure instead of being guessed away.
    """
    if data.get("lane") != "backfill" or data.get("state") != "fetched":
        return None
    notebook_name = _notebook_output_name(str(data.get("keyword_zh") or ""))
    notebook = notebooks.get(notebook_name)
    if notebook is None:
        return None
    query = (notebook.get("search_queries") or {}).get(str(data.get("query_id") or ""))
    if not isinstance(query, dict):
        return None
    provider = str(data.get("provider") or "")
    provider_state = (query.get("providers") or {}).get(provider)
    backfill = provider_state.get("backfill") if isinstance(provider_state, dict) else None
    if not isinstance(backfill, dict):
        return None
    if int(data.get("generation") or 1) != int(backfill.get("generation") or 1):
        return None
    if str(backfill.get("last_committed_page_id") or "") != str(data.get("page_id") or ""):
        return None
    if str(backfill.get("cursor") or INITIAL_CURSOR) != str(data.get("next_cursor") or ""):
        return None
    data["state"] = "cursor_committed"
    data["cursor_committed_at"] = (
        data.get("cursor_committed_at")
        or data.get("fetched_at")
        or migration_at
    )
    return {
        "page_id": str(data.get("page_id") or ""),
        "target_path": target_rel,
        "keyword_id": str(data.get("keyword_id") or ""),
        "query_id": str(data.get("query_id") or ""),
        "provider": provider,
        "generation": int(data.get("generation") or 1),
        "from_state": "fetched",
        "to_state": "cursor_committed",
        "proof": "last_committed_page_id_and_cursor_match",
    }


def _source_records(source_dir: Path) -> dict[str, dict[str, Any]]:
    return {path.name: _read_json(path) for path in sorted(source_dir.glob("*.json"))}


def _page_records(pending_dir: Path) -> dict[str, dict[str, Any]]:
    if not pending_dir.is_dir():
        return {}
    return {
        path.relative_to(pending_dir).as_posix(): _read_json(path)
        for path in sorted(pending_dir.rglob("*.json"))
    }


def _legacy_query_identity_pairs(data: dict[str, Any]) -> set[tuple[str, str]]:
    """Return durable legacy page identities owned by one archived notebook.

    Retired notebook archives are an explicit provenance boundary.  Their
    pages may still be non-terminal, so the migration must archive them as
    bytes instead of treating them as unknown data or silently dropping them.
    This helper intentionally reads only the legacy identity container; it
    never infers a new Chinese topic from the archived query text.
    """
    old_keyword_id = str(data.get("keyword_id") or "")
    if not old_keyword_id:
        return set()
    version = str(data.get("schema_version") or "")
    if version in ("1.0", "2.0"):
        rows = data.get("expansions")
    elif version == SCHEMA_VERSION:
        rows = data.get("search_queries")
    else:
        return set()
    identities: set[tuple[str, str]] = set()
    if isinstance(rows, dict):
        iterable = rows.items()
    elif isinstance(rows, list):
        iterable = ((str(index), value) for index, value in enumerate(rows))
    else:
        iterable = ()
    for source_query_id, raw in iterable:
        if not isinstance(raw, dict):
            continue
        for query_id in (source_query_id, raw.get("query_id")):
            value = str(query_id or "")
            if value:
                identities.add((old_keyword_id, value))
    return identities


def _retired_page_identity_map(retired_dir: Path) -> dict[tuple[str, str], str]:
    """Index pages whose source notebook is already in an explicit archive.

    The runtime archive currently stores legacy notebooks one directory below
    ``retired_dir`` (for example ``english/foo.json``).  Transaction archives
    have a deeper ``<tx>/notebooks`` or ``<tx>/pending_pages`` layout and are
    deliberately excluded so a resumed migration cannot use its own backup as
    an unreviewed source of identity.
    """
    result: dict[tuple[str, str], str] = {}
    root = Path(retired_dir)
    if not root.is_dir():
        return result
    for path in sorted(root.rglob("*.json")):
        parts = path.relative_to(root).parts
        if len(parts) > 2 or "notebooks" in parts or "pending_pages" in parts:
            continue
        try:
            data = _read_json(path)
        except MigrationBlocked:
            continue
        for identity in _legacy_query_identity_pairs(data):
            owner = str(path.relative_to(root).as_posix())
            existing = result.get(identity)
            if existing is not None and existing != owner:
                raise MigrationBlocked(
                    f"retired notebook identity collision: {identity}",
                    findings={"retired_identity_conflicts": [{
                        "keyword_id": identity[0], "query_id": identity[1],
                        "left": existing, "right": owner,
                    }]},
                )
            result[identity] = owner
    return result


def _legacy_page_identity(data: dict[str, Any]) -> tuple[str, str] | None:
    version = str(data.get("schema_version") or "")
    old_keyword_id = str(data.get("keyword_id") or "")
    if version == PAGE_SCHEMA_VERSION:
        old_query_id = str(data.get("query_id") or "")
    elif version in ("1.0", ""):
        old_query_id = str(data.get("expansion_id") or "")
    else:
        return None
    if not old_keyword_id or not old_query_id:
        return None
    return old_keyword_id, old_query_id


def _page_belongs_to_retired_notebook(
    data: dict[str, Any], retired_identities: dict[tuple[str, str], str],
) -> bool:
    identity = _legacy_page_identity(data)
    return identity is not None and identity in retired_identities


def _page_is_fully_terminal(data: dict[str, Any]) -> bool:
    """Return true only when an unmapped legacy page has no pending work."""
    if data.get("state") != "drained" or not isinstance(data.get("candidates"), list):
        return False
    nonterminal = {"pending", "claimed", "failed_retryable", "staged"}
    return all(
        isinstance(row, dict) and str(row.get("status") or "") not in nonterminal
        for row in data["candidates"]
    )


def _build_plan(
    *, roots: dict[str, Path], tx_id: str, migration_at: str,
    source_dir: Path | None = None, pending_dir: Path | None = None,
) -> dict[str, Any]:
    query_manifest = load_query_manifest(roots["query_manifest_path"])
    live_source_dir = source_dir or roots["notebook_dir"]
    mappings = load_mapping_manifest(
        roots["mapping_manifest_path"], topics=query_manifest,
        source_dir=live_source_dir,
    )
    sources = _source_records(live_source_dir)
    pages = _page_records(pending_dir or roots["pending_pages_dir"])
    outputs, identity_map, source_summary, cursor_merges = _build_notebooks(
        topics=query_manifest, mappings=mappings, source_records=sources,
        tx_id=tx_id, migration_at=migration_at,
        page_records=pages,
    )
    retired_identities = _retired_page_identity_map(roots["retired_dir"])
    page_outputs: dict[str, dict[str, Any]] = {}
    page_sources: dict[str, str] = {}
    page_state_reconciliations: list[dict[str, Any]] = []
    for rel, data in pages.items():
        try:
            target_rel, transformed = _transform_page(
                data, relative_path=rel, identity_map=identity_map,
            )
        except MigrationBlocked as exc:
            if exc.findings.get("page_conflicts"):
                if _page_belongs_to_retired_notebook(data, retired_identities):
                    # The notebook is already explicitly retired.  Preserve
                    # every page byte in this transaction's verified archive,
                    # including pending candidates, without inventing a new
                    # topic assignment.
                    page_sources[rel] = "__archived_retired_source__"
                    continue
                if _page_is_fully_terminal(data):
                    # Preserve terminal orphan history in the verified
                    # transaction archive, but do not keep it in the active
                    # pending queue.
                    page_sources[rel] = "__archived_terminal_orphan__"
                    continue
            raise
        fingerprint = canonical_hash(transformed)
        reconciliation = _reconcile_page_state(
            transformed,
            target_rel=target_rel,
            notebooks=outputs,
            migration_at=migration_at,
        )
        if reconciliation is not None:
            page_state_reconciliations.append(reconciliation)
            fingerprint = canonical_hash(transformed)
        existing = page_outputs.get(target_rel)
        if existing is not None and canonical_hash(existing) != fingerprint:
            raise MigrationBlocked(
                f"multiple pending pages map to conflicting target: {target_rel}",
                findings={"page_conflicts": [{"path": target_rel}]},
            )
        page_outputs[target_rel] = transformed
        page_sources[rel] = target_rel
    source_manifest = {
        filename: sha256_file(live_source_dir / filename) for filename in sorted(sources)
    }
    page_source_manifest = {
        rel: sha256_file((pending_dir or roots["pending_pages_dir"]) / Path(rel))
        for rel in sorted(pages)
    }
    output_manifest = {
        name: canonical_hash(data) for name, data in sorted(outputs.items())
    }
    page_output_manifest = {
        rel: canonical_hash(data) for rel, data in sorted(page_outputs.items())
    }
    plan_identity = {
        "query_manifest_sha256": sha256_file(roots["query_manifest_path"]),
        "mapping_manifest_sha256": sha256_file(roots["mapping_manifest_path"]),
        "sources": source_manifest,
        "page_sources": page_source_manifest,
        "outputs": output_manifest,
        "page_outputs": page_output_manifest,
        "page_mapping": page_sources,
        "cursor_merges": cursor_merges,
        "page_state_reconciliations": page_state_reconciliations,
    }
    return {
        "plan_sha256": canonical_hash(plan_identity),
        "identity": plan_identity,
        "notebooks": outputs,
        "pages": page_outputs,
        "source_summary": source_summary,
        "cursor_merges": cursor_merges,
        "page_state_reconciliations": page_state_reconciliations,
    }


def _transaction_paths(transaction_root: Path, tx_id: str) -> dict[str, Path]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", tx_id):
        raise MigrationBlocked("transaction id must be 8-64 safe characters")
    base = transaction_root / "discovery_keyword_v3" / tx_id
    return {
        "base": base,
        "journal": base / "journal.json",
        "plan": base / "plan.json",
        "backup": base / "backup",
        "stage": base / "stage",
        "lock": base.parent / ".migration.lock",
    }


def _root_snapshot(roots: dict[str, Path]) -> dict[str, str]:
    return {key: str(value) for key, value in roots.items()}


def _read_journal(path: Path, *, tx_id: str, roots: dict[str, Path]) -> dict[str, Any]:
    data = _read_json(path)
    if data.get("schema_version") != JOURNAL_SCHEMA_VERSION \
            or data.get("transaction_kind") != TRANSACTION_KIND \
            or data.get("transaction_id") != tx_id:
        raise JournalConflict(f"invalid migration journal identity: {path}")
    if data.get("trusted_roots") != _root_snapshot(roots):
        raise JournalConflict("journal trusted roots differ from explicit caller roots")
    if data.get("query_manifest_sha256") != sha256_file(roots["query_manifest_path"]):
        raise JournalConflict("query manifest changed since transaction planning")
    if data.get("mapping_manifest_sha256") != sha256_file(roots["mapping_manifest_path"]):
        raise JournalConflict("mapping manifest changed since transaction planning")
    state = str(data.get("state") or "")
    if state not in set(PHASES) | {"rolled_back"}:
        raise JournalConflict(f"unknown migration journal state: {state!r}")
    return data


def _write_journal(path: Path, data: dict[str, Any], *, state: str, **updates: Any) -> dict:
    current = dict(data)
    history = list(current.get("history") or [])
    history.append({"state": state, "at": now_iso()})
    current.update(updates)
    current.update({"state": state, "updated_at": now_iso(), "history": history})
    atomic_write_json(path, current, indent=2)
    return current


def _copy_verified(source: Path, target: Path, expected: str) -> None:
    if sha256_file(source) != expected:
        raise JournalConflict(f"source changed before backup: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    if sha256_file(target) != expected:
        raise NotebookV3MigrationError(f"backup verification failed: {target}")


def _write_backup(
    *, paths: dict[str, Path], roots: dict[str, Path], plan: dict[str, Any],
) -> dict[str, Any]:
    backup = paths["backup"]
    notebook_backup = backup / "notebooks"
    page_backup = backup / "pending_pages"
    notebook_backup.mkdir(parents=True, exist_ok=True)
    page_backup.mkdir(parents=True, exist_ok=True)
    for name, expected in plan["identity"]["sources"].items():
        _copy_verified(roots["notebook_dir"] / name, notebook_backup / name, expected)
    for rel, expected in plan["identity"]["page_sources"].items():
        _copy_verified(
            roots["pending_pages_dir"] / Path(rel), page_backup / Path(rel), expected,
        )
    manifest = {
        "schema_version": "1.0",
        "notebooks": plan["identity"]["sources"],
        "pending_pages": plan["identity"]["page_sources"],
    }
    atomic_write_json(backup / "manifest.json", manifest, indent=2)
    _verify_backup(paths=paths, expected=manifest)
    return manifest


def _verify_backup(*, paths: dict[str, Path], expected: dict | None = None) -> dict:
    manifest_path = paths["backup"] / "manifest.json"
    manifest = _read_json(manifest_path)
    if expected is not None and manifest != expected:
        raise JournalConflict("backup manifest content mismatch")
    for name, digest in (manifest.get("notebooks") or {}).items():
        path = paths["backup"] / "notebooks" / _safe_filename(name, field="backup notebook")
        if not path.is_file() or sha256_file(path) != digest:
            raise JournalConflict(f"notebook backup missing or changed: {path}")
    for rel, digest in (manifest.get("pending_pages") or {}).items():
        rel_path = Path(rel)
        if rel_path.is_absolute() or ".." in rel_path.parts:
            raise JournalConflict(f"unsafe backup page path: {rel}")
        path = paths["backup"] / "pending_pages" / rel_path
        if not path.is_file() or sha256_file(path) != digest:
            raise JournalConflict(f"page backup missing or changed: {path}")
    return manifest


def _write_stage(paths: dict[str, Path], plan: dict[str, Any]) -> None:
    stage = paths["stage"]
    for name, data in plan["notebooks"].items():
        atomic_write_json(stage / "notebooks" / name, data, indent=2)
    for rel, data in plan["pages"].items():
        atomic_write_json(stage / "pending_pages" / Path(rel), data, indent=2)
    manifest = {
        "schema_version": "1.0",
        "notebooks": plan["identity"]["outputs"],
        "pending_pages": plan["identity"]["page_outputs"],
        "plan_sha256": plan["plan_sha256"],
    }
    atomic_write_json(stage / "manifest.json", manifest, indent=2)
    _verify_stage(paths, plan)


def _verify_stage(paths: dict[str, Path], plan: dict[str, Any]) -> None:
    manifest = _read_json(paths["stage"] / "manifest.json")
    if manifest.get("plan_sha256") != plan["plan_sha256"]:
        raise JournalConflict("staging plan hash mismatch")
    for name, digest in plan["identity"]["outputs"].items():
        data = _read_json(paths["stage"] / "notebooks" / name)
        if canonical_hash(data) != digest:
            raise JournalConflict(f"staged notebook changed: {name}")
    for rel, digest in plan["identity"]["page_outputs"].items():
        data = _read_json(paths["stage"] / "pending_pages" / Path(rel))
        if canonical_hash(data) != digest:
            raise JournalConflict(f"staged page changed: {rel}")


def _current_known_or_raise(
    *, roots: dict[str, Path], plan: dict[str, Any], allow_original: bool,
) -> None:
    source_hashes = plan["identity"]["sources"] if allow_original else {}
    output_hashes = plan["identity"]["outputs"]
    known_names = set(source_hashes) | set(output_hashes)
    current_names = {
        path.name for path in roots["notebook_dir"].glob("*.json") if path.is_file()
    }
    if not current_names <= known_names:
        raise JournalConflict(f"unexpected notebook files during resume: {sorted(current_names-known_names)}")
    for name in current_names:
        data_hash = canonical_hash(_read_json(roots["notebook_dir"] / name))
        file_hash = sha256_file(roots["notebook_dir"] / name)
        if name in output_hashes and data_hash == output_hashes[name]:
            continue
        if name in source_hashes and file_hash == source_hashes[name]:
            continue
        raise JournalConflict(f"notebook changed during transaction: {name}")
    source_pages = plan["identity"]["page_sources"] if allow_original else {}
    output_pages = plan["identity"]["page_outputs"]
    current_pages = _page_records(roots["pending_pages_dir"])
    known_pages = set(source_pages) | set(output_pages)
    if not set(current_pages) <= known_pages:
        raise JournalConflict(
            f"unexpected pending pages during resume: {sorted(set(current_pages)-known_pages)[:5]}"
        )
    for rel, data in current_pages.items():
        path = roots["pending_pages_dir"] / Path(rel)
        if rel in output_pages and canonical_hash(data) == output_pages[rel]:
            continue
        if rel in source_pages and sha256_file(path) == source_pages[rel]:
            continue
        raise JournalConflict(f"pending page changed during transaction: {rel}")


def _archive_inputs(paths: dict[str, Path], roots: dict[str, Path], plan: dict[str, Any], tx_id: str) -> None:
    archive = roots["retired_dir"] / tx_id
    for name, digest in plan["identity"]["sources"].items():
        source = paths["backup"] / "notebooks" / name
        target = archive / "notebooks" / name
        if target.is_file():
            if sha256_file(target) != digest:
                raise JournalConflict(f"retired archive collision: {target}")
        else:
            _copy_verified(source, target, digest)
    for rel, digest in plan["identity"]["page_sources"].items():
        source = paths["backup"] / "pending_pages" / Path(rel)
        target = archive / "pending_pages" / Path(rel)
        if target.is_file():
            if sha256_file(target) != digest:
                raise JournalConflict(f"retired page archive collision: {target}")
        else:
            _copy_verified(source, target, digest)


def _remove_empty_parents(path: Path, stop: Path) -> None:
    parent = path.parent
    stop = stop.resolve()
    while parent != stop and stop in parent.resolve().parents:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _install_outputs(
    *, paths: dict[str, Path], roots: dict[str, Path], plan: dict[str, Any], tx_id: str,
) -> None:
    _verify_backup(paths=paths)
    _verify_stage(paths, plan)
    _current_known_or_raise(roots=roots, plan=plan, allow_original=True)
    _archive_inputs(paths, roots, plan, tx_id)
    roots["notebook_dir"].mkdir(parents=True, exist_ok=True)
    roots["pending_pages_dir"].mkdir(parents=True, exist_ok=True)
    for name, data in plan["notebooks"].items():
        atomic_write_json(roots["notebook_dir"] / name, data, indent=2)
    for rel, data in plan["pages"].items():
        atomic_write_json(roots["pending_pages_dir"] / Path(rel), data, indent=2)
    for name in plan["identity"]["sources"]:
        if name not in plan["identity"]["outputs"]:
            (roots["notebook_dir"] / name).unlink(missing_ok=True)
    for rel in plan["identity"]["page_sources"]:
        if rel not in plan["identity"]["page_outputs"]:
            old = roots["pending_pages_dir"] / Path(rel)
            old.unlink(missing_ok=True)
            _remove_empty_parents(old, roots["pending_pages_dir"])


def _validate_installed(roots: dict[str, Path], plan: dict[str, Any]) -> None:
    _current_known_or_raise(roots=roots, plan=plan, allow_original=False)
    current_names = {path.name for path in roots["notebook_dir"].glob("*.json")}
    if current_names != set(plan["identity"]["outputs"]):
        raise JournalConflict("installed notebook set differs from migration plan")
    for name, digest in plan["identity"]["outputs"].items():
        data = _read_json(roots["notebook_dir"] / name)
        _validate_v3_notebook(data)
        if canonical_hash(data) != digest:
            raise JournalConflict(f"installed notebook hash mismatch: {name}")
    current_pages = _page_records(roots["pending_pages_dir"])
    if set(current_pages) != set(plan["identity"]["page_outputs"]):
        raise JournalConflict("installed pending-page set differs from migration plan")
    for rel, data in current_pages.items():
        _validate_page_v2(data, roots["pending_pages_dir"] / Path(rel))
        if canonical_hash(data) != plan["identity"]["page_outputs"][rel]:
            raise JournalConflict(f"installed page hash mismatch: {rel}")


def _plan_from_backup(
    paths: dict[str, Path], roots: dict[str, Path], tx_id: str, migration_at: str,
) -> dict:
    _verify_backup(paths=paths)
    plan = _build_plan(
        roots=roots, tx_id=tx_id, migration_at=migration_at,
        source_dir=paths["backup"] / "notebooks",
        pending_dir=paths["backup"] / "pending_pages",
    )
    return plan


def _new_journal(
    tx_id: str, roots: dict[str, Path], plan: dict[str, Any], *, migration_at: str,
) -> dict:
    return {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "transaction_kind": TRANSACTION_KIND,
        "transaction_id": tx_id,
        "migration_at": migration_at,
        "state": "planned",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "trusted_roots": _root_snapshot(roots),
        "query_manifest_sha256": plan["identity"]["query_manifest_sha256"],
        "mapping_manifest_sha256": plan["identity"]["mapping_manifest_sha256"],
        "plan_sha256": plan["plan_sha256"],
        "cursor_merges": list(plan.get("cursor_merges") or []),
        "page_state_reconciliations": list(plan.get("page_state_reconciliations") or []),
        "plan_identity": plan["identity"],
        "history": [{"state": "planned", "at": now_iso()}],
    }


def _phase_index(state: str) -> int:
    return PHASES.index(state)


def _safe_rmtree(path: Path, *, trusted_parent: Path) -> None:
    resolved = path.resolve()
    parent = trusted_parent.resolve()
    if resolved == parent or parent not in resolved.parents:
        raise JournalConflict(f"refusing recursive removal outside trusted root: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def rollback_migration(
    *, notebook_dir: Path, retired_dir: Path, pending_pages_dir: Path,
    locks_dir: Path, transaction_root: Path, query_manifest_path: Path,
    mapping_manifest_path: Path, tx_id: str,
) -> dict[str, Any]:
    roots = _resolve_roots(
        notebook_dir=notebook_dir, retired_dir=retired_dir,
        pending_pages_dir=pending_pages_dir, locks_dir=locks_dir,
        transaction_root=transaction_root, query_manifest_path=query_manifest_path,
        mapping_manifest_path=mapping_manifest_path,
    )
    paths = _transaction_paths(roots["transaction_root"], tx_id)
    if not paths["journal"].is_file():
        raise JournalConflict(f"migration journal not found: {paths['journal']}")
    paths["base"].mkdir(parents=True, exist_ok=True)
    with FileLock(str(paths["lock"])):
        journal = _read_journal(paths["journal"], tx_id=tx_id, roots=roots)
        if journal["state"] == "rolled_back":
            return {"transaction_id": tx_id, "status": "already_rolled_back"}
        backup = _verify_backup(paths=paths)
        output_names = set((journal.get("plan_identity") or {}).get("outputs") or {})
        source_names = set(backup.get("notebooks") or {})
        current_names = {path.name for path in roots["notebook_dir"].glob("*.json")}
        if not current_names <= output_names | source_names:
            raise JournalConflict("rollback found notebooks not owned by this transaction")
        output_pages = set((journal.get("plan_identity") or {}).get("page_outputs") or {})
        source_pages = set(backup.get("pending_pages") or {})
        current_pages = set(_page_records(roots["pending_pages_dir"]))
        if not current_pages <= output_pages | source_pages:
            raise JournalConflict("rollback found pending pages not owned by this transaction")
        for path in list(roots["notebook_dir"].glob("*.json")):
            path.unlink()
        for rel in current_pages:
            path = roots["pending_pages_dir"] / Path(rel)
            path.unlink()
            _remove_empty_parents(path, roots["pending_pages_dir"])
        for name, digest in backup["notebooks"].items():
            _copy_verified(
                paths["backup"] / "notebooks" / name,
                roots["notebook_dir"] / name,
                digest,
            )
        for rel, digest in backup["pending_pages"].items():
            _copy_verified(
                paths["backup"] / "pending_pages" / Path(rel),
                roots["pending_pages_dir"] / Path(rel),
                digest,
            )
        archive = roots["retired_dir"] / tx_id
        if archive.exists():
            _safe_rmtree(archive, trusted_parent=roots["retired_dir"])
        journal = _write_journal(paths["journal"], journal, state="rolled_back")
        return {"transaction_id": tx_id, "status": journal["state"]}


def migrate_notebooks_v3(
    *, notebook_dir: Path, retired_dir: Path, pending_pages_dir: Path,
    locks_dir: Path, transaction_root: Path, query_manifest_path: Path,
    mapping_manifest_path: Path, apply: bool = False,
    tx_id: str | None = None, resume: bool = False,
    write_plan: bool = False, expected_plan_sha256: str | None = None,
) -> dict[str, Any]:
    """Plan or apply a strict, resumable notebook/page migration.

    Dry-run is the default and performs no writes.  ``resume=True`` requires an
    existing nonterminal journal and reuses only byte-verified backups/staging.
    """
    roots = _resolve_roots(
        notebook_dir=notebook_dir, retired_dir=retired_dir,
        pending_pages_dir=pending_pages_dir, locks_dir=locks_dir,
        transaction_root=transaction_root, query_manifest_path=query_manifest_path,
        mapping_manifest_path=mapping_manifest_path,
    )
    run_id = tx_id or uuid.uuid4().hex
    paths = _transaction_paths(roots["transaction_root"], run_id)
    if not apply and not write_plan:
        if resume:
            raise MigrationBlocked("resume is an apply operation")
        plan = _build_plan(
            roots=roots, tx_id=run_id, migration_at=now_iso(),
        )
        return {
            "applied": False,
            "blocked": False,
            "transaction_id": run_id,
            "plan_sha256": plan["plan_sha256"],
            "source_notebooks": len(plan["identity"]["sources"]),
            "output_notebooks": len(plan["identity"]["outputs"]),
            "source_pages": len(plan["identity"]["page_sources"]),
            "output_pages": len(plan["identity"]["page_outputs"]),
            "source_summary": plan["source_summary"],
        }
    if write_plan:
        if apply or resume:
            raise MigrationBlocked("--write-plan cannot be combined with apply/resume")
        paths["base"].mkdir(parents=True, exist_ok=False)
        migration_at = now_iso()
        plan = _build_plan(roots=roots, tx_id=run_id, migration_at=migration_at)
        journal = _new_journal(run_id, roots, plan, migration_at=migration_at)
        atomic_write_json(paths["plan"], plan, indent=2)
        atomic_write_json(paths["journal"], journal, indent=2)
        return {
            "applied": False, "planned": True, "transaction_id": run_id,
            "plan_sha256": plan["plan_sha256"], "plan_path": str(paths["plan"]),
            "source_notebooks": len(plan["identity"]["sources"]),
            "output_notebooks": len(plan["identity"]["outputs"]),
            "source_pages": len(plan["identity"]["page_sources"]),
            "output_pages": len(plan["identity"]["page_outputs"]),
        }
    if not tx_id or not expected_plan_sha256:
        raise MigrationBlocked("apply requires transaction_id and expected_plan_sha256")
    paths["base"].mkdir(parents=True, exist_ok=True)
    with FileLock(str(paths["lock"])):
        existing = paths["journal"].is_file()
        if existing:
            journal = _read_journal(paths["journal"], tx_id=run_id, roots=roots)
            migration_at = str(journal.get("migration_at") or "")
            if not migration_at:
                raise JournalConflict("migration journal is missing migration_at")
            if journal["state"] == "committed":
                return {"applied": True, "transaction_id": run_id, "status": "already_committed"}
            if journal["state"] == "rolled_back":
                raise JournalConflict("rolled-back transaction cannot be resumed")
            if not resume and journal["state"] != "planned":
                raise JournalConflict("active journal exists; pass resume=True with the same tx_id")
            plan = _read_json(paths["plan"])
            if plan["plan_sha256"] != journal.get("plan_sha256"):
                raise JournalConflict("durable plan differs from journal")
        else:
            raise JournalConflict("planned journal not found; run --write-plan first")
        if plan.get("plan_sha256") != expected_plan_sha256:
            raise JournalConflict("expected plan SHA-256 does not match durable plan")
        if canonical_hash(plan.get("identity")) != plan.get("plan_sha256"):
            raise JournalConflict("durable plan content/hash mismatch")
        if sha256_file(roots["query_manifest_path"]) != plan["identity"]["query_manifest_sha256"]:
            raise JournalConflict("query manifest changed since planning")
        if sha256_file(roots["mapping_manifest_path"]) != plan["identity"]["mapping_manifest_sha256"]:
            raise JournalConflict("mapping manifest changed since planning")
        try:
            state = journal["state"]
            if _phase_index(state) < _phase_index("backed_up"):
                backup_manifest = _write_backup(paths=paths, roots=roots, plan=plan)
                journal = _write_journal(
                    paths["journal"], journal, state="backed_up",
                    backup_manifest_sha256=canonical_hash(backup_manifest),
                )
                state = journal["state"]
            else:
                _verify_backup(paths=paths)
            if _phase_index(state) < _phase_index("targets_staged"):
                _write_stage(paths, plan)
                journal = _write_journal(paths["journal"], journal, state="targets_staged")
                state = journal["state"]
            else:
                _verify_stage(paths, plan)
            if _phase_index(state) < _phase_index("targets_installed"):
                _install_outputs(paths=paths, roots=roots, plan=plan, tx_id=run_id)
                journal = _write_journal(paths["journal"], journal, state="targets_installed")
                state = journal["state"]
            if _phase_index(state) < _phase_index("sources_archived"):
                journal = _write_journal(paths["journal"], journal, state="sources_archived")
                state = journal["state"]
            if _phase_index(state) < _phase_index("verified"):
                _validate_installed(roots, plan)
                journal = _write_journal(paths["journal"], journal, state="verified")
                state = journal["state"]
            if _phase_index(state) < _phase_index("committed"):
                journal = _write_journal(paths["journal"], journal, state="committed")
            return {
                "applied": True,
                "blocked": False,
                "transaction_id": run_id,
                "status": journal["state"],
                "journal_path": str(paths["journal"]),
                "plan_sha256": plan["plan_sha256"],
                "source_notebooks": len(plan["identity"]["sources"]),
                "output_notebooks": len(plan["identity"]["outputs"]),
                "source_pages": len(plan["identity"]["page_sources"]),
                "output_pages": len(plan["identity"]["page_outputs"]),
            }
        except Exception as exc:
            # Keep the last durable nonterminal phase.  This is resumable; it is
            # never mislabeled as rolled_back until byte-exact restoration ends.
            journal["last_error"] = f"{type(exc).__name__}: {exc}"
            journal["updated_at"] = now_iso()
            atomic_write_json(paths["journal"], journal, indent=2)
            raise

"""Per-keyword discovery notebook: tracks Refresh/Backfill progress.

Schema v3: each notebook represents ONE Chinese classification concept
(``keyword_zh``).  ``search_queries`` holds all search queries (Chinese
+ English) submitted to OpenAlex / Crossref.  Discovery executes every
active search query each run; English queries participate in search but
NEVER create Catalog categories.

Identity: ``keyword_id`` is derived ONLY from ``keyword_zh`` (NFC-
normalized, whitespace-folded, casefolded).  Adding or removing English
search queries does not change ``keyword_id``, ``category_id``, or any
classification decision.  The filename carries a human-readable slug,
but uniqueness is enforced by the 16-hex ``keyword_id``.

Each search query stores independent per-provider Refresh / Backfill
state.  Cursors, statistics, and backoff tracking are per-query.

This module accepts ONLY schema v3 notebooks.  Legacy (v1/v2) notebooks
must be migrated via ``scripts/migrate_keyword_notebooks_v3.py`` before
active discovery can use them.

Concurrency: each notebook file has a companion ``.lock`` (via
``filelock``).  All updates read-modify-write inside the lock and only
merge the touched lane/provider/query node, so concurrent Refresh and
Backfill lanes for the same keyword cannot clobber each other.  The
write itself uses an inline tmp+``os.replace`` (NOT
``atomic_write_json``) so we do not re-acquire the same lock file we
already hold.

Corrupt JSON fails closed: ``load()`` raises ``NotebookCorruptError`` so
callers can surface a hard failure rather than silently re-initializing
and losing cursors.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from filelock import FileLock

from src.discovery.constants import BACKFILL_STATE_FIELDS, INITIAL_CURSOR
from src.utils.atomic_io import _fsync_dir as _fsync_dir_if_posix


SCHEMA_VERSION = "3.0"
PAGINATION_SCHEMA_VERSION = "2.0"

# Lane / provider literals (kept as plain strings for JSON readability).
LANES = ("refresh", "backfill")
PROVIDERS = ("openalex", "crossref")

# Legacy schema versions rejected by active code (accepted only by the
# migration module at src/discovery/notebook_v3_migration.py).
_REJECTED_SCHEMA_VERSIONS = {"1.0", "2.0"}


class NotebookCorruptError(RuntimeError):
    """Raised when a notebook file cannot be parsed as valid JSON dict."""


class LegacyNotebookError(RuntimeError):
    """Raised by active discovery when a v1 notebook would be unsafe to use."""


class LegacyNotebookSchemaError(RuntimeError):
    """Raised when active code encounters a v2 notebook that must be migrated."""


class UnsupportedNotebookSchemaError(RuntimeError):
    """Raised when a notebook has an unknown schema_version."""


class DiscoveryNotReadyError(RuntimeError):
    """Raised when a notebook lacks required bilingual queries for discovery."""


class CursorConflictError(RuntimeError):
    """Raised when expected-cursor CAS detects a stale writer."""


# ── Keyword normalization & identity ─────────────────────────────────


def normalize_keyword(keyword: str) -> str:
    """Strip + Unicode NFC + fold runs of whitespace.

    Case is NOT folded here (CJK has no case; English case is folded
    only at identity-comparison time via ``_identity_key``).
    """
    if not keyword:
        return ""
    value = unicodedata.normalize("NFC", keyword.strip())
    return re.sub(r"\s+", " ", value)


def _identity_key(normalized: str) -> str:
    """Casefolded key used for uniqueness / hashing."""
    return re.sub(r"\s+", " ", normalized).casefold().strip()


def keyword_id(keyword: str) -> str:
    """Stable 16-hex id from the normalized+casefolded keyword."""
    normalized = normalize_keyword(keyword)
    return hashlib.sha256(_identity_key(normalized).encode("utf-8")).hexdigest()[:16]


def keyword_fingerprint8(keyword: str) -> str:
    return keyword_id(keyword)[:8]


def safe_slug(keyword: str, max_len: int = 48) -> str:
    """Human-readable filename component (NOT the identity)."""
    s = re.sub(r"[^\w一-鿿]+", "_", keyword).strip("_")
    s = re.sub(r"_+", "_", s)
    return (s or "query")[:max_len].rstrip("_")


def notebook_filename(keyword: str) -> str:
    """``<safe_slug>__<fp8>.json`` — slug is cosmetic, fp8 is the identity."""
    return f"{safe_slug(keyword)}__{keyword_fingerprint8(keyword)}.json"


def notebook_path(keyword: str, notebook_dir: Path) -> Path:
    return Path(notebook_dir) / notebook_filename(keyword)


def resolve_existing_notebook(
    keyword: str, notebook_dir: Path
) -> Path | None:
    """Find the notebook for ``keyword`` if it already exists.

    Scans the directory for a file whose ``keyword_id`` matches. This
    handles the rare case where the slug changed (e.g. keyword edited in
    place) but the identity is unchanged.
    """
    target_id = keyword_id(keyword)
    nb_dir = Path(notebook_dir)
    if not nb_dir.is_dir():
        return None
    # Fast path: the canonical filename exists.
    canonical = notebook_path(keyword, nb_dir)
    if canonical.is_file():
        return canonical
    # Slow path: scan for a matching keyword_id in the JSON content.
    for p in sorted(nb_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict) and data.get("keyword_id") == target_id:
            return p
    return None


# ── Query language detection ─────────────────────────────────────────


_HAS_CJK = re.compile(r"[一-鿿㐀-䶿豈-﫿]")
_HAS_LATIN = re.compile(r"[a-zA-Z]")
_HAS_WORD_CHAR = re.compile(r"[\w]")


def detect_query_language(query: str) -> str:
    """Return ``"zh"``, ``"en"``, ``"mixed"``, or ``"invalid"``.

    Rules:
    - ``"zh"`` requires at least one CJK character.
    - ``"en"`` requires at least one Latin letter and no CJK.
    - ``"mixed"`` when both CJK and Latin are present.
    - ``"invalid"`` for empty, whitespace-only, pure-numeric, or
      pure-punctuation strings.
    """
    text = (query or "").strip()
    if not text:
        return "invalid"
    has_cjk = bool(_HAS_CJK.search(text))
    has_latin = bool(_HAS_LATIN.search(text))
    has_word = bool(_HAS_WORD_CHAR.search(text))
    # Pure numeric / punctuation — no CJK or Latin letters, even if \w matches digits.
    if not has_cjk and not has_latin:
        return "invalid"
    if has_cjk and has_latin:
        return "mixed"
    if has_cjk:
        return "zh"
    return "en"


def query_identity(language: str, normalized_query: str) -> str:
    """Stable 16-hex identity for one search query within a notebook.

    Two queries with the same language and normalized text share the
    same identity, regardless of ``source`` or creation time.
    """
    lang = str(language or "").strip().lower()
    if lang not in {"zh", "en", "mixed"}:
        raise ValueError(f"query language must be 'zh', 'en', or 'mixed', got {language!r}")
    normalized = normalize_keyword(normalized_query)
    if not normalized:
        raise ValueError("normalized_query must not be blank")
    payload = f"{lang}|{_identity_key(normalized)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ── Discovery readiness validation ───────────────────────────────────


class DiscoveryReadiness:
    """Result of ``validate_discovery_readiness``."""

    def __init__(self, ready: bool, keyword_zh: str, errors: list[str],
                 zh_count: int = 0, en_count: int = 0):
        self.ready = ready
        self.keyword_zh = keyword_zh
        self.errors = errors
        self.zh_count = zh_count
        self.en_count = en_count

    def __bool__(self) -> bool:
        return self.ready


def validate_discovery_readiness(nb: dict[str, Any]) -> DiscoveryReadiness:
    """Check that an enabled v3 notebook is ready for provider queries.

    An enabled notebook MUST have:
    - ``keyword_zh`` is a non-empty Chinese keyword
    - At least one active ``zh`` search query exactly represents ``keyword_zh``
    - At least one active ``en`` search query
    - All active queries pass language validation
    - All ``query_id`` values are unique

    Returns ``DiscoveryReadiness`` with ``ready=True`` if all checks pass.
    """
    kw_zh = str(nb.get("keyword_zh") or "").strip()
    if nb.get("enabled") is False:
        return DiscoveryReadiness(False, kw_zh, [f"notebook {kw_zh!r} is disabled"])
    errors: list[str] = []
    if "enabled" in nb and nb.get("enabled") is True:
        profile = nb.get("relevance_profile")
        if profile is None:
            errors.append("notebook missing relevance_profile")
        elif isinstance(profile, dict):
            from src.discovery.relevance import is_legacy_unbound_profile
            if is_legacy_unbound_profile(profile):
                errors.append(
                    "notebook relevance_profile is profile_unbound; configure a "
                    "taxonomy-resolved profile before discovery"
                )
    if not kw_zh:
        return DiscoveryReadiness(False, "", ["notebook missing keyword_zh"])
    sq = nb.get("search_queries")
    if not isinstance(sq, dict):
        return DiscoveryReadiness(False, kw_zh, ["notebook missing search_queries"])

    zh_queries: list[str] = []
    en_queries: list[str] = []
    seen_ids: set[str] = set()

    for qid, entry in sq.items():
        if not isinstance(entry, dict):
            errors.append(f"search_queries.{qid} is not a dict")
            continue
        if not entry.get("active", True):
            continue
        q = str(entry.get("query") or "").strip()
        declared_lang = str(entry.get("language") or "")
        detected = detect_query_language(q)

        if detected == "invalid":
            errors.append(f"query {q!r} is invalid (empty, numeric, or punctuation-only)")
            continue
        if declared_lang and declared_lang != detected:
            errors.append(
                f"query {q!r}: declared language {declared_lang!r} "
                f"does not match detected {detected!r}"
            )
            continue

        if qid in seen_ids:
            errors.append(f"duplicate query_id: {qid}")
        seen_ids.add(qid)

        if detected == "zh":
            zh_queries.append(q)
        elif detected == "en":
            en_queries.append(q)

    if not zh_queries:
        errors.append(f"no active Chinese search query in notebook {kw_zh!r}")
    elif _identity_key(normalize_keyword(kw_zh)) not in {
        _identity_key(normalize_keyword(query)) for query in zh_queries
    }:
        errors.append(
            f"no active Chinese search query exactly matches keyword_zh {kw_zh!r}"
        )
    if not en_queries:
        errors.append(f"no active English search query in notebook {kw_zh!r}")

    ready = len(errors) == 0
    return DiscoveryReadiness(
        ready=ready,
        keyword_zh=kw_zh,
        errors=errors,
        zh_count=len(zh_queries),
        en_count=len(en_queries),
    )


# ── Notebook v3 field accessors ──────────────────────────────────────


def _active_queries(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return active search queries from a v3 notebook.

    Each returned dict has: ``query``, ``language``, ``active``,
    ``source``, ``providers``, ``_key``.
    """
    validate_notebook(data)
    sq = data["search_queries"]
    result: list[dict[str, Any]] = []
    for qid, entry in sq.items():
        if not entry["active"]:
            continue
        result.append({
            "query": entry["query"],
            "query_id": qid,
            "language": entry["language"],
            "active": True,
            "source": entry["source"],
            "providers": entry["providers"],
            "_key": qid,
        })
    return result


# ── Pagination signature ─────────────────────────────────────────────


def pagination_signature(
    sort: str | None = None,
    page_size: int | None = None,
    schema_version: str = PAGINATION_SCHEMA_VERSION,
) -> str:
    """Hash of sort + page size + pagination schema."""
    parts = [str(sort or ""), str(page_size or ""), schema_version]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def composite_backfill_signature(
    *,
    page_size: int,
    openalex_backfill_sort: str | None = None,
    crossref_backfill_sort: str | None = None,
    schema_version: str = PAGINATION_SCHEMA_VERSION,
) -> str:
    """Expansion-level Backfill generation signature."""
    payload = {
        "page_size": int(page_size),
        "pagination_schema_version": schema_version,
        "providers": {
            "openalex": {"backfill_sort": openalex_backfill_sort or ""},
            "crossref": {"backfill_sort": crossref_backfill_sort or ""},
        },
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


# ── Notebook state factories ─────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_refresh_state() -> dict[str, Any]:
    return {
        "last_started_at": None,
        "last_success_at": None,
        "last_status": None,
        "pages_scanned_last_run": 0,
        "items_returned_last_run": 0,
        "last_error": None,
    }


def _empty_backfill_state(
    request_signature_hash: str = "",
    *,
    generation: int | None = None,
    generation_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    signature = str(request_signature_hash or "").strip()
    generation_value = int(generation) if generation is not None else 1
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
        "request_signature": signature,
        "generation": generation_value,
        "generation_history": list(generation_history or []),
        "consecutive_failures": 0,
        "last_failure_at": None,
        "last_error_type": None,
        "next_retry_at": None,
        "terminal_failure": False,
        "terminal_failure_at": None,
    }


def _empty_provider_lanes(request_signature_hash: str = "") -> dict[str, Any]:
    return {
        "refresh": _empty_refresh_state(),
        "backfill": _empty_backfill_state(request_signature_hash),
    }


_BACKOFF_DEFAULTS: dict[str, Any] = {
    "consecutive_failures": 0,
    "last_failure_at": None,
    "last_error_type": None,
    "next_retry_at": None,
    "terminal_failure": False,
    "terminal_failure_at": None,
}


def _empty_search_query(
    query: str,
    request_signature_hash: str = "",
    *,
    language: str = "",
    source: str = "curated",
) -> dict[str, Any]:
    """Build a fresh v3 search query entry with language detection."""
    lang = language or detect_query_language(query)
    norm = normalize_keyword(query)
    qid = query_identity(lang, norm)
    return {
        "query_id": qid,
        "query": query,
        "normalized_query": norm,
        "language": lang,
        "active": True,
        "source": source,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "providers": {
            "openalex": _empty_provider_lanes(request_signature_hash),
            "crossref": _empty_provider_lanes(request_signature_hash),
        },
    }


def empty_notebook(keyword_zh: str) -> dict[str, Any]:
    """Build a fresh v3 notebook dict for a new Chinese keyword concept."""
    if not isinstance(keyword_zh, str) or not _HAS_CJK.search(keyword_zh):
        raise ValueError("keyword_zh must contain Chinese text")
    from src.discovery.relevance import legacy_unbound_profile
    normalized = normalize_keyword(keyword_zh)
    return {
        "schema_version": SCHEMA_VERSION,
        "keyword_id": keyword_id(keyword_zh),
        "keyword_zh": keyword_zh.strip(),
        "normalized_keyword_zh": normalized,
        "enabled": True,
        "relevance_generation": 1,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "classification": {
            "guidance_zh": None,
            "aliases_zh": [],
            "exclusions_zh": [],
        },
        # A sentinel keeps historical v3 fixtures claimable; operator
        # notebooks must replace it with a taxonomy-resolved profile through
        # configure_relevance_profiles.py before production discovery.
        "relevance_profile": legacy_unbound_profile(),
        "search_queries": {},
        "definition_history": [],
        "lifetime_statistics": {
            "keyword_runs": 0,
            "refresh_lane_runs": 0,
            "backfill_lane_runs": 0,
            "provider_page_attempts": 0,
            "provider_page_successes": 0,
            "provider_page_failures": 0,
            "provider_items_returned": 0,
            "doi_observations": 0,
            "candidates_staged": 0,
            "candidates_existing": 0,
        },
        "pending": {"pages": 0, "candidates": 0, "last_drained_at": None},
        "backpressure": {
            "active": False,
            "entered_at": None,
            "last_pending_count": 0,
            "max_threshold": 1000,
            "resume_threshold": 700,
        },
        "reset_history": [],
        "migration_history": [],
    }


# ── Schema validation (v3 only) ──────────────────────────────────────


_TOP_LEVEL_REQUIRED = {
    "schema_version", "keyword_id", "keyword_zh", "normalized_keyword_zh",
    "enabled", "created_at", "updated_at", "classification", "search_queries",
    "definition_history", "lifetime_statistics", "pending", "backpressure",
    "reset_history", "migration_history",
}
_TOP_LEVEL_ALLOWED = _TOP_LEVEL_REQUIRED | {"relevance_profile", "relevance_generation"}
_QUERY_REQUIRED = {
    "query_id", "query", "normalized_query", "language", "active", "source",
    "created_at", "updated_at", "providers",
}
_REFRESH_REQUIRED = {
    "last_started_at", "last_success_at", "last_status", "pages_scanned_last_run",
    "items_returned_last_run", "last_error",
}
_BACKFILL_REQUIRED = {
    "cursor", "exhausted", "pages_succeeded", "pages_committed",
    "items_returned_total", "last_page_count", "last_committed_page_id",
    "cursor_conflicts", "last_success_at", "last_error", "request_signature",
    "generation", "generation_history", *_BACKOFF_DEFAULTS.keys(),
}
_LIFETIME_STAT_KEYS = {
    "keyword_runs", "refresh_lane_runs", "backfill_lane_runs",
    "provider_page_attempts", "provider_page_successes", "provider_page_failures",
    "provider_items_returned", "doi_observations", "candidates_staged",
    "candidates_existing",
}
_HEX16 = re.compile(r"^[0-9a-f]{16}$")


def _require_keys(value: dict[str, Any], required: set[str], path: str) -> None:
    missing = sorted(required - set(value))
    if missing:
        raise NotebookCorruptError(f"{path} missing keys: {missing}")


def _require_exact_keys(value: dict[str, Any], required: set[str], path: str) -> None:
    _require_keys(value, required, path)
    extra = sorted(set(value) - required)
    if extra:
        raise NotebookCorruptError(f"{path} has unexpected keys: {extra}")


def _require_dict(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise NotebookCorruptError(f"{path} must be an object")
    return value


def _require_nonnegative_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise NotebookCorruptError(f"{path} must be a non-negative integer")
    return value


def _require_optional_text(value: Any, path: str) -> None:
    if value is not None and not isinstance(value, str):
        raise NotebookCorruptError(f"{path} must be a string or null")


def _validate_string_list(value: Any, path: str) -> None:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise NotebookCorruptError(f"{path} must be a list of non-blank strings")


def _validate_refresh_state(value: Any, path: str) -> None:
    state = _require_dict(value, path)
    _require_keys(state, _REFRESH_REQUIRED, path)
    for key in ("last_started_at", "last_success_at", "last_error"):
        _require_optional_text(state[key], f"{path}.{key}")
    if state["last_status"] not in {None, "success", "partial_success", "failed"}:
        raise NotebookCorruptError(f"{path}.last_status is invalid")
    for key in ("pages_scanned_last_run", "items_returned_last_run"):
        _require_nonnegative_int(state[key], f"{path}.{key}")


def _validate_generation_history(value: Any, path: str) -> None:
    if not isinstance(value, list):
        raise NotebookCorruptError(f"{path} must be a list")
    previous = -1
    for index, item in enumerate(value):
        entry_path = f"{path}[{index}]"
        entry = _require_dict(item, entry_path)
        _require_keys(entry, {"generation", "request_signature", "closed_at", "reason"}, entry_path)
        generation = _require_nonnegative_int(entry["generation"], f"{entry_path}.generation")
        if generation < 1 or generation <= previous:
            raise NotebookCorruptError(f"{path} generations must be strictly increasing")
        previous = generation
        signature = entry["request_signature"]
        if not isinstance(signature, str) or (signature and not _HEX16.fullmatch(signature)):
            raise NotebookCorruptError(
                f"{entry_path}.request_signature must be empty or 16 lowercase hex"
            )
        for key in ("closed_at", "reason"):
            if not isinstance(entry[key], str) or not entry[key].strip():
                raise NotebookCorruptError(f"{entry_path}.{key} must be a non-blank string")


def _validate_backfill_state(value: Any, path: str) -> None:
    state = _require_dict(value, path)
    _require_exact_keys(state, BACKFILL_STATE_FIELDS, path)
    if not isinstance(state["cursor"], str) or not state["cursor"]:
        raise NotebookCorruptError(f"{path}.cursor must be a non-blank string")
    for key in ("exhausted", "terminal_failure"):
        if not isinstance(state[key], bool):
            raise NotebookCorruptError(f"{path}.{key} must be boolean")
    for key in (
        "pages_succeeded", "pages_committed", "items_returned_total",
        "last_page_count", "cursor_conflicts", "consecutive_failures", "generation",
    ):
        _require_nonnegative_int(state[key], f"{path}.{key}")
    if state["generation"] < 1:
        raise NotebookCorruptError(f"{path}.generation must be at least 1")
    if not isinstance(state["last_committed_page_id"], str):
        raise NotebookCorruptError(f"{path}.last_committed_page_id must be a string")
    for key in (
        "last_success_at", "last_error", "last_failure_at", "last_error_type",
        "next_retry_at", "terminal_failure_at",
    ):
        _require_optional_text(state[key], f"{path}.{key}")
    signature = state["request_signature"]
    if not isinstance(signature, str) or (signature and not _HEX16.fullmatch(signature)):
        raise NotebookCorruptError(
            f"{path}.request_signature must be empty or 16 lowercase hex"
        )
    _validate_generation_history(state["generation_history"], f"{path}.generation_history")
    if not signature:
        # Use the shared strict-pristine predicate so that every progress,
        # failure, retry, terminal, and history field is checked.
        from src.discovery.backfill_state import (
            describe_nonpristine_unbound_backfill,
            is_strictly_pristine_unbound_backfill,
        )
        if not is_strictly_pristine_unbound_backfill(state):
            reasons = describe_nonpristine_unbound_backfill(state)
            raise NotebookCorruptError(
                f"{path} has non-pristine state without request_signature: "
                f"{' '.join(reasons)}"
            )


def _validate_query_entry(map_key: str, value: Any, path: str) -> None:
    entry = _require_dict(value, path)
    _require_exact_keys(entry, _QUERY_REQUIRED, path)
    query = entry["query"]
    if not isinstance(query, str) or not query.strip():
        raise NotebookCorruptError(f"{path}.query must be a non-blank string")
    language = entry["language"]
    if language not in {"zh", "en", "mixed"}:
        raise NotebookCorruptError(f"{path}.language must be 'zh', 'en', or 'mixed'")
    detected = detect_query_language(query)
    if detected != language:
        raise NotebookCorruptError(
            f"{path}.language {language!r} does not match detected language {detected!r}"
        )
    normalized = normalize_keyword(query)
    if entry["normalized_query"] != normalized:
        raise NotebookCorruptError(f"{path}.normalized_query is not canonical")
    expected_id = query_identity(language, normalized)
    if map_key != expected_id or entry["query_id"] != expected_id:
        raise NotebookCorruptError(
            f"{path}.query_id/map key does not match canonical identity"
        )
    if not isinstance(entry["active"], bool):
        raise NotebookCorruptError(f"{path}.active must be boolean")
    for key in ("source", "created_at", "updated_at"):
        if not isinstance(entry[key], str) or not entry[key].strip():
            raise NotebookCorruptError(f"{path}.{key} must be a non-blank string")
    providers = _require_dict(entry["providers"], f"{path}.providers")
    if set(providers) != set(PROVIDERS):
        raise NotebookCorruptError(
            f"{path}.providers must contain exactly {list(PROVIDERS)}"
        )
    for provider in PROVIDERS:
        provider_path = f"{path}.providers.{provider}"
        state = _require_dict(providers[provider], provider_path)
        if set(state) != {"refresh", "backfill"}:
            raise NotebookCorruptError(
                f"{provider_path} must contain exactly refresh/backfill"
            )
        _validate_refresh_state(state["refresh"], f"{provider_path}.refresh")
        _validate_backfill_state(state["backfill"], f"{provider_path}.backfill")


def validate_notebook(data: Any) -> dict[str, Any]:
    """Strictly validate and return one active schema-v3 notebook.

    No migration or default injection occurs here.  Partial v3-shaped and
    legacy notebooks therefore fail closed at the active runtime boundary.
    """
    if not isinstance(data, dict):
        raise NotebookCorruptError(
            f"notebook root is {type(data).__name__}, expected dict"
        )
    version = str(data.get("schema_version") or "")
    if version in _REJECTED_SCHEMA_VERSIONS:
        raise LegacyNotebookSchemaError(
            f"notebook schema {version} must be migrated to v3; "
            "run scripts/migrate_keyword_notebooks_v3.py"
        )
    if version != SCHEMA_VERSION:
        raise UnsupportedNotebookSchemaError(
            f"unsupported notebook schema_version: {version!r}"
        )
    _require_keys(data, _TOP_LEVEL_REQUIRED, "notebook")
    extra = sorted(set(data) - _TOP_LEVEL_ALLOWED)
    if extra:
        raise NotebookCorruptError(f"notebook has unexpected keys: {extra}")
    if "relevance_profile" in data and data["relevance_profile"] is not None:
        from src.discovery.relevance import validate_relevance_profile
        try:
            data["relevance_profile"] = validate_relevance_profile(data["relevance_profile"])
        except ValueError as exc:
            raise NotebookCorruptError(f"notebook.relevance_profile is invalid: {exc}") from exc

    keyword_zh = data["keyword_zh"]
    if (
        not isinstance(keyword_zh, str)
        or not keyword_zh.strip()
        or not _HAS_CJK.search(keyword_zh)
    ):
        raise NotebookCorruptError("notebook.keyword_zh must contain Chinese text")
    normalized_keyword_zh = normalize_keyword(keyword_zh)
    if data["normalized_keyword_zh"] != normalized_keyword_zh:
        raise NotebookCorruptError(
            "notebook.normalized_keyword_zh is not canonical"
        )
    expected_keyword_id = keyword_id(keyword_zh)
    if (
        data["keyword_id"] != expected_keyword_id
        or not _HEX16.fullmatch(str(data["keyword_id"]))
    ):
        raise NotebookCorruptError("notebook.keyword_id does not match keyword_zh")
    if not isinstance(data["enabled"], bool):
        raise NotebookCorruptError("notebook.enabled must be boolean")
    if "relevance_generation" in data:
        _require_nonnegative_int(data["relevance_generation"], "notebook.relevance_generation")
        if data["relevance_generation"] < 1:
            raise NotebookCorruptError("notebook.relevance_generation must be at least 1")
    for key in ("created_at", "updated_at"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise NotebookCorruptError(f"notebook.{key} must be a non-blank string")

    classification = _require_dict(
        data["classification"], "notebook.classification"
    )
    if set(classification) != {"guidance_zh", "aliases_zh", "exclusions_zh"}:
        raise NotebookCorruptError(
            "notebook.classification must contain exactly "
            "guidance_zh/aliases_zh/exclusions_zh"
        )
    _require_optional_text(
        classification["guidance_zh"], "notebook.classification.guidance_zh"
    )
    _validate_string_list(
        classification["aliases_zh"], "notebook.classification.aliases_zh"
    )
    _validate_string_list(
        classification["exclusions_zh"], "notebook.classification.exclusions_zh"
    )

    search_queries = _require_dict(
        data["search_queries"], "notebook.search_queries"
    )
    for query_id_value, entry in search_queries.items():
        if not isinstance(query_id_value, str) or not _HEX16.fullmatch(query_id_value):
            raise NotebookCorruptError(
                "notebook.search_queries map keys must be 16 lowercase hex"
            )
        _validate_query_entry(
            query_id_value,
            entry,
            f"notebook.search_queries.{query_id_value}",
        )

    for history_name in ("definition_history", "reset_history", "migration_history"):
        history = data[history_name]
        if not isinstance(history, list) or any(
            not isinstance(item, dict) for item in history
        ):
            raise NotebookCorruptError(
                f"notebook.{history_name} must be a list of objects"
            )

    statistics = _require_dict(
        data["lifetime_statistics"], "notebook.lifetime_statistics"
    )
    _require_keys(
        statistics, _LIFETIME_STAT_KEYS, "notebook.lifetime_statistics"
    )
    for key in _LIFETIME_STAT_KEYS:
        _require_nonnegative_int(
            statistics[key], f"notebook.lifetime_statistics.{key}"
        )

    pending = _require_dict(data["pending"], "notebook.pending")
    _require_keys(
        pending, {"pages", "candidates", "last_drained_at"}, "notebook.pending"
    )
    _require_nonnegative_int(pending["pages"], "notebook.pending.pages")
    _require_nonnegative_int(
        pending["candidates"], "notebook.pending.candidates"
    )
    _require_optional_text(
        pending["last_drained_at"], "notebook.pending.last_drained_at"
    )

    backpressure = _require_dict(
        data["backpressure"], "notebook.backpressure"
    )
    _require_keys(
        backpressure,
        {"active", "entered_at", "last_pending_count", "max_threshold", "resume_threshold"},
        "notebook.backpressure",
    )
    if not isinstance(backpressure["active"], bool):
        raise NotebookCorruptError("notebook.backpressure.active must be boolean")
    _require_optional_text(
        backpressure["entered_at"], "notebook.backpressure.entered_at"
    )
    for key in ("last_pending_count", "max_threshold", "resume_threshold"):
        _require_nonnegative_int(
            backpressure[key], f"notebook.backpressure.{key}"
        )
    if (
        backpressure["max_threshold"] < 1
        or backpressure["resume_threshold"] >= backpressure["max_threshold"]
    ):
        raise NotebookCorruptError("notebook.backpressure thresholds are invalid")
    return data


_validate_notebook = validate_notebook


# ── Store ────────────────────────────────────────────────────────────


@dataclass
class LaneRunResult:
    """Summary of one lane run for the per-keyword report."""
    lane: str
    status: str
    pages: int
    items_returned: int
    provider_failures: int
    exhausted_states: int


class KeywordNotebookStore:
    """File-backed store with per-keyword locking and field-level merge.

    Accepts ONLY v3 notebooks.  v1/v2 notebooks raise
    ``LegacyNotebookSchemaError`` on load.
    """

    def __init__(self, notebook_dir: Path | str):
        self.notebook_dir = Path(notebook_dir)

    # ── path / lock resolution ───────────────────────────────────────

    def _path_for(self, keyword: str) -> Path:
        existing = resolve_existing_notebook(keyword, self.notebook_dir)
        return existing if existing is not None else notebook_path(keyword, self.notebook_dir)

    def _lock_for(self, keyword: str) -> FileLock:
        nb_path = self._path_for(keyword)
        return FileLock(str(nb_path.with_suffix(nb_path.suffix + ".lock")))

    # ── load / save ──────────────────────────────────────────────────

    def load(self, keyword: str) -> dict[str, Any] | None:
        """Load a v3 notebook or return None if absent.

        Rejects v1/v2 notebooks with ``LegacyNotebookSchemaError``.
        Corrupt JSON raises ``NotebookCorruptError``.
        """
        path = self._path_for(keyword)
        if not path.is_file():
            return None
        with self._lock_for(keyword):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise NotebookCorruptError(f"notebook JSON corrupt: {path}: {exc}") from exc
            return validate_notebook(data)

    def require_v3(self, keyword: str) -> dict[str, Any]:
        """Load a v3 notebook or raise.  v1/v2 → LegacyNotebookSchemaError."""
        nb = self.load(keyword)
        if nb is None:
            raise FileNotFoundError(f"no notebook for keyword: {keyword!r}")
        return nb

    def require(self, keyword: str) -> dict[str, Any]:
        """Load a notebook or raise FileNotFoundError."""
        nb = self.load(keyword)
        if nb is None:
            raise FileNotFoundError(f"no notebook for keyword: {keyword!r}")
        return nb

    def _read_or_init(self, path: Path, keyword: str) -> dict[str, Any]:
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise NotebookCorruptError(
                    f"notebook JSON corrupt: {path}: {exc}"
                ) from exc
            return validate_notebook(data)
        return empty_notebook(keyword)

    def _save(self, path: Path, nb: dict[str, Any]) -> None:
        """Write inline (tmp + os.replace + fsync) — caller already holds the lock."""
        nb["updated_at"] = _now_iso()
        validate_notebook(nb)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write(json.dumps(nb, ensure_ascii=False, indent=2))
                fh.flush()
                os.fsync(fh.fileno())
            last_exc: Exception | None = None
            for _ in range(5):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError as exc:
                    last_exc = exc
                    time.sleep(0.05)
            else:
                if last_exc:
                    try:
                        os.replace(tmp, path)
                    except Exception:
                        raise last_exc
            _fsync_dir_if_posix(path.parent)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def _mutate(self, keyword: str, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """Read-modify-write inside the per-keyword lock."""
        path = self._path_for(keyword)
        if not path.is_file():
            raise FileNotFoundError(f"no notebook for keyword: {keyword!r}")
        with FileLock(str(path.with_suffix(path.suffix + ".lock"))):
            if not path.is_file():
                raise FileNotFoundError(f"no notebook for keyword: {keyword!r}")
            nb = self._read_or_init(path, keyword)
            mutator(nb)
            self._save(path, nb)
            return nb

    # ── notebook lifecycle ────────────────────────────────────────────

    def ensure_notebook(self, keyword_zh: str) -> dict[str, Any]:
        """Get or create a v3 notebook for ``keyword_zh``.

        Does NOT touch search queries, cursors, or backfill state.
        """
        if not isinstance(keyword_zh, str) or not _HAS_CJK.search(keyword_zh):
            raise ValueError("keyword_zh must contain Chinese text")
        path = self._path_for(keyword_zh)
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(path.with_suffix(path.suffix + ".lock"))):
            if path.is_file():
                nb = self._read_or_init(path, keyword_zh)
            else:
                nb = empty_notebook(keyword_zh)
                # An empty notebook cannot satisfy bilingual discovery
                # readiness.  Create it as an explicit draft so callers may
                # build its query set before the atomic enable transition.
                nb["enabled"] = False
                self._save(path, nb)
            return nb

    def create_notebook(
        self,
        keyword_zh: str,
        *,
        classification: dict[str, Any] | None = None,
        search_queries: list[dict[str, Any]] | None = None,
        enabled: bool = True,
        pag_sig: str = "",
        reason: str = "notebook_created",
        operator: str = "unspecified",
    ) -> dict[str, Any]:
        """Atomically create one complete v3 notebook.

        Enabled notebooks must be bilingual-ready before the single durable
        replace.  Disabled notebooks may be created as incomplete drafts.
        """
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        path = self._path_for(keyword_zh)
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(path.with_suffix(path.suffix + ".lock"))):
            if path.is_file():
                raise FileExistsError(f"notebook already exists: {keyword_zh!r}")
            nb = empty_notebook(keyword_zh)
            nb["enabled"] = False
            if classification is not None:
                nb["classification"] = dict(classification)
            # Build the complete object in memory; no empty intermediate file.
            for raw in search_queries or []:
                query = str(raw.get("query") or "").strip()
                language = str(raw.get("language") or "").strip().lower()
                detected = detect_query_language(query)
                if detected not in {"zh", "en", "mixed"} or language != detected:
                    raise ValueError(f"invalid query/language pair: {query!r}/{language!r}")
                normalized = normalize_keyword(query)
                qid = query_identity(language, normalized)
                if qid in nb["search_queries"]:
                    raise ValueError(f"duplicate normalized query: {query!r}")
                entry = _empty_search_query(
                    query, pag_sig, language=language,
                    source=str(raw.get("source") or "curated"),
                )
                entry["active"] = raw.get("active", True)
                if not isinstance(entry["active"], bool):
                    raise ValueError(f"query active must be boolean: {query!r}")
                nb["search_queries"][qid] = entry
            nb["enabled"] = enabled
            readiness = validate_discovery_readiness(nb)
            if enabled and not readiness:
                raise DiscoveryNotReadyError("cannot create enabled notebook: " + "; ".join(readiness.errors))
            nb["definition_history"].append({
                "at": _now_iso(), "operation": "create", "reason": reason,
                "operator": operator,
                "added_query_ids": sorted(nb["search_queries"]),
                "disabled_query_ids": [], "enabled_query_ids": [],
                "classification_changes": sorted((classification or {}).keys()),
            })
            self._save(path, nb)
            return nb

    def require_v3_ready(self, keyword_zh: str) -> dict[str, Any]:
        """Load a v3 notebook and validate discovery readiness.

        Raises ``DiscoveryNotReadyError`` if the notebook lacks required
        bilingual queries.
        """
        nb = self.require_v3(keyword_zh)
        if nb["enabled"] is False:
            raise DiscoveryNotReadyError(f"notebook {keyword_zh!r} is disabled")
        readiness = validate_discovery_readiness(nb)
        if not readiness:
            raise DiscoveryNotReadyError(
                f"notebook {keyword_zh!r} is not discovery-ready:\n  " +
                "\n  ".join(readiness.errors)
            )
        return nb

    # ── sync search queries (management only) ─────────────────────────

    def sync_search_queries(
        self,
        keyword_zh: str,
        *,
        add: list[dict[str, str]] | None = None,
        disable: list[str] | None = None,
        enable: list[str] | None = None,
        pag_sig: str = "",
        reason: str = "search_query_sync",
        operator: str = "unspecified",
    ) -> dict[str, Any]:
        """Explicitly manage search queries in a v3 notebook.

        ``add``: list of ``{"query": ..., "language": "zh"|"en", "source": ...}``
        dicts.  Existing queries (same language + normalized query) are
        left untouched.  ``source`` defaults to ``"curated"``.

        ``disable`` / ``enable``: lists of query strings to toggle ``active``.

        This is the ONLY path that modifies search query definitions.
        Normal discovery runs never call this method.
        """
        signature = str(pag_sig or "").strip()
        if signature and not _HEX16.fullmatch(signature):
            raise ValueError("pag_sig must be empty or 16 lowercase hex")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-blank string")
        if not isinstance(operator, str) or not operator.strip():
            raise ValueError("operator must be a non-blank string")

        add_ops: dict[str, dict[str, str]] = {}
        for index, raw in enumerate(add or []):
            if not isinstance(raw, dict):
                raise ValueError(f"add[{index}] must be an object")
            query = str(raw.get("query") or "").strip()
            language = str(raw.get("language") or "").strip().lower()
            source = str(raw.get("source") or "curated").strip()
            detected = detect_query_language(query)
            if detected not in {"zh", "en", "mixed"}:
                raise ValueError(f"add[{index}].query is not a valid text query")
            if language not in {"zh", "en", "mixed"}:
                raise ValueError(f"add[{index}].language must be 'zh', 'en', or 'mixed'")
            if language != detected:
                raise ValueError(
                    f"add[{index}] declared language {language!r} does not match {detected!r}"
                )
            if not source:
                raise ValueError(f"add[{index}].source must be non-blank")
            normalized = normalize_keyword(query)
            query_id_value = query_identity(language, normalized)
            candidate = {
                "query": query,
                "language": language,
                "source": source,
            }
            existing_op = add_ops.get(query_id_value)
            if existing_op is not None and (
                existing_op["language"] != language or existing_op["source"] != source
            ):
                raise ValueError(
                    f"add contains duplicate canonical query identity: {query!r}"
                )
            add_ops.setdefault(query_id_value, candidate)

        def normalize_toggles(values: list[str] | None, action: str) -> dict[str, str]:
            operations: dict[str, str] = {}
            for index, raw in enumerate(values or []):
                if not isinstance(raw, str):
                    raise ValueError(f"{action}[{index}] must be a string")
                query = raw.strip()
                language = detect_query_language(query)
                if language not in {"zh", "en", "mixed"}:
                    raise ValueError(
                        f"{action}[{index}] is not a valid Chinese or English query"
                    )
                query_id_value = query_identity(language, normalize_keyword(query))
                operations[query_id_value] = query
            return operations

        disable_ops = normalize_toggles(disable, "disable")
        enable_ops = normalize_toggles(enable, "enable")
        conflicting = set(disable_ops).intersection(enable_ops)
        if conflicting:
            raise ValueError("the same query cannot be enabled and disabled in one batch")
        if set(add_ops).intersection(disable_ops):
            raise ValueError("a query cannot be added/reactivated and disabled in one batch")

        def m(nb: dict[str, Any]) -> None:
            readiness_before = validate_discovery_readiness(nb)
            sq = nb["search_queries"]
            known_ids = set(sq).union(add_ops)
            unknown_disable = sorted(set(disable_ops) - known_ids)
            unknown_enable = sorted(set(enable_ops) - known_ids)
            if unknown_disable or unknown_enable:
                details = []
                if unknown_disable:
                    details.append(
                        "unknown disable queries: "
                        + ", ".join(repr(disable_ops[qid]) for qid in unknown_disable)
                    )
                if unknown_enable:
                    details.append(
                        "unknown enable queries: "
                        + ", ".join(repr(enable_ops[qid]) for qid in unknown_enable)
                    )
                raise ValueError("; ".join(details))

            changes: dict[str, list[str]] = {
                "added": [],
                "reactivated": [],
                "enabled": [],
                "disabled": [],
            }
            now = _now_iso()
            for query_id_value, operation in add_ops.items():
                if query_id_value not in sq:
                    sq[query_id_value] = _empty_search_query(
                        operation["query"],
                        signature,
                        language=operation["language"],
                        source=operation["source"],
                    )
                    changes["added"].append(query_id_value)
                elif sq[query_id_value]["active"] is False:
                    sq[query_id_value]["active"] = True
                    sq[query_id_value]["updated_at"] = now
                    changes["reactivated"].append(query_id_value)
            for query_id_value in disable_ops:
                if sq[query_id_value]["active"] is True:
                    sq[query_id_value]["active"] = False
                    sq[query_id_value]["updated_at"] = now
                    changes["disabled"].append(query_id_value)
            for query_id_value in enable_ops:
                if sq[query_id_value]["active"] is False:
                    sq[query_id_value]["active"] = True
                    sq[query_id_value]["updated_at"] = now
                    changes["enabled"].append(query_id_value)

            if any(changes.values()):
                nb["definition_history"].append({
                    "at": now,
                    "action": "search_queries_updated",
                    "reason": reason.strip(),
                    "operator": operator.strip(),
                    "changes": changes,
                })
            readiness_after = validate_discovery_readiness(nb)
            if nb["enabled"] and not readiness_after.ready:
                raise DiscoveryNotReadyError(
                    "query mutation would make enabled notebook not ready: "
                    + "; ".join(readiness_after.errors)
                )

        return self._mutate(keyword_zh, m)

    # ── active query accessor ─────────────────────────────────────────

    def active_search_queries(self, keyword: str) -> list[dict[str, Any]]:
        """Return all active search queries for a v3 keyword."""
        nb = self.require_v3(keyword)
        return _active_queries(nb)

    # ── refresh state ────────────────────────────────────────────────

    def begin_refresh(self, keyword: str, query_id: str, provider: str) -> None:
        def m(nb: dict[str, Any]) -> None:
            entry = nb["search_queries"].get(query_id)
            if not entry:
                return
            r = entry["providers"].get(provider, {}).get("refresh")
            if r is None:
                return
            r["last_started_at"] = _now_iso()
            r["last_error"] = None
        self._mutate(keyword, m)

    def complete_refresh(
        self, keyword: str, query_id: str, provider: str, *,
        status: str, pages_scanned: int, items_returned: int,
        error: str | None = None,
    ) -> None:
        def m(nb: dict[str, Any]) -> None:
            entry = nb["search_queries"].get(query_id)
            if not entry:
                return
            r = entry["providers"].get(provider, {}).get("refresh")
            if r is None:
                return
            r["last_status"] = status
            r["pages_scanned_last_run"] = pages_scanned
            r["items_returned_last_run"] = items_returned
            r["last_error"] = error
            if status in ("success", "partial_success"):
                r["last_success_at"] = _now_iso()
            nb["lifetime_statistics"]["refresh_lane_runs"] = (
                int(nb["lifetime_statistics"].get("refresh_lane_runs", 0)) + 1
            )
            nb["lifetime_statistics"]["provider_items_returned"] = (
                int(nb["lifetime_statistics"].get("provider_items_returned", 0)) + items_returned
            )
        self._mutate(keyword, m)

    # ── backfill state ───────────────────────────────────────────────

    def get_backfill_cursor(self, keyword: str, query_id: str, provider: str) -> str:
        """Return the current backfill cursor (``"*"`` if fresh)."""
        nb = self.require_v3(keyword)
        entry = nb["search_queries"].get(query_id)
        if not entry:
            return INITIAL_CURSOR
        bf = entry["providers"].get(provider, {}).get("backfill", {})
        return bf.get("cursor") or INITIAL_CURSOR

    def get_backfill_state(self, keyword: str, query_id: str, provider: str) -> dict[str, Any]:
        nb = self.require_v3(keyword)
        entry = nb["search_queries"].get(query_id)
        if not entry:
            return {}
        return dict(entry["providers"].get(provider, {}).get("backfill", {}))

    def ensure_backfill_generation(
        self,
        keyword: str,
        query_id: str,
        provider: str,
        *,
        request_signature_hash: str,
    ) -> dict[str, Any]:
        """Bind or roll one provider Backfill state to a request signature.

        A signature change archives the previous generation and resets the
        cursor to ``"*"`` before any provider request can run.  This prevents a
        cursor obtained under one sort/page-size contract from being reused by
        a different provider request contract.
        """
        signature = str(request_signature_hash or "").strip()
        if not _HEX16.fullmatch(signature):
            raise ValueError("request_signature_hash must be 16 lowercase hex")
        result: dict[str, Any] = {}

        def m(nb: dict[str, Any]) -> None:
            nonlocal result
            entry = nb["search_queries"].get(query_id)
            if entry is None:
                raise KeyError(f"unknown query_id: {query_id}")
            if provider not in PROVIDERS:
                raise KeyError(f"unknown provider: {provider}")
            bf = entry["providers"][provider]["backfill"]
            current_signature = bf["request_signature"]
            if current_signature == signature:
                result = dict(bf)
                return

            from src.discovery.backfill_state import (
                BackfillBindDecision,
                BackfillBindError,
                resolve_backfill_generation_binding,
            )
            try:
                decision = resolve_backfill_generation_binding(bf, signature)
            except BackfillBindError as exc:
                raise NotebookCorruptError(str(exc)) from exc

            if decision == BackfillBindDecision.FIRST_BIND:
                bf["request_signature"] = signature
                result = dict(bf)
                return

            # ROLL_GENERATION — current signature differs from requested.
            history = list(bf["generation_history"])
            history.append({
                "generation": int(bf["generation"]),
                "request_signature": current_signature,
                "closed_at": _now_iso(),
                "reason": "request_signature_changed",
                "cursor": bf["cursor"],
                "exhausted": bf["exhausted"],
                "pages_succeeded": bf["pages_succeeded"],
                "pages_committed": bf["pages_committed"],
                "items_returned_total": bf["items_returned_total"],
                "last_committed_page_id": bf["last_committed_page_id"],
            })
            next_generation = max(1, int(bf["generation"]) + 1)
            replacement = _empty_backfill_state(
                signature,
                generation=next_generation,
                generation_history=history,
            )
            entry["providers"][provider]["backfill"] = replacement
            result = dict(replacement)

        self._mutate(keyword, m)
        return result

    def is_backfill_exhausted(self, keyword: str, query_id: str, provider: str) -> bool:
        nb = self.require_v3(keyword)
        entry = nb["search_queries"].get(query_id)
        if not entry:
            return False
        bf = entry["providers"].get(provider, {}).get("backfill", {})
        return bool(bf.get("exhausted"))

    def advance_backfill(
        self, keyword: str, query_id: str, provider: str, *,
        next_cursor: str | None, items_this_page: int,
        exhausted: bool = False,
    ) -> None:
        def m(nb: dict[str, Any]) -> None:
            entry = nb["search_queries"].get(query_id)
            if not entry:
                return
            bf = entry["providers"].get(provider, {}).get("backfill")
            if bf is None:
                return
            if next_cursor is not None:
                bf["cursor"] = next_cursor
            bf["exhausted"] = bool(bf.get("exhausted") or exhausted)
            bf["pages_succeeded"] = int(bf.get("pages_succeeded", 0)) + 1
            bf["items_returned_total"] = int(bf.get("items_returned_total", 0)) + items_this_page
            bf["last_page_count"] = items_this_page
            bf["last_success_at"] = _now_iso()
            bf["last_error"] = None
        self._mutate(keyword, m)

    def commit_backfill_cursor(
        self, keyword: str, query_id: str, provider: str, *,
        expected_cursor: str, next_cursor: str | None,
        committed_page_id: str, exhausted: bool,
        items_this_page: int = 0,
    ) -> "CursorCommitResult":
        result: CursorCommitResult | None = None
        conflict_occurred = False
        conflict_msg = ""

        def m(nb: dict[str, Any]) -> None:
            nonlocal result, conflict_occurred, conflict_msg
            entry = nb["search_queries"].get(query_id)
            if not entry:
                conflict_occurred = True
                conflict_msg = f"missing entry for CAS: {query_id}"
                result = CursorCommitResult(committed=False, previous_cursor=expected_cursor,
                                            current_cursor=INITIAL_CURSOR, conflict=True)
                return
            bf = entry["providers"].get(provider, {}).get("backfill")
            if bf is None:
                conflict_occurred = True
                conflict_msg = f"missing provider backfill state: {provider}"
                result = CursorCommitResult(committed=False, previous_cursor=expected_cursor,
                                            current_cursor=INITIAL_CURSOR, conflict=True)
                return
            current = bf.get("cursor") or INITIAL_CURSOR
            if current != expected_cursor:
                bf["cursor_conflicts"] = int(bf.get("cursor_conflicts", 0)) + 1
                conflict_occurred = True
                conflict_msg = (
                    f"cursor conflict for {keyword}/{query_id}/{provider}: "
                    f"expected {expected_cursor!r}, current {current!r}"
                )
                result = CursorCommitResult(committed=False, previous_cursor=expected_cursor,
                                            current_cursor=current, conflict=True)
                return
            if next_cursor is not None:
                bf["cursor"] = next_cursor
            bf["exhausted"] = bool(bf.get("exhausted") or exhausted)
            bf["pages_succeeded"] = int(bf.get("pages_succeeded", 0)) + 1
            bf["pages_committed"] = int(bf.get("pages_committed", 0)) + 1
            bf["items_returned_total"] = int(bf.get("items_returned_total", 0)) + int(items_this_page)
            bf["last_page_count"] = int(items_this_page)
            bf["last_committed_page_id"] = committed_page_id
            bf["last_success_at"] = _now_iso()
            bf["last_error"] = None
            result = CursorCommitResult(committed=True, previous_cursor=expected_cursor,
                                        current_cursor=bf.get("cursor") or INITIAL_CURSOR, conflict=False)

        self._mutate(keyword, m)
        assert result is not None
        if conflict_occurred:
            raise CursorConflictError(conflict_msg)
        return result

    def record_backfill_error(self, keyword: str, query_id: str, provider: str, *, error: str) -> None:
        def m(nb: dict[str, Any]) -> None:
            entry = nb["search_queries"].get(query_id)
            if not entry:
                return
            bf = entry["providers"].get(provider, {}).get("backfill")
            if bf is None:
                return
            bf["last_error"] = error
        self._mutate(keyword, m)

    def record_backfill_run(self, keyword: str, *, items_returned: int) -> None:
        def m(nb: dict[str, Any]) -> None:
            nb["lifetime_statistics"]["backfill_lane_runs"] = (
                int(nb["lifetime_statistics"].get("backfill_lane_runs", 0)) + 1
            )
            nb["lifetime_statistics"]["provider_items_returned"] = (
                int(nb["lifetime_statistics"].get("provider_items_returned", 0)) + items_returned
            )
        self._mutate(keyword, m)

    # ── lifetime statistics ──────────────────────────────────────────

    def record_stage_outcome(self, keyword: str, *, doi_observations: int = 0,
                             new_staged: int = 0, existing_skipped: int = 0) -> None:
        def m(nb: dict[str, Any]) -> None:
            stats = nb["lifetime_statistics"]
            stats["doi_observations"] = int(stats.get("doi_observations", 0)) + doi_observations
            stats["candidates_staged"] = int(stats.get("candidates_staged", 0)) + new_staged
            stats["candidates_existing"] = int(stats.get("candidates_existing", 0)) + existing_skipped
        self._mutate(keyword, m)

    def update_pending_counts(self, keyword: str, *, pages: int, candidates: int) -> None:
        def m(nb: dict[str, Any]) -> None:
            nb["pending"] = {"pages": int(pages), "candidates": int(candidates), "last_drained_at": _now_iso()}
        self._mutate(keyword, m)

    def update_backpressure(self, keyword: str, *, pending_count: int,
                            max_threshold: int, resume_threshold: int) -> dict[str, Any]:
        if resume_threshold < 0 or resume_threshold >= max_threshold:
            raise ValueError("resume_pending_candidates must satisfy 0 <= resume < max")
        result: dict[str, Any] = {}
        def m(nb: dict[str, Any]) -> None:
            nonlocal result
            current = nb.get("backpressure") if isinstance(nb.get("backpressure"), dict) else {}
            active = bool(current.get("active"))
            pending = int(pending_count)
            if active:
                active = pending > resume_threshold
            else:
                active = pending >= max_threshold
            entered_at = current.get("entered_at")
            if active and not current.get("active"):
                entered_at = _now_iso()
            if not active:
                entered_at = None
            result = {"active": active, "entered_at": entered_at, "last_pending_count": pending,
                      "max_threshold": int(max_threshold), "resume_threshold": int(resume_threshold)}
            nb["backpressure"] = result
        self._mutate(keyword, m)
        return result

    # ── management operations ────────────────────────────────────────

    def set_enabled(self, keyword: str, enabled: bool) -> dict[str, Any] | None:
        self.require(keyword)

        def m(nb: dict[str, Any]) -> None:
            requested = bool(enabled)
            if requested:
                nb["enabled"] = True
                readiness = validate_discovery_readiness(nb)
                if not readiness.ready:
                    nb["enabled"] = False
                    raise DiscoveryNotReadyError(
                        "cannot enable an unready notebook: "
                        + "; ".join(readiness.errors)
                    )
            else:
                nb["enabled"] = False

        return self._mutate(keyword, m)

    def set_relevance_profile(
        self,
        keyword: str,
        profile: dict[str, Any],
        *,
        generation: int,
        expected_profile_hash: str | None = None,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        """Atomically bind one validated notebook profile at a generation."""
        from src.discovery.relevance import validate_relevance_profile

        normalized = validate_relevance_profile(profile)
        if isinstance(expected_profile_hash, str) and expected_profile_hash:
            current = self.require_v3(keyword).get("relevance_profile")
            current_hash = current.get("profile_hash") if isinstance(current, dict) else ""
            if current_hash != expected_profile_hash:
                raise CursorConflictError(
                    f"relevance profile changed for {keyword!r}: expected {expected_profile_hash}"
                )

        def m(nb: dict[str, Any]) -> None:
            if isinstance(expected_profile_hash, str) and expected_profile_hash:
                current_profile = nb.get("relevance_profile")
                current_hash = (
                    current_profile.get("profile_hash")
                    if isinstance(current_profile, dict) else ""
                )
                if current_hash != expected_profile_hash:
                    raise CursorConflictError(
                        f"relevance profile changed for {keyword!r}: expected {expected_profile_hash}"
                    )
            if expected_generation is not None and int(nb.get("relevance_generation") or 1) != int(expected_generation):
                raise CursorConflictError(
                    f"relevance generation changed for {keyword!r}: expected {expected_generation}"
                )
            nb["relevance_profile"] = normalized
            nb["relevance_generation"] = int(generation)

        return self._mutate(keyword, m)

    def reset_backfill(self, keyword: str, *, reason: str, pag_sig: str | None = None) -> dict[str, Any] | None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reset reason must be a non-blank string")
        sig = str(pag_sig or "").strip()
        if sig and not _HEX16.fullmatch(sig):
            raise ValueError("pag_sig must be empty or 16 lowercase hex")

        def m(nb: dict[str, Any]) -> None:
            for entry in nb["search_queries"].values():
                for prov in PROVIDERS:
                    bf = entry["providers"][prov]["backfill"]
                    history = list(bf["generation_history"])
                    has_generation = bool(
                        bf["request_signature"]
                        or bf["cursor"] != INITIAL_CURSOR
                        or bf["pages_succeeded"]
                        or bf["pages_committed"]
                        or bf["last_committed_page_id"]
                    )
                    next_generation = int(bf["generation"])
                    if has_generation:
                        history.append({
                            "generation": int(bf["generation"]),
                            "request_signature": bf["request_signature"],
                            "closed_at": _now_iso(),
                            "reason": f"explicit_reset:{reason.strip()}",
                            "cursor": bf["cursor"],
                            "exhausted": bf["exhausted"],
                            "pages_succeeded": bf["pages_succeeded"],
                            "pages_committed": bf["pages_committed"],
                            "items_returned_total": bf["items_returned_total"],
                            "last_committed_page_id": bf["last_committed_page_id"],
                        })
                        next_generation = max(1, next_generation + 1)
                    entry["providers"][prov]["backfill"] = _empty_backfill_state(
                        sig,
                        generation=next_generation if next_generation else None,
                        generation_history=history,
                    )
            nb["reset_history"].append({
                "at": _now_iso(),
                "reason": reason.strip(),
                "scope": "backfill",
            })
        self.require(keyword)
        return self._mutate(keyword, m)

    def list_keywords(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        # Notebook filenames are canonical ``<keyword>__<id>.json``.  The
        # directory may also contain explicitly configured state JSON files;
        # those are not notebook candidates and must not be parsed as such.
        for p in sorted(self.notebook_dir.glob("*__*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise NotebookCorruptError(f"notebook JSON corrupt: {p}: {exc}") from exc
            validate_notebook(data)
            out.append(self._summarize(data))
        return out

    def show(self, keyword: str) -> dict[str, Any] | None:
        nb = self.load(keyword)
        if nb is None:
            return None
        return self._summarize(nb)

    @staticmethod
    def _summarize(data: dict[str, Any]) -> dict[str, Any]:
        validate_notebook(data)
        kw_zh = data["keyword_zh"]
        queries_list = list(data["search_queries"].values())
        active = [e for e in queries_list if e["active"]]
        readiness = validate_discovery_readiness(data)
        return {
            "keyword_zh": kw_zh,
            "keyword_id": data["keyword_id"],
            "enabled": data["enabled"],
            "ready": readiness.ready,
            "active_queries": len(active),
            "queries": [
                {
                    "query_id": e.get("query_id", ""),
                    "query": e.get("query", ""),
                    "language": e.get("language", ""),
                    "active": e.get("active", False),
                    "source": e.get("source", ""),
                    "providers": {
                        prov: {
                            "refresh_status": e.get("providers", {}).get(prov, {}).get("refresh", {}).get("last_status"),
                            "backfill_cursor": e.get("providers", {}).get(prov, {}).get("backfill", {}).get("cursor"),
                            "backfill_exhausted": e.get("providers", {}).get(prov, {}).get("backfill", {}).get("exhausted"),
                            "backfill_pages": e.get("providers", {}).get(prov, {}).get("backfill", {}).get("pages_succeeded"),
                        }
                        for prov in PROVIDERS
                    },
                }
                for e in queries_list
            ],
        }


@dataclass(frozen=True)
class CursorCommitResult:
    committed: bool
    previous_cursor: str
    current_cursor: str
    conflict: bool

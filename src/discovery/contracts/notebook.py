"""Discovery v4 strict notebook contract.

Canonical model for a v4 keyword notebook, plus the single authority for
v4 notebook schema validation and helpers: keyword identity and
normalization, query language detection, discovery readiness validation,
lane-state factories, and the strict ``validate_notebook`` field-set
validators.  Those helpers previously lived in
``src/discovery/keyword_notebook.py``; that retired alias shell is deleted
and this contract module is the sole implementation.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.discovery.contracts.lane_history import (
    ExhaustionEvidence,
    GenerationHistoryEntry,
)
from src.discovery.contracts.errors import (
    CursorConflictError,
    DiscoveryNotReadyError,
    NotebookContractError,
    NotebookCorruptError,
    UnsupportedNotebookSchemaError,
)
from src.utils.timestamps import utc_now_iso as _now_iso
from src.discovery.constants import (
    BACKFILL_STATE_ACCEPTED_FIELDS,
    BACKFILL_STATE_FIELDS,
    INITIAL_CURSOR,
)

NOTEBOOK_SCHEMA_VERSION_V4 = "4.0"

# Top-level fields known to be part of a v4 notebook.  The reporting builder
# and store may emit additional internal keys, but any persisted notebook must
# carry at least these fields and no unknown top-level keys.
NOTEBOOK_TOP_LEVEL_FIELDS: frozenset[str] = frozenset({
    "schema_version", "keyword_id", "keyword_zh", "normalized_keyword_zh",
    "enabled", "created_at", "updated_at", "classification", "search_queries",
    "definition_history", "lifetime_statistics", "pending", "backpressure",
    "reset_history", "migration_history", "relevance_profile",
    "relevance_generation",
})

NOTEBOOK_TOP_LEVEL_REQUIRED: frozenset[str] = frozenset({
    "schema_version", "keyword_id", "keyword_zh", "normalized_keyword_zh",
    "enabled", "created_at", "updated_at", "classification", "search_queries",
    "definition_history", "lifetime_statistics", "pending", "backpressure",
    "reset_history", "migration_history",
})


def _check_str(value: Any, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str, got {type(value).__name__}")


@dataclass(frozen=True)
class KeywordNotebookV4:
    """Canonical v4 keyword notebook contract."""

    keyword_id: str
    keyword_zh: str
    normalized_keyword_zh: str
    enabled: bool
    created_at: str
    updated_at: str
    schema_version: str = NOTEBOOK_SCHEMA_VERSION_V4
    classification: Mapping[str, Any] = field(default_factory=dict)
    search_queries: Mapping[str, Any] = field(default_factory=dict)
    definition_history: list[Any] = field(default_factory=list)
    lifetime_statistics: Mapping[str, Any] = field(default_factory=dict)
    pending: Mapping[str, Any] = field(default_factory=dict)
    backpressure: Mapping[str, Any] = field(default_factory=dict)
    reset_history: list[Any] = field(default_factory=list)
    migration_history: list[Any] = field(default_factory=list)
    relevance_profile: Mapping[str, Any] | None = None
    relevance_generation: int = 1

    def __post_init__(self) -> None:
        for name in ("keyword_id", "keyword_zh", "normalized_keyword_zh", "schema_version", "created_at", "updated_at"):
            _check_str(getattr(self, name), name)
        if self.schema_version != NOTEBOOK_SCHEMA_VERSION_V4:
            raise NotebookContractError(
                f"schema_version must be {NOTEBOOK_SCHEMA_VERSION_V4!r}, "
                f"got {self.schema_version!r}"
            )
        if not isinstance(self.enabled, bool):
            raise TypeError(f"enabled must be bool, got {type(self.enabled).__name__}")
        if not isinstance(self.relevance_generation, int) or isinstance(self.relevance_generation, bool):
            raise TypeError(f"relevance_generation must be int, got {type(self.relevance_generation).__name__}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "keyword_id": self.keyword_id,
            "keyword_zh": self.keyword_zh,
            "normalized_keyword_zh": self.normalized_keyword_zh,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "classification": dict(self.classification),
            "search_queries": dict(self.search_queries),
            "definition_history": list(self.definition_history),
            "lifetime_statistics": dict(self.lifetime_statistics),
            "pending": dict(self.pending),
            "backpressure": dict(self.backpressure),
            "reset_history": list(self.reset_history),
            "migration_history": list(self.migration_history),
            "relevance_profile": dict(self.relevance_profile) if self.relevance_profile is not None else None,
            "relevance_generation": self.relevance_generation,
        }

    @classmethod
    def from_dict_strict(cls, data: Mapping[str, Any]) -> "KeywordNotebookV4":
        if not isinstance(data, dict):
            raise NotebookContractError("notebook must be a JSON object")
        extra = set(data) - NOTEBOOK_TOP_LEVEL_FIELDS
        if extra:
            raise NotebookContractError(
                f"notebook has unknown top-level fields: {sorted(extra)}"
            )
        missing = NOTEBOOK_TOP_LEVEL_REQUIRED - set(data)
        if missing:
            raise NotebookContractError(
                f"notebook missing top-level fields: {sorted(missing)}"
            )
        if data.get("schema_version") != NOTEBOOK_SCHEMA_VERSION_V4:
            raise NotebookContractError(
                f"schema_version must be {NOTEBOOK_SCHEMA_VERSION_V4!r}, "
                f"got {data.get('schema_version')!r}"
            )
        return cls(
            keyword_id=str(data["keyword_id"]),
            keyword_zh=str(data["keyword_zh"]),
            normalized_keyword_zh=str(data["normalized_keyword_zh"]),
            enabled=bool(data["enabled"]),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            classification=dict(data.get("classification", {})),
            search_queries=dict(data.get("search_queries", {})),
            definition_history=list(data.get("definition_history", [])),
            lifetime_statistics=dict(data.get("lifetime_statistics", {})),
            pending=dict(data.get("pending", {})),
            backpressure=dict(data.get("backpressure", {})),
            reset_history=list(data.get("reset_history", [])),
            migration_history=list(data.get("migration_history", [])),
            relevance_profile=dict(data["relevance_profile"]) if data.get("relevance_profile") is not None else None,
            relevance_generation=int(data.get("relevance_generation", 1)) if not isinstance(data.get("relevance_generation"), bool) else 1,
        )


__all__ = [
    "NOTEBOOK_SCHEMA_VERSION_V4",
    "NOTEBOOK_TOP_LEVEL_FIELDS",
    "NOTEBOOK_TOP_LEVEL_REQUIRED",
    "NotebookContractError",
    "KeywordNotebookV4",
]


SCHEMA_VERSION = "4.0"
PAGINATION_SCHEMA_VERSION = "2.0"

# Lane / provider literals (kept as plain strings for JSON readability).
LANES = ("refresh", "backfill")
PROVIDERS = ("openalex", "crossref")


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
    """Check that an enabled v4 notebook is ready for provider queries.

    An enabled notebook MUST have:
    - ``keyword_zh`` is a non-empty Chinese keyword
    - A taxonomy-resolved ``relevance_profile`` passing the active validator
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
            errors.append(
                "notebook relevance_profile is unconfigured; bind a "
                "taxonomy-resolved profile through configure_relevance_profiles.py "
                "before enabling discovery"
            )
        elif isinstance(profile, dict):
            from src.discovery.relevance import (
                RelevanceProfileError,
                validate_relevance_profile,
            )
            # Active validator — must resolve taxonomy, non-empty filter_ids,
            # valid matcher schema, correct profile_hash.  This is independent
            # of validate_notebook(); readiness owns its own active-profile
            # gate so that an unresolved profile never reaches a provider.
            try:
                validate_relevance_profile(profile)
            except RelevanceProfileError as exc:
                errors.append(
                    f"notebook relevance_profile is not active-ready: {exc}"
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


# ── Notebook v4 field accessors ──────────────────────────────────────


def _active_queries(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return active search queries from a v4 notebook.

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



def _empty_refresh_state() -> dict[str, Any]:
    """Fresh refresh lane state (repeated-head-window model).

    The window fields are additive extensions: notebooks written before the
    execution contract freeze lack them and remain readable (validators use
    ``_require_keys`` for refresh state); the migration tool backfills them.
    """
    return {
        "last_started_at": None,
        "last_success_at": None,
        "last_status": None,
        "pages_scanned_last_run": 0,
        "items_returned_last_run": 0,
        "last_error": None,
        # Repeated-head-window extension (discovery execution contract):
        "last_window_completed_at": None,
        "last_window_pages": 0,
        "last_window_signature": "",
        "last_window_page_ids": [],
        "consecutive_failures": 0,
        "next_retry_at": None,
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
    """Build a fresh v4 search query entry with language detection."""
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
    """Build a fresh v4 notebook dict for a new Chinese keyword concept.

    The notebook starts as a disabled draft with an unconfigured
    ``relevance_profile`` (``None``).  Enabling discovery requires binding a
    taxonomy-resolved profile through ``configure_relevance_profiles.py``;
    readiness fails closed until then.
    """
    if not isinstance(keyword_zh, str) or not _HAS_CJK.search(keyword_zh):
        raise ValueError("keyword_zh must contain Chinese text")
    normalized = normalize_keyword(keyword_zh)
    return {
        "schema_version": SCHEMA_VERSION,
        "keyword_id": keyword_id(keyword_zh),
        "keyword_zh": keyword_zh.strip(),
        "normalized_keyword_zh": normalized,
        "enabled": False,
        "relevance_generation": 1,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "classification": {
            "guidance_zh": None,
            "aliases_zh": [],
            "exclusions_zh": [],
        },
        "relevance_profile": None,
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


# ── Schema validation (v4 only) ──────────────────────────────────────


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


_REFRESH_WINDOW_ALLOWED = {
    "last_window_completed_at", "last_window_pages", "last_window_signature",
    "last_window_page_ids", "consecutive_failures", "next_retry_at",
}


def _validate_refresh_state(value: Any, path: str) -> None:
    state = _require_dict(value, path)
    _require_keys(state, _REFRESH_REQUIRED, path)
    unknown = sorted(set(state) - _REFRESH_REQUIRED - _REFRESH_WINDOW_ALLOWED)
    if unknown:
        raise NotebookCorruptError(f"{path} has unexpected keys: {unknown}")
    for key in ("last_started_at", "last_success_at", "last_error"):
        _require_optional_text(state[key], f"{path}.{key}")
    if state["last_status"] not in {None, "success", "partial_success", "failed"}:
        raise NotebookCorruptError(f"{path}.last_status is invalid")
    for key in ("pages_scanned_last_run", "items_returned_last_run"):
        _require_nonnegative_int(state[key], f"{path}.{key}")
    # Window extension fields: optional (absent in pre-contract notebooks),
    # but well-formed when present.
    if "last_window_completed_at" in state:
        _require_optional_text(
            state["last_window_completed_at"], f"{path}.last_window_completed_at"
        )
    if "last_window_pages" in state:
        _require_nonnegative_int(state["last_window_pages"], f"{path}.last_window_pages")
    if "last_window_signature" in state:
        sig = state["last_window_signature"]
        if not isinstance(sig, str) or (sig and not _HEX16.fullmatch(sig)):
            raise NotebookCorruptError(
                f"{path}.last_window_signature must be empty or 16 lowercase hex"
            )
    if "last_window_page_ids" in state:
        ids = state["last_window_page_ids"]
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
            raise NotebookCorruptError(
                f"{path}.last_window_page_ids must be a list of strings"
            )
    if "consecutive_failures" in state:
        _require_nonnegative_int(
            state["consecutive_failures"], f"{path}.consecutive_failures"
        )
    if "next_retry_at" in state:
        _require_optional_text(state["next_retry_at"], f"{path}.next_retry_at")


def _validate_generation_history(value: Any, path: str) -> None:
    """Validate generation history entries using strict typed parsing.

    Each entry is validated through ``GenerationHistoryEntry.from_dict_strict``,
    which checks all 10 fields for correct types, missing keys, and unknown extras.
    Additional invariants (strictly increasing generations, hex signatures,
    non-blank closed_at/reason) are checked after the typed parse.
    """

    if not isinstance(value, list):
        raise NotebookCorruptError(f"{path} must be a list")
    previous = -1
    for index, item in enumerate(value):
        entry_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            raise NotebookCorruptError(f"{entry_path} must be a dict")
        try:
            entry = GenerationHistoryEntry.from_dict_strict(item)
        except (TypeError, ValueError, KeyError) as exc:
            raise NotebookCorruptError(f"{entry_path} invalid: {exc}") from exc
        if entry.generation < 1 or entry.generation <= previous:
            raise NotebookCorruptError(f"{path} generations must be strictly increasing")
        previous = entry.generation
        signature = item["request_signature"]
        if not isinstance(signature, str) or (signature and not _HEX16.fullmatch(signature)):
            raise NotebookCorruptError(
                f"{entry_path}.request_signature must be empty or 16 lowercase hex"
            )
        for key in ("closed_at", "reason"):
            if not isinstance(item[key], str) or not item[key].strip():
                raise NotebookCorruptError(f"{entry_path}.{key} must be a non-blank string")


def _validate_exhaustion_evidence(value: Any, path: str) -> None:
    """Validate the mandatory exhaustion evidence payload.

    Required whenever ``exhausted=True`` is committed.  ``None`` is only
    acceptable while ``exhausted`` is False.
    """
    evidence = _require_dict(value, path)
    try:
        ExhaustionEvidence.from_dict_strict(evidence)
    except (TypeError, ValueError) as exc:
        raise NotebookCorruptError(f"{path} is invalid: {exc}") from exc


def _validate_backfill_state(value: Any, path: str) -> None:
    state = _require_dict(value, path)
    missing = sorted(BACKFILL_STATE_FIELDS - set(state))
    if missing:
        raise NotebookCorruptError(f"{path} missing keys: {missing}")
    extra = sorted(set(state) - BACKFILL_STATE_ACCEPTED_FIELDS)
    if extra:
        raise NotebookCorruptError(f"{path} has unexpected keys: {extra}")
    # Optional extension fields are not required to be present, but when
    # present they must be well-formed.  ``exhausted=True`` REQUIRES
    # exhaustion_evidence (invariant: exhaustion must carry evidence).
    if "exhaustion_evidence" in state:
        evidence = state["exhaustion_evidence"]
        if evidence is not None:
            _validate_exhaustion_evidence(
                evidence, f"{path}.exhaustion_evidence"
            )
    # NOTE: ``exhausted=True`` without ``exhaustion_evidence`` is flagged as
    # ``repair_required`` by the migration tool (legacy state must never be
    # silently reopened), but the notebook *reader* stays tolerant: existing
    # notebooks remain loadable, and the write site (commit_backfill_cursor)
    # is where evidence becomes mandatory.
    if "repair_required" in state and not isinstance(state["repair_required"], bool):
        raise NotebookCorruptError(f"{path}.repair_required must be boolean")
    for opt_key in ("repair_reason", "repair_flagged_at"):
        if opt_key in state:
            _require_optional_text(state[opt_key], f"{path}.{opt_key}")
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
    """Strictly validate and return one active schema-v4 notebook.

    No migration or default injection occurs here.  Partial v4-shaped and
    legacy notebooks therefore fail closed at the active runtime boundary.
    """
    if not isinstance(data, dict):
        raise NotebookCorruptError(
            f"notebook root is {type(data).__name__}, expected dict"
        )
    version = str(data.get("schema_version") or "")
    if version in ("1.0", "2.0", "3.0"):
        raise UnsupportedNotebookSchemaError(
            f"notebook schema {version} must be migrated to v4"
        )
    if version != "4.0":
        raise UnsupportedNotebookSchemaError(
            f"unsupported notebook schema_version: {version!r}"
        )
    _require_keys(data, _TOP_LEVEL_REQUIRED, "notebook")
    extra = sorted(set(data) - _TOP_LEVEL_ALLOWED)
    if extra:
        raise NotebookCorruptError(f"notebook has unexpected keys: {extra}")
    if "relevance_profile" in data and data["relevance_profile"] is not None:
        from src.discovery.relevance import (
            validate_relevance_profile,
            validate_relevance_profile_source,
        )
        profile = data["relevance_profile"]
        openalex = profile.get("openalex") if isinstance(profile, dict) else None
        if (
            isinstance(openalex, dict)
            and openalex.get("filter_ids") == ["S0"]
            and openalex.get("filter_labels") == ["__legacy_unbound__"]
        ):
            raise NotebookCorruptError(
                "notebook.relevance_profile is the retired profile_unbound "
                "sentinel; bind a taxonomy-resolved profile through "
                "configure_relevance_profiles.py"
            )
        try:
            # Enabled notebooks must pass the full active-profile validator.
            # Disabled/draft notebooks may carry a source-only profile.
            if data.get("enabled", True):
                data["relevance_profile"] = validate_relevance_profile(profile)
            else:
                data["relevance_profile"] = validate_relevance_profile_source(profile)
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

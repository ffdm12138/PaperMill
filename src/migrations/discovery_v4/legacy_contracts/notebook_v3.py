"""Legacy schema-3.0 keyword notebook contract used only by the v4 migration.

The production validator in ``src/discovery/contracts/notebook.py`` rejects
schema 1.0/2.0/3.0 notebooks by design, so the migration cannot use it on
legacy input.  ``LegacyNotebookV3.from_dict_strict`` is the fail-closed
legacy-input gate: it accepts exactly the on-disk v3 field set (strict, no
unknown fields) and verifies canonical identities.  It never calls the
production ``validate_notebook`` on v3 input.

``convert_notebook_v3_to_v4`` performs the one-way v3 -> v4 conversion
(KEEP config, RESET provider progress, ADD a migration history entry) and
then validates the *product* with the production ``validate_notebook`` and
``validate_discovery_readiness``; any product failure raises
``LegacyNotebookContractError``.

These types must never be imported by production discovery runtime code.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from src.discovery.constants import BACKFILL_STATE_FIELDS, INITIAL_CURSOR
from src.discovery.contracts.notebook import (
    NotebookCorruptError,
    UnsupportedNotebookSchemaError,
    detect_query_language,
    keyword_id as compute_keyword_id,
    normalize_keyword,
    query_identity,
    validate_discovery_readiness,
    validate_notebook,
)

LEGACY_NOTEBOOK_SCHEMA_V3 = "3.0"
V4_NOTEBOOK_SCHEMA = "4.0"

_PROVIDERS = ("openalex", "crossref")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_HAS_CJK = re.compile(r"[一-鿿㐀-䶿豈-﫿]")

_V3_TOP_LEVEL_REQUIRED = frozenset({
    "schema_version", "keyword_id", "keyword_zh", "normalized_keyword_zh",
    "enabled", "created_at", "updated_at", "classification", "search_queries",
    "definition_history", "lifetime_statistics", "pending", "backpressure",
    "reset_history", "migration_history",
})
_V3_TOP_LEVEL_ALLOWED = _V3_TOP_LEVEL_REQUIRED | {
    "relevance_profile", "relevance_generation",
}
_V3_QUERY_FIELDS = frozenset({
    "query_id", "query", "normalized_query", "language", "active", "source",
    "created_at", "updated_at", "providers",
})
_V3_REFRESH_FIELDS = frozenset({
    "last_started_at", "last_success_at", "last_status",
    "pages_scanned_last_run", "items_returned_last_run", "last_error",
})
_V3_GENERATION_HISTORY_FIELDS = frozenset({
    "generation", "request_signature", "closed_at", "reason", "cursor",
    "exhausted", "pages_succeeded", "pages_committed",
    "items_returned_total", "last_committed_page_id",
})
_V3_LIFETIME_STAT_KEYS = frozenset({
    "keyword_runs", "refresh_lane_runs", "backfill_lane_runs",
    "provider_page_attempts", "provider_page_successes",
    "provider_page_failures", "provider_items_returned", "doi_observations",
    "candidates_staged", "candidates_existing",
})


class LegacyNotebookContractError(ValueError):
    """Raised when a legacy v3 notebook violates the strict v3 contract,
    or when its converted v4 product fails closed validation."""


def _fail(path: str, message: str) -> None:
    raise LegacyNotebookContractError(f"{path}: {message}")


def _require_dict(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(path, f"must be an object, got {type(value).__name__}")
    return value


def _require_exact_keys(value: dict[str, Any], expected: frozenset, path: str) -> None:
    missing = sorted(expected - set(value))
    if missing:
        _fail(path, f"missing keys: {missing}")
    extra = sorted(set(value) - expected)
    if extra:
        _fail(path, f"unknown keys: {extra}")


def _require_nonblank_str(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(path, "must be a non-blank string")
    return value


def _require_optional_text(value: Any, path: str) -> None:
    if value is not None and not isinstance(value, str):
        _fail(path, "must be a string or null")


def _require_nonnegative_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(path, "must be a non-negative integer")
    return value


def _require_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        _fail(path, "must be boolean")
    return value


def _validate_string_list(value: Any, path: str) -> None:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        _fail(path, "must be a list of non-blank strings")


def _validate_history_list(value: Any, path: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        _fail(path, "must be a list of objects")


def _validate_refresh_state(value: Any, path: str) -> None:
    state = _require_dict(value, path)
    _require_exact_keys(state, _V3_REFRESH_FIELDS, path)
    for key in ("last_started_at", "last_success_at", "last_error"):
        _require_optional_text(state[key], f"{path}.{key}")
    if state["last_status"] not in {None, "success", "partial_success", "failed"}:
        _fail(f"{path}.last_status", "is invalid")
    for key in ("pages_scanned_last_run", "items_returned_last_run"):
        _require_nonnegative_int(state[key], f"{path}.{key}")


def _validate_generation_history(value: Any, path: str) -> None:
    if not isinstance(value, list):
        _fail(path, "must be a list")
    previous = -1
    for index, item in enumerate(value):
        entry_path = f"{path}[{index}]"
        entry = _require_dict(item, entry_path)
        _require_exact_keys(entry, _V3_GENERATION_HISTORY_FIELDS, entry_path)
        generation = _require_nonnegative_int(
            entry["generation"], f"{entry_path}.generation"
        )
        if generation < 1 or generation <= previous:
            _fail(path, "generations must be strictly increasing from 1")
        previous = generation
        signature = entry["request_signature"]
        if not isinstance(signature, str) or (signature and not _HEX16.fullmatch(signature)):
            _fail(f"{entry_path}.request_signature", "must be empty or 16 lowercase hex")
        for key in ("closed_at", "reason"):
            _require_nonblank_str(entry[key], f"{entry_path}.{key}")
        _require_nonblank_str(entry["cursor"], f"{entry_path}.cursor")
        _require_bool(entry["exhausted"], f"{entry_path}.exhausted")
        for key in ("pages_succeeded", "pages_committed", "items_returned_total"):
            _require_nonnegative_int(entry[key], f"{entry_path}.{key}")
        if not isinstance(entry["last_committed_page_id"], str):
            _fail(f"{entry_path}.last_committed_page_id", "must be a string")


def _validate_backfill_state(value: Any, path: str) -> None:
    state = _require_dict(value, path)
    _require_exact_keys(state, BACKFILL_STATE_FIELDS, path)
    _require_nonblank_str(state["cursor"], f"{path}.cursor")
    _require_bool(state["exhausted"], f"{path}.exhausted")
    _require_bool(state["terminal_failure"], f"{path}.terminal_failure")
    for key in (
        "pages_succeeded", "pages_committed", "items_returned_total",
        "last_page_count", "cursor_conflicts", "consecutive_failures",
        "generation",
    ):
        _require_nonnegative_int(state[key], f"{path}.{key}")
    if state["generation"] < 1:
        _fail(f"{path}.generation", "must be at least 1")
    if not isinstance(state["last_committed_page_id"], str):
        _fail(f"{path}.last_committed_page_id", "must be a string")
    for key in (
        "last_success_at", "last_error", "last_failure_at", "last_error_type",
        "next_retry_at", "terminal_failure_at",
    ):
        _require_optional_text(state[key], f"{path}.{key}")
    signature = state["request_signature"]
    if not isinstance(signature, str) or (signature and not _HEX16.fullmatch(signature)):
        _fail(f"{path}.request_signature", "must be empty or 16 lowercase hex")
    _validate_generation_history(
        state["generation_history"], f"{path}.generation_history"
    )


def _validate_query_entry(map_key: str, value: Any, path: str) -> None:
    entry = _require_dict(value, path)
    _require_exact_keys(entry, _V3_QUERY_FIELDS, path)
    query = _require_nonblank_str(entry["query"], f"{path}.query")
    language = entry["language"]
    if language not in {"zh", "en", "mixed"}:
        _fail(f"{path}.language", "must be 'zh', 'en', or 'mixed'")
    detected = detect_query_language(query)
    if detected != language:
        _fail(
            f"{path}.language",
            f"declared {language!r} does not match detected {detected!r}",
        )
    normalized = normalize_keyword(query)
    if entry["normalized_query"] != normalized:
        _fail(f"{path}.normalized_query", "is not canonical")
    expected_id = query_identity(language, normalized)
    if map_key != expected_id or entry["query_id"] != expected_id:
        _fail(f"{path}.query_id", "does not match canonical identity")
    _require_bool(entry["active"], f"{path}.active")
    for key in ("source", "created_at", "updated_at"):
        _require_nonblank_str(entry[key], f"{path}.{key}")
    providers = _require_dict(entry["providers"], f"{path}.providers")
    if set(providers) != set(_PROVIDERS):
        _fail(f"{path}.providers", f"must contain exactly {list(_PROVIDERS)}")
    for provider in _PROVIDERS:
        provider_path = f"{path}.providers.{provider}"
        state = _require_dict(providers[provider], provider_path)
        if set(state) != {"refresh", "backfill"}:
            _fail(provider_path, "must contain exactly refresh/backfill")
        _validate_refresh_state(state["refresh"], f"{provider_path}.refresh")
        _validate_backfill_state(state["backfill"], f"{provider_path}.backfill")


def _validate_relevance_profile_structure(value: Any, path: str) -> None:
    """Structurally validate the bound profile (hash included).

    Active-readiness (taxonomy-resolved, non-sentinel) is enforced later on
    the converted v4 product by ``validate_discovery_readiness``.
    """
    from src.discovery.relevance import (
        RelevanceProfileError,
        validate_relevance_profile_source,
    )

    profile = _require_dict(value, path)
    try:
        validate_relevance_profile_source(profile)
    except RelevanceProfileError as exc:
        _fail(path, f"is invalid: {exc}")


@dataclass(frozen=True)
class LegacyNotebookV3:
    """Strictly validated schema-3.0 keyword notebook (migration input)."""

    keyword_id: str
    keyword_zh: str
    normalized_keyword_zh: str
    enabled: bool
    created_at: str
    updated_at: str
    classification: Mapping[str, Any] = field(default_factory=dict)
    search_queries: Mapping[str, Any] = field(default_factory=dict)
    relevance_profile: Mapping[str, Any] | None = None
    relevance_generation: int = 1
    definition_history: list[Any] = field(default_factory=list)
    lifetime_statistics: Mapping[str, Any] = field(default_factory=dict)
    pending: Mapping[str, Any] = field(default_factory=dict)
    backpressure: Mapping[str, Any] = field(default_factory=dict)
    reset_history: list[Any] = field(default_factory=list)
    migration_history: list[Any] = field(default_factory=list)
    schema_version: str = LEGACY_NOTEBOOK_SCHEMA_V3

    @classmethod
    def from_dict_strict(cls, data: Any) -> "LegacyNotebookV3":
        """Parse and strictly validate one schema-3.0 notebook dict.

        Raises:
            LegacyNotebookContractError: on any schema, field-set, type, or
                canonical-identity violation.  This is the only failure type
                for legacy input; the production ``validate_notebook`` is
                never called here.
        """
        if not isinstance(data, dict):
            raise LegacyNotebookContractError(
                f"notebook root is {type(data).__name__}, expected dict"
            )
        version = str(data.get("schema_version") or "")
        if version != LEGACY_NOTEBOOK_SCHEMA_V3:
            raise LegacyNotebookContractError(
                f"legacy notebook schema_version must be "
                f"{LEGACY_NOTEBOOK_SCHEMA_V3!r}, got {version!r}"
            )
        extra = sorted(set(data) - _V3_TOP_LEVEL_ALLOWED)
        if extra:
            _fail("notebook", f"unknown keys: {extra}")
        missing = sorted(_V3_TOP_LEVEL_REQUIRED - set(data))
        if missing:
            _fail("notebook", f"missing keys: {missing}")

        keyword_zh = data["keyword_zh"]
        if (
            not isinstance(keyword_zh, str)
            or not keyword_zh.strip()
            or not _HAS_CJK.search(keyword_zh)
        ):
            _fail("notebook.keyword_zh", "must contain Chinese text")
        normalized_keyword_zh = normalize_keyword(keyword_zh)
        if data["normalized_keyword_zh"] != normalized_keyword_zh:
            _fail("notebook.normalized_keyword_zh", "is not canonical")
        expected_keyword_id = compute_keyword_id(keyword_zh)
        if (
            data["keyword_id"] != expected_keyword_id
            or not _HEX16.fullmatch(str(data["keyword_id"]))
        ):
            _fail("notebook.keyword_id", "does not match keyword_zh")
        enabled = _require_bool(data["enabled"], "notebook.enabled")
        for key in ("created_at", "updated_at"):
            _require_nonblank_str(data[key], f"notebook.{key}")

        classification = _require_dict(data["classification"], "notebook.classification")
        _require_exact_keys(
            classification,
            frozenset({"guidance_zh", "aliases_zh", "exclusions_zh"}),
            "notebook.classification",
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

        search_queries = _require_dict(data["search_queries"], "notebook.search_queries")
        for map_key, entry in search_queries.items():
            if not isinstance(map_key, str) or not _HEX16.fullmatch(map_key):
                _fail(
                    "notebook.search_queries",
                    "map keys must be 16 lowercase hex",
                )
            _validate_query_entry(
                map_key, entry, f"notebook.search_queries.{map_key}"
            )

        relevance_profile = data.get("relevance_profile")
        if relevance_profile is not None:
            _validate_relevance_profile_structure(
                relevance_profile, "notebook.relevance_profile"
            )
        relevance_generation = data.get("relevance_generation", 1)
        if (
            isinstance(relevance_generation, bool)
            or not isinstance(relevance_generation, int)
            or relevance_generation < 1
        ):
            _fail("notebook.relevance_generation", "must be an integer >= 1")

        for history_name in ("definition_history", "reset_history", "migration_history"):
            _validate_history_list(data[history_name], f"notebook.{history_name}")

        statistics = _require_dict(
            data["lifetime_statistics"], "notebook.lifetime_statistics"
        )
        _require_exact_keys(
            statistics, _V3_LIFETIME_STAT_KEYS, "notebook.lifetime_statistics"
        )
        for key in _V3_LIFETIME_STAT_KEYS:
            _require_nonnegative_int(
                statistics[key], f"notebook.lifetime_statistics.{key}"
            )

        pending = _require_dict(data["pending"], "notebook.pending")
        _require_exact_keys(
            pending,
            frozenset({"pages", "candidates", "last_drained_at"}),
            "notebook.pending",
        )
        _require_nonnegative_int(pending["pages"], "notebook.pending.pages")
        _require_nonnegative_int(pending["candidates"], "notebook.pending.candidates")
        _require_optional_text(
            pending["last_drained_at"], "notebook.pending.last_drained_at"
        )

        backpressure = _require_dict(data["backpressure"], "notebook.backpressure")
        _require_exact_keys(
            backpressure,
            frozenset({
                "active", "entered_at", "last_pending_count",
                "max_threshold", "resume_threshold",
            }),
            "notebook.backpressure",
        )
        _require_bool(backpressure["active"], "notebook.backpressure.active")
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
            _fail("notebook.backpressure", "thresholds are invalid")

        return cls(
            keyword_id=str(data["keyword_id"]),
            keyword_zh=keyword_zh,
            normalized_keyword_zh=normalized_keyword_zh,
            enabled=enabled,
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            classification=copy.deepcopy(classification),
            search_queries=copy.deepcopy(search_queries),
            relevance_profile=(
                copy.deepcopy(relevance_profile)
                if relevance_profile is not None
                else None
            ),
            relevance_generation=relevance_generation,
            definition_history=copy.deepcopy(data["definition_history"]),
            lifetime_statistics=copy.deepcopy(statistics),
            pending=copy.deepcopy(pending),
            backpressure=copy.deepcopy(backpressure),
            reset_history=copy.deepcopy(data["reset_history"]),
            migration_history=copy.deepcopy(data["migration_history"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "keyword_id": self.keyword_id,
            "keyword_zh": self.keyword_zh,
            "normalized_keyword_zh": self.normalized_keyword_zh,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "classification": copy.deepcopy(dict(self.classification)),
            "search_queries": copy.deepcopy(dict(self.search_queries)),
            "definition_history": copy.deepcopy(list(self.definition_history)),
            "lifetime_statistics": copy.deepcopy(dict(self.lifetime_statistics)),
            "pending": copy.deepcopy(dict(self.pending)),
            "backpressure": copy.deepcopy(dict(self.backpressure)),
            "reset_history": copy.deepcopy(list(self.reset_history)),
            "migration_history": copy.deepcopy(list(self.migration_history)),
            "relevance_profile": (
                copy.deepcopy(dict(self.relevance_profile))
                if self.relevance_profile is not None
                else None
            ),
            "relevance_generation": self.relevance_generation,
        }


def _fresh_provider_refresh_state() -> dict[str, Any]:
    """Return a pristine refresh state for one provider lane."""
    return {
        "last_started_at": None,
        "last_success_at": None,
        "last_status": None,
        "pages_scanned_last_run": 0,
        "items_returned_last_run": 0,
        "last_error": None,
    }


def _fresh_provider_backfill_state(generation: int = 1) -> dict[str, Any]:
    """Return a pristine backfill state for one provider lane."""
    return {
        "generation": generation,
        "request_signature": "",
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
        "consecutive_failures": 0,
        "last_failure_at": None,
        "last_error_type": None,
        "next_retry_at": None,
        "terminal_failure": False,
        "terminal_failure_at": None,
        "generation_history": [],
    }


def convert_notebook_v3_to_v4(legacy: LegacyNotebookV3) -> dict[str, Any]:
    """Convert one validated legacy v3 notebook into a clean v4 notebook.

    Migration rules:
    - KEEP: keyword_zh, enabled, search query config (text + language +
      source), relevance_profile, classification, definition_history,
      created_at
    - RESET: cursor=*, exhausted=false, generation=1, all counter fields=0,
      generation_history=[], last_* fields=null, relevance_generation=1,
      lifetime_statistics/pending/backpressure to pristine defaults
    - ADD: migration_history entry recording this migration

    The converted product is then validated with the production
    ``validate_notebook`` and ``validate_discovery_readiness``; any product
    failure raises ``LegacyNotebookContractError`` (fail closed).
    """
    if not isinstance(legacy, LegacyNotebookV3):
        raise LegacyNotebookContractError(
            f"convert_notebook_v3_to_v4 expects LegacyNotebookV3, "
            f"got {type(legacy).__name__}"
        )

    now = datetime.now(timezone.utc).isoformat()

    # Rebuild search_queries: keep query identity and config, reset providers.
    clean_queries: dict[str, dict[str, Any]] = {}
    for qid, entry in sorted(legacy.search_queries.items()):
        clean_queries[qid] = {
            "query_id": entry["query_id"],
            "query": entry["query"],
            "normalized_query": entry["normalized_query"],
            "language": entry["language"],
            "active": entry["active"],
            "source": entry.get("source", "curated"),
            "created_at": entry.get("created_at", now),
            "updated_at": now,
            "providers": {
                "openalex": {
                    "refresh": _fresh_provider_refresh_state(),
                    "backfill": _fresh_provider_backfill_state(generation=1),
                },
                "crossref": {
                    "refresh": _fresh_provider_refresh_state(),
                    "backfill": _fresh_provider_backfill_state(generation=1),
                },
            },
        }

    v4 = {
        "schema_version": V4_NOTEBOOK_SCHEMA,
        "keyword_id": legacy.keyword_id,
        "keyword_zh": legacy.keyword_zh,
        "normalized_keyword_zh": legacy.normalized_keyword_zh,
        "enabled": legacy.enabled,
        "classification": copy.deepcopy(dict(legacy.classification)),
        "search_queries": clean_queries,
        "relevance_profile": (
            copy.deepcopy(dict(legacy.relevance_profile))
            if legacy.relevance_profile is not None
            else None
        ),
        "relevance_generation": 1,
        "definition_history": copy.deepcopy(list(legacy.definition_history)),
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
        "pending": {
            "pages": 0,
            "candidates": 0,
            "last_drained_at": None,
        },
        "backpressure": {
            "active": False,
            "entered_at": None,
            "last_pending_count": 0,
            "max_threshold": 1000,
            "resume_threshold": 700,
        },
        "reset_history": [],
        "migration_history": copy.deepcopy(list(legacy.migration_history))
        + [{
            "from_schema": legacy.schema_version,
            "to_schema": V4_NOTEBOOK_SCHEMA,
            "migrated_at": now,
            "reason": "discovery_v4_one_time_migration",
        }],
        "created_at": legacy.created_at,
        "updated_at": now,
    }

    try:
        validate_notebook(v4)
    except (NotebookCorruptError, UnsupportedNotebookSchemaError) as exc:
        raise LegacyNotebookContractError(
            f"converted v4 notebook for {legacy.keyword_zh!r} failed "
            f"production validation: {exc}"
        ) from exc
    readiness = validate_discovery_readiness(v4)
    if not readiness.ready:
        raise LegacyNotebookContractError(
            f"converted v4 notebook for {legacy.keyword_zh!r} is not "
            f"discovery-ready: {'; '.join(readiness.errors)}"
        )
    return v4

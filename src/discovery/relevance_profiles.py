"""Plan-bound relevance-profile taxonomy resolution and application.

The module deliberately keeps profile semantics in :mod:`relevance` and owns
only the durable configuration transaction: taxonomy snapshots, plan hashes,
page closure mutations, and the final notebook profile/generation commit.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
import unicodedata
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from filelock import FileLock

from src.discovery.contracts.notebook import resolve_existing_notebook, validate_notebook
from src.discovery.stores.notebook_store import NotebookStoreV4 as KeywordNotebookStore
from src.discovery.contracts.page_journal import (
    CandidateLifecycleClass,
    classify_candidate_lifecycle,
    is_profile_closeable_candidate,
    transform_page_for_profile_closure,
)
from src.discovery.stores.page_journal_store import PageJournalStoreV4 as PageJournalStore
from src.discovery.relevance import (
    MATCHER_SCHEMA_VERSION,
    RelevanceReason,
    validate_relevance_profile,
    validate_relevance_profile_source,
)
from src.utils.atomic_io import atomic_replace_bytes, atomic_write_json
from src.utils.timestamps import utc_now_iso as _now


OPENALEX_SUBFIELDS_URL = "https://api.openalex.org/subfields"
TRANSACTION_SCHEMA_VERSION = "2.0"


class RelevanceProfileTransactionError(RuntimeError):
    """Raised when a profile plan/apply operation must fail closed."""


class RelevanceProfilePlanError(RelevanceProfileTransactionError):
    """Plan failure carrying a safe, explicitly non-applicable report."""

    def __init__(self, message: str, report: Mapping[str, Any]):
        super().__init__(message)
        self.report = dict(report)



def _sha_bytes(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_hash(path: Path) -> str:
    return _sha_bytes(path) if path.exists() else ""


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def _profile_change_relevance_state(candidate: Mapping[str, Any]) -> bool:
    relevance = candidate.get("relevance")
    state = (
        str(relevance.get("state") or "profile_unbound")
        if isinstance(relevance, Mapping) else "profile_unbound"
    )
    return state in {"profile_unbound", "passed", "verification_deferred"}


def is_profile_closeable_candidate_state(
    candidate: Mapping[str, Any], target_profile_hash: str,
) -> bool:
    """Return whether relevance is stale, independent of lifecycle safety."""
    relevance = candidate.get("relevance")
    old_hash = (
        str(relevance.get("profile_hash") or "")
        if isinstance(relevance, Mapping) else ""
    )
    return _profile_change_relevance_state(candidate) and old_hash != target_profile_hash


# Re-exported from the neutral relevance_runtime module so that every
# consumer (coordinator, migration, etc.) can import without pulling in
# the full profile transaction machinery.
from src.discovery.relevance_runtime import RelevanceRuntimePaths  # noqa: F401


@dataclass(frozen=True)
class TaxonomySnapshot:
    pages: tuple[dict[str, Any], ...]
    entities: tuple[dict[str, Any], ...]
    retrieved_at: str
    page_hashes: tuple[str, ...]
    snapshot_sha256: str
    schema_version: str = "1.0"
    raw_snapshot_sha256: str = ""
    taxonomy_semantic_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "retrieved_at": self.retrieved_at,
            "page_hashes": list(self.page_hashes),
            "snapshot_sha256": self.snapshot_sha256,
            "raw_snapshot_sha256": self.raw_snapshot_sha256,
            "taxonomy_semantic_sha256": self.taxonomy_semantic_sha256,
            "pages": list(self.pages),
            "entities": list(self.entities),
        }


def _provider_taxonomy_getter(url: str, *, params: dict[str, Any] | None = None,
                              timeout: float = 20.0, **_unused: Any) -> Any:
    """Default taxonomy HTTP getter routed through the unified ProviderClient.

    Replaces the legacy ``requests.get`` default so taxonomy fetches share the
    provider limiter, retry/backoff, circuit breaker and request budget.  The
    ``http_get`` injection seam remains for tests.
    """
    from src.discovery.providers.provider_client import ProviderRuntime, RequestSpec

    spec = RequestSpec(
        provider="openalex",
        purpose="metadata_resolution",
        url=url,
        params=params or {},
        timeout_seconds=timeout,
    )
    outcome = ProviderRuntime.get().client("openalex").execute(spec)

    class _Resp:
        def raise_for_status(self) -> None:
            return None  # ProviderClient already raised on non-2xx

        def json(self) -> Any:
            return outcome.json()

    return _Resp()


def fetch_subfields_taxonomy(
    *,
    http_get: Callable[..., Any] | None = None,
    per_page: int = 100,
    timeout: float = 20.0,
) -> TaxonomySnapshot:
    """Read every OpenAlex ``/subfields`` page and retain raw responses."""
    if per_page < 1 or per_page > 200:
        raise ValueError("per_page must be between 1 and 200")
    getter = http_get or _provider_taxonomy_getter
    cursor = "*"
    pages: list[dict[str, Any]] = []
    entities: list[dict[str, Any]] = []
    seen_cursors: set[str] = set()
    retrieved_at = _now()
    while True:
        if cursor in seen_cursors:
            raise RelevanceProfileTransactionError("OpenAlex taxonomy cursor did not advance")
        seen_cursors.add(cursor)
        response = getter(
            OPENALEX_SUBFIELDS_URL,
            params={"per-page": per_page, "cursor": cursor},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise RelevanceProfileTransactionError("OpenAlex taxonomy response is malformed")
        page_hash = _canonical_hash(payload)
        pages.append(payload)
        for entity in payload["results"]:
            if not isinstance(entity, dict):
                raise RelevanceProfileTransactionError("OpenAlex taxonomy entity is malformed")
            entities.append(entity)
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        next_cursor = meta.get("next_cursor")
        if not next_cursor or not payload["results"]:
            break
        cursor = str(next_cursor)
    page_hashes = tuple(_canonical_hash(page) for page in pages)
    snapshot_sha = _canonical_hash({"pages": pages, "entities": entities})

    # ── Dual hashes (Phase 6.2) ────────────────────────────────────────
    # raw_snapshot_sha256 — proves raw pages were not modified.
    raw_payload = b"".join(
        _json_bytes(page) for page in pages
    )
    raw_snapshot_sha256 = hashlib.sha256(raw_payload).hexdigest()

    # taxonomy_semantic_sha256 — from canonical entities rebuilt from pages.
    canonical_entities = _rebuild_canonical_entities(pages)
    taxonomy_semantic_sha256 = _canonical_hash(
        {"entities": canonical_entities}
    )

    # ── Validate entity cache consistency (Phase 6.3) ──────────────────
    raw_entity_ids: set[str] = set()
    for page in pages:
        for entity in page.get("results", []):
            if not isinstance(entity, dict):
                raise RelevanceProfileTransactionError(
                    "OpenAlex taxonomy entity is malformed"
                )
            eid = _entity_id(entity)
            if eid in raw_entity_ids:
                raise RelevanceProfileTransactionError(
                    f"duplicate taxonomy entity ID across pages: {eid}"
                )
            raw_entity_ids.add(eid)

    canonical_ids = {e["id"] for e in canonical_entities}
    if canonical_ids != raw_entity_ids:
        missing_from_canonical = sorted(raw_entity_ids - canonical_ids)
        missing_from_raw = sorted(canonical_ids - raw_entity_ids)
        raise RelevanceProfileTransactionError(
            f"taxonomy entity/page mismatch: "
            f"canonical={len(canonical_ids)}, raw={len(raw_entity_ids)}; "
            + (f"in-pages-not-in-canonical: {missing_from_canonical[:5]}; "
               if missing_from_canonical else "")
            + (f"in-canonical-not-in-pages: {missing_from_raw[:5]}"
               if missing_from_raw else "")
        )

    return TaxonomySnapshot(
        pages=tuple(pages), entities=tuple(entities), retrieved_at=retrieved_at,
        page_hashes=page_hashes, snapshot_sha256=snapshot_sha,
        raw_snapshot_sha256=raw_snapshot_sha256,
        taxonomy_semantic_sha256=taxonomy_semantic_sha256,
    )


def validate_taxonomy_snapshot(snapshot: dict[str, Any]) -> list[str]:
    """Offline validation: re-derive every hash and structural invariant.

    Returns a list of violation descriptions (empty ⇒ valid).  Call this
    before trusting a stored or user-supplied taxonomy snapshot in any
    plan, resolve, or CLI operation.

    This function must NEVER raise an exception — all failures return as
    violation strings.
    """
    violations: list[str] = []

    # ── Phase 0: Top-level type guard ─────────────────────────────────
    if not isinstance(snapshot, dict):
        violations.append("taxonomy snapshot is not a dict")
        return violations

    # ── Phase 1: Schema version gate ──────────────────────────────────
    schema = str(snapshot.get("schema_version") or "")
    if schema != "1.0":
        violations.append(
            f"unsupported taxonomy snapshot schema_version: {schema!r}")
        return violations  # cannot trust structure of unknown schema

    # ── Phase 2: Required fields ─────────────────────────────────────
    for field in ("pages", "entities", "retrieved_at", "page_hashes",
                  "snapshot_sha256", "raw_snapshot_sha256",
                  "taxonomy_semantic_sha256"):
        if field not in snapshot:
            violations.append(f"taxonomy snapshot missing field: {field}")
    if violations:
        return violations

    pages = snapshot.get("pages")
    entities = snapshot.get("entities")
    page_hashes = snapshot.get("page_hashes")
    retrieved_at = snapshot.get("retrieved_at")

    # ── Phase 3: Structural pre-checks BEFORE any hash computation ───
    # All malformed inputs are collected as violations and trigger an
    # early return so hashes are never computed on known-bad data.

    # 3a. pages must be a list (not string, not None).
    if not isinstance(pages, list):
        violations.append("taxonomy pages must be a list")
    else:
        # 3b. Each page must be a dict.
        for i, page in enumerate(pages):
            if not isinstance(page, dict):
                violations.append(f"taxonomy page[{i}] is not a dict")
                continue
            # 3c. Each page's results must be a list.
            results = page.get("results")
            if not isinstance(results, list):
                violations.append(
                    f"taxonomy page[{i}].results is not a list")
                continue
            # 3d. Each result in results must be a dict.
            # 3e. Each entity ID must be a valid subfield URI
            #     (catch errors from _entity_id).
            for j, result in enumerate(results):
                if not isinstance(result, dict):
                    violations.append(
                        f"taxonomy page[{i}].results[{j}] is not a dict")
                    continue
                try:
                    _entity_id(result)
                except RelevanceProfileTransactionError as exc:
                    violations.append(
                        f"taxonomy page[{i}].results[{j}] entity ID "
                        f"invalid: {exc}")

    # 3f. entities must be a list.
    if not isinstance(entities, list):
        violations.append("taxonomy entities must be a list")
    else:
        # 3g. Each entity must be a dict.
        # 3h. Each entity ID must be a valid subfield URI.
        for i, entity in enumerate(entities):
            if not isinstance(entity, dict):
                violations.append(
                    f"taxonomy entity[{i}] is not a dict")
                continue
            try:
                _entity_id(entity)
            except RelevanceProfileTransactionError as exc:
                violations.append(
                    f"taxonomy entity[{i}] ID invalid: {exc}")

    # 3i. page_hashes must be a list of strings.
    if not isinstance(page_hashes, list):
        violations.append("taxonomy page_hashes must be a list")
    elif isinstance(pages, list):
        # 3j. Length of page_hashes must equal length of pages.
        if len(page_hashes) != len(pages):
            violations.append(
                f"page_hashes length ({len(page_hashes)}) != "
                f"pages length ({len(pages)})")
        # 3k. Each page_hash must be 64 hex characters.
        for i, h in enumerate(page_hashes):
            if not isinstance(h, str) or not re.fullmatch(
                    r"[0-9a-f]{64}", str(h)):
                violations.append(
                    f"page_hashes[{i}] is not a 64-char hex string")

    # 3l. retrieved_at must be a valid timezone-aware ISO-8601 string.
    retrieved_at_str = str(retrieved_at or "")
    try:
        dt = datetime.fromisoformat(retrieved_at_str)
        if dt.tzinfo is None:
            violations.append("retrieved_at is not timezone-aware")
    except (ValueError, TypeError):
        violations.append(
            f"retrieved_at is not a valid ISO-8601 string: "
            f"{retrieved_at_str!r}")

    # If any structural pre-check failed, return early — do not compute
    # hashes on data known to be malformed.
    if violations:
        return violations

    # ── Phase 4: Hash computations (safe now that structure is valid) ──

    # 4a. Re-derive raw_snapshot_sha256.
    raw_payload = b"".join(_json_bytes(page) for page in pages)
    expected_raw = hashlib.sha256(raw_payload).hexdigest()
    stored_raw = str(snapshot.get("raw_snapshot_sha256") or "")
    if expected_raw != stored_raw:
        violations.append(
            f"raw_snapshot_sha256 mismatch: computed={expected_raw[:16]}..., "
            f"stored={stored_raw[:16]}...")

    # 4b. Raw pages must not contain duplicate subfield IDs.
    raw_page_ids: set[str] = set()
    for page in pages:
        for raw in page.get("results", []):
            if isinstance(raw, dict):
                try:
                    eid = _entity_id(raw)
                except RelevanceProfileTransactionError:
                    continue
                if eid in raw_page_ids:
                    violations.append(
                        f"duplicate subfield ID in raw pages: {eid}")
                raw_page_ids.add(eid)

    # 4c. Rebuild canonical entities and re-derive semantic hash.
    canonical = _rebuild_canonical_entities(pages)
    expected_semantic = _canonical_hash({"entities": canonical})
    stored_semantic = str(
        snapshot.get("taxonomy_semantic_sha256") or "")
    if expected_semantic != stored_semantic:
        violations.append(
            f"taxonomy_semantic_sha256 mismatch: "
            f"computed={expected_semantic[:16]}..., "
            f"stored={stored_semantic[:16]}...")

    # 4d. Snapshot hash.
    expected_snapshot = _canonical_hash(
        {"pages": pages, "entities": entities})
    stored_snapshot = str(snapshot.get("snapshot_sha256") or "")
    if expected_snapshot != stored_snapshot:
        violations.append("snapshot_sha256 mismatch")

    # 4e. Page hashes.
    stored_page_hashes = snapshot.get("page_hashes")
    if isinstance(stored_page_hashes, list):
        expected_page_hashes = [
            _canonical_hash(page) for page in pages]
        if expected_page_hashes != stored_page_hashes:
            violations.append("page_hashes mismatch")

    # 4f. Validate entity IDs are subfield URIs (deep entity validation).
    seen_eids: set[str] = set()
    for entity in entities:
        if not isinstance(entity, dict):
            violations.append("taxonomy entity is not a dict")
            continue
        eid = str(entity.get("id") or "")
        if not eid.startswith("https://openalex.org/subfields/"):
            violations.append(
                f"taxonomy entity ID is not a subfield URI: {eid!r}")
            continue
        short = eid.rsplit("/", 1)[-1]
        if not re.fullmatch(r"[A-Za-z0-9]+", short):
            violations.append(
                f"taxonomy subfield ID is invalid: {short!r}")
        try:
            short_id = _entity_id(entity)
        except RelevanceProfileTransactionError as exc:
            violations.append(
                f"taxonomy entity {eid} ID invalid for dedup: {exc}")
            continue
        if short_id in seen_eids:
            violations.append(
                f"duplicate taxonomy entity ID: {short_id}")
        seen_eids.add(short_id)
        display = str(entity.get("display_name") or "")
        if not display.strip():
            violations.append(
                f"taxonomy entity {eid} has empty display_name")
        for parent_key in ("field", "domain"):
            parent = entity.get(parent_key)
            if not isinstance(parent, dict) or not parent.get("id"):
                violations.append(
                    f"taxonomy entity {eid} missing {parent_key}.id")

    # 4g. Cross-check entities vs pages — full canonical content comparison.
    #     Not just IDs: display_name, field, and domain must also match,
    #     because profile label resolution uses snapshot.entities, not pages.
    canonical_from_pages = _rebuild_canonical_entities(pages)
    canonical_from_snapshot = _rebuild_canonical_entities(
        [{"results": entities}])
    if canonical_from_pages != canonical_from_snapshot:
        violations.append(
            "canonical entities rebuilt from pages do not match "
            "snapshot.entities cache — labels may be inconsistent")

    return violations


def _rebuild_canonical_entities(
    pages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rebuild canonical entities from raw taxonomy pages.

    Each entity preserves only ``id``, ``display_name``, ``field``, and
    ``domain`` — the minimal stable identity.  Results are sorted by ID.
    """
    seen: set[str] = set()
    entities: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        for raw in page.get("results", []):
            if not isinstance(raw, dict):
                continue
            eid = _entity_id(raw)
            if eid in seen:
                continue
            seen.add(eid)
            field = raw.get("field") if isinstance(raw.get("field"), dict) else {}
            domain = raw.get("domain") if isinstance(raw.get("domain"), dict) else {}
            entities.append({
                "id": eid,
                "display_name": str(raw.get("display_name") or ""),
                "field": {
                    "id": str(field.get("id") or ""),
                    "display_name": str(field.get("display_name") or ""),
                },
                "domain": {
                    "id": str(domain.get("id") or ""),
                    "display_name": str(domain.get("display_name") or ""),
                },
            })
    entities.sort(key=lambda e: e["id"])
    return entities


def _entity_id(entity: Mapping[str, Any]) -> str:
    value = str(entity.get("id") or "").strip()
    if not value.startswith("https://openalex.org/subfields/"):
        raise RelevanceProfileTransactionError("taxonomy entity is not an OpenAlex subfield URI")
    value = value.rsplit("/", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9]+", value):
        raise RelevanceProfileTransactionError("taxonomy subfield ID is invalid")
    return value


def resolve_subfield_labels(
    snapshot: TaxonomySnapshot,
    labels: Iterable[str],
) -> list[dict[str, Any]]:
    """Resolve each display label with NFKC exact, case-sensitive matching."""
    resolved: list[dict[str, Any]] = []
    for raw_label in labels:
        label = unicodedata.normalize("NFKC", str(raw_label)).strip()
        matches = [
            entity for entity in snapshot.entities
            if unicodedata.normalize("NFKC", str(entity.get("display_name") or "")).strip() == label
        ]
        if len(matches) != 1:
            raise RelevanceProfileTransactionError(
                f"taxonomy label {raw_label!r} resolved to {len(matches)} entities"
            )
        entity = matches[0]
        subfield_id = _entity_id(entity)
        field = entity.get("field") if isinstance(entity.get("field"), dict) else {}
        domain = entity.get("domain") if isinstance(entity.get("domain"), dict) else {}
        field_id = str(field.get("id") or "")
        domain_id = str(domain.get("id") or "")
        if (
            not re.fullmatch(r"https://openalex\.org/fields/[A-Za-z0-9]+", field_id)
            or not re.fullmatch(r"https://openalex\.org/domains/[A-Za-z0-9]+", domain_id)
        ):
            raise RelevanceProfileTransactionError(
                f"taxonomy entity {label!r} lacks parent Field/Domain"
            )
        resolved.append({
            "id": subfield_id,
            "uri": str(entity.get("id") or ""),
            "label": str(entity.get("display_name") or ""),
            "field": {"id": field_id, "label": str(field.get("display_name") or "")},
            "domain": {"id": domain_id, "label": str(domain.get("display_name") or "")},
        })
    return resolved


def _load_profile_definitions(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RelevanceProfileTransactionError(f"cannot read profiles: {path}: {exc}") from exc
    if isinstance(raw, dict) and isinstance(raw.get("profiles"), list):
        values = raw["profiles"]
    elif isinstance(raw, dict) and isinstance(raw.get("notebooks"), list):
        values = raw["notebooks"]
    elif isinstance(raw, dict):
        values = [{"keyword_zh": key, "profile": value} for key, value in raw.items()]
    else:
        raise RelevanceProfileTransactionError("profiles file must be an object or profiles list")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(values):
        if not isinstance(item, dict):
            raise RelevanceProfileTransactionError(f"profiles[{index}] must be an object")
        keyword = str(item.get("keyword_zh") or item.get("keyword") or "").strip()
        profile = item.get("profile") if isinstance(item.get("profile"), dict) else item.get("relevance_profile")
        if not keyword or not isinstance(profile, dict):
            raise RelevanceProfileTransactionError(
                f"profiles[{index}] needs keyword_zh and profile/relevance_profile"
            )
        result.append({"keyword_zh": keyword, "profile": profile})
    if not result:
        raise RelevanceProfileTransactionError("profiles file contains no profiles")
    return result


def _profile_with_resolved_taxonomy(raw: dict[str, Any], snapshot: TaxonomySnapshot) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    profile = json.loads(json.dumps(raw, ensure_ascii=False))
    openalex = profile.setdefault("openalex", {})
    labels = openalex.get("filter_labels") or []
    if not isinstance(labels, list) or not labels:
        raise RelevanceProfileTransactionError("each profile needs non-empty openalex.filter_labels")
    resolved = resolve_subfield_labels(snapshot, labels)
    profile["matcher_schema_version"] = MATCHER_SCHEMA_VERSION
    openalex["resolved"] = True
    openalex["filter_ids"] = [entity["id"] for entity in resolved]
    openalex["filter_labels"] = [entity["label"] for entity in resolved]
    normalized = validate_relevance_profile(profile)
    return normalized, resolved


def build_relevance_profile_plan(
    *,
    profiles_path: Path,
    notebook_dir: Path,
    pending_pages_dir: Path,
    transaction_root: Path | None = None,
    runtime_paths: RelevanceRuntimePaths | None = None,
    taxonomy: TaxonomySnapshot | None = None,
    taxonomy_fetcher: Callable[[], TaxonomySnapshot] | None = None,
) -> dict[str, Any]:
    """Create a complete, hash-bound plan without mutating notebooks/pages."""
    if runtime_paths is None:
        if transaction_root is None:
            raise RelevanceProfileTransactionError(
                "transaction_root or runtime_paths is required for a profile plan"
            )
        runtime_paths = RelevanceRuntimePaths.resolve(
            notebook_root=Path(notebook_dir),
            journal_root=Path(pending_pages_dir),
            transaction_root=Path(transaction_root),
        )
    else:
        supplied = {
            "notebook_root": Path(notebook_dir).resolve(),
            "journal_root": Path(pending_pages_dir).resolve(),
            "transaction_root": (
                Path(transaction_root).resolve() if transaction_root is not None
                else runtime_paths.transaction_root
            ),
        }
        expected = {
            "notebook_root": runtime_paths.notebook_root,
            "journal_root": runtime_paths.journal_root,
            "transaction_root": runtime_paths.transaction_root,
        }
        if supplied != expected:
            raise RelevanceProfileTransactionError(
                "supplied relevance roots do not match runtime_paths"
            )
    notebook_dir = runtime_paths.notebook_root
    pending_pages_dir = runtime_paths.journal_root
    transaction_root = runtime_paths.transaction_root
    # ── Step 1: Load and structurally validate profile definitions ─────
    definitions = _load_profile_definitions(Path(profiles_path))
    # Validate every source definition locally before any network access.
    for definition in definitions:
        validate_relevance_profile_source(definition["profile"])
    # ── Step 2: Fetch taxonomy (only after local validation passes) ────
    if taxonomy is not None:
        if isinstance(taxonomy, TaxonomySnapshot):
            # Always validate — a TaxonomySnapshot can be constructed
            # from untrusted data by tests, CLI, or other callers.
            raw = taxonomy.to_dict()
        elif isinstance(taxonomy, dict):
            raw = dict(taxonomy)
        else:
            raise RelevanceProfileTransactionError(
                f"taxonomy must be a TaxonomySnapshot or dict, got {type(taxonomy)}")
        # Both paths go through the same validation gate.
        violations = validate_taxonomy_snapshot(raw)
        if violations:
            raise RelevanceProfileTransactionError(
                "taxonomy snapshot validation failed: " + "; ".join(violations))
        snapshot = TaxonomySnapshot(
            pages=tuple(raw.get("pages") or ()),
            entities=tuple(raw.get("entities") or ()),
            retrieved_at=str(raw.get("retrieved_at") or ""),
            page_hashes=tuple(raw.get("page_hashes") or ()),
            snapshot_sha256=str(raw.get("snapshot_sha256") or ""),
            schema_version=str(raw.get("schema_version") or "1.0"),
            raw_snapshot_sha256=str(raw.get("raw_snapshot_sha256") or ""),
            taxonomy_semantic_sha256=str(raw.get("taxonomy_semantic_sha256") or ""),
        )
    else:
        snapshot = taxonomy_fetcher() if taxonomy_fetcher else fetch_subfields_taxonomy()
        # Validate the fetched snapshot — the fetcher path previously
        # bypassed validate_taxonomy_snapshot entirely.
        violations = validate_taxonomy_snapshot(snapshot.to_dict())
        if violations:
            raise RelevanceProfileTransactionError(
                "taxonomy snapshot validation failed: " + "; ".join(violations))
    notebook_store = KeywordNotebookStore(Path(notebook_dir))
    planned: list[dict[str, Any]] = []
    seen_keywords: set[str] = set()
    journal_store = PageJournalStore(Path(pending_pages_dir))
    # Unknown/recovery lifecycle facts are checked before creating any
    # transaction identity or timestamp.  A failed plan is diagnostic only;
    # it must not even manufacture values that could be mistaken for an
    # applicable transaction.
    early_unknown: list[dict[str, str]] = []
    early_recovery: list[dict[str, str]] = []
    early_terminal = 0
    early_closeable = 0
    for definition in definitions:
        keyword = definition["keyword_zh"]
        nb = notebook_store.require_v4(keyword)
        profile, _resolved = _profile_with_resolved_taxonomy(definition["profile"], snapshot)
        kid = str(nb["keyword_id"])
        for page_path in sorted(Path(pending_pages_dir).glob(f"{kid}/*/*/*/*.json")):
            try:
                raw_page = json.loads(page_path.read_bytes().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RelevanceProfileTransactionError(
                    f"journal JSON is unreadable during profile plan: {page_path}"
                ) from exc
            candidates = raw_page.get("candidates") if isinstance(raw_page, dict) else None
            if not isinstance(candidates, list):
                continue
            for candidate in candidates:
                if not isinstance(candidate, Mapping):
                    continue
                lifecycle = classify_candidate_lifecycle(candidate.get("status"))
                detail = {
                    "page": str(page_path.resolve()),
                    "candidate_id": str(candidate.get("candidate_id") or ""),
                    "status": str(candidate.get("status") or ""),
                }
                stale = is_profile_closeable_candidate_state(candidate, profile["profile_hash"])
                if lifecycle is CandidateLifecycleClass.INVALID:
                    early_unknown.append(detail)
                elif lifecycle is CandidateLifecycleClass.RECOVERY_REQUIRED and stale:
                    early_recovery.append(detail)
                elif lifecycle is CandidateLifecycleClass.COMPLETED_TERMINAL and stale:
                    early_terminal += 1
                elif lifecycle is CandidateLifecycleClass.PRE_STAGING_CLOSEABLE and stale:
                    early_closeable += 1
    if early_unknown or early_recovery:
        report = {
            "schema_version": TRANSACTION_SCHEMA_VERSION, "status": "failed",
            "applicable": False,
            "unknown_lifecycle_candidates": early_unknown,
            "recovery_required_candidates": early_recovery,
            "historical_terminal_untouched": early_terminal,
            "closeable_candidates": early_closeable,
        }
        raise RelevanceProfilePlanError(
            "unknown candidate lifecycle in target journal scope"
            if early_unknown else
            "recovery-required candidate blocks relevance profile plan",
            report,
        )
    transaction_id = str(uuid.uuid4())
    closure_timestamp = _now()
    unknown_lifecycle_candidates: list[dict[str, str]] = []
    recovery_required_candidates: list[dict[str, str]] = []
    for definition in definitions:
        keyword = definition["keyword_zh"]
        if keyword in seen_keywords:
            raise RelevanceProfileTransactionError(f"duplicate profile keyword: {keyword}")
        seen_keywords.add(keyword)
        notebook_path = resolve_existing_notebook(keyword, Path(notebook_dir))
        if notebook_path is None:
            raise RelevanceProfileTransactionError(f"notebook not found for {keyword!r}")
        nb = notebook_store.require_v4(keyword)
        profile, resolved = _profile_with_resolved_taxonomy(definition["profile"], snapshot)
        old_generation = int(nb.get("relevance_generation") or 1)
        new_generation = old_generation + 1
        page_mutations: list[dict[str, Any]] = []
        kid = str(nb["keyword_id"])
        historical_terminal_untouched = 0
        closeable_candidates = 0
        for page_path in sorted(Path(pending_pages_dir).glob(f"{kid}/*/*/*/*.json")):
            before_bytes = page_path.read_bytes()
            try:
                raw_page = json.loads(before_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RelevanceProfileTransactionError(
                    f"journal JSON is unreadable during profile plan: {page_path}"
                ) from exc
            raw_candidates = raw_page.get("candidates") if isinstance(raw_page, dict) else None
            page_has_unknown_lifecycle = False
            if isinstance(raw_candidates, list):
                for candidate in raw_candidates:
                    if not isinstance(candidate, dict):
                        continue
                    lifecycle = classify_candidate_lifecycle(candidate.get("status"))
                    detail = {
                        "page": str(page_path.resolve()),
                        "candidate_id": str(candidate.get("candidate_id") or ""),
                        "status": str(candidate.get("status") or ""),
                    }
                    if lifecycle is CandidateLifecycleClass.INVALID:
                        unknown_lifecycle_candidates.append(detail)
                        page_has_unknown_lifecycle = True
                    elif (
                        lifecycle is CandidateLifecycleClass.RECOVERY_REQUIRED
                        and is_profile_closeable_candidate_state(candidate, profile["profile_hash"])
                    ):
                        recovery_required_candidates.append(detail)
            # Strict page validation intentionally rejects an unknown status.
            # Preserve the global diagnostic contract by collecting every raw
            # unknown in scope and withholding all transformations for its page.
            if page_has_unknown_lifecycle:
                continue
            page = journal_store.read(page_path)
            mutations: list[dict[str, str]] = []
            for candidate in page.get("candidates", []):
                lifecycle = classify_candidate_lifecycle(candidate.get("status"))
                relevance = candidate.get("relevance")
                old_hash = (
                    str(relevance.get("profile_hash") or "")
                    if isinstance(relevance, Mapping) else ""
                )
                stale_relevance = (
                    _profile_change_relevance_state(candidate)
                    and old_hash != profile["profile_hash"]
                )
                if lifecycle is CandidateLifecycleClass.COMPLETED_TERMINAL:
                    if stale_relevance:
                        historical_terminal_untouched += 1
                    continue
                if lifecycle is CandidateLifecycleClass.PRE_STAGING_CLOSEABLE and stale_relevance:
                    cid = str(candidate.get("candidate_id") or "")
                    mutations.append({
                        "candidate_id": cid,
                        "mutation_id": _canonical_hash({
                            "transaction_id": transaction_id,
                            "page": str(page_path.resolve()),
                            "candidate_id": cid,
                        })[:24],
                    })
            if mutations:
                after_bytes = transform_page_for_profile_closure(
                    before_bytes,
                    planned_mutations=tuple(mutations),
                    closure_timestamp=closure_timestamp,
                    transaction_id=transaction_id,
                    reason=RelevanceReason.STALE_PROFILE_CLOSED_BY_PROFILE_APPLY,
                    target_profile_hash=profile["profile_hash"],
                )
                closeable_candidates += len(mutations)
                page_mutations.append({
                    "path": str(page_path.resolve()),
                    "before_sha256": hashlib.sha256(before_bytes).hexdigest(),
                    "after_sha256": hashlib.sha256(after_bytes).hexdigest(),
                    "candidate_count": len(mutations),
                    "candidate_ids": [item["candidate_id"] for item in mutations],
                    "planned_mutations": mutations,
                    "identity": {
                        "keyword_id": kid,
                        "query_id": str(page.get("query_id") or ""),
                        "provider": str(page.get("provider") or ""),
                        "lane": str(page.get("lane") or ""),
                        "request_profile_hash": str(
                            ((page.get("request_signature") or {}).get("filters") or {}).get(
                                "profile_hash"
                            ) or ""
                        ),
                        "candidate_profile_hashes": sorted({
                            str((item.get("relevance") or {}).get("profile_hash") or "")
                            for item in page.get("candidates", [])
                            if isinstance(item.get("relevance"), dict)
                        }),
                        "request_signature": page.get("request_signature"),
                    },
                    "applied": False,
                })
        notebook_after = deepcopy(nb)
        notebook_after["relevance_profile"] = profile
        notebook_after["relevance_generation"] = new_generation
        notebook_after["updated_at"] = closure_timestamp
        validate_notebook(notebook_after)
        notebook_after_bytes = _json_bytes(notebook_after)
        planned.append({
            "keyword_zh": keyword,
            "keyword_id": kid,
            "notebook_path": str(notebook_path.resolve()),
            "notebook_before_sha256": _sha_bytes(notebook_path),
            "notebook_after_sha256": hashlib.sha256(notebook_after_bytes).hexdigest(),
            "notebook_after": notebook_after,
            "old_profile_hash": (nb.get("relevance_profile") or {}).get("profile_hash", "") if isinstance(nb.get("relevance_profile"), dict) else "",
            "old_generation": old_generation,
            "new_generation": new_generation,
            "profile": profile,
            "profile_hash": profile["profile_hash"],
            "resolved_entities": resolved,
            "page_mutations": page_mutations,
            "historical_terminal_untouched": historical_terminal_untouched,
            "closed_stale_candidates": closeable_candidates,
            "query_configuration_hash": _canonical_hash(nb.get("search_queries") or {}),
        })
    if unknown_lifecycle_candidates or recovery_required_candidates:
        report = {
            "schema_version": TRANSACTION_SCHEMA_VERSION,
            "status": "failed",
            "applicable": False,
            "unknown_lifecycle_candidates": unknown_lifecycle_candidates,
            "recovery_required_candidates": recovery_required_candidates,
            "historical_terminal_untouched": sum(
                item["historical_terminal_untouched"] for item in planned
            ),
            "closeable_candidates": sum(item["closed_stale_candidates"] for item in planned),
        }
        reason = (
            "unknown candidate lifecycle in target journal scope"
            if unknown_lifecycle_candidates
            else "recovery-required candidate blocks relevance profile plan"
        )
        raise RelevanceProfilePlanError(reason, report)
    plan_core = {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "created_at": closure_timestamp,
        "closure_timestamp": closure_timestamp,
        "closure_reason": RelevanceReason.STALE_PROFILE_CLOSED_BY_PROFILE_APPLY.value,
        "matcher_schema_version": MATCHER_SCHEMA_VERSION,
        "profiles_path": str(Path(profiles_path).resolve()),
        "resolved_notebook_root": str(runtime_paths.notebook_root),
        "resolved_journal_root": str(runtime_paths.journal_root),
        "resolved_transaction_root": str(runtime_paths.transaction_root),
        "resolved_lock_path": str(runtime_paths.lock_path),
        "taxonomy_snapshot_sha256": snapshot.snapshot_sha256,
        "taxonomy_retrieved_at": snapshot.retrieved_at,
        "taxonomy_page_hashes": list(snapshot.page_hashes),
        "taxonomy_snapshot": snapshot.to_dict(),
        "resolved_taxonomy_entities": [
            entity for item in planned for entity in item["resolved_entities"]
        ],
        "notebooks": planned,
    }
    page_order = sorted(
        mutation["path"]
        for item in planned for mutation in item["page_mutations"]
    )
    plan_core["transaction_journal_path"] = str(
        (runtime_paths.transaction_root / f"{plan_core['transaction_id']}.json").resolve()
    )
    plan_core["page_mutation_order"] = page_order
    plan_core["expected_after_decision_count"] = sum(
        int(mutation.get("candidate_count") or 0)
        for item in planned for mutation in item["page_mutations"]
    )
    plan_core["commit_point_files"] = [item["notebook_path"] for item in planned] + [
        str((runtime_paths.transaction_root / f"{plan_core['transaction_id']}.commit.json").resolve())
    ]
    plan_core["resume_policy"] = (
        "resume the sole applying journal; skip applied paths only when after_sha256 matches; "
        "fail closed on before-hash or generation drift"
    )
    plan = dict(plan_core)
    plan["plan_hash"] = _canonical_hash(plan_core)
    return plan


def _journal_path(plan: Mapping[str, Any]) -> Path:
    runtime_paths = RelevanceRuntimePaths.from_plan(plan)
    return runtime_paths.transaction_root / f"{plan['transaction_id']}.json"


def _write_journal(path: Path, journal: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, journal, indent=2)


def list_applying_relevance_profile_transactions(root: Path) -> list[Path]:
    """Return durable applying journals; unreadable journals fail closed."""
    root = Path(root)
    if not root.exists():
        return []
    applying: list[Path] = []
    for path in sorted(root.glob("*.json")):
        if path.name.endswith(".commit.json") or path.name.endswith(".manifest.json"):
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RelevanceProfileTransactionError(
                f"unreadable profile transaction journal: {path}: {exc}"
            ) from exc
        if isinstance(raw, dict) and raw.get("state") == "applying":
            applying.append(path.resolve())
    return applying


def _assert_sole_applying_journal(plan: Mapping[str, Any]) -> None:
    runtime_paths = RelevanceRuntimePaths.from_plan(plan)
    applying = list_applying_relevance_profile_transactions(runtime_paths.transaction_root)
    current = _journal_path(plan).resolve()
    foreign = [path for path in applying if path != current]
    if foreign:
        raise RelevanceProfileTransactionError(
            "another relevance profile transaction is applying; resume it first: "
            + ",".join(map(str, foreign))
        )


def _validate_plan_identity(plan: Mapping[str, Any], expected_plan_hash: str) -> None:
    if plan.get("schema_version") != TRANSACTION_SCHEMA_VERSION:
        raise RelevanceProfileTransactionError("unsupported relevance profile plan schema")
    core = dict(plan)
    supplied_hash = core.pop("plan_hash", None)
    if supplied_hash != _canonical_hash(core) or expected_plan_hash != supplied_hash:
        raise RelevanceProfileTransactionError("expected plan hash does not match plan content")
    try:
        transaction_id = str(uuid.UUID(str(plan.get("transaction_id") or "")))
    except ValueError as exc:
        raise RelevanceProfileTransactionError("profile plan transaction ID is invalid") from exc
    if transaction_id != str(plan["transaction_id"]):
        raise RelevanceProfileTransactionError("profile plan transaction ID is not canonical")
    if Path(str(plan.get("transaction_journal_path") or "")).resolve() != _journal_path(plan).resolve():
        raise RelevanceProfileTransactionError("profile plan journal path is not identity-bound")
    if plan.get("matcher_schema_version") != MATCHER_SCHEMA_VERSION:
        raise RelevanceProfileTransactionError("profile plan matcher schema drift")
    taxonomy = plan.get("taxonomy_snapshot")
    if not isinstance(taxonomy, Mapping) or taxonomy.get("snapshot_sha256") != plan.get("taxonomy_snapshot_sha256"):
        raise RelevanceProfileTransactionError("profile plan taxonomy snapshot drift")
    taxonomy_violations = validate_taxonomy_snapshot(dict(taxonomy))
    if taxonomy_violations:
        raise RelevanceProfileTransactionError(
            "profile plan taxonomy snapshot invalid: " + "; ".join(taxonomy_violations))
    for item in plan.get("notebooks") or []:
        normalized = validate_relevance_profile(item.get("profile"))
        if normalized.get("profile_hash") != item.get("profile_hash"):
            raise RelevanceProfileTransactionError("profile plan target profile hash drift")
    # All four resolved roots are checked by the one runtime-path authority.
    try:
        RelevanceRuntimePaths.from_plan(plan)
    except ValueError as exc:
        raise RelevanceProfileTransactionError(str(exc)) from exc


def validate_relevance_profile_transaction_journal(
    journal: Mapping[str, Any], *, journal_path: Path | None = None,
) -> dict[str, Any]:
    """Validate the complete durable shape of one profile transaction journal."""
    if not isinstance(journal, Mapping):
        raise RelevanceProfileTransactionError("profile transaction journal must be an object")
    required = {
        "schema_version", "transaction_id", "plan_hash", "state", "started_at",
        "preflight", "plan", "page_mutations", "notebook_mutations", "commit_points",
    }
    optional = {"committed_at", "aborted_at"}
    if set(journal) - required - optional:
        raise RelevanceProfileTransactionError("profile transaction journal has unknown fields")
    if required - set(journal):
        raise RelevanceProfileTransactionError("profile transaction journal is missing fields")
    if journal["schema_version"] != TRANSACTION_SCHEMA_VERSION:
        raise RelevanceProfileTransactionError("unsupported profile transaction journal schema")
    state = journal["state"]
    if state not in {"applying", "committed", "aborted"}:
        raise RelevanceProfileTransactionError(f"unknown profile transaction state: {state!r}")
    transaction_id = str(journal["transaction_id"] or "")
    plan = journal["plan"]
    if not isinstance(plan, Mapping) or transaction_id != str(plan.get("transaction_id") or ""):
        raise RelevanceProfileTransactionError("profile transaction journal plan identity drift")
    _validate_plan_identity(plan, str(journal["plan_hash"] or ""))
    if journal_path is not None and Path(journal_path).resolve() != _journal_path(plan).resolve():
        raise RelevanceProfileTransactionError("profile transaction journal path is not plan-bound")

    preflight = journal["preflight"]
    if not isinstance(preflight, Mapping) or set(preflight) != {
        "planned_stale_candidates", "historical_terminal_untouched", "completed_at",
    }:
        raise RelevanceProfileTransactionError("profile transaction preflight shape is invalid")
    for key in ("planned_stale_candidates", "historical_terminal_untouched"):
        if type(preflight[key]) is not int or preflight[key] < 0:
            raise RelevanceProfileTransactionError(f"profile transaction preflight {key} is invalid")

    commit_points = journal["commit_points"]
    expected_points = {"pages", "prepared_notebooks", "notebook_profiles_and_generation", "commit_record"}
    if not isinstance(commit_points, Mapping) or set(commit_points) != expected_points:
        raise RelevanceProfileTransactionError("profile transaction commit_points shape is invalid")
    if any(type(value) is not bool for value in commit_points.values()):
        raise RelevanceProfileTransactionError("profile transaction commit_points must be boolean")

    planned_pages = {
        str(item["path"]): item
        for notebook in plan["notebooks"] for item in notebook["page_mutations"]
    }
    page_mutations = journal["page_mutations"]
    if not isinstance(page_mutations, list):
        raise RelevanceProfileTransactionError("profile transaction page_mutations must be a list")
    if {str(item.get("path") or "") for item in page_mutations if isinstance(item, Mapping)} != set(planned_pages):
        raise RelevanceProfileTransactionError("profile transaction page mutation set drift")
    page_fields = {
        "path", "before_sha256", "after_sha256", "candidate_count", "candidate_ids",
        "planned_mutations", "identity", "applied",
    }
    for mutation in page_mutations:
        if not isinstance(mutation, Mapping) or set(mutation) != page_fields:
            raise RelevanceProfileTransactionError("profile transaction page mutation shape is invalid")
        expected = planned_pages[str(mutation["path"])]
        for key in page_fields - {"applied"}:
            if mutation[key] != expected[key]:
                raise RelevanceProfileTransactionError(f"profile transaction page mutation drift: {key}")
        if type(mutation["applied"]) is not bool:
            raise RelevanceProfileTransactionError("profile transaction page applied flag is invalid")

    planned_notebooks = {str(item["notebook_path"]): item for item in plan["notebooks"]}
    notebook_mutations = journal["notebook_mutations"]
    if not isinstance(notebook_mutations, list):
        raise RelevanceProfileTransactionError("profile transaction notebook_mutations must be a list")
    if {str(item.get("path") or "") for item in notebook_mutations if isinstance(item, Mapping)} != set(planned_notebooks):
        raise RelevanceProfileTransactionError("profile transaction notebook mutation set drift")
    notebook_fields = {"path", "before_sha256", "after_sha256", "applied", "prepared_path"}
    for mutation in notebook_mutations:
        if not isinstance(mutation, Mapping) or not set(mutation).issubset(notebook_fields):
            raise RelevanceProfileTransactionError("profile transaction notebook mutation shape is invalid")
        if set(mutation) - {"prepared_path"} != {"path", "before_sha256", "after_sha256", "applied"}:
            raise RelevanceProfileTransactionError("profile transaction notebook mutation fields are incomplete")
        expected = planned_notebooks[str(mutation["path"])]
        if mutation["before_sha256"] != expected["notebook_before_sha256"] or mutation["after_sha256"] != expected["notebook_after_sha256"]:
            raise RelevanceProfileTransactionError("profile transaction notebook hash drift")
        if type(mutation["applied"]) is not bool:
            raise RelevanceProfileTransactionError("profile transaction notebook applied flag is invalid")
        if "prepared_path" in mutation:
            prepared = Path(str(mutation["prepared_path"]))
            if not prepared.is_absolute() or prepared.parent != (
                RelevanceRuntimePaths.from_plan(plan).transaction_root
                / f"{transaction_id}.prepared"
            ).resolve():
                raise RelevanceProfileTransactionError("profile transaction prepared path drift")
    if state == "committed" and not all(commit_points.values()):
        raise RelevanceProfileTransactionError("committed profile transaction has incomplete commit points")
    if state == "aborted" and any(item["applied"] for item in page_mutations + notebook_mutations):
        raise RelevanceProfileTransactionError("aborted profile transaction contains applied mutations")
    return dict(journal)


def _preflight_plan(plan: Mapping[str, Any], *, resume: bool) -> dict[str, Any]:
    planned_stale: set[tuple[str, str]] = set()
    actual_stale: set[tuple[str, str]] = set()
    historical_terminal = 0
    runtime_paths = RelevanceRuntimePaths.from_plan(plan)
    page_store = PageJournalStore(runtime_paths.journal_root)
    for notebook in plan["notebooks"]:
        notebook_path = Path(str(notebook["notebook_path"]))
        if not notebook_path.exists():
            raise RelevanceProfileTransactionError(f"notebook missing before apply: {notebook_path}")
        current_hash = _sha_bytes(notebook_path)
        allowed = {str(notebook["notebook_before_sha256"])}
        if resume:
            allowed.add(str(notebook["notebook_after_sha256"]))
        if current_hash not in allowed:
            raise RelevanceProfileTransactionError(f"notebook drift before apply: {notebook_path}")
        current = json.loads(notebook_path.read_text(encoding="utf-8"))
        validate_notebook(current)
        if current_hash == notebook["notebook_before_sha256"]:
            if str(current.get("keyword_id")) != str(notebook["keyword_id"]):
                raise RelevanceProfileTransactionError(f"notebook identity drift: {notebook_path}")
            if int(current.get("relevance_generation") or 1) != int(notebook["old_generation"]):
                raise RelevanceProfileTransactionError(f"notebook generation drift: {notebook_path}")
            if _canonical_hash(current.get("search_queries") or {}) != notebook["query_configuration_hash"]:
                raise RelevanceProfileTransactionError(f"notebook query configuration drift: {notebook_path}")
        kid = str(notebook["keyword_id"])
        target_hash = str(notebook["profile_hash"])
        mutation_by_path = {
            str(Path(mutation["path"]).resolve()): mutation
            for mutation in notebook["page_mutations"]
        }
        for mutation in notebook["page_mutations"]:
            planned_stale.update(
                (str(Path(mutation["path"]).resolve()), str(cid))
                for cid in mutation["candidate_ids"]
            )
        for page_path in sorted(runtime_paths.journal_root.glob(f"{kid}/*/*/*/*.json")):
            page_bytes = page_path.read_bytes()
            page = page_store.read(page_path)
            mutation = mutation_by_path.get(str(page_path.resolve()))
            current_hash = hashlib.sha256(page_bytes).hexdigest()
            already_applied = bool(
                resume and mutation is not None
                and current_hash == mutation["after_sha256"]
            )
            if already_applied:
                actual_stale.update(
                    (str(page_path.resolve()), str(cid))
                    for cid in mutation["candidate_ids"]
                )
            for candidate in page["candidates"]:
                lifecycle = classify_candidate_lifecycle(candidate.get("status"))
                if lifecycle is CandidateLifecycleClass.INVALID:
                    raise RelevanceProfileTransactionError(
                        f"unknown candidate lifecycle during preflight: {page_path}:"
                        f"{candidate.get('candidate_id')}:{candidate.get('status')}"
                    )
                stale = is_profile_closeable_candidate_state(candidate, target_hash)
                if lifecycle is CandidateLifecycleClass.RECOVERY_REQUIRED and stale:
                    raise RelevanceProfileTransactionError(
                        f"recovery-required candidate blocks apply: {page_path}:"
                        f"{candidate.get('candidate_id')}"
                    )
                if lifecycle is CandidateLifecycleClass.COMPLETED_TERMINAL and stale:
                    historical_terminal += 1
                if lifecycle is CandidateLifecycleClass.PRE_STAGING_CLOSEABLE and stale:
                    actual_stale.add((str(page_path.resolve()), str(candidate["candidate_id"])))
            if mutation is None:
                continue
            if already_applied:
                continue
            if current_hash != mutation["before_sha256"]:
                raise RelevanceProfileTransactionError(f"page drift before apply: {page_path}")
            transformed = transform_page_for_profile_closure(
                page_bytes,
                planned_mutations=tuple(mutation["planned_mutations"]),
                closure_timestamp=str(plan["closure_timestamp"]),
                transaction_id=str(plan["transaction_id"]),
                reason=RelevanceReason(str(plan["closure_reason"])),
                target_profile_hash=target_hash,
            )
            if hashlib.sha256(transformed).hexdigest() != mutation["after_sha256"]:
                raise RelevanceProfileTransactionError(f"page expected-after drift: {page_path}")
    if actual_stale != planned_stale:
        raise RelevanceProfileTransactionError(
            "plan_incomplete_due_to_new_stale_candidates"
        )
    return {
        "planned_stale_candidates": len(planned_stale),
        "historical_terminal_untouched": historical_terminal,
        "completed_at": str(plan["closure_timestamp"]),
    }


def _new_journal(plan: dict[str, Any], preflight: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "transaction_id": plan["transaction_id"],
        "plan_hash": plan["plan_hash"],
        "state": "applying",
        "started_at": plan["closure_timestamp"],
        "preflight": dict(preflight),
        "plan": plan,
        "page_mutations": [
            dict(mutation)
            for item in plan["notebooks"] for mutation in item["page_mutations"]
        ],
        "notebook_mutations": [
            {
                "path": item["notebook_path"],
                "before_sha256": item["notebook_before_sha256"],
                "after_sha256": item["notebook_after_sha256"],
                "applied": False,
            }
            for item in plan["notebooks"]
        ],
        "commit_points": {
            "pages": False,
            "prepared_notebooks": False,
            "notebook_profiles_and_generation": False,
            "commit_record": False,
        },
    }


def _execute_transaction(
    plan: dict[str, Any], journal_path: Path, journal: dict[str, Any],
) -> dict[str, Any]:
    runtime_paths = RelevanceRuntimePaths.from_plan(plan)
    page_store = PageJournalStore(runtime_paths.journal_root)
    owner_by_path = {
        mutation["path"]: notebook
        for notebook in plan["notebooks"] for mutation in notebook["page_mutations"]
    }
    for mutation in sorted(
        journal["page_mutations"],
        key=lambda value: (
            owner_by_path[value["path"]]["keyword_id"],
            value["identity"]["provider"], value["identity"]["lane"],
            value["identity"]["query_id"], value["path"],
        ),
    ):
        path = Path(mutation["path"])
        current_hash = _sha_bytes(path)
        if current_hash == mutation["after_sha256"]:
            if not mutation.get("applied"):
                mutation["applied"] = True
                _write_journal(journal_path, journal)
            continue
        if current_hash != mutation["before_sha256"]:
            raise RelevanceProfileTransactionError(f"page drift during resume: {path}")
        owner = owner_by_path[mutation["path"]]
        page_store.close_stale_profile_candidates(
            path,
            new_profile_hash=str(owner["profile_hash"]),
            planned_mutations=tuple(mutation["planned_mutations"]),
            closure_timestamp=str(plan["closure_timestamp"]),
            transaction_id=str(plan["transaction_id"]),
            reason=RelevanceReason(str(plan["closure_reason"])),
        )
        if _sha_bytes(path) != mutation["after_sha256"]:
            raise RelevanceProfileTransactionError(f"page expected-after write mismatch: {path}")
        mutation["applied"] = True
        _write_journal(journal_path, journal)
    journal["commit_points"]["pages"] = True
    _write_journal(journal_path, journal)

    runtime_paths = RelevanceRuntimePaths.from_plan(plan)
    prepared_dir = runtime_paths.transaction_root / f"{plan['transaction_id']}.prepared"
    prepared_dir.mkdir(parents=True, exist_ok=True)
    manifest_entries: list[dict[str, str]] = []
    notebook_by_path = {item["notebook_path"]: item for item in plan["notebooks"]}
    for mutation in journal["notebook_mutations"]:
        item = notebook_by_path[mutation["path"]]
        prepared = prepared_dir / (hashlib.sha256(mutation["path"].encode("utf-8")).hexdigest() + ".json")
        atomic_replace_bytes(prepared, _json_bytes(item["notebook_after"]))
        if _sha_bytes(prepared) != mutation["after_sha256"]:
            raise RelevanceProfileTransactionError(f"prepared notebook hash mismatch: {prepared}")
        mutation["prepared_path"] = str(prepared.resolve())
        manifest_entries.append({
            "target": mutation["path"], "prepared": str(prepared.resolve()),
            "after_sha256": mutation["after_sha256"],
        })
    manifest_path = runtime_paths.transaction_root / f"{plan['transaction_id']}.manifest.json"
    _write_journal(manifest_path, {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "transaction_id": plan["transaction_id"],
        "plan_hash": plan["plan_hash"],
        "notebooks": manifest_entries,
    })
    journal["commit_points"]["prepared_notebooks"] = True
    _write_journal(journal_path, journal)

    for mutation in sorted(journal["notebook_mutations"], key=lambda item: item["path"]):
        path = Path(mutation["path"])
        current_hash = _sha_bytes(path)
        if current_hash == mutation["after_sha256"]:
            mutation["applied"] = True
            _write_journal(journal_path, journal)
            continue
        if current_hash != mutation["before_sha256"]:
            raise RelevanceProfileTransactionError(f"notebook drift during commit: {path}")
        item = notebook_by_path[mutation["path"]]
        atomic_replace_bytes(path, _json_bytes(item["notebook_after"]))
        if _sha_bytes(path) != mutation["after_sha256"]:
            raise RelevanceProfileTransactionError(f"notebook expected-after write mismatch: {path}")
        mutation["applied"] = True
        _write_journal(journal_path, journal)
    journal["commit_points"]["notebook_profiles_and_generation"] = True

    for mutation in journal["page_mutations"]:
        if _sha_bytes(Path(mutation["path"])) != mutation["after_sha256"]:
            raise RelevanceProfileTransactionError("page closure recheck failed")
    for mutation in journal["notebook_mutations"]:
        if _sha_bytes(Path(mutation["path"])) != mutation["after_sha256"]:
            raise RelevanceProfileTransactionError("notebook closure recheck failed")
    commit_record = journal_path.with_name(journal_path.stem + ".commit.json")
    _write_journal(commit_record, {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "transaction_id": plan["transaction_id"],
        "plan_hash": plan["plan_hash"],
        "state": "committed",
        "committed_at": plan["closure_timestamp"],
    })
    journal["commit_points"]["commit_record"] = True
    journal["state"] = "committed"
    journal["committed_at"] = plan["closure_timestamp"]
    _write_journal(journal_path, journal)
    return journal


def apply_relevance_profile_plan(
    plan: dict[str, Any],
    *,
    expected_plan_hash: str,
) -> dict[str, Any]:
    """Apply a new plan after a complete zero-write global preflight."""
    _validate_plan_identity(plan, expected_plan_hash)
    try:
        runtime_paths = RelevanceRuntimePaths.from_plan(plan)
    except ValueError as exc:
        raise RelevanceProfileTransactionError(str(exc)) from exc
    lock_path = runtime_paths.lock_path
    journal_path = _journal_path(plan)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(lock_path)):
        _assert_sole_applying_journal(plan)
        if journal_path.exists():
            existing = json.loads(journal_path.read_text(encoding="utf-8"))
            if existing.get("plan_hash") != plan["plan_hash"]:
                raise RelevanceProfileTransactionError("profile transaction journal plan hash mismatch")
            if existing.get("state") == "committed":
                return existing
            if existing.get("state") == "applying":
                raise RelevanceProfileTransactionError(
                    f"profile transaction is applying; use --resume: {journal_path}"
                )
            raise RelevanceProfileTransactionError(
                f"profile transaction state does not allow apply: {existing.get('state')}"
            )
        preflight = _preflight_plan(plan, resume=False)
        journal = _new_journal(plan, preflight)
        _write_journal(journal_path, journal)
        return _execute_transaction(plan, journal_path, journal)


def resume_relevance_profile_transaction(transaction_path: Path) -> dict[str, Any]:
    journal_path = Path(transaction_path)
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if not isinstance(journal, dict) or journal.get("schema_version") != TRANSACTION_SCHEMA_VERSION:
        raise RelevanceProfileTransactionError("unsupported relevance profile transaction journal")
    if journal.get("state") == "committed":
        return journal
    if journal.get("state") != "applying" or not isinstance(journal.get("plan"), dict):
        raise RelevanceProfileTransactionError("transaction is not resumable")
    plan = journal["plan"]
    _validate_plan_identity(plan, str(journal.get("plan_hash") or ""))
    if _journal_path(plan).resolve() != journal_path.resolve():
        raise RelevanceProfileTransactionError("transaction path is not plan-bound")
    try:
        runtime_paths = RelevanceRuntimePaths.from_plan(plan)
    except ValueError as exc:
        raise RelevanceProfileTransactionError(str(exc)) from exc
    lock_path = runtime_paths.lock_path
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(lock_path)):
        applying = list_applying_relevance_profile_transactions(runtime_paths.transaction_root)
        if applying != [journal_path.resolve()]:
            raise RelevanceProfileTransactionError("resume requires the sole applying transaction")
        _preflight_plan(plan, resume=True)
        return _execute_transaction(plan, journal_path, journal)


def inspect_relevance_profile_transaction(transaction_path: Path) -> dict[str, Any]:
    value = json.loads(Path(transaction_path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RelevanceProfileTransactionError("transaction journal must be an object")
    return value


def abort_relevance_profile_transaction(transaction_path: Path) -> dict[str, Any]:
    journal_path = Path(transaction_path)
    journal = inspect_relevance_profile_transaction(journal_path)
    if journal.get("state") != "applying" or not isinstance(journal.get("plan"), dict):
        raise RelevanceProfileTransactionError("only an applying transaction may be aborted")
    if any(item.get("applied") for item in journal.get("page_mutations", [])) or any(
        item.get("applied") for item in journal.get("notebook_mutations", [])
    ):
        raise RelevanceProfileTransactionError("transaction has mutations; resume or repair it")
    plan = journal["plan"]
    try:
        runtime_paths = RelevanceRuntimePaths.from_plan(plan)
    except ValueError as exc:
        raise RelevanceProfileTransactionError(str(exc)) from exc
    lock_path = runtime_paths.lock_path
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(lock_path)):
        for mutation in journal.get("page_mutations", []):
            if _sha_bytes(Path(mutation["path"])) != mutation["before_sha256"]:
                raise RelevanceProfileTransactionError("page changed; transaction cannot be aborted")
        for mutation in journal.get("notebook_mutations", []):
            if _sha_bytes(Path(mutation["path"])) != mutation["before_sha256"]:
                raise RelevanceProfileTransactionError("notebook changed; transaction cannot be aborted")
        journal["state"] = "aborted"
        journal["aborted_at"] = plan["closure_timestamp"]
        _write_journal(journal_path, journal)
        return journal

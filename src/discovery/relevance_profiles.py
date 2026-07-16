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

import requests
from filelock import FileLock

from src.discovery.keyword_notebook import KeywordNotebookStore, resolve_existing_notebook, validate_notebook
from src.discovery.page_journal import (
    CandidateLifecycleClass,
    PageJournalStore,
    classify_candidate_lifecycle,
    is_profile_closeable_candidate,
    transform_page_for_profile_closure,
)
from src.discovery.relevance import (
    MATCHER_SCHEMA_VERSION,
    RelevanceReason,
    validate_relevance_profile,
)
from src.utils.atomic_io import atomic_replace_bytes, atomic_write_json


OPENALEX_SUBFIELDS_URL = "https://api.openalex.org/subfields"
TRANSACTION_SCHEMA_VERSION = "2.0"


class RelevanceProfileTransactionError(RuntimeError):
    """Raised when a profile plan/apply operation must fail closed."""


class RelevanceProfilePlanError(RelevanceProfileTransactionError):
    """Plan failure carrying a safe, explicitly non-applicable report."""

    def __init__(self, message: str, report: Mapping[str, Any]):
        super().__init__(message)
        self.report = dict(report)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


@dataclass(frozen=True)
class TaxonomySnapshot:
    pages: tuple[dict[str, Any], ...]
    entities: tuple[dict[str, Any], ...]
    retrieved_at: str
    page_hashes: tuple[str, ...]
    snapshot_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "retrieved_at": self.retrieved_at,
            "page_hashes": list(self.page_hashes),
            "snapshot_sha256": self.snapshot_sha256,
            "pages": list(self.pages),
            "entities": list(self.entities),
        }


def fetch_subfields_taxonomy(
    *,
    http_get: Callable[..., Any] | None = None,
    per_page: int = 100,
    timeout: float = 20.0,
) -> TaxonomySnapshot:
    """Read every OpenAlex ``/subfields`` page and retain raw responses."""
    if per_page < 1 or per_page > 200:
        raise ValueError("per_page must be between 1 and 200")
    getter = http_get or requests.get
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
    return TaxonomySnapshot(
        pages=tuple(pages), entities=tuple(entities), retrieved_at=retrieved_at,
        page_hashes=page_hashes, snapshot_sha256=snapshot_sha,
    )


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
    transaction_root: Path,
    taxonomy: TaxonomySnapshot | None = None,
    taxonomy_fetcher: Callable[[], TaxonomySnapshot] | None = None,
) -> dict[str, Any]:
    """Create a complete, hash-bound plan without mutating notebooks/pages."""
    snapshot = taxonomy or (taxonomy_fetcher() if taxonomy_fetcher else fetch_subfields_taxonomy())
    definitions = _load_profile_definitions(Path(profiles_path))
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
        nb = notebook_store.require_v3(keyword)
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
        nb = notebook_store.require_v3(keyword)
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
        "notebook_dir": str(Path(notebook_dir).resolve()),
        "pending_pages_dir": str(Path(pending_pages_dir).resolve()),
        "transaction_root": str(Path(transaction_root).resolve()),
        "lock_path": str((Path(transaction_root).parent / "locks" / "relevance_profiles.lock").resolve()),
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
        (Path(transaction_root) / f"{plan_core['transaction_id']}.json").resolve()
    )
    plan_core["page_mutation_order"] = page_order
    plan_core["expected_after_decision_count"] = sum(
        int(mutation.get("candidate_count") or 0)
        for item in planned for mutation in item["page_mutations"]
    )
    plan_core["commit_point_files"] = [item["notebook_path"] for item in planned] + [
        str((Path(transaction_root) / f"{plan_core['transaction_id']}.commit.json").resolve())
    ]
    plan_core["resume_policy"] = (
        "resume the sole applying journal; skip applied paths only when after_sha256 matches; "
        "fail closed on before-hash or generation drift"
    )
    plan = dict(plan_core)
    plan["plan_hash"] = _canonical_hash(plan_core)
    return plan


def _journal_path(plan: Mapping[str, Any]) -> Path:
    return Path(str(plan["transaction_root"])) / f"{plan['transaction_id']}.json"


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
    applying = list_applying_relevance_profile_transactions(
        Path(str(plan["transaction_root"]))
    )
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
    for item in plan.get("notebooks") or []:
        normalized = validate_relevance_profile(item.get("profile"))
        if normalized.get("profile_hash") != item.get("profile_hash"):
            raise RelevanceProfileTransactionError("profile plan target profile hash drift")


def _preflight_plan(plan: Mapping[str, Any], *, resume: bool) -> dict[str, Any]:
    planned_stale: set[tuple[str, str]] = set()
    actual_stale: set[tuple[str, str]] = set()
    historical_terminal = 0
    page_store = PageJournalStore(Path(str(plan["pending_pages_dir"])))
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
        for page_path in sorted(Path(str(plan["pending_pages_dir"])).glob(f"{kid}/*/*/*/*.json")):
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
    page_store = PageJournalStore(Path(str(plan["pending_pages_dir"])))
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

    prepared_dir = Path(str(plan["transaction_root"])) / f"{plan['transaction_id']}.prepared"
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
    manifest_path = Path(str(plan["transaction_root"])) / f"{plan['transaction_id']}.manifest.json"
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
    lock_path = Path(str(plan.get("lock_path") or (Path(str(plan["transaction_root"])).parent / "locks" / "relevance_profiles.lock")))
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
    lock_path = Path(str(plan["lock_path"]))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(lock_path)):
        applying = list_applying_relevance_profile_transactions(Path(str(plan["transaction_root"])))
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
    lock_path = Path(str(plan["lock_path"]))
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

"""Deterministic, notebook-scoped relevance decisions for discovery pages.

The relevance decision is deliberately separate from the candidate lifecycle.
The staging queue still owns ``pending``/``processing``/``staged`` while this
module only answers whether a candidate is allowed to enter that lifecycle.
No decision is cached by DOI: only raw OpenAlex Work evidence may be reused.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol

from src.discovery.models import PaperCandidate
from src.utils.canonical_json import canonical_sha256
from src.utils.identifiers import normalize_doi


RELEVANCE_PROFILE_SCHEMA_VERSION = "1.0"
MATCHER_SCHEMA_VERSION = "1.0"
RELEVANCE_STATES = frozenset({
    "profile_unbound",
    "passed",
    "rejected",
    "verification_deferred",
    "candidate_invalid",
})


class RelevanceState(str, Enum):
    """Authoritative relevance-state values shared across the codebase."""
    PROFILE_UNBOUND = "profile_unbound"
    PASSED = "passed"
    REJECTED = "rejected"
    VERIFICATION_DEFERRED = "verification_deferred"
    CANDIDATE_INVALID = "candidate_invalid"


SCOPE_POLICY_REQUIRE_OPENALEX_SUBFIELD = "require_openalex_subfield"
_OPENALEX_RELEVANCE_FIELD = "relevance" + "_score"


class RelevanceProfileError(ValueError):
    """Raised when a relevance profile is unsafe or incomplete."""


class RelevanceVerificationError(RuntimeError):
    """Raised when an OpenAlex verification response cannot be classified."""


class RelevanceReason(str, Enum):
    """Authoritative persisted reason codes for relevance decisions."""

    PROFILE_MATCH = "profile_match"
    MISSING_REQUIRED_GROUP = "missing_required_group"
    MISSING_REQUIRED_GROUP_MISSING_ABSTRACT = "missing_required_group_missing_abstract"
    NEGATIVE_TERM_MATCH = "negative_term_match"
    OPENALEX_VERIFICATION_DEFERRED = "openalex_verification_deferred"
    OPENALEX_WORK_STRUCTURE_INVALID = "openalex_work_structure_invalid"
    SUBFIELD_MISMATCH = "subfield_mismatch"
    OPENALEX_SCOPE_REQUIRES_DOI = "openalex_scope_requires_doi"
    REJECT_TOPIC_UNVERIFIED = "reject_topic_unverified"
    CANDIDATE_PAYLOAD_INVALID = "candidate_payload_invalid"
    PROFILE_UNBOUND_CLOSED_BY_PROFILE_APPLY = "profile_unbound_closed_by_profile_apply"
    STALE_PROFILE_CLOSED_BY_PROFILE_APPLY = "stale_profile_closed_by_profile_apply"


RELEVANCE_REASON_VALUES = frozenset(reason.value for reason in RelevanceReason)


def _canonical_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    # Normalize all Unicode dash variants and whitespace around dashes.
    text = re.sub(r"[\u2010-\u2015\u2212\uFE58\uFE63\uFF0D]", "-", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*-\s*", "-", text)
    return text.strip()


def normalize_relevance_term(value: Any) -> str:
    return _canonical_text(value)


def _require_string_list(value: Any, path: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise RelevanceProfileError(f"{path} must be a non-empty list")
    result: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, str) or not raw.strip():
            raise RelevanceProfileError(f"{path}[{index}] must be a non-blank string")
        term = normalize_relevance_term(raw)
        if not term:
            raise RelevanceProfileError(f"{path}[{index}] normalizes to empty")
        if term in seen:
            raise RelevanceProfileError(f"{path} contains duplicate term {raw!r}")
        seen.add(term)
        result.append(raw.strip())
    return result


def _normalize_ids(value: Any, path: str) -> list[str]:
    if not isinstance(value, list):
        raise RelevanceProfileError(f"{path} must be a list")
    result: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, str) or not raw.strip():
            raise RelevanceProfileError(f"{path}[{index}] must be a non-blank string")
        value = raw.strip()
        value = re.sub(r"^https?://openalex\.org/(?:subfields|topics|fields)/", "", value)
        if not re.fullmatch(r"[A-Za-z0-9]+", value):
            raise RelevanceProfileError(f"{path}[{index}] is not an OpenAlex ID")
        if value in seen:
            raise RelevanceProfileError(f"{path} contains duplicate ID {value!r}")
        seen.add(value)
        result.append(value)
    return result


def _validate_sort(value: Any, *, provider: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RelevanceProfileError(f"{provider}.{field_name} must be non-blank")
    value = value.strip()
    if provider == "openalex":
        allowed = {
            f"{_OPENALEX_RELEVANCE_FIELD}:asc", f"{_OPENALEX_RELEVANCE_FIELD}:desc",
            "cited_by_count:asc", "cited_by_count:desc",
            "publication_date:asc", "publication_date:desc",
        }
        tokens = [token.strip() for token in value.split(",")]
        if any(token not in allowed for token in tokens):
            raise RelevanceProfileError(f"invalid OpenAlex sort: {value!r}")
        if any(token.startswith(f"{_OPENALEX_RELEVANCE_FIELD}:") for token in tokens):
            # All active discovery queries use the search parameter.  The
            # caller still supplies this invariant explicitly to the helper.
            pass
        return ",".join(tokens)
    if provider == "crossref":
        if value not in {"relevance", "published", "cited"}:
            raise RelevanceProfileError(f"invalid Crossref sort: {value!r}")
        return value
    raise RelevanceProfileError(f"unknown provider: {provider!r}")


def _validate_order(value: Any, path: str) -> str:
    if value not in {"asc", "desc"}:
        raise RelevanceProfileError(f"{path} must be asc or desc")
    return str(value)


def _validate_groups(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) < 2:
        raise RelevanceProfileError("anchors.required_groups must contain at least two groups")
    groups: list[dict[str, Any]] = []
    names: set[str] = set()
    all_terms: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise RelevanceProfileError(f"anchors.required_groups[{index}] must be an object")
        if set(raw) != {"name", "terms"}:
            raise RelevanceProfileError(
                f"anchors.required_groups[{index}] must contain exactly name/terms"
            )
        name = normalize_relevance_term(raw["name"])
        if not name or name in names:
            raise RelevanceProfileError("required group names must be unique and non-blank")
        terms = _require_string_list(raw["terms"], f"anchors.required_groups[{index}].terms")
        normalized_terms = [normalize_relevance_term(term) for term in terms]
        conflicting = all_terms.intersection(normalized_terms)
        if conflicting:
            raise RelevanceProfileError(
                f"terms may not occur in multiple required groups: "
                f"{', '.join(sorted(conflicting))} already in "
                f"{', '.join(sorted(names))}"
            )
        all_terms.update(normalized_terms)
        names.add(name)
        groups.append({"name": str(raw["name"]).strip(), "terms": terms})
    return groups


def _canonical_profile_semantics(profile: Mapping[str, Any]) -> dict[str, Any]:
    openalex = profile["openalex"]
    crossref = profile["crossref"]
    anchors = profile["anchors"]
    groups: list[dict[str, Any]] = []
    for group in anchors["required_groups"]:
        groups.append({
            "name": normalize_relevance_term(group["name"]),
            "terms": sorted({normalize_relevance_term(term) for term in group["terms"]}),
        })
    groups.sort(key=lambda group: group["name"])
    return {
        "matcher_schema_version": MATCHER_SCHEMA_VERSION,
        "openalex": {
            "filter_level": str(openalex["filter_level"]),
            "resolved": bool(openalex["resolved"]),
            "filter_ids": sorted(set(openalex["filter_ids"])),
            "refresh_sort": str(openalex["refresh_sort"]),
            "backfill_sort": str(openalex["backfill_sort"]),
        },
        "crossref": {
            "scope_policy": str(crossref["scope_policy"]),
            "refresh_sort": str(crossref["refresh_sort"]),
            "refresh_order": str(crossref["refresh_order"]),
            "backfill_sort": str(crossref["backfill_sort"]),
            "backfill_order": str(crossref["backfill_order"]),
        },
        "anchors": {
            "required_groups": groups,
            "negative_any": sorted({
                normalize_relevance_term(term) for term in anchors["negative_any"]
            }),
            "missing_abstract_policy": str(anchors["missing_abstract_policy"]),
        },
    }


def profile_hash(profile: Mapping[str, Any]) -> str:
    return canonical_sha256(_canonical_profile_semantics(profile))


def validate_relevance_profile(value: Any) -> dict[str, Any]:
    """Validate a fully-resolved active relevance profile.

    An active profile must have resolved taxonomy (resolved=True, non-empty
    filter_ids).  Source definitions that are deliberately unresolved should
    pass through :func:`validate_relevance_profile_source` first, then resolve
    labels, then call this function.
    """
    normalized = validate_relevance_profile_source(value, require_resolved=True)
    openalex = normalized["openalex"]
    if not openalex["resolved"]:
        raise RelevanceProfileError("active relevance profiles must resolve OpenAlex subfields")
    if not openalex["filter_ids"]:
        raise RelevanceProfileError("openalex.filter_ids must contain at least one ID")
    return normalized


def validate_relevance_profile_source(
    value: Any, *, require_resolved: bool = False,
) -> dict[str, Any]:
    """Validate the structure and anchors of a relevance profile definition.

    Unlike :func:`validate_relevance_profile`, this function does not require
    taxonomy resolution: ``resolved`` may be ``False`` and ``filter_ids`` may
    be empty.  Set *require_resolved* to ``True`` to enforce the same
    resolution checks as the active-profile validator (useful as a pre-flight
    before the full ``validate_relevance_profile`` call).

    Returns a normalized dict whose ``profile_hash`` is always present.
    """
    if not isinstance(value, dict):
        raise RelevanceProfileError("relevance_profile must be an object")
    allowed_keys = {
        "schema_version", "matcher_schema_version", "openalex", "crossref",
        "anchors", "profile_hash",
    }
    if not set(value).issubset(allowed_keys) or not {
        "schema_version", "matcher_schema_version", "openalex", "crossref", "anchors",
    }.issubset(value):
        raise RelevanceProfileError(
            "relevance_profile must contain schema/matcher/openalex/crossref/anchors"
        )
    if value["schema_version"] != RELEVANCE_PROFILE_SCHEMA_VERSION:
        raise RelevanceProfileError("unsupported relevance_profile schema_version")
    if value["matcher_schema_version"] != MATCHER_SCHEMA_VERSION:
        raise RelevanceProfileError("unsupported relevance_profile matcher_schema_version")
    openalex = value["openalex"]
    if not isinstance(openalex, dict) or set(openalex) != {
        "filter_level", "resolved", "filter_ids", "filter_labels",
        "refresh_sort", "backfill_sort"
    }:
        raise RelevanceProfileError("invalid relevance_profile.openalex fields")
    if openalex["filter_level"] != "subfield":
        raise RelevanceProfileError("openalex.filter_level must be subfield")
    filter_ids = _normalize_ids(openalex["filter_ids"], "openalex.filter_ids")
    resolved = openalex["resolved"]
    if not isinstance(resolved, bool):
        raise RelevanceProfileError("openalex.resolved must be boolean")
    if require_resolved and not resolved:
        raise RelevanceProfileError("active relevance profiles must resolve OpenAlex subfields")
    if require_resolved and not filter_ids:
        raise RelevanceProfileError("openalex.filter_ids must contain at least one ID")
    if not isinstance(openalex["filter_labels"], list):
        raise RelevanceProfileError("openalex.filter_labels must be a list")
    if filter_ids and len(openalex["filter_labels"]) != len(filter_ids):
        raise RelevanceProfileError("openalex.filter_labels must align with filter_ids")
    labels = [str(label).strip() for label in openalex["filter_labels"]]
    if any(not label for label in labels):
        raise RelevanceProfileError("openalex.filter_labels must be non-blank")
    refresh_sort = _validate_sort(openalex["refresh_sort"], provider="openalex", field_name="refresh_sort")
    backfill_sort = _validate_sort(openalex["backfill_sort"], provider="openalex", field_name="backfill_sort")

    crossref = value["crossref"]
    if not isinstance(crossref, dict) or set(crossref) != {
        "scope_policy", "refresh_sort", "refresh_order", "backfill_sort", "backfill_order"
    }:
        raise RelevanceProfileError("invalid relevance_profile.crossref fields")
    if crossref["scope_policy"] != SCOPE_POLICY_REQUIRE_OPENALEX_SUBFIELD:
        raise RelevanceProfileError("crossref.scope_policy must require OpenAlex subfields")
    crossref_refresh_sort = _validate_sort(crossref["refresh_sort"], provider="crossref", field_name="refresh_sort")
    crossref_backfill_sort = _validate_sort(crossref["backfill_sort"], provider="crossref", field_name="backfill_sort")
    refresh_order = _validate_order(crossref["refresh_order"], "crossref.refresh_order")
    backfill_order = _validate_order(crossref["backfill_order"], "crossref.backfill_order")

    anchors = value["anchors"]
    if not isinstance(anchors, dict) or set(anchors) != {
        "required_groups", "negative_any", "missing_abstract_policy"
    }:
        raise RelevanceProfileError("invalid relevance_profile.anchors fields")
    groups = _validate_groups(anchors["required_groups"])
    if {group["name"] for group in groups} != {"object", "process"}:
        raise RelevanceProfileError("required group names must be exactly object and process")
    if not isinstance(anchors["negative_any"], list):
        raise RelevanceProfileError("anchors.negative_any must be a list")
    negative_any = (
        _require_string_list(anchors["negative_any"], "anchors.negative_any")
        if anchors["negative_any"] else []
    )
    normalized_required = {
        normalize_relevance_term(term)
        for group in groups for term in group["terms"]
    }
    normalized_negative = {normalize_relevance_term(term) for term in negative_any}
    if normalized_required.intersection(normalized_negative):
        raise RelevanceProfileError("required and negative terms may not overlap")
    if anchors["missing_abstract_policy"] != "require_all_groups_in_title":
        raise RelevanceProfileError("unsupported missing_abstract_policy")

    normalized = {
        "schema_version": RELEVANCE_PROFILE_SCHEMA_VERSION,
        "matcher_schema_version": MATCHER_SCHEMA_VERSION,
        "openalex": {
            "filter_level": openalex["filter_level"],
            "resolved": resolved,
            "filter_ids": filter_ids,
            "filter_labels": labels,
            "refresh_sort": refresh_sort,
            "backfill_sort": backfill_sort,
        },
        "crossref": {
            "scope_policy": crossref["scope_policy"],
            "refresh_sort": crossref_refresh_sort,
            "refresh_order": refresh_order,
            "backfill_sort": crossref_backfill_sort,
            "backfill_order": backfill_order,
        },
        "anchors": {
            "required_groups": groups,
            "negative_any": negative_any,
            "missing_abstract_policy": anchors["missing_abstract_policy"],
        },
    }
    calculated_hash = profile_hash(normalized)
    if "profile_hash" in value and value["profile_hash"] != calculated_hash:
        raise RelevanceProfileError("relevance_profile.profile_hash does not match content")
    normalized["profile_hash"] = calculated_hash
    return normalized


def openalex_topic_filter(profile: Mapping[str, Any]) -> str:
    profile = validate_relevance_profile(profile)
    prefix = {
        "field": "topics.field.id",
        "subfield": "topics.subfield.id",
        "topic": "topics.id",
    }[profile["openalex"]["filter_level"]]
    ids = profile["openalex"]["filter_ids"]
    if not ids:
        raise RelevanceProfileError("OpenAlex relevance profile must contain filter IDs")
    return f"{prefix}:{'|'.join(ids)}"


def _ascii_token_pattern(term: str) -> re.Pattern[str] | None:
    if re.fullmatch(r"[a-z0-9]+(?:[ -][a-z0-9]+)*", term):
        parts = re.split(r"[ -]+", term)
        phrase = r"[\s-]+".join(re.escape(part) for part in parts)
        return re.compile(
            r"(?<![A-Za-z0-9_])" + phrase + r"(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
    return None


def _field_texts(candidate: PaperCandidate) -> dict[str, str]:
    raw = candidate.raw if isinstance(candidate.raw, dict) else {}
    keywords: list[str] = []
    for key in ("keywords", "keywords_list", "concepts", "subject", "subjects", "keyword"):
        value = raw.get(key)
        if isinstance(value, list):
            keywords.extend(str(item.get("display_name") if isinstance(item, dict) else item) for item in value)
        elif isinstance(value, str):
            keywords.append(value)
    return {
        "title": _canonical_text(candidate.title),
        "abstract": _canonical_text(candidate.abstract),
        "provider_keywords": _canonical_text(" ".join(keywords)),
    }


def _matches(term: str, text: str) -> bool:
    if not text:
        return False
    pattern = _ascii_token_pattern(term)
    if pattern is not None:
        return bool(pattern.search(text))
    return term in text


def _matches_group(group: Mapping[str, Any], texts: Mapping[str, str], fields: Iterable[str]) -> list[dict[str, str]]:
    evidence: list[dict[str, str]] = []
    for term_value in group["terms"]:
        term = normalize_relevance_term(term_value)
        for field_name in fields:
            if _matches(term, texts.get(field_name, "")):
                evidence.append({"term": term_value, "field": field_name})
                break
    return evidence


def _negative_matches(profile: Mapping[str, Any], texts: Mapping[str, str], matched_groups: Mapping[str, Any]) -> list[dict[str, str]]:
    matches: list[dict[str, str]] = []
    all_groups_in_title = all(
        any(item["field"] == "title" for item in items)
        for items in matched_groups.values()
    )
    for raw_term in profile["anchors"]["negative_any"]:
        term = normalize_relevance_term(raw_term)
        for field_name in ("title", "provider_keywords", "abstract"):
            if not _matches(term, texts.get(field_name, "")):
                continue
            if field_name == "abstract" and all_groups_in_title:
                continue
            matches.append({"term": raw_term, "field": field_name})
            break
    return matches


def _evaluate_anchor_candidate(
    candidate: PaperCandidate,
    profile: Mapping[str, Any],
) -> "RelevanceDecision | None":
    """Evaluate title/abstract/provider-keyword anchors only.

    ``None`` means that all required groups passed and no negative matched.
    Crossref pages use this inexpensive phase before issuing the shared
    OpenAlex DOI batch request, so candidates that fail the anchor gate never
    consume verification capacity.
    """
    texts = _field_texts(candidate)
    abstract_present = bool(texts["abstract"])
    fields = ("title", "abstract", "provider_keywords") if abstract_present else ("title",)
    matched_groups = {
        group["name"]: _matches_group(group, texts, fields)
        for group in profile["anchors"]["required_groups"]
    }
    if not all(matched_groups.values()):
        return RelevanceDecision(
            state="rejected",
            reason=(
                RelevanceReason.MISSING_REQUIRED_GROUP.value
                if abstract_present
                else RelevanceReason.MISSING_REQUIRED_GROUP_MISSING_ABSTRACT.value
            ),
            matched_groups=matched_groups,
        )
    negatives = _negative_matches(profile, texts, matched_groups)
    if negatives:
        return RelevanceDecision(
            state="rejected",
            reason=RelevanceReason.NEGATIVE_TERM_MATCH.value,
            matched_groups=matched_groups,
            negative_matches=negatives,
        )
    return None


@dataclass(frozen=True)
class ScopeVerification:
    status: str
    raw_work: dict[str, Any] | None = None
    error_class: str | None = None
    http_status: int | None = None
    retry_after_seconds: float | None = None


@dataclass(frozen=True)
class ScopeClassification:
    """Single authoritative OpenAlex Work → subfield scope classification.

    Canonical evidence is profile-independent: *subfield_ids* from the
    target profile are not embedded in the evidence hash.  The
    profile-specific match verdict is computed separately from the
    stable evidence.
    """

    verdict: str  # verified | mismatch | invalid
    canonical_evidence: dict[str, Any]
    evidence_hash: str
    invalid_reason: str | None = None
    malformed_types: tuple[str, ...] = ()

    @classmethod
    def classify(
        cls,
        work: dict[str, Any],
        subfield_ids: list[str],
    ) -> "ScopeClassification":
        """The single entry point for both production and frozen verifiers."""
        import re as _re

        canonical_doi = normalize_doi(str(work.get("doi") or ""))
        malformed: list[str] = []

        topics = work.get("topics") if isinstance(work, dict) else None
        if topics is None:
            malformed.append("topics_missing")
            return cls._invalid(
                canonical_doi, "topics_missing", tuple(malformed),
            )
        if not isinstance(topics, list):
            malformed.append("topics_not_list")
            return cls._invalid(
                canonical_doi, "topics_not_list", tuple(malformed),
            )

        wanted = {
            _re.sub(r"^https?://openalex\.org/subfields/", "", str(item))
            for item in subfield_ids
        }
        canonical_topics: list[dict[str, Any]] = []
        canonical_entity_pairs: set[tuple[str, str]] = set()

        for topic in topics:
            if not isinstance(topic, dict):
                malformed.append("topic_not_object")
                continue
            subfield = topic.get("subfield")
            if not isinstance(subfield, dict) or not subfield.get("id"):
                if "subfield_missing" not in malformed:
                    malformed.append("subfield_missing")
                continue
            sid = str(subfield.get("id") or "")
            sid = _re.sub(r"^https?://openalex\.org/subfields/", "", sid)
            if not _re.fullmatch(r"[A-Za-z0-9]+", sid):
                if "subfield_id_type_error" not in malformed:
                    malformed.append("subfield_id_type_error")
                continue
            topic_id = str(topic.get("id") or "")
            pair = (topic_id, sid)
            if pair not in canonical_entity_pairs:
                canonical_entity_pairs.add(pair)
                canonical_topics.append({
                    "id": topic_id,
                    "subfield": {"id": sid},
                })

        if malformed:
            return cls._invalid(
                canonical_doi,
                "multiple_malformed" if len(malformed) > 1 else malformed[0],
                tuple(sorted(set(malformed))),
            )

        # Stable sort for canonical evidence.
        canonical_topics.sort(key=lambda t: (t["id"], t["subfield"]["id"]))
        evidence = {
            "schema_version": "1.0",
            "normalized_doi": canonical_doi,
            "classification_input_state": "valid",
            "topics": canonical_topics,
        }
        evidence_hash = canonical_sha256(evidence)

        # Check subfield match.
        matched = {t["subfield"]["id"] for t in canonical_topics}
        if matched & wanted:
            return cls(
                verdict="verified",
                canonical_evidence=evidence,
                evidence_hash=evidence_hash,
            )
        return cls(
            verdict="mismatch",
            canonical_evidence=evidence,
            evidence_hash=evidence_hash,
        )

    @classmethod
    def _invalid(
        cls, doi: str, reason: str, malformed_types: tuple[str, ...],
    ) -> "ScopeClassification":
        evidence = {
            "schema_version": "1.0",
            "normalized_doi": doi,
            "classification_input_state": reason,
            "topics": None,
        }
        evidence_hash = canonical_sha256(evidence)
        return cls(
            verdict="invalid",
            canonical_evidence=evidence,
            evidence_hash=evidence_hash,
            invalid_reason=reason,
            malformed_types=malformed_types,
        )


@dataclass(frozen=True)
class RelevanceDecision:
    state: str
    reason: str = ""
    matched_groups: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    negative_matches: list[dict[str, str]] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    next_retry_at: str | None = None
    last_error_class: str | None = None
    last_http_status: int | None = None


class ScopeVerifier(Protocol):
    def verify_doi(self, doi: str, subfield_ids: list[str]) -> ScopeVerification:
        ...


def _deferred_decision(verification: ScopeVerification, prior_attempts: int = 0) -> RelevanceDecision:
    delay = min(3600, 2 ** min(10, prior_attempts + 1))
    retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
    return RelevanceDecision(
        state="verification_deferred",
        reason=RelevanceReason.OPENALEX_VERIFICATION_DEFERRED.value,
        verification={"status": verification.status},
        next_retry_at=retry_at,
        last_error_class=verification.error_class,
        last_http_status=verification.http_status,
    )


def evaluate_candidate(
    candidate: PaperCandidate,
    profile: Mapping[str, Any],
    *,
    provider: str,
    scope_verifier: ScopeVerifier | None = None,
    prior_attempts: int = 0,
) -> RelevanceDecision:
    profile = validate_relevance_profile(profile)
    anchor_decision = _evaluate_anchor_candidate(candidate, profile)
    if anchor_decision is not None:
        return anchor_decision
    texts = _field_texts(candidate)
    abstract_present = bool(texts["abstract"])
    fields = ("title", "abstract", "provider_keywords") if abstract_present else ("title",)
    matched_groups = {
        group["name"]: _matches_group(group, texts, fields)
        for group in profile["anchors"]["required_groups"]
    }
    verification: dict[str, Any] = {"status": "not_required"}
    if provider == "openalex":
        raw_topics = candidate.raw.get("topics") if isinstance(candidate.raw, dict) else None
        if not isinstance(raw_topics, list):
            return RelevanceDecision(
                state="candidate_invalid",
                reason=RelevanceReason.OPENALEX_WORK_STRUCTURE_INVALID.value,
                matched_groups=matched_groups,
            )
        wanted = set(profile["openalex"]["filter_ids"])
        matched_subfields: list[str] = []
        for topic in raw_topics:
            if not isinstance(topic, dict):
                return RelevanceDecision(
                    state="candidate_invalid",
                    reason=RelevanceReason.OPENALEX_WORK_STRUCTURE_INVALID.value,
                    matched_groups=matched_groups,
                )
            subfield = topic.get("subfield")
            if not isinstance(subfield, dict) or not subfield.get("id"):
                return RelevanceDecision(
                    state="candidate_invalid",
                    reason=RelevanceReason.OPENALEX_WORK_STRUCTURE_INVALID.value,
                    matched_groups=matched_groups,
                )
            sid = re.sub(r"^https?://openalex\.org/subfields/", "", str(subfield.get("id") or ""))
            if not re.fullmatch(r"[A-Za-z0-9]+", sid):
                return RelevanceDecision(
                    state="candidate_invalid",
                    reason=RelevanceReason.OPENALEX_WORK_STRUCTURE_INVALID.value,
                    matched_groups=matched_groups,
                )
            if sid in wanted:
                matched_subfields.append(sid)
        if not matched_subfields:
            return RelevanceDecision(
                state="rejected",
                reason=RelevanceReason.SUBFIELD_MISMATCH.value,
                matched_groups=matched_groups,
                verification={"status": "mismatch", "required_subfield_ids": list(wanted)},
            )
        verification = {"status": "verified", "matched_subfield_ids": matched_subfields}
    if provider == "crossref":
        doi = normalize_doi(candidate.doi)
        if not doi:
            return RelevanceDecision(
                state="rejected",
                reason=RelevanceReason.OPENALEX_SCOPE_REQUIRES_DOI.value,
                matched_groups=matched_groups,
            )
        if scope_verifier is None:
            raise RelevanceVerificationError("Crossref relevance requires an OpenAlex scope verifier")
        result = scope_verifier.verify_doi(
            doi,
            list(profile["openalex"]["filter_ids"]),
        )
        verification = {
            "status": result.status,
            "normalized_doi": doi,
            "required_subfield_ids": list(profile["openalex"]["filter_ids"]),
        }
        if result.status == "deferred":
            return _deferred_decision(result, prior_attempts)
        if result.status in {"not_found", "mismatch"}:
            return RelevanceDecision(
                state="rejected",
                reason=(
                    RelevanceReason.REJECT_TOPIC_UNVERIFIED.value
                    if result.status == "not_found"
                    else RelevanceReason.SUBFIELD_MISMATCH.value
                ),
                matched_groups=matched_groups,
                verification=verification,
            )
        if result.status != "verified":
            return RelevanceDecision(
                state="candidate_invalid",
                reason=RelevanceReason.OPENALEX_WORK_STRUCTURE_INVALID.value,
                matched_groups=matched_groups,
                verification=verification,
            )
    return RelevanceDecision(
        state="passed",
        reason=RelevanceReason.PROFILE_MATCH.value,
        matched_groups=matched_groups,
        verification=verification,
    )


class _MappedScopeVerifier:
    def __init__(self, values: Mapping[str, ScopeVerification]):
        self.values = values

    def verify_doi(self, doi: str, subfield_ids: list[str]) -> ScopeVerification:
        return self.values.get(normalize_doi(doi), ScopeVerification(status="not_found"))


def evaluate_page_candidates(
    candidates: Iterable[Mapping[str, Any]],
    profile: Mapping[str, Any],
    *,
    provider: str,
    scope_verifier: "OpenAlexDoiVerifier | ScopeVerifier | None" = None,
) -> dict[str, dict[str, Any]]:
    """Evaluate one fetched page without changing candidate lifecycle state.

    Crossref candidates pass the local anchor/negative gate first.  Only the
    survivors are sent to the batched OpenAlex DOI verifier (at most 100 per
    request), and the verifier/cache never receives a profile verdict.
    """
    normalized_profile = validate_relevance_profile(profile)
    materialized = [dict(item) for item in candidates]
    decisions: dict[str, dict[str, Any]] = {}
    pending_scope: list[tuple[str, PaperCandidate, Mapping[str, Any], dict[str, Any]]] = []

    def materialize_decision(
        cid: str,
        decision: RelevanceDecision,
        relevance: Mapping[str, Any],
    ) -> None:
        attempt_count = int(relevance.get("attempt_count") or 0)
        if decision.state == "verification_deferred":
            attempt_count += 1
        decisions[cid] = relevance_record(
            decision,
            profile_hash_value=normalized_profile["profile_hash"],
            attempt_count=attempt_count,
        )

    for item in materialized:
        cid = str(item.get("candidate_id") or "")
        if not cid:
            continue
        relevance = item.get("relevance") if isinstance(item.get("relevance"), dict) else {}
        old_state = str(relevance.get("state") or "profile_unbound")
        # A passed or negative decision is immutable within its generation.
        if old_state in {"passed", "rejected", "candidate_invalid"}:
            continue
        try:
            payload = item.get("candidate")
            if not isinstance(payload, dict):
                raise ValueError("candidate payload is not an object")
            candidate = PaperCandidate.from_dict(payload)
            if provider == "crossref":
                # Anchor filtering is deliberately local and precedes any
                # OpenAlex DOI request.  Missing DOI is a local rejection.
                anchor_decision = _evaluate_anchor_candidate(candidate, normalized_profile)
                if anchor_decision is not None:
                    materialize_decision(cid, anchor_decision, relevance)
                    continue
                if not normalize_doi(candidate.doi):
                    materialize_decision(
                        cid,
                        RelevanceDecision(
                            state="rejected",
                            reason=RelevanceReason.OPENALEX_SCOPE_REQUIRES_DOI.value,
                            matched_groups={
                                group["name"]: _matches_group(
                                    group,
                                    _field_texts(candidate),
                                    ("title", "abstract", "provider_keywords")
                                    if _field_texts(candidate)["abstract"] else ("title",),
                                )
                                for group in normalized_profile["anchors"]["required_groups"]
                            },
                        ),
                        relevance,
                    )
                    continue
                pending_scope.append((cid, candidate, relevance, item))
                continue
            decision = evaluate_candidate(
                candidate,
                normalized_profile,
                provider=provider,
                scope_verifier=scope_verifier,
                prior_attempts=int(relevance.get("attempt_count") or 0),
            )
            materialize_decision(cid, decision, relevance)
        except RelevanceVerificationError:
            raise
        except Exception as exc:
            materialize_decision(
                cid,
                RelevanceDecision(
                state="candidate_invalid",
                reason=RelevanceReason.CANDIDATE_PAYLOAD_INVALID.value,
                verification={"error": type(exc).__name__},
                ),
                relevance,
            )

    if pending_scope:
        if scope_verifier is None:
            raise RelevanceVerificationError("Crossref relevance requires an OpenAlex scope verifier")
        dois = []
        for _cid, candidate, _relevance, _item in pending_scope:
            doi = normalize_doi(candidate.doi)
            if doi and doi not in dois:
                dois.append(doi)
        if hasattr(scope_verifier, "verify_many"):
            mapped = dict(scope_verifier.verify_many(  # type: ignore[attr-defined]
                dois,
                list(normalized_profile["openalex"]["filter_ids"]),
            ))
        else:
            mapped = {
                doi: scope_verifier.verify_doi(  # type: ignore[attr-defined]
                    doi,
                    list(normalized_profile["openalex"]["filter_ids"]),
                )
                for doi in dois
            }
        normalized_mapped: dict[str, ScopeVerification] = {}
        for raw_doi, raw_result in mapped.items():
            doi = normalize_doi(str(raw_doi))
            if not doi:
                continue
            if isinstance(raw_result, ScopeVerification):
                normalized_mapped[doi] = raw_result
            elif isinstance(raw_result, Mapping):
                normalized_mapped[doi] = ScopeVerification(
                    status=str(raw_result.get("status") or "invalid"),
                    error_class=(str(raw_result["error_class"]) if raw_result.get("error_class") else None),
                    http_status=(int(raw_result["http_status"]) if raw_result.get("http_status") is not None else None),
                )
            else:
                normalized_mapped[doi] = ScopeVerification(status="invalid", error_class="invalid_verifier_result")
        if any(
            value.status == "invalid" and value.error_class == "provider_configuration"
            for value in normalized_mapped.values()
        ):
            raise RelevanceVerificationError("OpenAlex verification provider configuration error")
        mapped_verifier = _MappedScopeVerifier(normalized_mapped)
        for cid, candidate, relevance, _item in pending_scope:
            decision = evaluate_candidate(
                candidate,
                normalized_profile,
                provider="crossref",
                scope_verifier=mapped_verifier,
                prior_attempts=int(relevance.get("attempt_count") or 0),
            )
            materialize_decision(cid, decision, relevance)
    return decisions


def relevance_record(
    decision: RelevanceDecision,
    *,
    profile_hash_value: str,
    attempt_count: int = 0,
) -> dict[str, Any]:
    return {
        "state": decision.state,
        "profile_hash": profile_hash_value,
        "matched_groups": decision.matched_groups,
        "negative_matches": decision.negative_matches,
        "reason": decision.reason,
        "verification": decision.verification,
        "attempt_count": int(attempt_count),
        "next_retry_at": decision.next_retry_at,
        "last_attempt_at": datetime.now(timezone.utc).isoformat(),
        "last_error_class": decision.last_error_class,
        "last_http_status": decision.last_http_status,
    }


class RawOpenAlexWorkCache:
    """Small file-backed raw Work cache; never stores profile decisions."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def path_for(self, doi: str) -> Path:
        key = hashlib.sha256(normalize_doi(doi).encode("utf-8")).hexdigest()
        return self.root / f"{key}.json"

    def get(self, doi: str) -> dict[str, Any] | None:
        path = self.path_for(doi)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if normalize_doi(payload.get("normalized_doi")) != normalize_doi(doi):
            return None
        return payload

    def put(self, doi: str, work: Mapping[str, Any]) -> None:
        from src.utils.atomic_io import atomic_write_json
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path_for(doi), {
            "normalized_doi": normalize_doi(doi),
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "work": dict(work),
        }, indent=2)


class OpenAlexDoiVerifier:
    """Verify a DOI against OpenAlex while caching only raw Work evidence.

    ``fetch_batch`` is injectable so unit/integration tests never contact the
    real provider.  It receives normalized DOIs and returns either a mapping
    of DOI to raw Work or a :class:`ScopeVerification` error result.
    """

    def __init__(
        self,
        *,
        cache: RawOpenAlexWorkCache | None = None,
        fetch_batch: Callable[[list[str]], Mapping[str, Any] | ScopeVerification] | None = None,
        client: Any | None = None,
        batch_size: int = 100,
    ) -> None:
        if batch_size < 1 or batch_size > 100:
            raise ValueError("OpenAlex DOI batch_size must be between 1 and 100")
        self.cache = cache
        if fetch_batch is None and client is None:
            raise ValueError(
                "OpenAlexDoiVerifier requires a batch ProviderClient or explicit fetch_batch"
            )
        self.fetch_batch = fetch_batch or (lambda dois: self._fetch_batch_http(dois, client))
        self.batch_size = batch_size

    def verify_doi(self, doi: str, subfield_ids: list[str]) -> ScopeVerification:
        return self.verify_many([doi], subfield_ids).get(
            normalize_doi(doi), ScopeVerification(status="not_found")
        )

    def verify_many(self, dois: list[str], subfield_ids: list[str]) -> dict[str, ScopeVerification]:
        normalized_dois = []
        for doi in dois:
            normalized = normalize_doi(doi)
            if normalized and normalized not in normalized_dois:
                normalized_dois.append(normalized)
        results: dict[str, ScopeVerification] = {}
        missing: list[str] = []
        for normalized in normalized_dois:
            envelope = self.cache.get(normalized) if self.cache else None
            work = envelope.get("work") if isinstance(envelope, dict) else None
            if isinstance(work, dict):
                results[normalized] = self._scope_result(work, subfield_ids)
            else:
                missing.append(normalized)
        for offset in range(0, len(missing), self.batch_size):
            batch = missing[offset:offset + self.batch_size]
            result = self.fetch_batch(batch)
            if isinstance(result, ScopeVerification):
                for normalized in batch:
                    if result.status == "not_found":
                        results[normalized] = ScopeVerification(status="not_found", http_status=result.http_status)
                    else:
                        results[normalized] = result
                continue
            result = {
                normalize_doi(str(key)): value
                for key, value in result.items()
                if normalize_doi(str(key))
            }
            for normalized in batch:
                work = result.get(normalized)
                if work is None:
                    results[normalized] = ScopeVerification(status="not_found")
                    continue
                if not isinstance(work, dict):
                    results[normalized] = ScopeVerification(status="invalid", raw_work=None)
                    continue
                if self.cache:
                    self.cache.put(normalized, work)
                results[normalized] = self._scope_result(work, subfield_ids)
        return results

    @staticmethod
    def _scope_result(work: dict[str, Any], subfield_ids: list[str]) -> ScopeVerification:
        classification = ScopeClassification.classify(work, subfield_ids)
        return ScopeVerification(
            status=classification.verdict,
            raw_work=work,
        )

    def _fetch_batch_http(
        self, dois: list[str], client: Any,
    ) -> Mapping[str, Any] | ScopeVerification:
        from src.discovery.providers.provider_client import RequestSpec
        from src.discovery.providers.provider_errors import (
            ProviderAuthError,
            ProviderError,
            ProviderPermanentError,
            ProviderRequestBudgetExhausted,
        )
        from src.fetch.openalex_credentials import load_openalex_credentials

        credentials = load_openalex_credentials()
        params: dict[str, Any] = {
            "filter": "doi:" + "|".join(f"https://doi.org/{doi}" for doi in dois),
            "per-page": len(dois),
        }
        if credentials.email:
            params["mailto"] = credentials.email
        headers = {"User-Agent": "mineru-literature-library/0.1"}
        if credentials.api_key:
            headers["Authorization"] = f"Bearer {credentials.api_key}"
        spec = RequestSpec(
            provider="openalex",
            purpose="metadata_resolution",
            url="https://api.openalex.org/works",
            params=params,
            headers=headers,
            timeout_seconds=20,
        )
        try:
            outcome = client.execute(spec)
            payload = outcome.json()
        except (ProviderAuthError, ProviderPermanentError) as exc:
            status = getattr(exc, "http_status", None)
            if status == 404:
                return ScopeVerification(status="not_found", http_status=status)
            return ScopeVerification(
                status="invalid", error_class="provider_configuration", http_status=status,
            )
        except ProviderRequestBudgetExhausted:
            # This is a batch-wide clean valve, not a deferred scope result.
            # Let the active lane translate it to BUDGET_STOPPED so no adapter
            # can misreport a budget boundary as a provider failure.
            raise
        except ProviderError as exc:
            # Transient/rate-limit/timeout/connection/protocol: retry later.
            return ScopeVerification(
                status="deferred",
                error_class=type(exc).__name__,
                http_status=getattr(exc, "http_status", None),
            )
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            return ScopeVerification(status="invalid", error_class="invalid_response")
        mapped: dict[str, Any] = {}
        for work in results:
            if not isinstance(work, dict):
                return ScopeVerification(status="invalid", error_class="invalid_work")
            work_doi = normalize_doi(work.get("doi"))
            if work_doi:
                mapped[work_doi] = work
        return mapped

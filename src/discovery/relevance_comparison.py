"""Fetch-once, replay-only relevance comparison over a frozen corpus.

Sampling, request evidence, corpus publication, and corpus verification live
in this module.  Provider scripts only adapt a single page request to a
``DiscoveryPage``; they do not own cursor or budget state.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping

from src.discovery.models import PaperCandidate
from src.utils.identifiers import normalize_doi, normalize_title
from src.discovery.providers.provider_models import DiscoveryPage
from src.discovery.providers.provider_request_evidence import (
    ActualRequestEvidence,
    RequestEvidenceError,
    scan_safe_signature_for_credentials,
)
from src.discovery.relevance import (
    MATCHER_SCHEMA_VERSION,
    RELEVANCE_REASON_VALUES,
    ScopeVerification,
    evaluate_candidate,
    validate_relevance_profile,
)
from src.utils.atomic_io import atomic_replace_bytes


CORPUS_SCHEMA_VERSION = "2.1"
REPLAY_SCHEMA_VERSION = "1.1"
SAMPLING_PROFILE_SCHEMA_VERSION = "1.0"
REPLAY_DISCLOSURE = (
    "Frozen replay 比较 matcher/profile decision；"
    "不直接度量不同 provider request filter/sort 的召回率。"
)
PROVIDER_RELEVANCE_FIELD = "provider_relevance" + "_" + "score"
_VALID_PROVIDERS = frozenset({"openalex", "crossref"})
_VALID_LANES = frozenset({"refresh", "backfill"})
_SUBFIELD_SHORT = re.compile(r"^S(\d+)$")
_SUBFIELD_URI = re.compile(r"^https://openalex\.org/subfields/S(\d+)$")
_IDENTITY_ID = re.compile(r"^[0-9a-f]{16}$")
_OPENALEX_RELEVANCE_SORT = "relevance" + "_" + "score"
_OPENALEX_SORTS = frozenset({
    f"{_OPENALEX_RELEVANCE_SORT}:asc", f"{_OPENALEX_RELEVANCE_SORT}:desc",
    "cited_by_count:asc", "cited_by_count:desc",
    "publication_date:asc", "publication_date:desc",
    "",
})
_CROSSREF_SORTS = frozenset({"relevance", "published", "cited", ""})


def _canonical_subfield_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = _SUBFIELD_SHORT.fullmatch(value) or _SUBFIELD_URI.fullmatch(value)
    if match is None:
        return None
    number = int(match.group(1))
    return f"S{number}"


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_hash(value: Any) -> str:
    return _sha256(_canonical_bytes(value))


def _budget_id(keyword_id: str, provider: str, lane: str, query_id: str) -> str:
    return ":".join((keyword_id, provider, lane, query_id))


@dataclass(frozen=True)
class SamplingBudget:
    keyword_id: str
    provider: str
    lane: str
    query_id: str
    query: str
    target: int
    order: str = ""
    time_window: Mapping[str, str] = dataclasses.field(
        default_factory=lambda: {"from": "", "to": ""}
    )
    # Direct callers may bind an externally meaningful budget identity.  The
    # profile parser leaves this empty and receives the canonical composite
    # identity in ``__post_init__``.
    budget_id: str = ""

    def __post_init__(self) -> None:
        expected = _budget_id(self.keyword_id, self.provider, self.lane, self.query_id)
        if self.budget_id:
            if not isinstance(self.budget_id, str) or not self.budget_id.strip():
                raise ValueError("budget_id must be a non-empty string when supplied")
        else:
            object.__setattr__(self, "budget_id", expected)

    def to_dict(self) -> dict[str, Any]:
        value = {
            "budget_id": self.budget_id,
            "keyword_id": self.keyword_id,
            "provider": self.provider,
            "lane": self.lane,
            "query_id": self.query_id,
            "query": self.query,
            "target": self.target,
            "order": self.order,
        }
        return value


@dataclass(frozen=True)
class SamplingProfile:
    """Strict, normalized sampling profile shared by fetch and verify."""

    subfield_union: tuple[str, ...]
    provider_sort: Mapping[str, Any]
    queries: tuple[str, ...]
    lanes: tuple[str, ...]
    time_window: Mapping[str, str]
    budgets: tuple[SamplingBudget, ...]
    schema_version: str = SAMPLING_PROFILE_SCHEMA_VERSION

    @classmethod
    def parse_and_validate(cls, value: Mapping[str, Any]) -> "SamplingProfile":
        if not isinstance(value, Mapping):
            raise ValueError("sampling_profile must be a mapping")
        violations = _sampling_profile_violations(value)
        if violations:
            raise ValueError("sampling_profile validation failed: " + "; ".join(violations))
        budgets = tuple(_budget_from_mapping(item, value) for item in value["budgets"])
        return cls(
            schema_version=str(value["schema_version"]),
            subfield_union=tuple(
                _canonical_subfield_id(item)  # type: ignore[misc]
                for item in value["subfield_union"]
            ),
            provider_sort=json.loads(json.dumps(value["provider_sort"], ensure_ascii=False)),
            queries=tuple(str(item) for item in value["queries"]),
            lanes=tuple(str(item) for item in value["lanes"]),
            time_window={
                "from": str(value["time_window"]["from"]),
                "to": str(value["time_window"]["to"]),
            },
            budgets=budgets,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "subfield_union": list(self.subfield_union),
            "provider_sort": json.loads(json.dumps(self.provider_sort, ensure_ascii=False)),
            "queries": list(self.queries),
            "lanes": list(self.lanes),
            "time_window": dict(self.time_window),
            "budgets": [
                {
                    "keyword_id": budget.keyword_id,
                    "provider": budget.provider,
                    "lane": budget.lane,
                    "query_id": budget.query_id,
                    "query": budget.query,
                    "target": budget.target,
                    **({"order": budget.order} if budget.provider == "crossref" else {}),
                }
                for budget in self.budgets
            ],
        }


def _budget_from_mapping(value: Mapping[str, Any], profile: Mapping[str, Any]) -> SamplingBudget:
    return SamplingBudget(
        keyword_id=str(value["keyword_id"]),
        provider=str(value["provider"]),
        lane=str(value["lane"]),
        query_id=str(value["query_id"]),
        query=str(value["query"]),
        target=value["target"],
        order=str(value.get("order") or ""),
        time_window=dict(profile["time_window"]),
    )


def _sampling_profile_violations(profile: Mapping[str, Any]) -> list[str]:
    violations: list[str] = []
    required = {
        "schema_version", "subfield_union", "provider_sort", "queries",
        "lanes", "time_window", "budgets",
    }
    unknown = sorted(set(profile) - required)
    missing = sorted(required - set(profile))
    if unknown:
        violations.append(f"unknown top-level fields: {unknown}")
    if missing:
        violations.append(f"missing top-level fields: {missing}")
    if profile.get("schema_version") != SAMPLING_PROFILE_SCHEMA_VERSION:
        violations.append(
            f"unsupported sampling schema_version: {profile.get('schema_version')!r}"
        )

    subfields = profile.get("subfield_union")
    if not isinstance(subfields, list) or not subfields:
        violations.append("subfield_union must be a non-empty list")
    else:
        seen: set[str] = set()
        for index, item in enumerate(subfields):
            canonical = _canonical_subfield_id(item)
            if canonical is None:
                violations.append(f"subfield_union[{index}] is not a valid subfield ID")
                continue
            if canonical in seen:
                violations.append("subfield_union contains duplicate IDs")
            seen.add(canonical)

    queries = profile.get("queries")
    query_values: list[str] = []
    if not isinstance(queries, list) or not queries:
        violations.append("queries must be a non-empty list")
    else:
        seen_queries: set[str] = set()
        for index, item in enumerate(queries):
            if not isinstance(item, str) or not item.strip():
                violations.append(f"queries[{index}] must be a non-empty string")
                continue
            if item in seen_queries:
                violations.append(f"queries contains duplicate value: {item!r}")
            seen_queries.add(item)
            query_values.append(item)

    lanes = profile.get("lanes")
    lane_values: list[str] = []
    if not isinstance(lanes, list) or not lanes:
        violations.append("lanes must be a non-empty list")
    else:
        seen_lanes: set[str] = set()
        for item in lanes:
            if item not in _VALID_LANES:
                violations.append(f"invalid lane: {item!r}")
            if item in seen_lanes:
                violations.append(f"duplicate lane: {item!r}")
            seen_lanes.add(str(item))
            lane_values.append(str(item))

    provider_sort = profile.get("provider_sort")
    if not isinstance(provider_sort, Mapping):
        violations.append("provider_sort must be a mapping")
    else:
        for provider, value in provider_sort.items():
            if provider not in _VALID_PROVIDERS:
                violations.append(f"provider_sort has unknown provider: {provider!r}")
                continue
            if isinstance(value, str):
                allowed_sorts = _OPENALEX_SORTS if provider == "openalex" else _CROSSREF_SORTS
                if value not in allowed_sorts:
                    violations.append(f"provider_sort.{provider} has invalid sort: {value!r}")
                continue
            if not isinstance(value, Mapping):
                violations.append(f"provider_sort.{provider} must be a string or lane mapping")
                continue
            for lane, sort_value in value.items():
                if lane not in _VALID_LANES:
                    violations.append(f"provider_sort.{provider} has unknown lane: {lane!r}")
                elif lane not in lane_values:
                    violations.append(
                        f"provider_sort.{provider}.{lane} is not declared in lanes"
                    )
                if not isinstance(sort_value, str):
                    violations.append(f"provider_sort.{provider}.{lane} must be a string")
                elif sort_value not in (_OPENALEX_SORTS if provider == "openalex" else _CROSSREF_SORTS):
                    violations.append(
                        f"provider_sort.{provider}.{lane} has invalid sort: {sort_value!r}"
                    )

    time_window = profile.get("time_window")
    if not isinstance(time_window, Mapping):
        violations.append("time_window must be a mapping")
    else:
        if set(time_window) != {"from", "to"}:
            violations.append("time_window fields must be exactly from/to")
        for key in ("from", "to"):
            value = time_window.get(key, "")
            if not isinstance(value, str):
                violations.append(f"time_window.{key} must be a string")
            elif value:
                try:
                    date.fromisoformat(value)
                except ValueError:
                    violations.append(f"time_window.{key} must be a valid ISO date")
        if (
            isinstance(time_window.get("from"), str)
            and isinstance(time_window.get("to"), str)
            and time_window.get("from")
            and time_window.get("to")
        ):
            try:
                if date.fromisoformat(time_window["from"]) > date.fromisoformat(time_window["to"]):
                    violations.append("time_window.from must be <= time_window.to")
            except ValueError:
                pass

    budgets = profile.get("budgets")
    allowed_budget_fields = {
        "keyword_id", "provider", "lane", "query_id", "query", "target", "order",
    }
    budget_ids: set[tuple[str, str, str, str]] = set()
    if not isinstance(budgets, list) or not budgets:
        violations.append("budgets must be a non-empty list")
    else:
        for index, budget in enumerate(budgets):
            if not isinstance(budget, Mapping):
                violations.append(f"budget[{index}] must be an object")
                continue
            extra = sorted(set(budget) - allowed_budget_fields)
            if extra:
                violations.append(f"budget[{index}] has unknown fields: {extra}")
            for field_name in ("keyword_id", "provider", "lane", "query_id", "query", "target"):
                if field_name not in budget:
                    violations.append(f"budget[{index}] missing required field: {field_name}")
            provider = budget.get("provider")
            lane = budget.get("lane")
            if provider not in _VALID_PROVIDERS:
                violations.append(f"budget[{index}] has invalid provider: {provider!r}")
            if lane not in _VALID_LANES:
                violations.append(f"budget[{index}] has invalid lane: {lane!r}")
            elif lane not in lane_values:
                violations.append(
                    f"budget[{index}] lane {lane!r} is not declared in profile lanes"
                )
            if provider == "openalex" and "order" in budget:
                violations.append(f"budget[{index}] order is only valid for Crossref")
            if provider == "crossref" and budget.get("order") not in {"asc", "desc"}:
                violations.append(f"budget[{index}] Crossref order must be asc or desc")
            target = budget.get("target")
            if type(target) is not int or target <= 0:
                violations.append(f"budget[{index}].target must be a positive integer")
            query = budget.get("query")
            if query not in query_values:
                violations.append(f"budget[{index}].query is not in queries")
            identity = tuple(str(budget.get(field_name) or "") for field_name in (
                "keyword_id", "provider", "lane", "query_id"
            ))
            if not all(identity):
                violations.append(f"budget[{index}] identity fields must be non-empty")
            for field_name in ("keyword_id", "query_id"):
                field_value = budget.get(field_name)
                if not isinstance(field_value, str) or _IDENTITY_ID.fullmatch(field_value) is None:
                    violations.append(
                        f"budget[{index}].{field_name} must be a lowercase 16-hex ID"
                    )
            if identity in budget_ids:
                violations.append(f"duplicate budget identity: {identity}")
            budget_ids.add(identity)
    return violations


def resolve_provider_sort(provider_sort: Mapping[str, Any], provider: str, lane: str) -> str:
    value = provider_sort.get(provider, "")
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return str(value.get(lane, ""))
    return ""


@dataclass(frozen=True)
class SamplePageRequest:
    budget: SamplingBudget
    cursor: str
    page_size: int
    request_sequence: int
    request_observer: Callable[[ActualRequestEvidence], None] | None = None
    sampling_profile: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def query(self) -> str:
        return self.budget.query

    @property
    def provider(self) -> str:
        return self.budget.provider

    @property
    def lane(self) -> str:
        return self.budget.lane


@dataclass(frozen=True)
class BudgetSampleResult:
    observations: tuple[dict, ...]
    request_evidence: tuple[ActualRequestEvidence, ...]
    raw_actual: int
    unique_actual: int
    duplicate_observation_count: int
    exhausted: bool
    final_cursor: str


def _paper_identity(candidate: Mapping[str, Any], provider: str) -> str:
    source_id = str(candidate.get("source_id") or "").strip()
    doi = normalize_doi(candidate.get("doi") or "")
    if doi:
        identity = {"doi": doi}
    elif source_id:
        identity = {"provider": provider, "source_id": source_id}
    else:
        identity = {
            "provider": provider,
            "title": normalize_title(str(candidate.get("title") or "")),
            "year": candidate.get("year"),
            "authors": [str(item).strip().casefold() for item in candidate.get("authors", [])],
            "venue": normalize_title(str(candidate.get("venue") or "")),
        }
    return _canonical_hash(identity)


def _observation_id(
    budget_id: str, request_sequence: int, provider_rank: int, paper_identity: str
) -> str:
    return _canonical_hash({
        "budget_id": budget_id,
        "request_sequence": request_sequence,
        "provider_rank": provider_rank,
        "paper_identity": paper_identity,
    })


def _normalise_observation(
    value: Any, budget: SamplingBudget, request_sequence: int, rank: int
) -> dict[str, Any]:
    if isinstance(value, PaperCandidate):
        value = {"candidate": value.to_dict()}
    if not isinstance(value, Mapping):
        raise ValueError("provider observation must be an object or PaperCandidate")
    candidate_value = value.get("candidate", value)
    if isinstance(candidate_value, PaperCandidate):
        candidate_value = candidate_value.to_dict()
    if not isinstance(candidate_value, Mapping):
        raise ValueError("provider observation candidate must be an object")
    candidate = PaperCandidate.from_dict(dict(candidate_value)).to_dict()
    supplied_rank = value.get("provider_rank")
    if supplied_rank is not None and supplied_rank != rank:
        raise ValueError("provider observation provider_rank disagrees with global page rank")
    provider_rank = rank
    raw_observation = value.get("provider_observation", candidate.get("raw"))
    if not isinstance(raw_observation, Mapping):
        raw_observation = {}
    relevance_value = (
        value.get(PROVIDER_RELEVANCE_FIELD)
        if value.get(PROVIDER_RELEVANCE_FIELD) is not None
        else raw_observation.get(PROVIDER_RELEVANCE_FIELD) or 0.0
    )
    try:
        provider_score = float(relevance_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("provider relevance score must be numeric") from exc
    if not math.isfinite(provider_score):
        raise ValueError("provider relevance score must be finite")
    paper_identity = _paper_identity(candidate, budget.provider)
    observation_id = (
        f"{request_sequence}-"
        f"{_observation_id(budget.budget_id, request_sequence, provider_rank, paper_identity)}"
    )
    result = {
        "budget_id": budget.budget_id,
        "keyword_id": budget.keyword_id,
        "provider": budget.provider,
        "lane": budget.lane,
        "query_id": budget.query_id,
        "query": budget.query,
        "provider_rank": provider_rank,
        PROVIDER_RELEVANCE_FIELD: provider_score,
        "cited_by_count": int(
            value.get("cited_by_count")
            if value.get("cited_by_count") is not None
            else candidate.get("citation_count") or 0
        ),
        "provider_observation": dict(raw_observation),
        "candidate": candidate,
        "page_identity": {
            "keyword_id": budget.keyword_id,
            "provider": budget.provider,
            "lane": budget.lane,
            "query_id": budget.query_id,
        },
        "paper_identity": paper_identity,
        "observation_id": observation_id,
        "raw_candidate_sha256": _sha256(_canonical_bytes(candidate)),
    }
    return result


def collect_budget_sample(
    budget: SamplingBudget,
    *,
    fetch_page: Callable[[SamplePageRequest], DiscoveryPage],
    sampling_profile: Mapping[str, Any] | None = None,
) -> BudgetSampleResult:
    """Collect one budget until its unique paper target or provider exhaustion."""
    cursor = "*"
    request_sequence = 0
    observations: list[dict[str, Any]] = []
    evidence: list[ActualRequestEvidence] = []
    seen_papers: set[str] = set()
    exhausted = False
    max_pages = 10_000

    while len(seen_papers) < budget.target and not exhausted:
        request_sequence += 1
        if request_sequence > max_pages:
            raise ValueError(f"sampling budget {budget.budget_id} exceeded page safety limit")

        def observe(value: ActualRequestEvidence, sequence: int = request_sequence) -> None:
            if not isinstance(value, ActualRequestEvidence):
                raise RequestEvidenceError("request observer must receive ActualRequestEvidence")
            decorated = dataclasses.replace(
                value,
                budget_id=budget.budget_id,
                request_sequence=sequence,
                semantic_hash="",
                observation_hash="",
                response_blob_sha256=value.response_hash,
                response_blob_path="",
            )
            evidence.append(decorated)

        page_size = min(100, max(1, budget.target - len(seen_papers)))
        request = SamplePageRequest(
            budget=budget,
            cursor=cursor,
            page_size=page_size,
            request_sequence=request_sequence,
            request_observer=observe,
            sampling_profile=dict(sampling_profile or {}),
        )
        page = fetch_page(request)
        if not isinstance(page, DiscoveryPage):
            raise TypeError("sampling fetch_page must return DiscoveryPage")
        if page.status == "failed":
            raise RuntimeError(
                f"provider sampling failed: {budget.provider}/{budget.query_id}"
            )
        if page.provider != budget.provider or page.query != budget.query or page.lane != budget.lane:
            raise ValueError(f"provider page identity disagrees with budget {budget.budget_id}")
        if page.keyword_zh != budget.keyword_id:
            raise ValueError(f"provider page keyword identity disagrees with budget {budget.budget_id}")
        if page.query_id != budget.query_id:
            raise ValueError(f"provider page query_id disagrees with budget {budget.budget_id}")
        if page.request_cursor != cursor:
            raise ValueError(
                f"provider returned mismatched request cursor for {budget.budget_id}"
            )
        if type(page.page_size) is not int or page.page_size != page_size:
            raise ValueError(f"provider page_size disagrees with request for {budget.budget_id}")
        if type(page.returned_count) is not int or page.returned_count != len(page.candidates):
            raise ValueError(f"provider returned_count disagrees with candidates for {budget.budget_id}")
        if len(page.candidates) > page_size:
            raise ValueError(f"provider returned more candidates than page_size for {budget.budget_id}")
        if not isinstance(page.exhausted, bool):
            raise ValueError(f"provider exhausted flag is invalid for {budget.budget_id}")
        if page.next_cursor is not None and not isinstance(page.next_cursor, str):
            raise ValueError(f"provider next cursor is invalid for {budget.budget_id}")
        if not page.exhausted and not page.next_cursor:
            raise ValueError(f"provider page has no cursor but is not exhausted for {budget.budget_id}")
        page_start_rank = len(observations)
        for page_rank, candidate in enumerate(page.candidates, 1):
            # Provider rank is the global order of the frozen provider stream,
            # not the one-based rank inside each independently fetched page.
            item = _normalise_observation(
                candidate, budget, request_sequence, page_start_rank + page_rank
            )
            observations.append(item)
            if item["paper_identity"] in seen_papers:
                continue
            seen_papers.add(item["paper_identity"])
        next_cursor = str(page.next_cursor) if page.next_cursor is not None else ""
        if page.exhausted and next_cursor:
            raise ValueError(
                f"provider page marks {budget.budget_id} exhausted but returned a next cursor"
            )
        if page.exhausted or not next_cursor:
            exhausted = True
            final_cursor = cursor
            break
        if next_cursor == cursor:
            raise ValueError(f"provider cursor did not advance for {budget.budget_id}")
        cursor = next_cursor
        # The target is measured in unique papers, but the cursor in the
        # evidence record is still the provider's next resume cursor when a
        # target is reached before exhaustion.
        if len(seen_papers) >= budget.target:
            final_cursor = cursor
            break
    else:
        final_cursor = cursor

    if not observations:
        final_cursor = cursor
    duplicate_count = len(observations) - len(seen_papers)
    return BudgetSampleResult(
        observations=tuple(observations),
        request_evidence=tuple(evidence),
        raw_actual=len(observations),
        unique_actual=len(seen_papers),
        duplicate_observation_count=duplicate_count,
        exhausted=exhausted,
        final_cursor=final_cursor,
    )


def _synthetic_fixture_sample(
    raw: Iterable[Any], budget: SamplingBudget,
) -> BudgetSampleResult:
    values = list(raw)
    observations = tuple(
        _normalise_observation(value, budget, 1, rank)
        for rank, value in enumerate(values, 1)
    )
    unique = {item["paper_identity"] for item in observations}
    return BudgetSampleResult(
        observations=observations,
        request_evidence=(),
        raw_actual=len(observations),
        unique_actual=len(unique),
        duplicate_observation_count=len(observations) - len(unique),
        exhausted=True,
        final_cursor="*",
    )


def _validate_synthetic_sample(
    sample: BudgetSampleResult, budget: SamplingBudget,
) -> BudgetSampleResult:
    if not isinstance(sample, BudgetSampleResult):
        raise TypeError("synthetic fixture must produce BudgetSampleResult")
    observations = tuple(sample.observations)
    raw_actual = len(observations)
    unique_actual = len({item["paper_identity"] for item in observations})
    if sample.request_evidence:
        raise ValueError("synthetic fixture must not carry request evidence")
    if unique_actual > budget.target:
        raise ValueError(
            f"synthetic fixture exceeds target for {budget.budget_id}: "
            f"unique={unique_actual} target={budget.target}"
        )
    if sample.raw_actual != raw_actual or sample.unique_actual != unique_actual:
        raise ValueError(f"synthetic fixture counts disagree with observations for {budget.budget_id}")
    if sample.duplicate_observation_count != raw_actual - unique_actual:
        raise ValueError(f"synthetic fixture duplicate count is inconsistent for {budget.budget_id}")
    if not isinstance(sample.exhausted, bool) or not isinstance(sample.final_cursor, str):
        raise ValueError(f"synthetic fixture cursor/exhausted fields are invalid for {budget.budget_id}")
    return BudgetSampleResult(
        observations=observations,
        request_evidence=(),
        raw_actual=raw_actual,
        unique_actual=unique_actual,
        duplicate_observation_count=raw_actual - unique_actual,
        exhausted=sample.exhausted,
        final_cursor=sample.final_cursor,
    )


def _collect_synthetic_fixture(
    fetcher: Callable[..., Any], budget: SamplingBudget,
    sampling_profile: Mapping[str, Any],
) -> BudgetSampleResult:
    """Collect a deliberately separate, evidence-free synthetic fixture."""
    raw = fetcher(
        key=budget.to_dict() | {"time_window": dict(sampling_profile["time_window"])},
        sampling_profile=dict(sampling_profile),
    )
    if isinstance(raw, BudgetSampleResult):
        return _validate_synthetic_sample(raw, budget)
    if isinstance(raw, DiscoveryPage):
        if (
            raw.provider != budget.provider
            or raw.keyword_zh != budget.keyword_id
            or raw.query != budget.query
            or raw.query_id != budget.query_id
            or raw.lane != budget.lane
            or raw.returned_count != len(raw.candidates)
            or len(raw.candidates) > raw.page_size
        ):
            raise ValueError(f"synthetic provider page disagrees with budget {budget.budget_id}")
        observations = tuple(
            _normalise_observation(candidate, budget, 1, rank)
            for rank, candidate in enumerate(raw.candidates, 1)
        )
        unique = {item["paper_identity"] for item in observations}
        return _validate_synthetic_sample(BudgetSampleResult(
            observations=observations,
            request_evidence=(),
            raw_actual=len(observations),
            unique_actual=len(unique),
            duplicate_observation_count=len(observations) - len(unique),
            exhausted=bool(raw.exhausted),
            final_cursor=str(raw.next_cursor or raw.request_cursor or "*"),
        ), budget)
    if isinstance(raw, (str, bytes, Mapping)):
        raise TypeError("synthetic fixture fetcher must return an iterable of observations")
    return _validate_synthetic_sample(_synthetic_fixture_sample(raw, budget), budget)


def _budget_report(budget: SamplingBudget, sample: BudgetSampleResult) -> dict[str, Any]:
    return {
        "budget_id": budget.budget_id,
        "keyword_id": budget.keyword_id,
        "provider": budget.provider,
        "lane": budget.lane,
        "query_id": budget.query_id,
        "query": budget.query,
        "target": budget.target,
        "raw_actual": sample.raw_actual,
        "unique_actual": sample.unique_actual,
        "duplicate_observation_count": sample.duplicate_observation_count,
        "sample_budget_incomplete": sample.unique_actual < budget.target,
        "exhausted": sample.exhausted,
        "final_cursor": sample.final_cursor,
    }


_BUDGET_REPORT_FIELDS = frozenset({
    "budget_id", "keyword_id", "provider", "lane", "query_id", "query", "target",
    "raw_actual", "unique_actual", "duplicate_observation_count",
    "sample_budget_incomplete", "exhausted", "final_cursor",
})


def _validate_posix_relative_path(root: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("manifest file path must be a POSIX relative path")
    raw_parts = value.split("/")
    if ".." in raw_parts:
        raise ValueError(f"manifest file path escapes corpus root: {value!r}")
    if any(part in {"", "."} for part in raw_parts):
        raise ValueError(f"manifest file path is not normalized: {value!r}")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError(f"manifest file path escapes corpus root: {value!r}")
    root_abs = Path(root).absolute()
    candidate = root_abs.joinpath(*pure.parts)
    try:
        candidate.relative_to(root_abs)
    except ValueError as exc:
        raise ValueError(f"manifest file path escapes corpus root: {value!r}") from exc
    _assert_no_reparse_component(candidate, label="manifest")
    if not candidate.exists() or not candidate.is_file():
        raise ValueError(f"manifest file is not a regular file: {value!r}")
    if candidate.is_symlink():
        raise ValueError(f"manifest file is a symlink: {value!r}")
    return candidate


def _assert_no_reparse_component(path: Path, *, label: str) -> None:
    """Reject symlink/junction/reparse components without resolving them."""
    absolute = Path(path).absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if not os.path.lexists(str(current)):
            continue
        try:
            info = current.lstat()
        except OSError as exc:
            raise ValueError(f"{label} path component is unreadable: {current}") from exc
        attrs = getattr(info, "st_file_attributes", 0)
        if os.path.islink(current) or bool(
            attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise ValueError(f"{label} path contains symlink/reparse point: {current}")


def _assert_safe_corpus_publish_root(
    output_root: Path, relative_paths: Iterable[str],
) -> None:
    """Preflight every existing component touched by a corpus publication."""
    root = Path(output_root).absolute()
    _assert_no_reparse_component(root, label="corpus output")
    if os.path.lexists(str(root)) and not root.is_dir():
        raise ValueError(f"corpus output root is not a directory: {root}")
    for relative in relative_paths:
        if not isinstance(relative, str) or not relative or "\\" in relative:
            raise ValueError(f"corpus publish path is not POSIX-relative: {relative!r}")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            raise ValueError(f"corpus publish path escapes output root: {relative!r}")
        target = root.joinpath(*pure.parts)
        _assert_no_reparse_component(target, label="corpus output")
        parent = root
        for part in pure.parts[:-1]:
            parent = parent / part
            if os.path.lexists(str(parent)) and not parent.is_dir():
                raise ValueError(f"corpus publish parent is not a directory: {parent}")
        if os.path.lexists(str(target)) and not target.is_file():
            raise ValueError(f"corpus publish target is not a regular file: {target}")
        for sidecar in (
            target.with_suffix(target.suffix + ".lock"),
            target.with_suffix(target.suffix + ".tmp"),
        ):
            _assert_no_reparse_component(sidecar, label="corpus output")
            if os.path.lexists(str(sidecar)) and not sidecar.is_file():
                raise ValueError(f"corpus publish sidecar is not a regular file: {sidecar}")


def _fact(path: str, payload: bytes, records: int) -> dict[str, Any]:
    return {
        "path": path,
        "sha256": _sha256(payload),
        "size": len(payload),
        "record_count": records,
    }


def _jsonl_records(payload: bytes, label: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(payload.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line.decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"{label} line {line_number} is invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{label} line {line_number} must be an object")
        records.append(value)
    return records


def _prepare_evidence(
    values: Iterable[Any],
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    records: list[dict[str, Any]] = []
    blobs: dict[str, bytes] = {}
    seen: set[tuple[str, int]] = set()
    for value in values:
        if isinstance(value, ActualRequestEvidence):
            if value.response_bytes:
                if _sha256(value.response_bytes) != value.response_hash:
                    raise ValueError("request evidence response body hash mismatch")
                path = f"blobs/provider_response/{value.response_hash}.json"
                blobs[path] = bytes(value.response_bytes)
                value = dataclasses.replace(
                    value,
                    response_blob_sha256=value.response_hash,
                    response_blob_path=path,
                    semantic_hash="",
                    observation_hash="",
                )
            record = value.to_dict()
        elif isinstance(value, Mapping):
            record = dict(value)
        else:
            raise ValueError("request_evidence entries must be objects")
        parsed = ActualRequestEvidence.from_dict(record)
        identity = (parsed.budget_id, parsed.request_sequence)
        if identity in seen:
            raise ValueError(f"duplicate request evidence: {identity}")
        seen.add(identity)
        records.append(parsed.to_dict())
    return records, blobs


def _build_manifest(
    *, profile: SamplingProfile, budget_reports: list[dict[str, Any]],
    request_evidence: list[dict[str, Any]], files: dict[str, dict[str, Any]],
    evidence_mode: str,
) -> dict[str, Any]:
    manifest = {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "evidence_mode": evidence_mode,
        "sampling_profile": profile.to_dict(),
        "budgets": budget_reports,
        "budget_results": [
            {
                "budget_id": report["budget_id"],
                "raw_actual": report["raw_actual"],
                "unique_actual": report["unique_actual"],
                "duplicate_observation_count": report["duplicate_observation_count"],
                "sample_budget_incomplete": report["sample_budget_incomplete"],
                "exhausted": report["exhausted"],
                "final_cursor": report["final_cursor"],
            }
            for report in budget_reports
        ],
        "request_evidence": request_evidence,
        "files": files,
    }
    manifest["corpus_hash"] = _canonical_hash(manifest)
    return manifest


def _write_staging_corpus(
    staging: Path, manifest: Mapping[str, Any], blobs: Mapping[str, bytes],
    candidate_payload: bytes, raw_payload: bytes,
) -> None:
    staging.mkdir(parents=True, exist_ok=True)
    for relative, payload in {
        **blobs,
        str(manifest["files"]["candidates"]["path"]): candidate_payload,
        str(manifest["files"]["raw_openalex_works"]["path"]): raw_payload,
    }.items():
        path = staging.joinpath(*PurePosixPath(relative).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    (staging / "corpus_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _publish_corpus_samples(
    *,
    profile: SamplingProfile,
    samples: Iterable[tuple[SamplingBudget, BudgetSampleResult]],
    output_root: Path,
    evidence_mode: str,
) -> dict[str, Any]:
    """Publish already-collected samples after a complete semantic preflight."""
    all_observations: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    captured_evidence: list[Any] = []
    seen_budget_ids: set[str] = set()

    for budget, sample in samples:
        if budget.budget_id in seen_budget_ids:
            raise ValueError(f"duplicate sampling budget: {budget.budget_id}")
        seen_budget_ids.add(budget.budget_id)
        all_observations.extend(sample.observations)
        reports.append(_budget_report(budget, sample))
        captured_evidence.extend(sample.request_evidence)

    if evidence_mode == "synthetic" and captured_evidence:
        raise ValueError("synthetic corpus must not carry actual request evidence")
    evidence_records, response_blobs = _prepare_evidence(captured_evidence) if captured_evidence else ([], {})
    if evidence_mode == "actual" and not evidence_records:
        raise ValueError("actual corpus requires non-empty request_evidence")

    all_observations.sort(key=lambda item: (
        item["budget_id"], item["observation_id"],
    ))
    candidate_payload = b"".join(_canonical_bytes(item) for item in all_observations)
    raw_works: list[dict[str, Any]] = []
    for item in all_observations:
        if item["provider"] != "openalex":
            continue
        raw = item["candidate"].get("raw")
        if isinstance(raw, Mapping):
            raw_works.append({
                "observation_id": item["observation_id"],
                "doi": normalize_doi(item["candidate"].get("doi")),
                "work": dict(raw),
            })
    raw_works.sort(key=lambda item: (item["doi"], item["observation_id"], _canonical_hash(item["work"])))
    raw_payload = b"".join(_canonical_bytes(item) for item in raw_works)
    candidate_path = f"blobs/candidates/{_sha256(candidate_payload)}.jsonl"
    raw_path = f"blobs/openalex_works/{_sha256(raw_payload)}.jsonl"
    manifest = _build_manifest(
        profile=profile,
        budget_reports=reports,
        request_evidence=evidence_records,
        files={
            "candidates": _fact(candidate_path, candidate_payload, len(all_observations)),
            "raw_openalex_works": _fact(raw_path, raw_payload, len(raw_works)),
        },
        evidence_mode=evidence_mode,
    )

    # Verify a complete temporary view before touching the destination.  A
    # failed verification leaves both a new and an existing destination
    # untouched.
    with tempfile.TemporaryDirectory(prefix="mineru-corpus-") as temporary:
        staging = Path(temporary)
        _write_staging_corpus(staging, manifest, response_blobs, candidate_payload, raw_payload)
        verified = verify_corpus(staging)
        if verified.get("corpus_hash") != manifest.get("corpus_hash"):
            raise ValueError("generated corpus verifier returned a different corpus hash")

        publish_paths = [
            *response_blobs,
            candidate_path,
            raw_path,
            "corpus_manifest.json",
        ]
        _assert_safe_corpus_publish_root(output_root, publish_paths)
        output_root.mkdir(parents=True, exist_ok=True)
        for relative, payload in {
            **response_blobs,
            candidate_path: candidate_payload,
            raw_path: raw_payload,
        }.items():
            _assert_safe_corpus_publish_root(output_root, [relative])
            destination = output_root.joinpath(*PurePosixPath(relative).parts)
            atomic_replace_bytes(destination, payload)
        _assert_safe_corpus_publish_root(output_root, ["corpus_manifest.json"])
        atomic_replace_bytes(
            output_root / "corpus_manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    return manifest


def fetch_openalex_corpus(
    *,
    sampling_profile: Mapping[str, Any],
    output_root: Path,
    provider_fetchers: Mapping[str, Callable[[SamplePageRequest], DiscoveryPage]],
) -> dict[str, Any]:
    """Fetch and publish the actual OpenAlex comparison corpus."""
    profile = SamplingProfile.parse_and_validate(sampling_profile)
    if any(budget.provider != "openalex" for budget in profile.budgets):
        raise ValueError("actual_crossref_comparison_not_supported")
    fetcher = provider_fetchers.get("openalex")
    if fetcher is None:
        raise ValueError("no OpenAlex page fetcher was supplied")
    samples = [
        (
            budget,
            collect_budget_sample(
                budget, fetch_page=fetcher, sampling_profile=profile.to_dict()
            ),
        )
        for budget in profile.budgets
    ]
    return _publish_corpus_samples(
        profile=profile, samples=samples, output_root=Path(output_root),
        evidence_mode="actual",
    )


def fetch_synthetic_corpus(
    *,
    sampling_profile: Mapping[str, Any],
    output_root: Path,
    provider_fetchers: Mapping[str, Callable[..., Any]],
) -> dict[str, Any]:
    """Fetch and publish an explicitly synthetic, evidence-free fixture corpus."""
    profile = SamplingProfile.parse_and_validate(sampling_profile)
    samples = []
    for budget in profile.budgets:
        fetcher = provider_fetchers.get(budget.provider)
        if fetcher is None:
            raise ValueError(f"no synthetic fixture fetcher for provider {budget.provider}")
        samples.append((
            budget,
            _collect_synthetic_fixture(fetcher, budget, profile.to_dict()),
        ))
    return _publish_corpus_samples(
        profile=profile, samples=samples, output_root=Path(output_root),
        evidence_mode="synthetic",
    )


def _read_manifest(root: Path) -> dict[str, Any]:
    root = Path(root).absolute()
    _assert_no_reparse_component(root, label="corpus root")
    if not root.is_dir():
        raise ValueError(f"corpus root is not a safe directory: {root}")
    path = root / "corpus_manifest.json"
    _assert_no_reparse_component(path, label="corpus manifest")
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"corpus manifest is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"corpus manifest is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError("corpus manifest must be an object")
    allowed = {
        "schema_version", "evidence_mode", "sampling_profile", "budgets",
        "budget_results", "request_evidence", "files", "corpus_hash",
    }
    unknown = sorted(set(value) - allowed)
    missing = sorted(allowed - set(value))
    if unknown:
        raise ValueError(f"corpus manifest has unknown fields: {unknown}")
    if missing:
        raise ValueError(f"corpus manifest is missing fields: {missing}")
    if value["schema_version"] != CORPUS_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported corpus schema_version: {value['schema_version']!r}; re-fetch the corpus"
        )
    if value["evidence_mode"] not in {"actual", "synthetic"}:
        raise ValueError(f"unknown evidence_mode: {value['evidence_mode']!r}")
    return value


def _verify_files(root: Path, manifest: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    files = manifest["files"]
    if not isinstance(files, Mapping) or set(files) != {"candidates", "raw_openalex_works"}:
        raise ValueError("manifest files must contain exactly candidates/raw_openalex_works")
    payloads: dict[str, list[dict[str, Any]]] = {}
    for name, fact in files.items():
        if not isinstance(fact, Mapping) or set(fact) != {"path", "sha256", "size", "record_count"}:
            raise ValueError(f"file fact for {name} has an invalid field set")
        path = _validate_posix_relative_path(root, fact["path"])
        payload = path.read_bytes()
        if type(fact["size"]) is not int or fact["size"] != len(payload):
            raise ValueError(f"frozen corpus file size drift: {fact['path']}")
        if fact["sha256"] != _sha256(payload):
            raise ValueError(f"frozen corpus file hash drift: {fact['path']}")
        records = _jsonl_records(payload, name)
        if type(fact["record_count"]) is not int or fact["record_count"] != len(records):
            raise ValueError(f"frozen corpus record-count drift: {fact['path']}")
        payloads[name] = records
    return payloads


def _verify_budget_reports(
    profile: SamplingProfile, budgets: Any, budget_results: Any,
) -> dict[str, dict[str, Any]]:
    if not isinstance(budgets, list) or len(budgets) != len(profile.budgets):
        raise ValueError("manifest budgets do not match sampling profile")
    reports: dict[str, dict[str, Any]] = {}
    for index, report in enumerate(budgets):
        if not isinstance(report, Mapping) or set(report) != _BUDGET_REPORT_FIELDS:
            raise ValueError(f"budget report {index} has an invalid field set")
        budget = profile.budgets[index]
        expected_identity = {
            "budget_id": budget.budget_id, "keyword_id": budget.keyword_id,
            "provider": budget.provider, "lane": budget.lane,
            "query_id": budget.query_id, "query": budget.query,
            "target": budget.target,
        }
        if any(report[key] != expected for key, expected in expected_identity.items()):
            raise ValueError(f"budget report {index} identity drift")
        for key in ("raw_actual", "unique_actual", "duplicate_observation_count"):
            if type(report[key]) is not int or report[key] < 0:
                raise ValueError(f"budget report {index}.{key} must be a non-negative integer")
        if report["duplicate_observation_count"] != report["raw_actual"] - report["unique_actual"]:
            raise ValueError(f"budget report {index} duplicate count is inconsistent")
        if report["sample_budget_incomplete"] != (report["unique_actual"] < report["target"]):
            raise ValueError(f"budget report {index} incomplete flag is inconsistent")
        if not isinstance(report["exhausted"], bool) or not isinstance(report["final_cursor"], str):
            raise ValueError(f"budget report {index} cursor/exhausted fields are invalid")
        reports[budget.budget_id] = dict(report)
    if not isinstance(budget_results, list) or len(budget_results) != len(budgets):
        raise ValueError("budget_results do not match budgets")
    expected_results = [
        {
            "budget_id": reports[budget.budget_id]["budget_id"],
            "raw_actual": reports[budget.budget_id]["raw_actual"],
            "unique_actual": reports[budget.budget_id]["unique_actual"],
            "duplicate_observation_count": reports[budget.budget_id]["duplicate_observation_count"],
            "sample_budget_incomplete": reports[budget.budget_id]["sample_budget_incomplete"],
            "exhausted": reports[budget.budget_id]["exhausted"],
            "final_cursor": reports[budget.budget_id]["final_cursor"],
        }
        for budget in profile.budgets
    ]
    if budget_results != expected_results:
        raise ValueError("budget_results do not close over budgets")
    return reports


def _verify_evidence(
    root: Path, manifest: Mapping[str, Any], profile: SamplingProfile,
    reports: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    evidence_mode = manifest["evidence_mode"]
    values = manifest["request_evidence"]
    if not isinstance(values, list):
        raise ValueError("request_evidence must be a list")
    if evidence_mode == "synthetic":
        if values:
            raise ValueError("synthetic corpus must not carry request evidence")
        return {}
    if not values:
        raise ValueError("actual corpus requires non-empty request_evidence")
    by_budget: dict[str, list[dict[str, Any]]] = {}
    for value in values:
        evidence = ActualRequestEvidence.from_dict(value)
        if evidence.budget_id not in reports:
            raise ValueError(f"request evidence references unknown budget: {evidence.budget_id}")
        credentials = scan_safe_signature_for_credentials(evidence.safe_signature)
        if credentials:
            raise ValueError(f"request evidence contains credentials: {credentials}")
        blob = _validate_posix_relative_path(root, evidence.response_blob_path)
        payload = blob.read_bytes()
        if _sha256(payload) != evidence.response_hash:
            raise ValueError(f"response blob hash drift: {evidence.response_blob_path}")
        if evidence.response_blob_sha256 != evidence.response_hash:
            raise ValueError("response blob hash and response_hash differ")
        by_budget.setdefault(evidence.budget_id, []).append(evidence.to_dict())

    budget_by_id = {budget.budget_id: budget for budget in profile.budgets}
    for budget_id, budget in budget_by_id.items():
        entries = by_budget.get(budget_id, [])
        if not entries:
            raise ValueError(f"budget {budget_id} has no request evidence")
        entries.sort(key=lambda item: item["request_sequence"])
        if [item["request_sequence"] for item in entries] != list(range(1, len(entries) + 1)):
            raise ValueError(f"budget {budget_id} request sequence is not consecutive")
        if entries[0]["cursor_in"] != "*":
            raise ValueError(f"budget {budget_id} evidence must start at the initial cursor")
        expected_sort = resolve_provider_sort(profile.provider_sort, budget.provider, budget.lane)
        expected_topic_filter = "topics.subfield.id:" + "|".join(profile.subfield_union)
        expected_filter = expected_topic_filter
        if profile.time_window["from"]:
            expected_filter += ",from_publication_date:" + profile.time_window["from"]
        if profile.time_window["to"]:
            expected_filter += ",to_publication_date:" + profile.time_window["to"]
        for index, entry in enumerate(entries):
            signature = entry["safe_signature"]
            if signature["provider"] != budget.provider or signature["lane"] != budget.lane:
                raise ValueError(f"budget {budget_id} evidence provider/lane mismatch")
            if signature["query"] != budget.query:
                raise ValueError(f"budget {budget_id} evidence query mismatch")
            if signature["sort"] != expected_sort:
                raise ValueError(f"budget {budget_id} evidence sort mismatch")
            if signature["pagination_schema_version"] != "2.0":
                raise ValueError(f"budget {budget_id} pagination schema mismatch")
            if type(signature["page_size"]) is not int or not 0 < signature["page_size"] <= 100:
                raise ValueError(f"budget {budget_id} evidence page size is invalid")
            if signature["time_window"] != dict(profile.time_window):
                raise ValueError(f"budget {budget_id} evidence time window mismatch")
            if budget.provider == "openalex":
                if signature["topic_filter"] != expected_topic_filter or signature["filter"] != expected_filter:
                    raise ValueError(f"budget {budget_id} OpenAlex filter mismatch")
            if budget.provider == "crossref" and signature["order"] != budget.order:
                raise ValueError(f"budget {budget_id} evidence order mismatch")
            if index and (entries[index - 1]["cursor_out"] or "") != (entry["cursor_in"] or ""):
                raise ValueError(f"budget {budget_id} evidence cursor chain is broken")
        total = sum(item["observation_count"] for item in entries)
        if total != reports[budget_id]["raw_actual"]:
            raise ValueError(f"budget {budget_id} evidence observation count mismatch")
        last = entries[-1]
        expected_final = last["cursor_out"] or last["cursor_in"] or "*"
        if reports[budget_id]["final_cursor"] != expected_final:
            raise ValueError(f"budget {budget_id} final cursor mismatch")
        if reports[budget_id]["exhausted"] != (not bool(last["cursor_out"] or "")):
            raise ValueError(f"budget {budget_id} exhausted flag mismatch")
    return by_budget


def _verify_evidence_candidate_counts(
    evidence_by_budget: Mapping[str, list[dict[str, Any]]],
    candidates: Iterable[dict[str, Any]],
) -> None:
    """Close each response's observation count over persisted page records."""
    counts: dict[str, Counter[int]] = {}
    for item in candidates:
        parts = str(item["observation_id"]).split("-", 1)
        if len(parts) != 2 or not parts[0].isdigit():
            raise ValueError("candidate observation sequence is malformed")
        counts.setdefault(item["budget_id"], Counter())[int(parts[0])] += 1
    for budget_id, entries in evidence_by_budget.items():
        expected = {entry["request_sequence"]: entry["observation_count"] for entry in entries}
        actual = dict(counts.get(budget_id, Counter()))
        if actual != expected:
            raise ValueError(f"budget {budget_id} evidence/candidate page counts do not match")


def _observation_sequence(value: Mapping[str, Any]) -> int:
    parts = str(value.get("observation_id") or "").split("-", 1)
    if len(parts) != 2 or not parts[0].isdigit():
        raise ValueError("candidate observation sequence is malformed")
    sequence = int(parts[0])
    if sequence < 1:
        raise ValueError("candidate observation sequence is invalid")
    return sequence


def _parse_provider_response(
    payload: bytes, budget: SamplingBudget,
) -> tuple[list[PaperCandidate], str | None]:
    """Parse one frozen provider response through the provider adapter model."""
    try:
        value = json.loads(payload.decode("utf-8"))
    except Exception as exc:
        raise ValueError(
            f"provider response for {budget.budget_id} is invalid JSON"
        ) from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"provider response for {budget.budget_id} must be an object")

    if budget.provider == "openalex":
        raw_values = value.get("results")
        if not isinstance(raw_values, list):
            raise ValueError(f"OpenAlex response for {budget.budget_id} has no results list")
        from src.discovery.search_openalex import parse_openalex_work

        def parse(item: Any) -> PaperCandidate:
            if not isinstance(item, Mapping):
                raise ValueError(f"OpenAlex response for {budget.budget_id} contains a non-object result")
            return parse_openalex_work(dict(item), query=budget.query)

        meta = value.get("meta")
        if isinstance(meta, Mapping) and "next_cursor" in meta:
            next_cursor = str(meta.get("next_cursor") or "")
        elif "next_cursor" in value:
            next_cursor = str(value.get("next_cursor") or "")
        else:
            next_cursor = None
    else:
        raise ValueError("actual_crossref_comparison_not_supported")
    return [parse(item) for item in raw_values], next_cursor


def _verify_provider_response_semantics(
    root: Path, evidence_by_budget: Mapping[str, list[dict[str, Any]]],
    candidates: Iterable[dict[str, Any]], profile: SamplingProfile,
) -> None:
    """Prove candidates are the parser output of each saved response blob."""
    budget_by_id = {budget.budget_id: budget for budget in profile.budgets}
    by_budget_sequence: dict[str, dict[int, list[dict[str, Any]]]] = {}
    for record in candidates:
        sequence = _observation_sequence(record)
        by_budget_sequence.setdefault(record["budget_id"], {}).setdefault(sequence, []).append(record)

    for budget_id, entries in evidence_by_budget.items():
        budget = budget_by_id[budget_id]
        page_records = by_budget_sequence.get(budget_id, {})
        global_rank = 0
        for entry in sorted(entries, key=lambda item: item["request_sequence"]):
            sequence = int(entry["request_sequence"])
            blob = _validate_posix_relative_path(root, entry["response_blob_path"])
            parsed, response_cursor_out = _parse_provider_response(blob.read_bytes(), budget)
            if len(parsed) != entry["observation_count"]:
                raise ValueError(
                    f"budget {budget_id} response observation count disagrees with parsed response"
                )
            page_size = entry["safe_signature"]["page_size"]
            if len(parsed) > page_size:
                raise ValueError(
                    f"budget {budget_id} response returned more items than page_size"
                )
            if response_cursor_out is not None and response_cursor_out != str(entry["cursor_out"] or ""):
                raise ValueError(f"budget {budget_id} response cursor disagrees with evidence")
            actual_page = page_records.get(sequence, [])
            if len(actual_page) != len(parsed):
                raise ValueError(
                    f"budget {budget_id} candidate page {sequence} count disagrees with response"
                )
            actual_by_id = {item["observation_id"]: item for item in actual_page}
            for page_rank, candidate in enumerate(parsed, 1):
                global_rank += 1
                expected = _normalise_observation(
                    candidate, budget, sequence, global_rank
                )
                actual = actual_by_id.get(expected["observation_id"])
                if actual is None:
                    raise ValueError(
                        f"budget {budget_id} candidate page {sequence} is not derived from response blob"
                    )
                if actual != expected:
                    raise ValueError(
                        f"budget {budget_id} candidate page {sequence} semantic response drift"
                    )
        if set(page_records) != {int(item["request_sequence"]) for item in entries}:
            raise ValueError(f"budget {budget_id} has candidates outside saved response pages")


def _verify_candidates(
    records: list[dict[str, Any]], profile: SamplingProfile,
    reports: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    expected_fields = {
        "budget_id", "keyword_id", "provider", "lane", "query_id", "query",
        "provider_rank", PROVIDER_RELEVANCE_FIELD, "cited_by_count",
        "provider_observation", "candidate", "page_identity", "paper_identity",
        "observation_id", "raw_candidate_sha256",
    }
    budget_by_id = {budget.budget_id: budget for budget in profile.budgets}
    by_budget: dict[str, list[dict[str, Any]]] = {}
    seen_observation_ids: set[str] = set()
    for index, record in enumerate(records, 1):
        if set(record) != expected_fields:
            raise ValueError(f"candidate record {index} has an invalid field set")
        budget_id = record["budget_id"]
        budget = budget_by_id.get(budget_id)
        if budget is None:
            raise ValueError(f"candidate record {index} references unknown budget")
        for key, expected in {
            "keyword_id": budget.keyword_id, "provider": budget.provider,
            "lane": budget.lane, "query_id": budget.query_id, "query": budget.query,
        }.items():
            if record[key] != expected:
                raise ValueError(f"candidate record {index} {key} disagrees with budget")
        page_identity = record["page_identity"]
        if page_identity != {
            "keyword_id": budget.keyword_id,
            "provider": budget.provider,
            "lane": budget.lane,
            "query_id": budget.query_id,
        }:
            raise ValueError(f"candidate record {index} page identity drift")
        rank = record["provider_rank"]
        if type(rank) is not int or rank <= 0:
            raise ValueError(f"candidate record {index} provider_rank is invalid")
        candidate = record["candidate"]
        if not isinstance(candidate, Mapping) or set(candidate) != set(PaperCandidate.__dataclass_fields__):
            raise ValueError(f"candidate record {index} candidate schema drift")
        if candidate.get("doi") != normalize_doi(candidate.get("doi")):
            raise ValueError(f"candidate record {index} DOI is not normalized")
        try:
            normalized_candidate = PaperCandidate.from_dict(dict(candidate)).to_dict()
        except Exception as exc:
            raise ValueError(f"candidate record {index} candidate is invalid") from exc
        if normalized_candidate != dict(candidate):
            raise ValueError(f"candidate record {index} candidate normalization drift")
        if record["raw_candidate_sha256"] != _sha256(_canonical_bytes(candidate)):
            raise ValueError(f"candidate record {index} raw candidate hash mismatch")
        expected_paper = _paper_identity(candidate, budget.provider)
        if record["paper_identity"] != expected_paper:
            raise ValueError(f"candidate record {index} paper identity mismatch")
        expected_observation = _observation_id(
            budget_id,  # request sequence is bound into the persisted ID below
            0,
            rank,
            expected_paper,
        )
        # The sequence is encoded in the observation ID and is recovered from
        # the request evidence/candidate page only by the ID itself.  Generated
        # records use a companion prefix so verifier can recover it without
        # trusting a mutable field.
        parts = str(record["observation_id"]).split("-")
        if len(parts) != 2 or not parts[0].isdigit() or len(parts[1]) != 64:
            raise ValueError(f"candidate record {index} observation_id is malformed")
        sequence = int(parts[0])
        expected_observation = f"{sequence}-{_observation_id(budget_id, sequence, rank, expected_paper)}"
        if record["observation_id"] != expected_observation:
            raise ValueError(f"candidate record {index} observation_id mismatch")
        if record["observation_id"] in seen_observation_ids:
            raise ValueError("observation_id is not globally unique")
        seen_observation_ids.add(record["observation_id"])
        by_budget.setdefault(budget_id, []).append(record)

    for budget in profile.budgets:
        values = by_budget.get(budget.budget_id, [])
        raw_actual = len(values)
        unique_actual = len({item["paper_identity"] for item in values})
        if raw_actual != reports[budget.budget_id]["raw_actual"]:
            raise ValueError(f"budget {budget.budget_id} raw observation count mismatch")
        if unique_actual != reports[budget.budget_id]["unique_actual"]:
            raise ValueError(f"budget {budget.budget_id} unique observation count mismatch")
        if raw_actual - unique_actual != reports[budget.budget_id]["duplicate_observation_count"]:
            raise ValueError(f"budget {budget.budget_id} duplicate observation count mismatch")
    return by_budget


def _verify_raw_works(records: list[dict[str, Any]], candidates: Iterable[dict[str, Any]]) -> None:
    expected_fields = {"observation_id", "doi", "work"}
    all_candidates = list(candidates)
    expected_by_id = {
        item["observation_id"]: item for item in all_candidates
        if item["provider"] == "openalex"
    }
    doi_hashes: dict[str, str] = {}
    seen_ids: set[str] = set()
    for index, record in enumerate(records, 1):
        if set(record) != expected_fields:
            raise ValueError(f"raw OpenAlex work {index} has an invalid field set")
        observation_id = record["observation_id"]
        if not isinstance(observation_id, str) or not observation_id:
            raise ValueError(f"raw OpenAlex work {index} must have an observation relation")
        if observation_id in seen_ids:
            raise ValueError(f"raw OpenAlex work {index} has a duplicate observation relation")
        seen_ids.add(observation_id)
        doi = normalize_doi(record["doi"])
        candidate = expected_by_id.get(observation_id)
        if candidate is None:
            raise ValueError(f"raw OpenAlex work {index} has no candidate relation")
        if record["doi"] != doi:
            raise ValueError(f"raw OpenAlex work {index} DOI is not normalized")
        if not isinstance(record["work"], Mapping):
            raise ValueError(f"raw OpenAlex work {index} work must be an object")
        candidate_doi = normalize_doi(candidate["candidate"].get("doi"))
        if candidate_doi != doi:
            raise ValueError(f"raw OpenAlex work {index} DOI relation mismatch")
        expected_work = candidate["candidate"].get("raw")
        if not isinstance(expected_work, Mapping) or dict(expected_work) != dict(record["work"]):
            raise ValueError(f"raw OpenAlex work {index} content is not closed over candidate evidence")
        if doi:
            fingerprint = _canonical_hash(record["work"])
            previous = doi_hashes.get(doi)
            if previous is not None and previous != fingerprint:
                raise ValueError(f"conflicting raw work evidence for DOI {doi}")
            doi_hashes[doi] = fingerprint
    if seen_ids != set(expected_by_id):
        raise ValueError("raw OpenAlex work set is not closed over OpenAlex candidates")


def verify_corpus(output_root: Path) -> dict[str, Any]:
    """Verify byte integrity and the complete semantic corpus closure."""
    root = Path(output_root)
    manifest = _read_manifest(root)
    stored_hash = manifest["corpus_hash"]
    if not isinstance(stored_hash, str) or len(stored_hash) != 64 or stored_hash != stored_hash.lower():
        raise ValueError("corpus_hash must be a lowercase SHA-256 hex string")
    unsigned = dict(manifest)
    del unsigned["corpus_hash"]
    if _canonical_hash(unsigned) != stored_hash:
        raise ValueError("corpus_hash mismatch")
    profile = SamplingProfile.parse_and_validate(manifest["sampling_profile"])
    if profile.to_dict() != manifest["sampling_profile"]:
        raise ValueError("sampling profile is not canonical")
    if manifest["evidence_mode"] == "actual" and any(
        budget.provider != "openalex" for budget in profile.budgets
    ):
        raise ValueError("actual_crossref_comparison_not_supported")
    reports = _verify_budget_reports(profile, manifest["budgets"], manifest["budget_results"])
    payloads = _verify_files(root, manifest)
    evidence_by_budget = _verify_evidence(root, manifest, profile, reports)
    candidates_by_budget = _verify_candidates(payloads["candidates"], profile, reports)
    all_candidates = [item for values in candidates_by_budget.values() for item in values]
    if manifest["evidence_mode"] == "actual":
        _verify_evidence_candidate_counts(evidence_by_budget, all_candidates)
        _verify_provider_response_semantics(
            root, evidence_by_budget, all_candidates, profile,
        )
    _verify_raw_works(payloads["raw_openalex_works"], all_candidates)
    return manifest


class FrozenScopeVerifier:
    """Answer DOI scope checks exclusively from frozen OpenAlex evidence."""

    def __init__(self, observations: Iterable[Mapping[str, Any]], raw_works: Iterable[Mapping[str, Any]] = ()):
        self._raw_by_doi: dict[str, dict[str, Any]] = {}
        self._evidence_by_doi: dict[str, str] = {}
        from src.discovery.relevance import ScopeClassification
        for item in observations:
            if item.get("provider") != "openalex":
                continue
            doi = normalize_doi(item.get("candidate", {}).get("doi"))
            raw = item.get("candidate", {}).get("raw")
            if doi and isinstance(raw, Mapping):
                self._ingest_doi_evidence(doi, dict(raw))
        for item in raw_works:
            doi = normalize_doi(item.get("doi"))
            work = item.get("work")
            if doi and isinstance(work, Mapping):
                self._ingest_doi_evidence(doi, dict(work))

    def _ingest_doi_evidence(self, doi: str, raw: dict[str, Any]) -> None:
        from src.discovery.relevance import ScopeClassification
        evidence_hash = ScopeClassification.classify(raw, []).evidence_hash
        if doi in self._evidence_by_doi and self._evidence_by_doi[doi] != evidence_hash:
            raise ValueError(f"corpus_evidence_conflict for DOI {doi}")
        self._raw_by_doi[doi] = raw
        self._evidence_by_doi[doi] = evidence_hash

    def verify_doi(self, doi: str, subfield_ids: list[str]) -> ScopeVerification:
        from src.discovery.relevance import ScopeClassification
        raw = self._raw_by_doi.get(normalize_doi(doi))
        if not isinstance(raw, Mapping):
            return ScopeVerification(status="not_found")
        classification = ScopeClassification.classify(raw, subfield_ids)
        return ScopeVerification(status=classification.verdict, raw_work=raw)


def _canonical_profile_groups(groups: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> tuple[dict[str, dict[str, Any]], dict[str, tuple[tuple[str, str], ...]]]:
    normalized: dict[str, dict[str, Any]] = {}
    identities: dict[str, tuple[tuple[str, str], ...]] = {}
    for arm in ("A", "B", "C"):
        values = groups.get(arm)
        if not isinstance(values, Mapping) or not values:
            raise ValueError(f"replay group {arm} is missing or empty")
        mapped: dict[str, Any] = {}
        for keyword_id, profile in values.items():
            normalized_profile = validate_relevance_profile(profile)
            mapped[str(keyword_id)] = normalized_profile
        normalized[arm] = mapped
        identities[arm] = tuple(sorted(
            (keyword_id, str(profile["profile_hash"]))
            for keyword_id, profile in mapped.items()
        ))
    if len({identities["A"], identities["B"], identities["C"]}) != 3:
        raise ValueError("A/B/C canonical profile identities must be distinct")
    if not ({frozenset(normalized[arm]) for arm in ("A", "B", "C")}.__len__() == 1):
        raise ValueError("A/B/C profile keyword key sets are not identical")
    return normalized, identities


_REPLAY_MANIFEST_FIELDS = frozenset({
    "schema_version", "run_id", "corpus_hash", "group_identities",
    "known_doi_set_hash", "result_hash", "matcher_schema_version", "created_at",
    "manifest_hash",
})
_REPLAY_REPORT_FIELDS = frozenset({
    "schema_version", "disclosure", "groups", "pairwise",
    "known_doi_set_hash", "result_hash", "run_id",
})


def _json_roundtrip(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _replay_result_hash(report: Mapping[str, Any], run_manifest: Mapping[str, Any]) -> str:
    return _canonical_hash({
        "schema": report["schema_version"],
        "disclosure": report["disclosure"],
        "groups": report["groups"],
        "pairwise": report["pairwise"],
        "known_doi_set_hash": report["known_doi_set_hash"],
        "corpus_hash": run_manifest["corpus_hash"],
        "matcher_schema": run_manifest["matcher_schema_version"],
        "group_identities": run_manifest["group_identities"],
    })


def _replay_run_id(run_manifest: Mapping[str, Any]) -> str:
    return _canonical_hash({
        "corpus_hash": run_manifest["corpus_hash"],
        "group_identities": run_manifest["group_identities"],
        "known_doi_set_hash": run_manifest["known_doi_set_hash"],
        "matcher_schema": run_manifest["matcher_schema_version"],
        "replay_schema": REPLAY_SCHEMA_VERSION,
    })


def _replay_manifest_hash(run_manifest: Mapping[str, Any]) -> str:
    return _canonical_hash({
        "created_at": run_manifest["created_at"],
        "run_id": run_manifest["run_id"],
        "result_hash": run_manifest["result_hash"],
        "corpus_hash": run_manifest["corpus_hash"],
        "group_identities": run_manifest["group_identities"],
        "known_doi_set_hash": run_manifest["known_doi_set_hash"],
        "matcher_schema_version": run_manifest["matcher_schema_version"],
        "replay_schema_version": run_manifest["schema_version"],
    })


def _validate_replay_run_created_at(value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("replay run created_at must be a non-empty string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("replay run created_at must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("replay run created_at must be timezone-aware")


def _validate_existing_replay_run(
    run_dir: Path, run_id: str, report: Mapping[str, Any],
    run_manifest: Mapping[str, Any],
) -> None:
    if not run_dir.is_dir() or run_dir.is_symlink():
        raise ValueError(f"replay run path is not a safe directory: {run_dir}")
    members = {path.name for path in run_dir.iterdir()}
    if members != {"COMMITTED", "manifest.json", "report.json"}:
        raise ValueError(f"existing replay run has unexpected members: {run_dir}")
    committed = run_dir / "COMMITTED"
    manifest_path = run_dir / "manifest.json"
    report_path = run_dir / "report.json"
    for path in (committed, manifest_path, report_path):
        _assert_no_reparse_component(path, label="replay run")
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"existing replay run member is not a regular file: {path}")
    if committed.read_bytes() != b"\n":
        raise ValueError(f"existing replay run COMMITTED marker is invalid: {run_dir}")
    try:
        existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        existing_report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"existing replay run is corrupt: {run_dir}") from exc
    if not isinstance(existing_manifest, dict) or set(existing_manifest) != _REPLAY_MANIFEST_FIELDS:
        raise ValueError(f"existing replay manifest field set is invalid: {run_dir}")
    if not isinstance(existing_report, dict) or set(existing_report) != _REPLAY_REPORT_FIELDS:
        raise ValueError(f"existing replay report field set is invalid: {run_dir}")
    _validate_replay_run_created_at(existing_manifest["created_at"])
    expected_manifest = {
        key: _json_roundtrip(run_manifest[key])
        for key in _REPLAY_MANIFEST_FIELDS - {"created_at", "manifest_hash"}
    }
    for key, expected in expected_manifest.items():
        if existing_manifest.get(key) != expected:
            raise ValueError(f"existing replay manifest drift: {key}")
    if existing_manifest["manifest_hash"] != _replay_manifest_hash(existing_manifest):
        raise ValueError(f"existing replay manifest_hash is invalid: {run_dir}")
    if existing_report != _json_roundtrip(report):
        raise ValueError(f"existing replay report drift: {run_dir}")
    if existing_report["result_hash"] != _replay_result_hash(existing_report, existing_manifest):
        raise ValueError(f"existing replay report result_hash is invalid: {run_dir}")
    if existing_manifest["run_id"] != _replay_run_id(existing_manifest):
        raise ValueError(f"existing replay run_id is invalid: {run_dir}")
    if existing_manifest["run_id"] != run_id or existing_report["run_id"] != run_id:
        raise ValueError(f"existing replay run identity mismatch: {run_dir}")


def _publish_replay_run(
    corpus_root: Path, run_id: str, report: dict[str, Any],
    run_manifest: dict[str, Any],
) -> None:
    runs_root = corpus_root / "replay_runs"
    _assert_no_reparse_component(runs_root, label="replay runs")
    if runs_root.exists() and (runs_root.is_symlink() or not runs_root.is_dir()):
        raise ValueError(f"replay_runs root is not a safe directory: {runs_root}")
    runs_root.mkdir(parents=True, exist_ok=True)
    run_dir = runs_root / run_id
    _assert_no_reparse_component(run_dir, label="replay run")
    if run_dir.exists():
        _validate_existing_replay_run(run_dir, run_id, report, run_manifest)
        return
    staging = runs_root / f".staging-{uuid.uuid4().hex}"
    _assert_no_reparse_component(staging, label="replay staging")
    staging.mkdir(parents=True, exist_ok=False)
    try:
        atomic_replace_bytes(
            staging / "manifest.json",
            json.dumps(run_manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        atomic_replace_bytes(
            staging / "report.json",
            json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        atomic_replace_bytes(staging / "COMMITTED", b"\n")
        os.replace(staging, run_dir)
    except Exception:
        # The staging directory is not authoritative and can be safely removed
        # only if it is ours; a failed publish never touches an existing run.
        for path in sorted(staging.rglob("*"), reverse=True):
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                path.rmdir()
        staging.rmdir()
        raise


def replay_frozen_corpus(
    *, corpus_root: Path, groups: Mapping[str, Mapping[str, Mapping[str, Any]]],
    known_dois: set[str] | None = None,
) -> dict[str, Any]:
    """Replay three normalized profile arms without provider or staging access."""
    manifest = verify_corpus(corpus_root)
    normalized_groups, group_identities = _canonical_profile_groups(groups)
    lines = Path(corpus_root).joinpath(*PurePosixPath(manifest["files"]["candidates"]["path"]).parts).read_bytes()
    observations = _jsonl_records(lines, "candidates")
    work_lines = Path(corpus_root).joinpath(*PurePosixPath(manifest["files"]["raw_openalex_works"]["path"]).parts).read_bytes()
    verifier = FrozenScopeVerifier(observations, _jsonl_records(work_lines, "raw_openalex_works"))
    corpus_keywords = {item["keyword_id"] for item in observations}
    for arm in ("A", "B", "C"):
        missing = sorted(corpus_keywords - set(normalized_groups[arm]))
        if missing:
            raise ValueError(f"replay arm {arm} is missing profiles for {missing}")

    report: dict[str, Any] = {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "disclosure": REPLAY_DISCLOSURE,
        "groups": {},
    }
    passed_dois: dict[str, set[str]] = {}
    known = {normalize_doi(value) for value in (known_dois or set())}
    for arm in ("A", "B", "C"):
        profiles = normalized_groups[arm]
        results: list[dict[str, Any]] = []
        for item in observations:
            profile = profiles.get(item["keyword_id"])
            if profile is None:
                raise ValueError(f"replay arm {arm} is missing profile for {item['keyword_id']!r}")
            decision = evaluate_candidate(
                PaperCandidate.from_dict(item["candidate"]), profile,
                provider=item["provider"], scope_verifier=verifier,
            )
            value = {**item, "relevance": decision.__dict__}
            if decision.reason not in RELEVANCE_REASON_VALUES:
                raise ValueError(f"unknown_reason: {decision.reason}")
            results.append(value)
        results.sort(key=lambda item: (
            item["provider_rank"], -item[PROVIDER_RELEVANCE_FIELD],
            -item["cited_by_count"], normalize_doi(item["candidate"].get("doi")),
            item["observation_id"],
        ))
        passed = [item for item in results if item["relevance"]["state"] == "passed"]
        dois = {normalize_doi(item["candidate"].get("doi")) for item in passed if normalize_doi(item["candidate"].get("doi"))}
        passed_dois[arm] = dois
        reasons = Counter(item["relevance"]["reason"] for item in results)
        report["groups"][arm] = {
            "candidate_count": len(results),
            "passed": len(passed),
            "reason_distribution": dict(sorted(reasons.items())),
            "unique_passed_dois": len(dois),
            "new_paper_ratio": (len(dois - known) / len(dois)) if known and dois else None,
            "top_50": [{**item, "human_relevant": None, "human_note": ""} for item in passed[:50]],
            "precision_at_50": None,
        }
    report["pairwise"] = {}
    for left, right in (("A", "B"), ("A", "C"), ("B", "C")):
        union = passed_dois[left] | passed_dois[right]
        report["pairwise"][f"{left}_{right}"] = {
            "overlap": len(passed_dois[left] & passed_dois[right]),
            "jaccard": len(passed_dois[left] & passed_dois[right]) / len(union) if union else 1.0,
        }
    known_doi_hash = _canonical_hash(sorted(known))
    report["known_doi_set_hash"] = known_doi_hash
    corpus_hash = manifest["corpus_hash"]
    result_payload = {
        "schema": report["schema_version"],
        "disclosure": report["disclosure"],
        "groups": report["groups"],
        "pairwise": report["pairwise"],
        "known_doi_set_hash": known_doi_hash,
        "corpus_hash": corpus_hash,
        "matcher_schema": MATCHER_SCHEMA_VERSION,
        "group_identities": group_identities,
    }
    report["result_hash"] = _canonical_hash(result_payload)
    run_id = _canonical_hash({
        "corpus_hash": corpus_hash,
        "group_identities": group_identities,
        "known_doi_set_hash": known_doi_hash,
        "matcher_schema": MATCHER_SCHEMA_VERSION,
        "replay_schema": REPLAY_SCHEMA_VERSION,
    })
    run_manifest = {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "run_id": run_id,
        "corpus_hash": corpus_hash,
        "group_identities": group_identities,
        "known_doi_set_hash": known_doi_hash,
        "result_hash": report["result_hash"],
        "matcher_schema_version": MATCHER_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    run_manifest["manifest_hash"] = _replay_manifest_hash(run_manifest)
    # The durable run is authoritative.  Returning the bound identity lets a
    # CLI or caller publish only a pointer at the corpus root.
    report["run_id"] = run_id
    _publish_replay_run(Path(corpus_root), run_id, report, run_manifest)
    return report

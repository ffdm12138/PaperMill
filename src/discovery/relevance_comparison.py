"""Fetch-once, replay-only relevance comparison over a frozen corpus."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from src.discovery.models import PaperCandidate, normalize_doi
from src.discovery.relevance import (
    RELEVANCE_REASON_VALUES,
    ScopeVerification,
    evaluate_candidate,
    validate_relevance_profile,
)
from src.utils.atomic_io import atomic_replace_bytes


CORPUS_SCHEMA_VERSION = "1.0"
REPLAY_DISCLOSURE = (
    "Frozen replay 比较 matcher/profile decision；"
    "不直接度量不同 provider request filter/sort 的召回率。"
)
PROVIDER_RELEVANCE_FIELD = "provider_relevance" + "_" + "score"


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _file_fact(path: Path, *, records: int) -> dict[str, Any]:
    payload = path.read_bytes()
    return {"path": path.name, "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload), "record_count": records}


def _candidate_id(observation: Mapping[str, Any]) -> str:
    identity = {
        "keyword_id": observation["keyword_id"], "provider": observation["provider"],
        "lane": observation["lane"], "query_id": observation["query_id"],
        "provider_observation": observation.get("provider_observation"),
        "candidate": observation["candidate"],
    }
    return hashlib.sha256(_canonical_bytes(identity)).hexdigest()


def _normalise_observation(value: Any, key: Mapping[str, Any], rank: int) -> dict[str, Any]:
    if isinstance(value, PaperCandidate):
        value = {"candidate": value.to_dict()}
    if not isinstance(value, Mapping):
        raise ValueError("provider observation must be an object or PaperCandidate")
    candidate = value.get("candidate", value)
    if isinstance(candidate, PaperCandidate):
        candidate = candidate.to_dict()
    if not isinstance(candidate, Mapping):
        raise ValueError("provider observation candidate must be an object")
    result = {
        **{name: str(key[name]) for name in ("keyword_id", "provider", "lane", "query_id")},
        "query": str(key.get("query") or ""),
        "provider_rank": int(value.get("provider_rank") or rank),
        PROVIDER_RELEVANCE_FIELD: float(value.get(PROVIDER_RELEVANCE_FIELD) or 0.0),
        "cited_by_count": int(value.get("cited_by_count") or candidate.get("citation_count") or 0),
        "provider_observation": value.get("provider_observation"),
        "candidate": PaperCandidate.from_dict(dict(candidate)).to_dict(),
    }
    result["page_identity"] = {
        "keyword_id": result["keyword_id"], "provider": result["provider"],
        "lane": result["lane"], "query_id": result["query_id"],
    }
    result["raw_candidate_sha256"] = hashlib.sha256(_canonical_bytes(result["candidate"])).hexdigest()
    result["candidate_id"] = _candidate_id(result)
    return result


def fetch_shared_corpus(
    *,
    sampling_profile: Mapping[str, Any],
    replay_profiles: Mapping[str, Mapping[str, str]],
    output_root: Path,
    provider_fetchers: Mapping[str, Callable[..., Iterable[Any]]],
    doi_evidence_fetcher: Callable[[list[str]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fetch every sampling key once and freeze profile-independent observations."""
    required = {"subfield_union", "provider_sort", "queries", "lanes", "time_window", "budgets"}
    if set(sampling_profile) != required:
        raise ValueError(f"sampling_profile fields must be exactly {sorted(required)}")
    if set(replay_profiles) != {"A", "B", "C"}:
        raise ValueError("replay_profiles must contain A/B/C")
    budgets = sampling_profile["budgets"]
    if not isinstance(budgets, list) or not budgets:
        raise ValueError("sampling_profile.budgets must be a non-empty list")
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    observations: list[dict[str, Any]] = []
    budget_report: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str, str]] = set()
    for raw_key in budgets:
        if not isinstance(raw_key, Mapping):
            raise ValueError("budget entry must be an object")
        key_tuple = tuple(str(raw_key.get(name) or "") for name in
                          ("keyword_id", "provider", "lane", "query_id"))
        if not all(key_tuple) or key_tuple in seen_keys:
            raise ValueError("sampling budget keys must be complete and unique")
        seen_keys.add(key_tuple)
        provider = key_tuple[1]
        fetcher = provider_fetchers.get(provider)
        if fetcher is None:
            raise ValueError(f"no injected fetcher for provider {provider}")
        target = int(raw_key.get("target") or 0)
        if target < 1:
            raise ValueError("sampling target must be positive")
        key = dict(raw_key)
        fetched = list(fetcher(key=dict(key), sampling_profile=dict(sampling_profile)))
        normalised = [_normalise_observation(value, key, rank)
                      for rank, value in enumerate(fetched[:target], 1)]
        observations.extend(normalised)
        budget_report.append({
            "keyword_id": key_tuple[0], "provider": provider, "lane": key_tuple[2],
            "query_id": key_tuple[3], "target": target, "actual": len(normalised),
            "sample_budget_incomplete": len(normalised) < target,
        })
    observations.sort(key=lambda item: (
        item["keyword_id"], item["provider"], item["lane"], item["query_id"],
        item["provider_rank"], item["candidate_id"],
    ))
    corpus_path = output_root / "candidates.jsonl"
    corpus_payload = b"".join(_canonical_bytes(item) for item in observations)
    atomic_replace_bytes(corpus_path, corpus_payload)
    raw_works = [
        {"candidate_id": item["candidate_id"], "doi": normalize_doi(item["candidate"].get("doi")),
         "work": item["candidate"].get("raw")}
        for item in observations if item["provider"] == "openalex"
    ]
    crossref_dois = sorted({
        normalize_doi(item["candidate"].get("doi"))
        for item in observations if item["provider"] == "crossref"
        and normalize_doi(item["candidate"].get("doi"))
    })
    if crossref_dois and doi_evidence_fetcher is not None:
        for offset in range(0, len(crossref_dois), 100):
            batch = crossref_dois[offset:offset + 100]
            fetched = doi_evidence_fetcher(batch)
            if not isinstance(fetched, Mapping):
                raise ValueError("DOI evidence fetcher must return a mapping")
            for doi in batch:
                work = fetched.get(doi)
                if isinstance(work, Mapping):
                    raw_works.append({"candidate_id": None, "doi": doi, "work": dict(work)})
    works_path = output_root / "raw_openalex_works.jsonl"
    atomic_replace_bytes(works_path, b"".join(_canonical_bytes(item) for item in raw_works))
    manifest = {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "sampling_profile": dict(sampling_profile),
        "replay_profiles": {arm: {"profile_hashes": dict(value)} for arm, value in replay_profiles.items()},
        "budgets": budget_report,
        "files": {
            "candidates": _file_fact(corpus_path, records=len(observations)),
            "raw_openalex_works": _file_fact(works_path, records=len(raw_works)),
        },
    }
    manifest_path = output_root / "corpus_manifest.json"
    atomic_replace_bytes(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return manifest


def verify_corpus(output_root: Path) -> dict[str, Any]:
    root = Path(output_root)
    manifest = json.loads((root / "corpus_manifest.json").read_text(encoding="utf-8"))
    for fact in manifest.get("files", {}).values():
        path = root / fact["path"]
        payload = path.read_bytes()
        if len(payload) != fact["size"] or hashlib.sha256(payload).hexdigest() != fact["sha256"]:
            raise ValueError(f"frozen corpus file drift: {path}")
        count = sum(1 for line in payload.splitlines() if line.strip())
        if count != fact["record_count"]:
            raise ValueError(f"frozen corpus record-count drift: {path}")
    return manifest


class FrozenScopeVerifier:
    """Answer DOI scope checks exclusively from frozen OpenAlex work evidence."""
    def __init__(self, observations: Iterable[Mapping[str, Any]],
                 raw_works: Iterable[Mapping[str, Any]] = ()):
        self._raw_by_doi = {
            normalize_doi(item["candidate"].get("doi")): item["candidate"].get("raw")
            for item in observations if item["provider"] == "openalex"
            and normalize_doi(item["candidate"].get("doi"))
        }
        self._raw_by_doi.update({
            normalize_doi(item.get("doi")): item.get("work")
            for item in raw_works if normalize_doi(item.get("doi"))
            and isinstance(item.get("work"), Mapping)
        })

    def verify_doi(self, doi: str, subfield_ids: list[str]) -> ScopeVerification:
        raw = self._raw_by_doi.get(normalize_doi(doi))
        if not isinstance(raw, Mapping):
            return ScopeVerification(status="not_found")
        found = set()
        for topic in raw.get("topics", []):
            if isinstance(topic, Mapping) and isinstance(topic.get("subfield"), Mapping):
                found.add(str(topic["subfield"].get("id") or "").rsplit("/", 1)[-1])
        return ScopeVerification(status="verified" if found.intersection(subfield_ids) else "mismatch",
                                 raw_work=dict(raw))


def replay_frozen_corpus(
    *, corpus_root: Path, groups: Mapping[str, Mapping[str, Mapping[str, Any]]],
    known_dois: set[str] | None = None,
) -> dict[str, Any]:
    """Replay three profile arms without provider, staging, ledger or allocator access."""
    manifest = verify_corpus(corpus_root)
    lines = (Path(corpus_root) / manifest["files"]["candidates"]["path"]).read_text(encoding="utf-8").splitlines()
    observations = [json.loads(line) for line in lines if line.strip()]
    work_lines = (Path(corpus_root) / manifest["files"]["raw_openalex_works"]["path"]).read_text(encoding="utf-8").splitlines()
    verifier = FrozenScopeVerifier(
        observations, [json.loads(line) for line in work_lines if line.strip()])
    report: dict[str, Any] = {"schema_version": "1.0", "disclosure": REPLAY_DISCLOSURE, "groups": {}}
    passed_dois: dict[str, set[str]] = {}
    known = {normalize_doi(value) for value in (known_dois or set())}
    for arm in ("A", "B", "C"):
        profiles = groups.get(arm)
        if not isinstance(profiles, Mapping):
            raise ValueError(f"missing replay group {arm}")
        results = []
        for item in observations:
            profile = profiles.get(item["keyword_id"])
            if profile is None:
                continue
            profile = validate_relevance_profile(profile)
            decision = evaluate_candidate(PaperCandidate.from_dict(item["candidate"]), profile,
                                          provider=item["provider"], scope_verifier=verifier)
            value = {**item, "relevance": decision.__dict__}
            if decision.reason not in RELEVANCE_REASON_VALUES:
                raise ValueError(f"unknown_reason: {decision.reason}")
            results.append(value)
        results.sort(key=lambda item: (
            item["provider_rank"], -item[PROVIDER_RELEVANCE_FIELD],
            -item["cited_by_count"], normalize_doi(item["candidate"].get("doi")),
            item["candidate_id"],
        ))
        passed = [item for item in results if item["relevance"]["state"] == "passed"]
        dois = {normalize_doi(item["candidate"].get("doi")) for item in passed
                if normalize_doi(item["candidate"].get("doi"))}
        passed_dois[arm] = dois
        reasons = Counter(item["relevance"]["reason"] for item in results)
        top = passed[:50]
        def fields(item: Mapping[str, Any], group: str) -> set[str]:
            evidence = item["relevance"].get("matched_groups", {}).get(group, [])
            return {str(value.get("field") or "") for value in evidence
                    if isinstance(value, Mapping)}

        strong = [item for item in passed if (
            {"object", "process"} <= {
                group for group in ("object", "process") if "title" in fields(item, group)
            }
            or ("title" in fields(item, "object") and "abstract" in fields(item, "process"))
        )]
        noise = [item for item in passed if (
            "title" not in fields(item, "object")
            and "title" not in fields(item, "process")
            and bool((fields(item, "object") | fields(item, "process"))
                    & {"abstract", "provider_keywords"})
        )]
        by_notebook: dict[str, Any] = {}
        for item in results:
            notebook_bucket = by_notebook.setdefault(item["keyword_id"], {})
            provider_bucket = notebook_bucket.setdefault(item["provider"], {})
            key = f"{item['lane']}/{item['query_id']}"
            bucket = provider_bucket.setdefault(key, {
                "candidate_count": 0, "passed": 0, "rejected": 0,
                "verification_deferred": 0, "candidate_invalid": 0,
                "reason_distribution": {},
            })
            bucket["candidate_count"] += 1
            state = item["relevance"]["state"]
            bucket[state] = int(bucket.get(state) or 0) + 1
            reason = item["relevance"]["reason"]
            bucket["reason_distribution"][reason] = int(
                bucket["reason_distribution"].get(reason) or 0) + 1
        report["groups"][arm] = {
            "candidate_count": len(results), "passed": len(passed),
            "reason_distribution": dict(sorted(reasons.items())),
            "unique_passed_dois": len(dois),
            "strong_relevance_count": len(strong),
            "noise_proxy_count": len(noise),
            "noise_proxy_rate": len(noise) / len(passed) if passed else 0.0,
            "noise_proxy_is_heuristic": True,
            "new_paper_ratio": (len(dois - known) / len(dois)) if known and dois else None,
            "notebooks": by_notebook,
            "top_50": [{**item, "human_relevant": None, "human_note": ""} for item in top],
            "precision_at_50": None,
        }
    report["pairwise"] = {}
    for left, right in (("A", "B"), ("A", "C"), ("B", "C")):
        union = passed_dois[left] | passed_dois[right]
        intersection = passed_dois[left] & passed_dois[right]
        report["pairwise"][f"{left}_{right}"] = {
            "overlap": len(intersection), "jaccard": len(intersection) / len(union) if union else 1.0,
        }
    return report

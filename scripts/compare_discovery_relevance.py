"""Fetch once and replay A/B/C discovery relevance profiles in isolation."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.discovery.relevance import validate_relevance_profile  # noqa: E402
from src.discovery.relevance import OpenAlexDoiVerifier, ScopeVerification  # noqa: E402
from src.discovery.keyword_notebook import KeywordNotebookStore  # noqa: E402
from src.discovery.relevance_comparison import (  # noqa: E402,F401
    fetch_shared_corpus,
    replay_frozen_corpus,
)
from src.discovery.resolve_crossref import search_crossref_page  # noqa: E402
from src.discovery.search_openalex import search_openalex_page  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Frozen Discovery relevance comparison")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--fetch", action="store_true")
    modes.add_argument("--replay", action="store_true")
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--notebook-root", type=Path, required=True)
    parser.add_argument("--sampling-config", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--known-dois", type=Path)
    parser.add_argument("--allow-network-fetch", action="store_true")
    return parser


def _load_groups(path: Path) -> dict[str, dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("groups"), dict):
        raw = raw["groups"]
    elif isinstance(raw, dict) and isinstance(raw.get("profiles"), list):
        raise ValueError("profiles must define three distinct groups A/B/C")
    if not isinstance(raw, dict):
        raise ValueError("profiles must contain groups A/B/C")
    result: dict[str, dict[str, Any]] = {}
    identities = []
    for arm in ("A", "B", "C"):
        values = raw.get(arm) or raw.get(arm.lower())
        if isinstance(values, dict) and isinstance(values.get("profiles"), list):
            values = values["profiles"]
        if isinstance(values, dict):
            values = [values]
        if not isinstance(values, list) or not values:
            raise ValueError(f"comparison group {arm} is missing profiles")
        profiles: dict[str, Any] = {}
        keyword_profiles: list[dict[str, Any]] = []
        for item in values:
            if not isinstance(item, dict):
                raise ValueError(f"group {arm} contains a non-object profile")
            keyword = str(item.get("keyword_id") or item.get("keyword_zh") or item.get("keyword") or "").strip()
            profile = item.get("profile") if isinstance(item.get("profile"), dict) else item.get("relevance_profile")
            if not keyword or not isinstance(profile, dict):
                raise ValueError(f"group {arm} profile needs keyword identity and profile")
            profile = validate_relevance_profile(profile)
            profiles[keyword] = profile
            keyword_profiles.append({"keyword_zh": keyword, "profile": profile})
        result[arm] = {"profiles": keyword_profiles, "profile_map": profiles,
                       "ground_truth_relevant_dois": set()}
        identities.append(tuple(sorted((key, value["profile_hash"]) for key, value in profiles.items())))
    if len(set(identities)) != 3:
        raise ValueError("comparison groups A/B/C must be semantically distinct")
    return result


def _group_metrics(results: list[dict[str, Any]], _ground_truth: set[str]) -> dict[str, Any]:
    """Compatibility summary; human labels are never synthesized."""
    top = results[:50]
    passed = [item for item in results if item.get("relevance", {}).get("state") == "passed"]
    reasons = Counter(str(item.get("relevance", {}).get("reason") or "unknown_reason") for item in results)
    return {
        "precision_at_50": None,
        "cross_disciplinary_noise_rate": None,
        "strong_relevance_count": sum(
            all(item.get("relevance", {}).get("matched_groups", {}).get(group)
                for group in ("object", "process")) for item in passed
        ),
        "new_paper_share": None,
        "rejection_reasons": dict(sorted(reasons.items())),
        "top_50": [{**item, "human_relevant": None, "human_note": ""} for item in top],
    }


def _known_dois(path: Path | None) -> set[str]:
    if path is None:
        return set()
    return {line.strip().lower() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()}


def _resolve_group_profile_maps(
    groups: dict[str, dict[str, Any]], notebook_root: Path,
) -> dict[str, dict[str, Any]]:
    """Resolve human notebook labels to the frozen corpus keyword IDs."""
    store = KeywordNotebookStore(notebook_root)
    resolved: dict[str, dict[str, Any]] = {}
    for arm, group in groups.items():
        mapped: dict[str, Any] = {}
        for identity, profile in group["profile_map"].items():
            if len(identity) == 16 and all(ch in "0123456789abcdef" for ch in identity.lower()):
                kid = identity.lower()
            else:
                notebook = store.require_v3(identity)
                kid = str(notebook["keyword_id"])
            mapped[kid] = profile
        resolved[arm] = mapped
    return resolved


def _network_fetcher(provider: str):
    """Build one explicitly authorized fetch lane; called once per sampling key."""
    fetch = search_openalex_page if provider == "openalex" else search_crossref_page

    def run(*, key, sampling_profile):
        target = int(key["target"])
        cursor = str(key.get("cursor") or "*")
        values = []
        while len(values) < target:
            page_size = min(100, target - len(values))
            sort_config = sampling_profile["provider_sort"].get(provider, {})
            sort = sort_config.get(key["lane"]) if isinstance(sort_config, dict) else sort_config
            kwargs = {
                "keyword_zh": str(key.get("keyword_zh") or key["keyword_id"]),
                "query_id": key["query_id"],
                "query_language": str(key.get("query_language") or ""),
                "lane": key["lane"], "page_size": page_size, "cursor": cursor,
                "sort": sort,
            }
            if provider == "openalex":
                ids = [str(value) for value in sampling_profile["subfield_union"]]
                kwargs["topic_filter"] = "topics.subfield.id:" + "|".join(ids)
            else:
                kwargs["order"] = str(key.get("order") or "desc")
            page = fetch(str(key.get("query") or ""), **kwargs)
            if page.status == "failed":
                raise RuntimeError(f"provider sampling failed: {provider}/{key['query_id']}")
            for candidate in page.candidates:
                raw = candidate.raw if isinstance(candidate.raw, dict) else {}
                values.append({
                    "candidate": candidate,
                    "provider_rank": len(values) + 1,
                    "provider_relevance" + "_" + "score": raw.get(
                        "relevance" + "_" + "score") or 0,
                    "cited_by_count": candidate.citation_count or 0,
                    "provider_observation": raw,
                })
            if page.exhausted or not page.next_cursor:
                break
            cursor = page.next_cursor
        return values

    return run


def _network_doi_evidence(dois: list[str]) -> dict[str, Any]:
    result = OpenAlexDoiVerifier(batch_size=100).verify_many(dois, [])
    return {
        doi: value.raw_work for doi, value in result.items()
        if isinstance(value, ScopeVerification) and isinstance(value.raw_work, dict)
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    groups = _load_groups(args.profiles)
    profile_maps = _resolve_group_profile_maps(groups, args.notebook_root)
    sampling = json.loads(args.sampling_config.read_text(encoding="utf-8"))
    if args.fetch:
        if not args.allow_network_fetch:
            raise SystemExit(
                "fetch requires explicit --allow-network-fetch; tests must call "
                "fetch_shared_corpus with injected fake providers"
            )
        replay_profiles = {
            arm: {key: profile["profile_hash"]
                  for key, profile in value.items()}
            for arm, value in profile_maps.items()
        }
        manifest = fetch_shared_corpus(
            sampling_profile=sampling, replay_profiles=replay_profiles,
            output_root=args.output_root,
            provider_fetchers={provider: _network_fetcher(provider)
                               for provider in ("openalex", "crossref")},
            doi_evidence_fetcher=_network_doi_evidence,
        )
        print(f"[CORPUS] {args.output_root / 'corpus_manifest.json'} records="
              f"{manifest['files']['candidates']['record_count']}")
        return 0
    report = replay_frozen_corpus(
        corpus_root=args.output_root,
        groups=profile_maps,
        known_dois=_known_dois(args.known_dois),
    )
    # Parsing the explicit sampling file is part of the replay isolation
    # boundary; it must match the frozen manifest rather than influence replay.
    manifest = json.loads((args.output_root / "corpus_manifest.json").read_text(encoding="utf-8"))
    if sampling != manifest["sampling_profile"]:
        raise SystemExit("sampling config does not match frozen corpus manifest")
    output = args.output_root / "relevance_comparison.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[REPORT] {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Fetch once and replay A/B/C discovery relevance profiles in isolation."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.discovery.relevance import validate_relevance_profile  # noqa: E402
from src.discovery.stores.notebook_store import NotebookStoreV4 as KeywordNotebookStore  # noqa: E402
from src.discovery.relevance_comparison import (  # noqa: E402,F401
    SamplePageRequest,
    fetch_openalex_corpus,
    replay_frozen_corpus,
    resolve_provider_sort,
)
from src.discovery.providers.provider_models import DiscoveryPage  # noqa: E402
from src.discovery.search_openalex import search_openalex_page  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Frozen Discovery relevance comparison")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--fetch", action="store_true")
    modes.add_argument("--replay", action="store_true")
    parser.add_argument("--profiles", type=Path)
    parser.add_argument("--notebook-root", type=Path)
    parser.add_argument("--sampling-config", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--known-dois", type=Path)
    parser.add_argument("--allow-network-fetch", action="store_true")
    return parser


def _validate_mode_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Validate mode-specific options before reading any input file."""
    if args.fetch:
        if not args.allow_network_fetch:
            parser.error("--fetch requires --allow-network-fetch")
        if args.profiles is not None:
            parser.error("--fetch does not accept --profiles")
        if args.notebook_root is not None:
            parser.error("--fetch does not accept --notebook-root")
        if args.known_dois is not None:
            parser.error("--fetch does not accept --known-dois")
        return

    if args.allow_network_fetch:
        parser.error("--replay does not accept --allow-network-fetch")
    missing = [
        option for option, value in (
            ("--profiles", args.profiles),
            ("--notebook-root", args.notebook_root),
        ) if value is None
    ]
    if missing:
        parser.error(f"--replay requires {', '.join(missing)}")


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
        seen_labels: set[str] = set()
        seen_keyword_ids: set[str] = set()
        for item in values:
            if not isinstance(item, dict):
                raise ValueError(f"group {arm} contains a non-object profile")
            keyword = str(item.get("keyword_id") or item.get("keyword_zh") or item.get("keyword") or "").strip()
            profile = item.get("profile") if isinstance(item.get("profile"), dict) else item.get("relevance_profile")
            if not keyword or not isinstance(profile, dict):
                raise ValueError(f"group {arm} profile needs keyword identity and profile")
            # Duplicate label check
            if keyword in seen_labels:
                raise ValueError(
                    f"group {arm}: duplicate keyword label {keyword!r}"
                )
            seen_labels.add(keyword)
            profile = validate_relevance_profile(profile)
            # Duplicate keyword_id check (labels that are already hex IDs)
            if len(keyword) == 16 and all(ch in "0123456789abcdef" for ch in keyword.lower()):
                kid = keyword.lower()
                if kid in seen_keyword_ids:
                    raise ValueError(
                        f"group {arm}: duplicate keyword_id {kid!r}"
                    )
                seen_keyword_ids.add(kid)
            profiles[keyword] = profile
            keyword_profiles.append({"keyword_zh": keyword, "profile": profile})
        result[arm] = {"profiles": keyword_profiles, "profile_map": profiles,
                       "ground_truth_relevant_dois": set()}
        identities.append(tuple(sorted((key, value["profile_hash"]) for key, value in profiles.items())))
    if len(set(identities)) != 3:
        raise ValueError("comparison groups A/B/C must be semantically distinct")
    return result


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
        # Track which labels resolved to each keyword_id for conflict detection
        label_by_kid: dict[str, str] = {}
        for identity, profile in group["profile_map"].items():
            if len(identity) == 16 and all(ch in "0123456789abcdef" for ch in identity.lower()):
                kid = identity.lower()
            else:
                notebook = store.require_v4(identity)
                kid = str(notebook["keyword_id"])
            if kid in label_by_kid:
                existing_label = label_by_kid[kid]
                raise ValueError(
                    f"group {arm}: two different labels resolve to the same "
                    f"keyword_id {kid!r}: {existing_label!r} and {identity!r}"
                )
            label_by_kid[kid] = identity
            mapped[kid] = profile
        resolved[arm] = mapped
    return resolved


def _openalex_page_adapter():
    """Return the actual OpenAlex single-page adapter."""
    fetch = search_openalex_page

    def fetch_page(request: SamplePageRequest) -> DiscoveryPage:
        profile = request.sampling_profile
        time_window = profile.get("time_window") if isinstance(profile, dict) else {}
        time_window = time_window if isinstance(time_window, dict) else {}
        sort = resolve_provider_sort(
            profile.get("provider_sort", {}) if isinstance(profile, dict) else {},
            "openalex",
            request.budget.lane,
        )
        kwargs: dict[str, Any] = {
            "keyword_zh": request.budget.keyword_id,
            "query_id": request.budget.query_id,
            "query_language": "",
            "lane": request.budget.lane,
            "page_size": request.page_size,
            "cursor": request.cursor,
            "sort": sort,
            "request_observer": request.request_observer,
            "from_date": str(time_window.get("from") or ""),
            "to_date": str(time_window.get("to") or ""),
        }
        subfields = profile.get("subfield_union", []) if isinstance(profile, dict) else []
        kwargs["topic_filter"] = "topics.subfield.id:" + "|".join(map(str, subfields))
        return fetch(request.budget.query, **kwargs)

    setattr(fetch_page, "__sample_page_fetcher__", True)
    return fetch_page


def fetch_openalex_sample_page(request: SamplePageRequest) -> DiscoveryPage:
    return _openalex_page_adapter()(request)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    _validate_mode_args(parser, args)
    sampling = json.loads(args.sampling_config.read_text(encoding="utf-8"))
    if args.fetch:
        manifest = fetch_openalex_corpus(
            sampling_profile=sampling,
            output_root=args.output_root,
            provider_fetchers={"openalex": fetch_openalex_sample_page},
        )
        print(f"[CORPUS] {args.output_root / 'corpus_manifest.json'} records="
              f"{manifest['files']['candidates']['record_count']}")
        return 0
    groups = _load_groups(args.profiles)
    profile_maps = _resolve_group_profile_maps(groups, args.notebook_root)
    # Parsing the explicit sampling file is part of the replay isolation
    # boundary; it must match the frozen manifest rather than influence replay.
    manifest = json.loads((args.output_root / "corpus_manifest.json").read_text(encoding="utf-8"))
    if sampling != manifest["sampling_profile"]:
        raise SystemExit("sampling config does not match frozen corpus manifest")
    report = replay_frozen_corpus(
        corpus_root=args.output_root,
        groups=profile_maps,
        known_dois=_known_dois(args.known_dois),
    )
    run_path = (args.output_root / "replay_runs" / report["run_id"]).absolute()
    print(f"Replay committed: {run_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

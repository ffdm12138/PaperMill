"""Read-only discovery planning helpers for CLI entry points.

The CLI selects a schema-v3 notebook by its Chinese classification keyword.
Provider queries are read exclusively from that notebook's active
``search_queries``.  These helpers deliberately do not acquire file locks or
create directories, so ``--dry-run`` can validate and display a plan without
mutating discovery state.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.discovery.keyword_notebook import (
    DiscoveryNotReadyError,
    NotebookCorruptError,
    resolve_existing_notebook,
    validate_discovery_readiness,
    validate_notebook,
)
from src.discovery.relevance import openalex_topic_filter


def _read_notebook(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise NotebookCorruptError(f"notebook JSON corrupt: {path}: {exc}") from exc
    except OSError as exc:
        raise NotebookCorruptError(f"cannot read notebook {path}: {exc}") from exc
    return validate_notebook(raw)


def load_keyword_plan(
    keyword_zh: str,
    notebook_dir: Path,
    *,
    mode: str,
    refresh_pages: int = 2,
    backfill_pages: int = 5,
    max_workers: int = 4,
    max_pages_total: int | None = None,
    max_provider_requests_total: int | None = None,
) -> dict[str, Any]:
    """Return a validated, mutation-free execution plan for one notebook."""
    path = resolve_existing_notebook(keyword_zh, Path(notebook_dir))
    if path is None:
        raise FileNotFoundError(f"no schema-v3 notebook for keyword_zh: {keyword_zh!r}")
    notebook = _read_notebook(path)
    if notebook["keyword_zh"] != keyword_zh.strip():
        raise NotebookCorruptError(
            "selected keyword_zh does not exactly match the notebook classification: "
            f"{keyword_zh!r} != {notebook['keyword_zh']!r}"
        )

    lanes = [mode] if mode in {"refresh", "backfill"} else ["refresh", "backfill"]
    if notebook["enabled"] is False:
        return {
            "keyword_zh": notebook["keyword_zh"],
            "keyword_id": notebook["keyword_id"],
            "enabled": False,
            "status": "disabled_skipped",
            "queries": [],
            "provider_lanes": [],
            "notebook_path": str(path),
        }

    readiness = validate_discovery_readiness(notebook)
    if not readiness.ready:
        raise DiscoveryNotReadyError(
            f"notebook {keyword_zh!r} is not discovery-ready: "
            + "; ".join(readiness.errors)
        )

    queries: list[dict[str, str]] = []
    page_budget = {
        "max_pages_total": max_pages_total,
        "max_provider_requests_total": max_provider_requests_total,
        "refresh_pages_per_lane": refresh_pages,
        "backfill_pages_per_lane": backfill_pages,
    }
    provider_lanes: list[dict[str, Any]] = []
    for query_id, entry in sorted(notebook["search_queries"].items()):
        if entry["active"] is False:
            continue
        query = {
            "query_id": query_id,
            "query": entry["query"],
            "language": entry["language"],
        }
        queries.append(query)
        for provider in ("openalex", "crossref"):
            lane_state = entry["providers"][provider]["backfill"]
            for lane in lanes:
                provider_lanes.append({
                    **query,
                    "provider": provider,
                    "lane": lane,
                    "generation": lane_state["generation"],
                    "request_signature": lane_state["request_signature"],
                    "cursor": lane_state["cursor"],
                    "exhausted": bool(lane_state.get("exhausted")),
                    "refresh_pages": refresh_pages,
                    "backfill_pages": backfill_pages,
                    "worker_count": max_workers,
                    "page_budget": page_budget,
                    "relevance_profile_hash": notebook["relevance_profile"]["profile_hash"],
                    "openalex_filter": (
                        openalex_topic_filter(notebook["relevance_profile"])
                        if provider == "openalex" else None
                    ),
                    "crossref_scope_policy": notebook["relevance_profile"]["crossref"]["scope_policy"],
                })

    return {
        "keyword_zh": notebook["keyword_zh"],
        "keyword_id": notebook["keyword_id"],
        "enabled": True,
        "status": "ready",
        "queries": queries,
        "provider_lanes": provider_lanes,
        "refresh_pages": refresh_pages,
        "backfill_pages": backfill_pages,
        "worker_count": max_workers,
        "page_budget": page_budget,
        "notebook_path": str(path),
        "relevance_profile_hash": notebook["relevance_profile"]["profile_hash"],
    }


def list_enabled_keyword_zh(notebook_dir: Path) -> list[str]:
    """Strictly read every notebook and list enabled Chinese identities.

    A corrupt or legacy file aborts the whole selection.  Silently ignoring it
    could omit a configured classification and is therefore unsafe.
    """
    root = Path(notebook_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"keyword notebook directory not found: {root}")

    keywords: list[str] = []
    seen_ids: set[str] = set()
    seen_keywords: set[str] = set()
    for path in sorted(root.glob("*.json")):
        notebook = _read_notebook(path)
        kid = notebook["keyword_id"]
        keyword = notebook["keyword_zh"]
        folded = keyword.casefold()
        if kid in seen_ids or folded in seen_keywords:
            raise NotebookCorruptError(
                f"duplicate notebook identity while reading {path}: {keyword!r}"
            )
        seen_ids.add(kid)
        seen_keywords.add(folded)
        if notebook["enabled"]:
            keywords.append(keyword)
    return keywords

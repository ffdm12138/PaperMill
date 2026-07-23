"""Notebook config migration from v3 to v4.

Extracts config-only data from v3 notebooks and writes clean v4 notebooks
with reset cursors, exhausted states, and generation counters.

Migration rules:
- KEEP: keyword_zh, enabled, search_queries (query text + language + source),
  relevance_profile, classification
- RESET: cursor=*, exhausted=false, generation=1, all counter fields=0,
  generation_history=[], last_* fields=null
- VERIFY: keyword_id matches canonical computation
- ADD: migration_history entry recording this migration
"""
from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.discovery.constants import INITIAL_CURSOR
from src.discovery.keyword_notebook import (
    _active_queries,
    keyword_id as compute_keyword_id,
    validate_discovery_readiness,
    validate_notebook,
)

V4_NOTEBOOK_SCHEMA = "4.0"


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
        "exhaustion_evidence": None,
        "repair_required": False,
        "repair_reason": None,
        "repair_flagged_at": None,
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


def extract_notebook_config(v3_notebook: dict[str, Any]) -> dict[str, Any]:
    """Extract config-only fields from a v3 notebook for v4 migration.

    Args:
        v3_notebook: A validated v3 notebook dict.

    Returns:
        A clean v4 notebook dict with reset cursors and generation counters.

    Raises:
        ValueError: If keyword_id does not match canonical computation.
    """
    validate_notebook(v3_notebook)  # Ensure v3 notebook is valid

    keyword_zh = v3_notebook["keyword_zh"]
    canon_id = compute_keyword_id(keyword_zh)
    if v3_notebook["keyword_id"] != canon_id:
        raise ValueError(
            f"keyword_id mismatch for {keyword_zh!r}: "
            f"notebook has {v3_notebook['keyword_id']}, "
            f"canonical is {canon_id}"
        )

    now = datetime.now(timezone.utc).isoformat()

    # Rebuild search_queries: keep query identity and config, reset providers
    clean_queries: dict[str, dict[str, Any]] = {}
    for qid, entry in sorted(v3_notebook["search_queries"].items()):
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

    # Always copy the relevance_profile — it is config
    relevance_profile = copy.deepcopy(v3_notebook.get("relevance_profile", {}))

    v4 = {
        "schema_version": V4_NOTEBOOK_SCHEMA,
        "keyword_id": canon_id,
        "keyword_zh": keyword_zh,
        "normalized_keyword_zh": v3_notebook.get("normalized_keyword_zh", keyword_zh),
        "enabled": v3_notebook.get("enabled", True),
        "classification": copy.deepcopy(v3_notebook.get("classification", {})),
        "search_queries": clean_queries,
        "relevance_profile": relevance_profile,
        "relevance_generation": 1,
        "definition_history": copy.deepcopy(v3_notebook.get("definition_history", [])),
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
        "migration_history": copy.deepcopy(v3_notebook.get("migration_history", []))
        + [{
            "from_schema": v3_notebook.get("schema_version", "3.0"),
            "to_schema": V4_NOTEBOOK_SCHEMA,
            "migrated_at": now,
            "reason": "discovery_v4_one_time_migration",
        }],
        "created_at": v3_notebook.get("created_at", now),
        "updated_at": now,
    }

    return v4


def migrate_all_notebooks(
    notebook_dir: Path,
    output_dir: Path,
) -> list[dict[str, Any]]:
    """Migrate all v3 notebooks to v4 and write to output directory.

    Args:
        notebook_dir: Source directory with v3 notebook JSON files.
        output_dir: Destination for v4 notebook JSON files.

    Returns:
        List of migration results per notebook.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    for path in sorted(Path(notebook_dir).glob("*.json")):
        entry: dict[str, Any] = {
            "source": str(path),
            "keyword_zh": "unknown",
            "keyword_id": "unknown",
            "success": False,
            "error": None,
        }

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            validate_notebook(raw)
            entry["keyword_zh"] = raw["keyword_zh"]
            entry["keyword_id"] = raw["keyword_id"]

            # Validate readiness before migration
            readiness = validate_discovery_readiness(raw)
            if not readiness.ready:
                entry["error"] = f"not discovery-ready: {'; '.join(readiness.errors)}"
                results.append(entry)
                continue

            v4 = extract_notebook_config(raw)

            # Write v4 notebook — use same filename convention
            keyword_zh = raw["keyword_zh"]
            kid = raw["keyword_id"]
            fp8 = kid[:8]
            out_path = output_dir / f"{keyword_zh}__{fp8}.json"

            payload = json.dumps(v4, ensure_ascii=False, indent=2)
            raw_bytes = payload.encode("utf-8")
            tmp = out_path.with_suffix(out_path.suffix + ".tmp")
            try:
                with tmp.open("wb") as fh:
                    fh.write(raw_bytes)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(str(tmp), str(out_path))
            except Exception:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise

            # Count active queries for report (from source, not v4)
            active_queries = list(_active_queries(raw))
            entry.update({
                "success": True,
                "output": str(out_path),
                "active_queries": len(active_queries),
                "lane_count": len(active_queries) * 2 * 2,  # queries × providers × modes
            })
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"

        results.append(entry)

    return results

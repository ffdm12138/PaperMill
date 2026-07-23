#!/usr/bin/env python
"""Read-only legacy inventory for Discovery v2/v3 runtime state.

Walks ``data/discovery/pending_pages/`` and ``data/discovery/keyword_notebooks/``
producing a complete inventory report with per-file schema versions, keyword
identities, provider/lane assignments, hashes, and sizes.

This module is read-only — it never moves, deletes, or rewrites files.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _sha256_hex(path: Path) -> str:
    """Stream SHA-256 of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)  # 1 MiB chunks
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _try_read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Attempt to read a file as JSON dict. Returns (data, error)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:
        return None, f"read_error:{type(exc).__name__}:{exc}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"json_decode_error:{exc}"
    if not isinstance(data, dict):
        return None, "not_a_dict"
    return data, None


def inventory_pending_pages(root: Path) -> dict[str, Any]:
    """Inventory every file under pending_pages.

    Returns a report with per-schema counts, per-keyword/per-provider breakdowns,
    and a summary suitable for migration planning.
    """
    if not root.exists():
        return {"root": str(root), "exists": False, "files": [], "counts": {}}

    files: list[dict[str, Any]] = []
    by_schema: dict[str, int] = {}
    by_keyword: dict[str, int] = {}
    by_provider: dict[str, int] = {}
    by_lane: dict[str, int] = {}
    corrupt_count = 0
    not_dict_count = 0
    total_size = 0

    for path in sorted(root.rglob("*.json")):
        size = path.stat().st_size
        total_size += size
        sha = _sha256_hex(path)
        data, error = _try_read_json(path)

        entry: dict[str, Any] = {
            "path": str(path.relative_to(root.parent)),
            "size": size,
            "sha256": sha,
        }

        if error:
            corrupt_count += 1
            entry["error"] = error
            entry["schema_version"] = "unknown"
            files.append(entry)
            continue

        if data is None:
            not_dict_count += 1
            entry["error"] = "not_a_dict"
            entry["schema_version"] = "unknown"
            files.append(entry)
            continue

        entry["schema_version"] = data.get("schema_version", "missing")
        entry["keyword_id"] = data.get("keyword_id", "missing")
        entry["keyword_zh"] = data.get("keyword_zh", "missing")
        entry["provider"] = data.get("provider", "missing")
        entry["lane"] = data.get("lane", "missing")
        entry["query_id"] = data.get("query_id", "missing")
        entry["state"] = data.get("state", "missing")
        entry["candidate_count"] = len(data.get("candidates", []))

        schema = entry["schema_version"]
        by_schema[schema] = by_schema.get(schema, 0) + 1
        kw = str(entry["keyword_zh"])
        by_keyword[kw] = by_keyword.get(kw, 0) + 1
        prov = str(entry["provider"])
        by_provider[prov] = by_provider.get(prov, 0) + 1
        lane = str(entry["lane"])
        by_lane[lane] = by_lane.get(lane, 0) + 1

        files.append(entry)

    return {
        "root": str(root),
        "exists": True,
        "total_files": len(files),
        "total_size_bytes": total_size,
        "total_size_mb": round(total_size / (1024 * 1024), 2),
        "corrupt_json": corrupt_count,
        "not_dict": not_dict_count,
        "by_schema_version": by_schema,
        "by_keyword": by_keyword,
        "by_provider": by_provider,
        "by_lane": by_lane,
        "files": files,
    }


def inventory_keyword_notebooks(root: Path) -> dict[str, Any]:
    """Inventory every notebook file."""
    if not root.exists():
        return {"root": str(root), "exists": False, "notebooks": []}

    notebooks: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        size = path.stat().st_size
        sha = _sha256_hex(path)
        data, error = _try_read_json(path)

        entry: dict[str, Any] = {
            "path": str(path.relative_to(root.parent)),
            "size": size,
            "sha256": sha,
        }

        if error or data is None:
            entry["error"] = error or "not_a_dict"
            notebooks.append(entry)
            continue

        entry["schema_version"] = data.get("schema_version", "missing")
        entry["keyword_id"] = data.get("keyword_id", "missing")
        entry["keyword_zh"] = data.get("keyword_zh", "missing")
        entry["enabled"] = data.get("enabled", False)
        entry["relevance_profile_hash"] = (
            data.get("relevance_profile", {}).get("profile_hash", "missing")
            if isinstance(data.get("relevance_profile"), dict) else "missing"
        )

        # Count active queries
        queries = data.get("search_queries", {})
        active_zh = sum(1 for q in queries.values()
                        if isinstance(q, dict) and q.get("active") and q.get("language") == "zh")
        active_en = sum(1 for q in queries.values()
                        if isinstance(q, dict) and q.get("active") and q.get("language") == "en")
        entry["active_zh_queries"] = active_zh
        entry["active_en_queries"] = active_en
        entry["total_queries"] = len(queries)

        notebooks.append(entry)

    return {
        "root": str(root),
        "exists": True,
        "total_notebooks": len(notebooks),
        "enabled_count": sum(1 for nb in notebooks if nb.get("enabled")),
        "disabled_count": sum(1 for nb in notebooks if not nb.get("enabled")),
        "notebooks": notebooks,
    }


def generate_inventory_report(
    pending_pages_dir: Path,
    notebooks_dir: Path,
) -> dict[str, Any]:
    """Generate a complete legacy inventory report."""
    now = datetime.now(timezone.utc).isoformat()
    pages = inventory_pending_pages(pending_pages_dir)
    notebooks = inventory_keyword_notebooks(notebooks_dir)

    # Compute aggregate hash from all file SHAs
    all_hashes = sorted(
        f["sha256"] for f in pages.get("files", [])
        if "sha256" in f and "error" not in f
    )
    aggregate_hash = hashlib.sha256(
        "".join(all_hashes).encode("utf-8")
    ).hexdigest() if all_hashes else None

    return {
        "report_type": "discovery_v4_legacy_inventory",
        "generated_at": now,
        "pending_pages": pages,
        "keyword_notebooks": notebooks,
        "aggregate": {
            "total_journal_files": pages.get("total_files", 0),
            "total_notebook_files": notebooks.get("total_notebooks", 0),
            "journal_aggregate_sha256": aggregate_hash,
            "v2_journal_count": pages.get("by_schema_version", {}).get("2.0", 0),
            "v3_journal_count": pages.get("by_schema_version", {}).get("3.0", 0),
            "corrupt_journals": pages.get("corrupt_json", 0),
        },
    }


if __name__ == "__main__":
    import sys

    project_root = Path(__file__).resolve().parent.parent.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from config.settings import DISCOVERY_KEYWORD_NOTEBOOK_DIR, DISCOVERY_PENDING_PAGES_DIR

    report = generate_inventory_report(
        pending_pages_dir=DISCOVERY_PENDING_PAGES_DIR,
        notebooks_dir=DISCOVERY_KEYWORD_NOTEBOOK_DIR,
    )
    output = project_root / "data" / "discovery" / "legacy_inventory_report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    agg = report["aggregate"]
    print(f"[INVENTORY] total journals: {agg['total_journal_files']}")
    print(f"[INVENTORY] v2 journals:    {agg['v2_journal_count']}")
    print(f"[INVENTORY] v3 journals:    {agg['v3_journal_count']}")
    print(f"[INVENTORY] corrupt:        {agg['corrupt_journals']}")
    print(f"[INVENTORY] notebooks:      {agg['total_notebook_files']}")
    print(f"[INVENTORY] aggregate hash: {agg['journal_aggregate_sha256']}")
    pages = report["pending_pages"]
    total_mb = pages.get("total_size_mb", 0)
    print(f"[INVENTORY] total size:      {total_mb} MB")
    print(f"[INVENTORY] by schema:       {pages.get('by_schema_version', {})}")
    print(f"[INVENTORY] by keyword:      {pages.get('by_keyword', {})}")
    print(f"[INVENTORY] by provider:     {pages.get('by_provider', {})}")
    print(f"[INVENTORY] by lane:         {pages.get('by_lane', {})}")
    print(f"[INVENTORY] report written:  {output}")

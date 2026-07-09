"""Safely migrate discovery keyword notebooks from schema v1 to v2.

v1 Backfill cursors are not trusted because earlier discovery could advance a
cursor before preserving all candidates. Migration archives the old file and
resets every Backfill state to ``"*"`` so duplicate guards can safely handle
rediscovery.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import DISCOVERY_KEYWORD_NOTEBOOK_DIR  # noqa: E402
from src.discovery.keyword_notebook import (  # noqa: E402
    INITIAL_CURSOR,
    SCHEMA_VERSION,
    LEGACY_SCHEMA_VERSION,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    json.loads(tmp.read_text(encoding="utf-8"))
    os.replace(tmp, path)


def _content_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def _archive_path(notebook_dir: Path, data: dict, raw: bytes) -> Path:
    keyword_id = str(data.get("keyword_id") or "unknown")
    updated = str(data.get("updated_at") or data.get("created_at") or "no_updated_at")
    safe_updated = "".join(ch if ch.isalnum() else "_" for ch in updated)[:40]
    return notebook_dir / "archive_v1" / f"{keyword_id}__{safe_updated}__{_content_hash(raw)}.json"


def _empty_backfill(old: dict | None = None) -> dict:
    old = old or {}
    sig = old.get("pagination_signature") or old.get("request_signature") or ""
    return {
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
        "pagination_signature": sig,
        "request_signature": sig,
        "legacy": {
            "cursor": old.get("cursor"),
            "exhausted": old.get("exhausted"),
            "pages_succeeded": old.get("pages_succeeded"),
            "items_returned_total": old.get("items_returned_total"),
        },
    }


def migrate_file(path: Path, *, apply: bool) -> dict:
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    version = str(data.get("schema_version") or "")
    if version == SCHEMA_VERSION:
        return {"path": str(path), "status": "already_v2"}
    if version != LEGACY_SCHEMA_VERSION:
        return {"path": str(path), "status": "skipped", "error": f"unsupported schema_version={version}"}

    archive = _archive_path(path.parent, data, raw)
    migrated = dict(data)
    migrated["schema_version"] = SCHEMA_VERSION
    migrated["pending"] = migrated.get("pending") or {"pages": 0, "candidates": 0, "last_drained_at": None}
    stats = dict(migrated.get("lifetime_statistics") or {})
    migrated["lifetime_statistics"] = {
        "keyword_runs": int(stats.get("keyword_runs", 0)),
        "refresh_lane_runs": int(stats.get("refresh_lane_runs", stats.get("refresh_runs", 0) or 0)),
        "backfill_lane_runs": int(stats.get("backfill_lane_runs", stats.get("backfill_runs", 0) or 0)),
        "provider_page_attempts": int(stats.get("provider_page_attempts", 0)),
        "provider_page_successes": int(stats.get("provider_page_successes", 0)),
        "provider_page_failures": int(stats.get("provider_page_failures", 0)),
        "provider_items_returned": int(stats.get("provider_items_returned", 0)),
        "doi_observations": int(stats.get("doi_observations", stats.get("unique_dois_seen", 0) or 0)),
        "candidates_staged": int(stats.get("candidates_staged", stats.get("new_dois_staged", 0) or 0)),
        "candidates_existing": int(stats.get("candidates_existing", stats.get("existing_dois_skipped", 0) or 0)),
    }
    for exp in (migrated.get("expansions") or {}).values():
        providers = exp.get("providers") if isinstance(exp.get("providers"), dict) else {}
        for provider_state in providers.values():
            old_bf = provider_state.get("backfill") if isinstance(provider_state.get("backfill"), dict) else {}
            provider_state["backfill"] = _empty_backfill(old_bf)
    migrated.setdefault("migration_history", []).append({
        "at": _now_iso(),
        "from_schema_version": LEGACY_SCHEMA_VERSION,
        "to_schema_version": SCHEMA_VERSION,
        "archive_path": str(archive),
        "reason": "v1 cursors may have advanced before durable page journals",
    })
    migrated["updated_at"] = _now_iso()
    if apply:
        archive.parent.mkdir(parents=True, exist_ok=True)
        if not archive.exists():
            archive.write_bytes(raw)
        _atomic_write_json(path, migrated)
    return {"path": str(path), "status": "migrated", "archive_path": str(archive), "applied": apply}


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate discovery notebooks to schema v2.")
    parser.add_argument("--keyword-notebook-dir", type=Path, default=DISCOVERY_KEYWORD_NOTEBOOK_DIR)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    items = []
    for path in sorted(args.keyword_notebook_dir.glob("*.json")):
        try:
            items.append(migrate_file(path, apply=args.apply))
        except Exception as exc:
            items.append({"path": str(path), "status": "failed", "error": str(exc)})
    summary = {
        "schema_version": "1.0",
        "applied": bool(args.apply),
        "total": len(items),
        "migrated": sum(1 for i in items if i.get("status") == "migrated"),
        "already_v2": sum(1 for i in items if i.get("status") == "already_v2"),
        "failed": sum(1 for i in items if i.get("status") == "failed"),
    }
    report = {"summary": summary, "items": items}
    if args.report:
        _atomic_write_json(args.report, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())


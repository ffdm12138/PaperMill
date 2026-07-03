# Legacy only. Predates metadata v2.0 / catalog v3.0 / paper_index v2.0.
# Imports migrate_catalog_to_v2_0 which no longer exists in src/ -> ImportError on load.
# Kept for historical reference. Do not run against the active library.
"""Migrate existing catalog files (v1.0/v1.1 with `display`) to catalog v2.0
(content-only, no bibliographic fields).

Scans data/paper_raw/**/*.catalog.json and data/papers/**/*.catalog.json,
strips FORBIDDEN_CATALOG_KEYS, maps old fields to v2.0, and (with --apply)
writes them back. Never reads or modifies metadata. Default is dry-run.

Output: data/catalog/catalog_migration_report.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import PAPER_RAW_DIR, PAPERS_DIR, CATALOG_DIR
from src.services.v2_library import (
    find_forbidden_catalog_keys,
    migrate_catalog_to_v2_0,
    validate_catalog_schema,
)
from src.utils.atomic_io import atomic_write_json


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _scan(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.catalog.json") if p.is_file())


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def migrate_one(path: Path, *, apply: bool) -> dict:
    item = {"path": str(path), "status": "planned"}
    old = _read(path)
    if old is None:
        item["status"] = "failed"
        item["error"] = "unreadable/invalid json"
        return item
    # set link fields from folder/paper_id if missing
    pid = path.parent.name
    if isinstance(old, dict):
        if not old.get("paper_id"):
            old["paper_id"] = pid
        # paper_number: best-effort from a sibling .paper.number marker
        if not old.get("paper_number"):
            markers = list(path.parent.glob("*.paper.number"))
            if markers:
                old["paper_number"] = markers[0].stem
    forbidden_before = find_forbidden_catalog_keys(old)
    new, removed = migrate_catalog_to_v2_0(old)
    # fill asset_refs from the actual folder (migration cannot infer paths otherwise)
    pid = path.parent.name
    ar = new.get("asset_refs") or {}
    if not ar.get("markdown") and (path.parent / f"{pid}.md").exists():
        ar["markdown"] = str(path.parent / f"{pid}.md")
    if not ar.get("pdf") and (path.parent / f"{pid}.pdf").exists():
        ar["pdf"] = str(path.parent / f"{pid}.pdf")
    if not ar.get("images_dir") and (path.parent / "images").exists():
        ar["images_dir"] = str(path.parent / "images")
    ar.setdefault("figures", [])
    new["asset_refs"] = ar
    schema_errors = validate_catalog_schema(new)
    item["removed_forbidden_fields"] = removed
    item["schema_errors"] = schema_errors
    if schema_errors:
        item["status"] = "failed"
        item["error"] = "; ".join(schema_errors)
        return item
    if not removed and str(old.get("schema_version")) == "2.0":
        item["status"] = "unchanged"
        return item
    if apply:
        try:
            atomic_write_json(path, new, indent=2)
            item["status"] = "migrated"
        except Exception as exc:
            item["status"] = "failed"
            item["error"] = str(exc)
    else:
        item["status"] = "would_migrate"
    return item


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate catalogs to content-only v2.0.")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path, default=CATALOG_DIR / "catalog_migration_report.json")
    args = parser.parse_args()
    apply = args.apply and not args.dry_run

    targets = _scan(args.paper_raw_dir) + _scan(args.papers_dir)
    results = [migrate_one(p, apply=apply) for p in targets]

    report = {
        "applied": apply,
        "scanned": len(results),
        "migrated": sum(1 for r in results if r["status"] == "migrated"),
        "would_migrate": sum(1 for r in results if r["status"] == "would_migrate"),
        "unchanged": sum(1 for r in results if r["status"] == "unchanged"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "removed_fields_total": sum(len(r.get("removed_forbidden_fields") or []) for r in results),
        "removed_fields_unique": sorted({
            f for r in results for f in (r.get("removed_forbidden_fields") or [])
        }),
        "items": results,
        "generated_at": _now_iso(),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "applied": apply,
        "scanned": report["scanned"],
        "migrated": report["migrated"],
        "would_migrate": report["would_migrate"],
        "unchanged": report["unchanged"],
        "failed": report["failed"],
        "removed_fields_unique": report["removed_fields_unique"],
        "report": str(args.report),
    }, ensure_ascii=False, indent=2))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

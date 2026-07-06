"""One-shot repair: delete stale catalogs, fix metadata to v2.0 only.

Strategy:
- Delete all data/papers/**/*.catalog.json and data/paper_raw/**/*.catalog.json
- Fix any remaining metadata v1.1 → v2.0 issues (old fields, inline raw_record)
- Reset all.catalog.json to empty v3.1 shell
- Validate metadata-only state

Default dry-run; use --apply to write changes.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import ALL_CATALOG_PATH, PAPER_RAW_DIR, PAPERS_DIR, PAPER_NUMBER_LEDGER_PATH
from src.services.v2_library import (
    PaperNumberLedger,
    empty_metadata,
    validate_metadata_schema,
)
from src.utils.atomic_io import atomic_write_json


FORBIDDEN_TOP = frozenset({"abstract", "keywords", "pdf", "content", "notes", "bibtex", "citation_key"})
FORBIDDEN_TITLE = frozenset({"short_zh", "translated_zh"})


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _paper_number_16(value: Any) -> str:
    s = str(value or "")
    if re.fullmatch(r"\d{16}", s):
        return s
    return ""


def _resolve_paper_number(folder: Path, meta: dict[str, Any]) -> str:
    # 1. From metadata.paper_number
    num = _paper_number_16(meta.get("paper_number"))
    if num:
        return num
    # 2. From metadata.paper_raw_id
    num = _paper_number_16(meta.get("paper_raw_id"))
    if num:
        return num
    # 3. From marker
    for marker in sorted(folder.glob("*.paper.number")):
        try:
            mdata = json.loads(marker.read_text(encoding="utf-8"))
            num = _paper_number_16(mdata.get("paper_number"))
        except Exception:
            num = _paper_number_16(marker.stem)
        if num:
            return num
    # 4. From folder name if it is a 16-digit number
    if re.fullmatch(r"\d{16}", folder.name):
        return folder.name
    return ""


def _repair_metadata(old: dict[str, Any], *, paper_number: str, folder: Path) -> tuple[dict[str, Any], list[str], list[str]]:
    """Repair a single metadata dict to v2.0. Returns (new_meta, errors, warnings)."""
    source_type = str(old.get("source_type") or (old.get("source") or {}).get("kind") or "manual_pdf")
    new = empty_metadata(paper_number, source_type=source_type)
    warnings: list[str] = []
    errors: list[str] = []

    # Copy allowed scalar fields
    for key in ("paper_id", "entry_type", "year", "language"):
        if key in old:
            new[key] = deepcopy(old[key])

    # Copy allowed sub-dict fields
    for key in ("title", "first_author", "date", "container", "publication", "identifiers", "links", "metadata_match"):
        if isinstance(old.get(key), dict) and isinstance(new.get(key), dict):
            for child_key in old[key]:
                if child_key not in FORBIDDEN_TITLE:
                    new[key][child_key] = deepcopy(old[key][child_key])

    # Authors
    if isinstance(old.get("authors"), list):
        new["authors"] = deepcopy(old["authors"])

    # Source
    old_source = old.get("source") if isinstance(old.get("source"), dict) else {}
    new["source"].update({
        "kind": old_source.get("kind") or source_type,
        "provider": old_source.get("provider") or "",
        "query": old_source.get("query") or "",
        "retrieved_at": old_source.get("retrieved_at") or "",
        "raw_record_path": old_source.get("raw_record_path") or "",
    })

    # Migrate inline raw_record → sidecar
    if old_source.get("raw_record"):
        provider = str(new["source"].get("provider") or "metadata").lower() or "metadata"
        rel = f"source_records/{provider}.json"
        new["source"]["raw_record_path"] = rel
        sidecar_path = folder / rel
        if not sidecar_path.exists():
            warnings.append(f"would write sidecar: {rel}")
            # In apply mode we'll write it; here we just warn
        else:
            warnings.append(f"sidecar already exists: {rel}")

    # Check for forbidden fields
    for key in sorted(FORBIDDEN_TOP):
        if key in old:
            warnings.append(f"remove metadata.{key}")

    title_obj = old.get("title") if isinstance(old.get("title"), dict) else {}
    for key in sorted(FORBIDDEN_TITLE):
        if key in title_obj:
            warnings.append(f"remove metadata.title.{key}")

    if old_source.get("raw_record"):
        warnings.append("migrate metadata.source.raw_record → sidecar")

    # Validate
    errors = validate_metadata_schema(new)
    return new, errors, warnings


def _prefix_from_metadata_path(folder: Path) -> str | None:
    metas = sorted(folder.glob("*.metadata.json"))
    if metas:
        return metas[0].name.removesuffix(".metadata.json")
    return None


def _process_papers(papers_dir: Path, *, apply: bool) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not papers_dir.exists():
        return items
    for folder in sorted(p for p in papers_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
        pid = folder.name
        result: dict[str, Any] = {"folder": str(folder), "scope": "papers", "paper_id": pid}

        # Collect catalogs to delete
        catalogs = sorted(folder.glob("*.catalog.json"))
        result["catalogs_to_delete"] = [c.name for c in catalogs]

        # Repair metadata
        prefix = _prefix_from_metadata_path(folder)
        meta_path = folder / f"{prefix}.metadata.json" if prefix else None
        if meta_path and meta_path.exists():
            old = _read_json(meta_path)
            if old:
                num = _resolve_paper_number(folder, old) or str(old.get("paper_number") or "")
                if not num:
                    num = pid  # fallback
                new, meta_errors, meta_warnings = _repair_metadata(old, paper_number=num, folder=folder)
                result["paper_number"] = num
                result["meta_errors"] = meta_errors
                result["meta_warnings"] = meta_warnings
                needs_write = new != old
                result["metadata_needs_repair"] = needs_write

                if apply and needs_write and not meta_errors:
                    # Write to temp, then os.replace
                    atomic_write_json(meta_path, new, indent=2)
                    # Write raw_record sidecar if needed
                    old_source = old.get("source") if isinstance(old.get("source"), dict) else {}
                    if old_source.get("raw_record"):
                        rel = new["source"].get("raw_record_path", "")
                        if rel:
                            sidecar_path = folder / rel
                            if not sidecar_path.exists():
                                sidecar_path.parent.mkdir(parents=True, exist_ok=True)
                                atomic_write_json(sidecar_path, old_source["raw_record"], indent=2)
                                result["sidecar_written"] = rel
            else:
                result["meta_errors"] = ["unreadable metadata"]
                result["metadata_needs_repair"] = False
        else:
            result["meta_errors"] = ["missing metadata"]
            result["metadata_needs_repair"] = False

        # Delete catalogs
        if apply and catalogs:
            for c in catalogs:
                c.unlink()
            result["catalogs_deleted"] = len(catalogs)
        elif catalogs:
            result["catalogs_deleted"] = 0  # dry-run

        items.append(result)
    return items


def _process_paper_raw(paper_raw_dir: Path, *, apply: bool) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not paper_raw_dir.exists():
        return items
    skip_names = {"quarantine", "output", "images", "__pycache__"}
    for folder in sorted(p for p in paper_raw_dir.iterdir() if p.is_dir()):
        if folder.name.startswith(".") or folder.name in skip_names:
            continue
        result: dict[str, Any] = {"folder": str(folder), "scope": "paper_raw"}

        # Collect catalogs to delete
        catalogs = sorted(folder.glob("*.catalog.json"))
        result["catalogs_to_delete"] = [c.name for c in catalogs]

        # Repair metadata
        prefix = _prefix_from_metadata_path(folder)
        meta_path = folder / f"{prefix}.metadata.json" if prefix else None
        if meta_path and meta_path.exists():
            old = _read_json(meta_path)
            if old:
                num = _resolve_paper_number(folder, old) or str(old.get("paper_number") or "")
                if not num:
                    num = prefix or ""
                new, meta_errors, meta_warnings = _repair_metadata(old, paper_number=num, folder=folder)
                result["paper_number"] = num
                result["meta_errors"] = meta_errors
                result["meta_warnings"] = meta_warnings
                needs_write = new != old
                result["metadata_needs_repair"] = needs_write

                if apply and needs_write and not meta_errors:
                    atomic_write_json(meta_path, new, indent=2)
                    old_source = old.get("source") if isinstance(old.get("source"), dict) else {}
                    if old_source.get("raw_record"):
                        rel = new["source"].get("raw_record_path", "")
                        if rel:
                            sidecar_path = folder / rel
                            if not sidecar_path.exists():
                                sidecar_path.parent.mkdir(parents=True, exist_ok=True)
                                atomic_write_json(sidecar_path, old_source["raw_record"], indent=2)
                                result["sidecar_written"] = rel
            else:
                result["meta_errors"] = ["unreadable metadata"]
                result["metadata_needs_repair"] = False
        else:
            result["meta_errors"] = []  # no metadata is OK for paper_raw staging
            result["metadata_needs_repair"] = False

        # Delete catalogs
        if apply and catalogs:
            for c in catalogs:
                c.unlink()
            result["catalogs_deleted"] = len(catalogs)
        elif catalogs:
            result["catalogs_deleted"] = 0  # dry-run

        items.append(result)
    return items


def _handle_all_catalog(all_catalog_path: Path, *, apply: bool) -> dict[str, Any]:
    result: dict[str, Any] = {"action": "none", "path": str(all_catalog_path)}
    if not all_catalog_path.exists():
        result["action"] = "already_missing"
        return result
    try:
        current = json.loads(all_catalog_path.read_text(encoding="utf-8"))
        is_empty = current == {"schema_version": "3.1", "papers": []}
    except Exception:
        is_empty = False
    if is_empty:
        result["action"] = "already_empty"
        return result
    result["action"] = "reset_to_empty" if apply else "would_reset"
    if apply:
        atomic_write_json(all_catalog_path, {"schema_version": "3.1", "papers": []}, indent=2)
    return result


def _check_paper_index_and_ledger(papers_dir: Path, ledger_path: Path) -> list[str]:
    """Check paper_index.json and paper_number_ledger.json for conflicts."""
    from config.settings import ALL_CATALOG_PATH
    warnings: list[str] = []
    catalog_dir = ALL_CATALOG_PATH.parent

    # Check paper_index.json
    index_path = catalog_dir / "paper_index.json"
    if index_path.exists():
        try:
            idx = json.loads(index_path.read_text(encoding="utf-8"))
            idx_entries = idx.get("entries") or idx.get("papers") or []
            if isinstance(idx_entries, list):
                for entry in idx_entries:
                    pid = entry.get("paper_id") or ""
                    pdir = papers_dir / pid
                    if not pdir.exists():
                        warnings.append(f"paper_index references paper_id '{pid}' but folder not found in papers_dir")
        except Exception as e:
            warnings.append(f"paper_index.json unreadable: {e}")

    # Check paper_number_ledger.json
    if ledger_path.exists():
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except Exception as e:
            warnings.append(f"ledger unreadable: {e}")
    return warnings


def _summary(items: list[dict], all_catalog_result: dict) -> dict:
    papers_items = [i for i in items if i.get("scope") == "papers"]
    raw_items = [i for i in items if i.get("scope") == "paper_raw"]
    return {
        "papers_scanned": len(papers_items),
        "paper_raw_scanned": len(raw_items),
        "catalogs_to_delete": sum(len(i.get("catalogs_to_delete", [])) for i in items),
        "catalogs_deleted": sum(i.get("catalogs_deleted", 0) for i in items),
        "metadata_repaired": sum(1 for i in items if i.get("metadata_needs_repair")),
        "sidecars_to_write": sum(1 for i in items if "sidecar_written" in i or any("sidecar" in w for w in i.get("meta_warnings", []))),
        "metadata_errors": sum(1 for i in items if i.get("meta_errors")),
        "all_catalog": all_catalog_result.get("action", "unknown"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="One-shot repair: delete stale catalogs, fix metadata to v2.0")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--all-catalog-path", type=Path, default=ALL_CATALOG_PATH)
    parser.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    parser.add_argument("--scope", choices=["papers", "paper_raw", "catalog", "all"], default="all")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    write = args.apply and not args.dry_run
    items: list[dict[str, Any]] = []

    if args.scope in ("papers", "all"):
        items += _process_papers(args.papers_dir, apply=write)
    if args.scope in ("paper_raw", "all"):
        items += _process_paper_raw(args.paper_raw_dir, apply=write)

    all_cat_result = _handle_all_catalog(args.all_catalog_path, apply=write) if args.scope in ("catalog", "all") else {}

    ledger_warnings = _check_paper_index_and_ledger(args.papers_dir, args.ledger_path)

    report = {
        "applied": write,
        "summary": _summary(items, all_cat_result),
        "all_catalog": all_cat_result,
        "ledger_warnings": ledger_warnings,
        "items": items,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(args.report, report, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))

    errors = sum(1 for i in items if i.get("meta_errors"))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

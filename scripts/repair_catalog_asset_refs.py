"""Repair stale catalog asset refs/provenance paths in papers and paper_raw."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import PAPER_RAW_DIR, PAPERS_DIR
from src.services.catalog_asset_refs import canonicalize_catalog_asset_refs
from src.services.ingest_ids import PAPER_NUMBER_RE
from src.utils.atomic_io import atomic_write_json


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _paper_number_from_marker(folder: Path) -> str:
    markers = sorted(folder.glob("*.paper.number"))
    if not markers:
        return ""
    marker = markers[0]
    try:
        data = _load_json(marker)
    except Exception:
        return marker.stem
    return str(data.get("paper_number") or marker.stem)


def _item(folder: Path, catalog_path: Path, old: dict, new: dict, *, apply: bool) -> dict:
    old_locator = old.get("library_locator") if isinstance(old.get("library_locator"), dict) else {}
    new_locator = new.get("library_locator") if isinstance(new.get("library_locator"), dict) else {}
    old_refs = old_locator.get("asset_refs") if isinstance(old_locator.get("asset_refs"), dict) else {}
    new_refs = new_locator.get("asset_refs") if isinstance(new_locator.get("asset_refs"), dict) else {}
    old_prov = old.get("provenance") if isinstance(old.get("provenance"), dict) else {}
    new_prov = new.get("provenance") if isinstance(new.get("provenance"), dict) else {}
    changed = old != new
    status = "repaired" if apply and changed else "would_repair" if changed else "skipped"
    if apply and changed:
        atomic_write_json(catalog_path, new, indent=2)
    return {
        "folder": str(folder),
        "catalog": catalog_path.name,
        "status": status,
        "old_markdown_path": old_prov.get("markdown_path") or old_refs.get("markdown") or "",
        "new_markdown_path": new_prov.get("markdown_path") or new_refs.get("markdown") or "",
        "old_pdf_path": old_refs.get("pdf") or "",
        "new_pdf_path": new_refs.get("pdf") or "",
    }


def _repair_papers(papers_dir: Path, *, apply: bool) -> list[dict]:
    items: list[dict] = []
    if not papers_dir.exists():
        return items
    for folder in sorted(p for p in papers_dir.iterdir() if p.is_dir()):
        pid = folder.name
        catalog_path = folder / f"{pid}.catalog.json"
        if not catalog_path.exists():
            continue
        try:
            old = _load_json(catalog_path)
            locator = old.get("library_locator") if isinstance(old.get("library_locator"), dict) else {}
            number = _paper_number_from_marker(folder) or str(locator.get("paper_number") or "")
            if not number:
                items.append({"folder": str(folder), "catalog": catalog_path.name, "status": "failed", "error": "missing paper_number"})
                continue
            new = canonicalize_catalog_asset_refs(
                old,
                folder=folder,
                paper_number=number,
                paper_id=pid,
                stage="papers",
            )
            items.append(_item(folder, catalog_path, old, new, apply=apply))
        except Exception as exc:
            items.append({"folder": str(folder), "catalog": catalog_path.name, "status": "failed", "error": str(exc)})
    return items


def _repair_paper_raw(paper_raw_dir: Path, *, apply: bool) -> list[dict]:
    items: list[dict] = []
    if not paper_raw_dir.exists():
        return items
    skip_names = {"quarantine", "output", "images", "__pycache__"}
    for folder in sorted(p for p in paper_raw_dir.iterdir() if p.is_dir()):
        if folder.name.startswith(".") or folder.name in skip_names:
            continue
        is_numbered = bool(PAPER_NUMBER_RE.match(folder.name))
        number = folder.name if is_numbered else _paper_number_from_marker(folder)
        if not number:
            catalog_candidates = list(folder.glob("*.catalog.json"))
            if len(catalog_candidates) == 1:
                try:
                    locator = _load_json(catalog_candidates[0]).get("library_locator") or {}
                    number = str(locator.get("paper_number") or "")
                except Exception:
                    number = ""
            if not number:
                continue
        pid = "" if is_numbered else folder.name
        prefix = number if is_numbered else pid
        catalog_path = folder / f"{prefix}.catalog.json"
        if not catalog_path.exists():
            continue
        try:
            old = _load_json(catalog_path)
            new = canonicalize_catalog_asset_refs(
                old,
                folder=folder,
                paper_number=number,
                paper_id=pid or None,
                stage="paper_raw" if is_numbered else "formalized",
            )
            items.append(_item(folder, catalog_path, old, new, apply=apply))
        except Exception as exc:
            items.append({"folder": str(folder), "catalog": catalog_path.name, "status": "failed", "error": str(exc)})
    return items


def _summary(items: list[dict]) -> dict:
    return {
        "scanned": len(items),
        "needs_repair": sum(1 for item in items if item.get("status") in {"would_repair", "repaired"}),
        "repaired": sum(1 for item in items if item.get("status") == "repaired"),
        "skipped": sum(1 for item in items if item.get("status") == "skipped"),
        "failed": sum(1 for item in items if item.get("status") == "failed"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair stale catalog asset_refs and provenance.markdown_path.")
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    write = args.apply and not args.dry_run
    items = _repair_papers(args.papers_dir, apply=write) + _repair_paper_raw(args.paper_raw_dir, apply=write)
    payload = {
        "applied": write,
        "summary": _summary(items),
        "items": items,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if payload["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

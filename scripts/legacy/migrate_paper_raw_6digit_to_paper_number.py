"""LEGACY-ONLY MIGRATION SCRIPT.

This script is not part of ingest-v2.3 normal workflow.
Do not run from agents unless explicitly repairing old snapshots.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import PAPER_NUMBER_LEDGER_PATH, PAPER_RAW_DIR
from scripts.legacy.ingest_ids import LEGACY_TEMP_SOURCE_ID_RE
from src.services.ingest_ids import PAPER_NUMBER_RE
from src.services.v2_library import PaperNumberLedger, now_iso
from src.utils.atomic_io import atomic_write_json


CORE_SUFFIXES = ("metadata.json", "catalog.json", "md", "pdf", "conversion.json")


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _update_metadata(path: Path, paper_number: str, legacy_source_id: str) -> None:
    data = _read_json(path, {})
    if not isinstance(data, dict):
        return
    data["source_id"] = paper_number
    data["paper_number"] = paper_number
    data.setdefault("source", {})["legacy_source_id"] = legacy_source_id
    pdf = data.get("pdf") if isinstance(data.get("pdf"), dict) else {}
    if pdf.get("path"):
        pdf["path"] = str(pdf["path"]).replace(f"/{legacy_source_id}/", f"/{paper_number}/").replace(
            f"\\{legacy_source_id}\\", f"\\{paper_number}\\"
        ).replace(f"{legacy_source_id}.pdf", f"{paper_number}.pdf")
        data["pdf"] = pdf
    atomic_write_json(path, data, indent=2)


def _update_json_sidecar(path: Path, paper_number: str, legacy_source_id: str) -> None:
    data = _read_json(path, {})
    if not isinstance(data, dict):
        return
    data["source_id"] = paper_number
    data["paper_number"] = paper_number
    data["paper_raw_id"] = paper_number
    data["legacy_source_id"] = legacy_source_id
    for key in ("markdown_path", "pdf_path", "conversion_manifest"):
        if isinstance(data.get(key), str):
            data[key] = data[key].replace(f"{legacy_source_id}.", f"{paper_number}.")
    atomic_write_json(path, data, indent=2)


def _migrate_one(folder: Path, ledger: PaperNumberLedger, *, write: bool) -> dict:
    legacy_id = folder.name
    marker_number = ledger.paper_number_from_marker(folder)
    if marker_number and not PAPER_NUMBER_RE.match(marker_number):
        return {"folder": str(folder), "legacy_source_id": legacy_id, "status": "failed", "error": "invalid marker"}
    paper_number = marker_number or ledger.peek_next_numbers(1)[0]
    target = folder.with_name(paper_number)
    item = {
        "folder": str(folder),
        "legacy_source_id": legacy_id,
        "paper_number": paper_number,
        "paper_raw_id": paper_number,
        "target": str(target),
        "status": "planned",
    }
    if target.exists() and target.resolve() != folder.resolve():
        item.update({"status": "failed", "error": f"target exists: {target}"})
        return item
    if not write:
        return item

    if marker_number:
        ledger.repoint_reserved(paper_number, folder)
    else:
        ledger.reserve_for_paper_raw(folder)
    folder.rename(target)
    for suffix in CORE_SUFFIXES:
        old = target / f"{legacy_id}.{suffix}"
        new = target / f"{paper_number}.{suffix}"
        if old.exists() and old != new:
            old.rename(new)

    metadata_path = target / f"{paper_number}.metadata.json"
    if metadata_path.exists():
        _update_metadata(metadata_path, paper_number, legacy_id)
    for name in (
        f"{paper_number}.conversion.json",
        "stage_manifest.json",
        ".import_status.json",
        f"{paper_number}.catalog.json",
    ):
        path = target / name
        if path.exists():
            _update_json_sidecar(path, paper_number, legacy_id)
    ledger.repoint_reserved(paper_number, target)
    status = _read_json(target / ".import_status.json", {})
    if isinstance(status, dict):
        status.setdefault("status", "migrated_legacy_source")
        status["paper_number"] = paper_number
        status["paper_raw_id"] = paper_number
        status["source_id"] = paper_number
        status["legacy_source_id"] = legacy_id
        status["migrated_at"] = now_iso()
        atomic_write_json(target / ".import_status.json", status, indent=2)
    item["status"] = "migrated"
    return item


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate legacy 6-digit paper_raw folders to 16-digit paper_number workspaces.")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    parser.add_argument("--source-id", default=None, help="Optional legacy 6-digit folder to migrate.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    write = args.apply and not args.dry_run
    if args.source_id:
        if not LEGACY_TEMP_SOURCE_ID_RE.match(args.source_id):
            parser.error("--source-id must be a legacy 6-digit id")
        folders = [args.paper_raw_dir / args.source_id]
    else:
        folders = sorted(p for p in args.paper_raw_dir.iterdir() if p.is_dir() and LEGACY_TEMP_SOURCE_ID_RE.match(p.name))
    ledger = PaperNumberLedger(args.ledger_path)
    report = [_migrate_one(folder, ledger, write=write) for folder in folders]
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"applied": write, "items": report}, ensure_ascii=False, indent=2))
    return 1 if any(i.get("status") == "failed" for i in report) else 0


if __name__ == "__main__":
    raise SystemExit(main())

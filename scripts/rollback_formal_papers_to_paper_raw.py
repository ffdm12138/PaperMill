"""Rollback formal ``data/papers`` assets into numbered ``data/paper_raw`` workspaces.

Default mode is dry-run. ``--apply`` performs a staged copy, installs a raw
workspace, archives the formal folder, rolls the ledger item from active back to
reserved, and rebuilds the runtime indexes.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import ALL_CATALOG_PATH, PAPER_NUMBER_LEDGER_PATH, PAPER_RAW_DIR, PAPERS_DIR
from src.path_utils import normalize_repo_path, resolve_stored_path
from src.services.asset_manifest import write_asset_manifest
from src.services.ingest_ids import PAPER_NUMBER_RE
from src.services.metadata_quality import bibliographic_identity_gate
from src.services.v2_library import (
    AllCatalogBuilder,
    PaperNumberLedger,
    now_iso,
    validate_metadata_schema,
    write_conversion_manifest_for_existing_assets,
)
from src.utils.atomic_io import atomic_write_json


FORBIDDEN_METADATA_TOP = {"abstract", "keywords", "pdf", "content", "notes", "bibtex", "citation_key"}
FORBIDDEN_TITLE = {"short" + "_zh", "translated" + "_zh"}
FORBIDDEN_SOURCE = {"raw" + "_record", "providers"}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _marker_number(marker: Path) -> str:
    if marker.name.endswith(".paper.number"):
        candidate = marker.name[: -len(".paper.number")]
        if PAPER_NUMBER_RE.match(candidate):
            return candidate
    try:
        data = _load_json(marker)
    except Exception:
        data = {}
    return str(data.get("paper_number") or "")


def _formal_dirs(papers_dir: Path, *, all_papers: bool, paper_id: str | None) -> list[Path]:
    if paper_id:
        return [papers_dir / paper_id]
    if not all_papers:
        raise ValueError("--all or --paper-id is required")
    if not papers_dir.exists():
        return []
    return sorted(p for p in papers_dir.iterdir() if p.is_dir() and not p.name.startswith("."))


def _preflight_folder(folder: Path, paper_raw_dir: Path, ledger: PaperNumberLedger) -> dict[str, Any]:
    pid = folder.name
    errors: list[str] = []
    warnings: list[str] = []
    markers = sorted(folder.glob("*.paper.number"))
    if len(markers) != 1:
        errors.append(f"{pid}: expected exactly one .paper.number marker, found {len(markers)}")
        number = ""
    else:
        number = _marker_number(markers[0])
        if not PAPER_NUMBER_RE.match(number):
            errors.append(f"{pid}: invalid paper_number marker {markers[0].name}")

    required = {
        "metadata": folder / f"{pid}.metadata.json",
        "markdown": folder / f"{pid}.md",
        "pdf": folder / f"{pid}.pdf",
        "images": folder / "images",
    }
    for label, path in required.items():
        if label == "images":
            if not path.is_dir():
                errors.append(f"{pid}: missing images/: {path}")
        elif not path.exists():
            errors.append(f"{pid}: missing {label}: {path}")

    manifest = folder / f"{pid}.asset_manifest.json"
    if not manifest.exists():
        warnings.append(f"{pid}: missing formal asset manifest; raw manifest will be rebuilt")

    metadata: dict[str, Any] = {}
    if required["metadata"].exists():
        try:
            metadata = _load_json(required["metadata"])
        except Exception as exc:
            errors.append(f"{pid}: metadata invalid JSON: {exc}")
        else:
            for err in validate_metadata_schema(metadata):
                errors.append(f"{pid}: {err}")
            source = metadata.get("source") if isinstance(metadata.get("source"), dict) else {}
            for key in sorted(FORBIDDEN_SOURCE):
                if key in source:
                    errors.append(f"{pid}: metadata.source.{key} is forbidden in rollback strict-only mode")
            if number:
                if metadata.get("paper_number") != number:
                    errors.append(f"{pid}: metadata.paper_number != marker paper_number")
                if metadata.get("paper_raw_id") != number:
                    errors.append(f"{pid}: metadata.paper_raw_id != marker paper_number")
            status = str(((metadata.get("metadata_match") or {}).get("status")) or "")
            if status in {"matched", "manual_confirmed"}:
                ready, reasons = bibliographic_identity_gate(metadata)
                if not ready:
                    warnings.append(f"{pid}: metadata {status} but not citation-ready; will downgrade on rollback: {', '.join(reasons)}")

    if number:
        target = paper_raw_dir / number
        if target.exists():
            errors.append(f"{pid}: target paper_raw already exists: {target}")
        ledger_item = (ledger.load().get("items") or {}).get(number)
        if not ledger_item:
            errors.append(f"{pid}: ledger missing paper_number {number}")
        else:
            state = ledger_item.get("state") or "active"
            if state != "active":
                errors.append(f"{pid}: ledger state is {state}; expected active")
            stored = ledger_item.get("folder_path") or ""
            resolved = resolve_stored_path(stored)
            try:
                same = resolved.resolve() == folder.resolve()
            except OSError:
                same = False
            if stored and not same:
                errors.append(f"{pid}: ledger folder_path does not point to formal folder")
    return {
        "paper_id": pid,
        "paper_number": number,
        "folder": str(folder),
        "target": str(paper_raw_dir / number) if number else "",
        "errors": errors,
        "warnings": warnings,
    }


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _clean_metadata(metadata: dict[str, Any], *, paper_number: str) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    metadata = dict(metadata)
    metadata["schema_version"] = "2.0"
    metadata["paper_number"] = paper_number
    metadata["paper_raw_id"] = paper_number
    for key in FORBIDDEN_METADATA_TOP:
        metadata.pop(key, None)
    title = metadata.get("title")
    if isinstance(title, dict):
        for key in FORBIDDEN_TITLE:
            title.pop(key, None)
    # Secondary defense: strict-only preflight already blocks source.raw_record
    # / providers, but pop them here so a bypassed preflight cannot smuggle
    # forbidden keys into the rolled-back metadata.
    source = metadata.get("source")
    if isinstance(source, dict):
        for key in FORBIDDEN_SOURCE:
            source.pop(key, None)
    errors = validate_metadata_schema(metadata)
    if errors:
        warnings.extend(errors)
    status = str(((metadata.get("metadata_match") or {}).get("status")) or "")
    if status in {"matched", "manual_confirmed"}:
        ready, reasons = bibliographic_identity_gate(metadata)
        if not ready:
            match = metadata.setdefault("metadata_match", {})
            match["status"] = "unmatched"
            existing = list(match.get("warnings") or [])
            match["warnings"] = existing + [f"rollback downgraded metadata_match: {reason}" for reason in reasons]
            warnings.extend(reasons)
    return metadata, warnings


def _stage_folder(formal: Path, staging: Path, *, paper_id: str, paper_number: str, keep_catalog: bool) -> dict[str, Any]:
    staging.mkdir(parents=True)
    _copy_file(formal / f"{paper_id}.metadata.json", staging / f"{paper_number}.metadata.json")
    _copy_file(formal / f"{paper_id}.md", staging / f"{paper_number}.md")
    _copy_file(formal / f"{paper_id}.pdf", staging / f"{paper_number}.pdf")
    manifest = formal / f"{paper_id}.asset_manifest.json"
    if manifest.exists():
        _copy_file(manifest, staging / f"{paper_number}.asset_manifest.json")
    if keep_catalog and (formal / f"{paper_id}.catalog.json").exists():
        _copy_file(formal / f"{paper_id}.catalog.json", staging / f"{paper_number}.catalog.json")
    shutil.copytree(formal / "images", staging / "images")
    source_records = formal / "source_records"
    if source_records.exists():
        shutil.copytree(source_records, staging / "source_records", dirs_exist_ok=True)

    metadata = _load_json(staging / f"{paper_number}.metadata.json")
    metadata, warnings = _clean_metadata(metadata, paper_number=paper_number)
    atomic_write_json(staging / f"{paper_number}.metadata.json", metadata, indent=2)
    write_conversion_manifest_for_existing_assets(staging, paper_number)
    write_asset_manifest(staging, prefix=paper_number, paper_number=paper_number, paper_id="", stage="paper_raw")

    citation_ready, reasons = bibliographic_identity_gate(metadata)
    status = str(((metadata.get("metadata_match") or {}).get("status")) or "")
    import_status = {
        "status": "converted" if citation_ready and status in {"matched", "manual_confirmed"} else "metadata_manual_review_required",
        "reason": (
            "rolled back from formal library; converted assets preserved; regenerate catalog before formalize"
            if citation_ready and status in {"matched", "manual_confirmed"}
            else "rolled back from formal library but metadata is not citation-ready"
        ),
        "paper_number": paper_number,
        "paper_raw_id": paper_number,
        "old_paper_id": paper_id,
        "metadata_match_status": status,
        "warnings": warnings + ([] if citation_ready else reasons),
        "rolled_back_at": now_iso(),
    }
    atomic_write_json(staging / ".import_status.json", import_status, indent=2)
    atomic_write_json(staging / f"{paper_number}.paper.number", {
        "paper_number": paper_number,
        "folder_name": paper_number,
        "state": "reserved",
        "planned_paper_id": paper_id,
    }, indent=2)
    post_errors = _postcheck_staging(staging, paper_number, keep_catalog=keep_catalog)
    return {"warnings": warnings, "postcheck_errors": post_errors}


def _postcheck_staging(folder: Path, paper_number: str, *, keep_catalog: bool) -> list[str]:
    errors: list[str] = []
    required = [
        folder / f"{paper_number}.metadata.json",
        folder / f"{paper_number}.md",
        folder / f"{paper_number}.pdf",
        folder / "images",
        folder / f"{paper_number}.asset_manifest.json",
        folder / f"{paper_number}.conversion.json",
        folder / f"{paper_number}.paper.number",
    ]
    for path in required:
        if path.name == "images":
            if not path.is_dir():
                errors.append(f"missing images/: {path}")
        elif not path.exists():
            errors.append(f"missing required rollback asset: {path}")
    try:
        metadata = _load_json(folder / f"{paper_number}.metadata.json")
        errors.extend(validate_metadata_schema(metadata))
    except Exception as exc:
        errors.append(f"metadata unreadable after rollback staging: {exc}")
    if not keep_catalog and list(folder.glob("*.catalog.json")):
        errors.append("catalog file exists despite default delete-catalog mode")
    return errors


def _install_one(
    *,
    formal: Path,
    paper_raw_dir: Path,
    archive_dir: Path,
    ledger: PaperNumberLedger,
    paper_id: str,
    paper_number: str,
    keep_catalog: bool,
) -> dict[str, Any]:
    staging = archive_dir / "staging" / paper_number
    backup = archive_dir / "papers_backup" / paper_id
    target = paper_raw_dir / paper_number
    if staging.exists():
        shutil.rmtree(staging)
    stage = _stage_folder(formal, staging, paper_id=paper_id, paper_number=paper_number, keep_catalog=keep_catalog)
    if stage["postcheck_errors"]:
        shutil.rmtree(staging, ignore_errors=True)
        return {"status": "failed", "errors": stage["postcheck_errors"], "warnings": stage["warnings"]}

    tmp_target = paper_raw_dir / f".rollback_{paper_number}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    archived = False
    installed = False
    try:
        paper_raw_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staging, tmp_target)
        os.replace(tmp_target, target)
        installed = True
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(formal), str(backup))
        archived = True
        ledger.rollback_active_to_reserved(paper_number, target, planned_paper_id=paper_id)
    except Exception:
        if tmp_target.exists():
            shutil.rmtree(tmp_target, ignore_errors=True)
        if archived and backup.exists() and not formal.exists():
            shutil.move(str(backup), str(formal))
        if installed and target.exists():
            shutil.rmtree(target, ignore_errors=True)
        raise
    return {
        "status": "rolled_back",
        "paper_id": paper_id,
        "paper_number": paper_number,
        "raw_folder": normalize_repo_path(target),
        "archive_folder": normalize_repo_path(backup),
        "catalog_deleted": not keep_catalog,
        "source_records_preserved": (target / "source_records").exists(),
        "warnings": stage["warnings"],
    }


def rollback_formal_papers(
    *,
    papers_dir: Path,
    paper_raw_dir: Path,
    ledger_path: Path,
    all_catalog_path: Path,
    archive_dir: Path,
    all_papers: bool = False,
    paper_id: str | None = None,
    keep_catalog: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    ledger = PaperNumberLedger(ledger_path)
    candidates = _formal_dirs(papers_dir, all_papers=all_papers, paper_id=paper_id)
    report: dict[str, Any] = {
        "applied": apply,
        "archive_dir": str(archive_dir),
        "items": [],
        "summary": {
            "planned": 0,
            "rolled_back": 0,
            "failed": 0,
            "catalogs_deleted": 0,
            "source_records_preserved": 0,
            "ledger_active_to_reserved": 0,
            "blocking_errors": 0,
        },
    }
    preflight = []
    for folder in candidates:
        item = _preflight_folder(folder, paper_raw_dir, ledger)
        item["status"] = "blocked" if item["errors"] else "planned"
        preflight.append(item)
    report["items"] = preflight
    report["summary"]["planned"] = sum(1 for item in preflight if not item["errors"])
    report["summary"]["blocking_errors"] = sum(len(item["errors"]) for item in preflight)
    if any(item["errors"] for item in preflight) or not apply:
        return report

    archive_dir.mkdir(parents=True, exist_ok=True)
    for item in preflight:
        result = _install_one(
            formal=Path(item["folder"]),
            paper_raw_dir=paper_raw_dir,
            archive_dir=archive_dir,
            ledger=ledger,
            paper_id=item["paper_id"],
            paper_number=item["paper_number"],
            keep_catalog=keep_catalog,
        )
        item.update(result)
        if result["status"] == "rolled_back":
            report["summary"]["rolled_back"] += 1
            report["summary"]["ledger_active_to_reserved"] += 1
            if result["catalog_deleted"]:
                report["summary"]["catalogs_deleted"] += 1
            if result["source_records_preserved"]:
                report["summary"]["source_records_preserved"] += 1
        elif result["status"] == "failed":
            report["summary"]["failed"] += 1
            report["summary"]["blocking_errors"] += len(result.get("errors") or []) or 1

    builder = AllCatalogBuilder(papers_dir, all_catalog_path, ledger)
    if report["summary"]["failed"] > 0:
        report["index_rebuild"] = {
            "status": "skipped_due_to_failed_items",
            "reason": f"{report['summary']['failed']} item(s) failed postcheck; index not rebuilt to avoid inconsistency",
        }
    elif all_papers:
        all_catalog = builder.build(write=True)
        report["index_rebuild"] = {
            "status": "written",
            "all_catalog_count": len(all_catalog.get("papers", [])),
            "errors": list(builder.last_errors),
        }
        if builder.last_errors:
            report["summary"]["blocking_errors"] += len(builder.last_errors)
    else:
        planned = builder.build(write=False)
        if builder.last_errors:
            report["index_rebuild"] = {
                "status": "failed_skipped_write",
                "all_catalog_count": len(planned.get("papers", [])),
                "errors": list(builder.last_errors),
            }
            report["summary"]["blocking_errors"] += len(builder.last_errors)
        else:
            all_catalog = builder.build(write=True)
            report["index_rebuild"] = {
                "status": "written",
                "all_catalog_count": len(all_catalog.get("papers", [])),
                "errors": [],
            }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely rollback formal papers into paper_raw workspaces.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="rollback all formal paper folders")
    group.add_argument("--paper-id", default=None, help="rollback a single formal paper_id")
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    parser.add_argument("--all-catalog-path", type=Path, default=ALL_CATALOG_PATH)
    parser.add_argument("--archive-dir", type=Path, default=None)
    catalog_group = parser.add_mutually_exclusive_group()
    catalog_group.add_argument("--keep-catalog", action="store_true", help="debug only: preserve catalog as <paper_number>.catalog.json; not for formal SOP")
    catalog_group.add_argument("--delete-catalog", action="store_true", help="default formal SOP behavior")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    applying = args.apply and not args.dry_run
    archive_dir = args.archive_dir or (args.papers_dir.parent / "transactions" / f"papers_rollback_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    report = rollback_formal_papers(
        papers_dir=args.papers_dir,
        paper_raw_dir=args.paper_raw_dir,
        ledger_path=args.ledger_path,
        all_catalog_path=args.all_catalog_path,
        archive_dir=archive_dir,
        all_papers=args.all,
        paper_id=args.paper_id,
        keep_catalog=args.keep_catalog,
        apply=applying,
    )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    summary = report.get("summary", {})
    return 1 if summary.get("blocking_errors") or summary.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())

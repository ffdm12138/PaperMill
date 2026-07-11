"""Validate the post-rollback, pre-catalog-regeneration paper_raw state."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import PAPER_NUMBER_LEDGER_PATH, PAPER_RAW_DIR, PAPERS_DIR
from src.path_utils import resolve_stored_path
from src.services.ingest_ids import PAPER_NUMBER_RE
from src.services.metadata_quality import bibliographic_identity_gate
from src.library.paper_number_ledger import PaperNumberLedger
from src.metadata.schema import validate_metadata_schema


FORBIDDEN_TOP = {"abstract", "keywords", "pdf", "content", "notes", "bibtex", "citation_key"}
FORBIDDEN_TITLE = {"short" + "_zh", "translated" + "_zh"}
FORBIDDEN_SOURCE = {"raw_record", "providers"}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _active_dirs(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".") and p.name != "quarantine")


def _has_formal_assets(folder: Path) -> bool:
    pid = folder.name
    return any((folder / f"{pid}.{suffix}").exists() for suffix in ("metadata.json", "catalog.json", "md", "pdf"))


def _has_paper_raw_workspace_assets(folder: Path) -> bool:
    if not folder.is_dir():
        return False
    if (folder / "images").is_dir() or (folder / "output").exists() or (folder / ".import_status.json").exists():
        return True
    if (folder / "stage_manifest.json").exists():
        return True
    for pattern in ("*.metadata.json", "*.md", "*.pdf", "*.paper.number", "*.conversion.json", "*.asset_manifest.json"):
        if list(folder.glob(pattern)):
            return True
    return False


def _source_record_exists(folder: Path, raw_record_path: str) -> bool:
    if not raw_record_path:
        return True
    rel = folder / raw_record_path
    if rel.exists():
        return True
    return resolve_stored_path(raw_record_path).exists()


def _metadata_errors(folder: Path, paper_number: str, metadata: dict[str, Any]) -> tuple[list[str], list[str], dict[str, Any]]:
    errors: list[str] = []
    warnings: list[str] = []
    states: dict[str, Any] = {}
    for err in validate_metadata_schema(metadata):
        errors.append(err)
    if metadata.get("paper_number") != paper_number:
        errors.append("metadata.paper_number must equal folder paper_number")
    if metadata.get("paper_raw_id") != paper_number:
        errors.append("metadata.paper_raw_id must equal folder paper_number")
    for key in sorted(FORBIDDEN_TOP):
        if key in metadata:
            errors.append(f"forbidden top-level key: {key}")
    title = metadata.get("title") if isinstance(metadata.get("title"), dict) else {}
    for key in sorted(FORBIDDEN_TITLE):
        if key in title:
            errors.append(f"forbidden title key: {key}")
    source = metadata.get("source") if isinstance(metadata.get("source"), dict) else {}
    for key in sorted(FORBIDDEN_SOURCE):
        if key in source:
            errors.append(f"forbidden source key: {key}")
    raw_record_path = str(source.get("raw_record_path") or "")
    if raw_record_path and not _source_record_exists(folder, raw_record_path):
        errors.append(f"source.raw_record_path does not exist: {raw_record_path}")
    citation_ready, reasons = bibliographic_identity_gate(metadata)
    receipt_path = folder / f"{paper_number}.metadata_match.json"
    receipt = _load_json(receipt_path) if receipt_path.is_file() else {}
    status = str(receipt.get("match_status") or "")
    states.update({
        "schema_valid": not validate_metadata_schema(metadata),
        "citation_ready": citation_ready,
        "matched_consistent": status not in {"matched", "manual_confirmed"} or citation_ready,
        "metadata_match_status": status,
        "citation_reasons": reasons,
    })
    if status == "matched" and not citation_ready:
        errors.append(f"metadata marked matched but not citation-ready: {', '.join(reasons)}")
    elif not citation_ready:
        warnings.append(f"metadata schema valid but citation not ready: {', '.join(reasons)}")
    return errors, warnings, states


def _check_empty_index(path: Path, *, schema_version: str, label: str) -> list[str]:
    if not path.exists():
        return []
    try:
        data = _load_json(path)
    except Exception as exc:
        return [f"{label} unreadable: {exc}"]
    errors: list[str] = []
    if str(data.get("schema_version") or "") != schema_version:
        errors.append(f"{label}.schema_version must be {schema_version}")
    papers = data.get("papers")
    if papers != []:
        errors.append(f"{label}.papers must be empty after full rollback")
    return errors


def validate_rolled_back_state(
    *,
    papers_dir: Path = PAPERS_DIR,
    paper_raw_dir: Path = PAPER_RAW_DIR,
    ledger_path: Path = PAPER_NUMBER_LEDGER_PATH,
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    errors: list[str] = []
    warnings: list[str] = []
    states: list[dict[str, Any]] = []

    for folder in _active_dirs(papers_dir):
        if _has_formal_assets(folder):
            errors.append(f"formal paper directory still exists after rollback: {folder}")

    ledger = PaperNumberLedger(ledger_path)
    ledger_data = ledger.load()
    active_raw_dirs = _active_dirs(paper_raw_dir)
    for folder in active_raw_dirs:
        if not PAPER_NUMBER_RE.match(folder.name) and _has_paper_raw_workspace_assets(folder):
            errors.append(f"non-numbered paper_raw workspace remains after full rollback: {folder}")
    raw_dirs = [p for p in active_raw_dirs if PAPER_NUMBER_RE.match(p.name)]
    raw_number_set = {p.name for p in raw_dirs}
    for folder in raw_dirs:
        number = folder.name
        required = [
            folder / f"{number}.metadata.json",
            folder / f"{number}.md",
            folder / f"{number}.pdf",
            folder / "images",
            folder / f"{number}.asset_manifest.json",
            folder / f"{number}.conversion.json",
            folder / f"{number}.paper.number",
        ]
        for path in required:
            if path.name == "images":
                if not path.is_dir():
                    errors.append(f"{number}: missing images/")
            elif not path.exists():
                errors.append(f"{number}: missing {path.name}")
        cats = sorted(folder.glob("*.catalog.json"))
        if cats:
            errors.append(f"{number}: catalog should be absent before regeneration: {', '.join(p.name for p in cats)}")
        meta_path = folder / f"{number}.metadata.json"
        if meta_path.exists():
            try:
                metadata = _load_json(meta_path)
            except Exception as exc:
                errors.append(f"{number}: metadata unreadable: {exc}")
            else:
                meta_errors, meta_warnings, state = _metadata_errors(folder, number, metadata)
                errors.extend([f"{number}: {err}" for err in meta_errors])
                warnings.extend([f"{number}: {warning}" for warning in meta_warnings])
                state["path"] = f"paper_raw/{number}"
                states.append(state)
        item = (ledger_data.get("items") or {}).get(number)
        if not item:
            errors.append(f"{number}: ledger item missing")
        else:
            state = item.get("state") or "active"
            if state != "reserved":
                errors.append(f"{number}: ledger state must be reserved, got {state}")
            stored = item.get("folder_path") or ""
            resolved = resolve_stored_path(stored)
            try:
                same = resolved.resolve() == folder.resolve()
            except OSError:
                same = False
            if stored and not same:
                errors.append(f"{number}: ledger folder_path must point to raw folder")

    # Post-rollback sweep: ledger must have no active items; any reserved item
    # without a corresponding paper_raw folder is an orphan (warning).
    for number, item in (ledger_data.get("items") or {}).items():
        if not PAPER_NUMBER_RE.match(number):
            errors.append(f"ledger: invalid paper_number key after rollback: {number}")
            continue
        if not isinstance(item, dict):
            errors.append(f"ledger: item must be object: {number}")
            continue
        item_state = item.get("state") or "active"
        if item_state == "active":
            errors.append(f"ledger: active item remains after rollback: {number}")
        elif item_state == "reserved" and number not in raw_number_set:
            warnings.append(f"ledger: reserved orphan (no paper_raw folder): {number}")


    for root in (papers_dir, paper_raw_dir):
        if not root.exists():
            continue
        for pattern in ("*.tmp", "*.bak", "*.old"):
            for path in root.rglob(pattern):
                if "quarantine" not in path.parts:
                    warnings.append(f"stale backup/temp file: {path}")
    return errors, warnings, states


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate rolled-back paper_raw state.")
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    args = parser.parse_args()

    errors, warnings, states = validate_rolled_back_state(
        papers_dir=args.papers_dir,
        paper_raw_dir=args.paper_raw_dir,
        ledger_path=args.ledger_path,
    )
    valid = not errors
    print(f"valid={'True' if valid else 'False'} errors={len(errors)} warnings={len(warnings)}")
    if states:
        print("\nMetadata states:")
        for state in states:
            print(
                "  "
                f"{state['path']}: "
                f"schema_valid={state['schema_valid']} "
                f"citation_ready={state['citation_ready']} "
                f"matched_consistent={state['matched_consistent']}"
            )
    if errors:
        print("\nErrors:")
        for error in errors:
            print(f"  {error}")
    if warnings:
        print("\nWarnings:")
        for warning in warnings:
            print(f"  {warning}")
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())

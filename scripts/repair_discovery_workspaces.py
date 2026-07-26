#!/usr/bin/env python
"""Explicit repair planner/writer for raw discovery workspaces."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from filelock import FileLock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

from config.settings import PAPER_NUMBER_LEDGER_PATH, PAPER_RAW_DIR, PAPERS_DIR
from src.discovery.workspace_registry import WorkspaceRegistrySnapshot, build_workspace_registry
from src.ingest.workspace_evidence import inspect_workspace_evidence
from src.ingest.workspace_readiness import evaluate_metadata_staged
from src.library.paper_number_ledger import PaperNumberLedger
from src.path_utils import resolve_stored_path
from src.services.ingest_state import METADATA_INCOMPLETE, write_import_status


def _duplicate_plan(
    snapshot: WorkspaceRegistrySnapshot, number: str,
) -> tuple[str, list[str], list[str]]:
    record = snapshot.records_by_number.get(number)
    if record is None:
        return "", [], []
    duplicate_numbers: set[str] = set()
    reasons: set[str] = set()
    for doi_ref in record.doi_refs:
        refs = snapshot.doi_index.lookup_doi(
            doi_ref.normalized_doi, exclude_paper_number=number)
        for ref in refs:
            duplicate_numbers.add(ref.paper_number)
        if refs:
            reasons.add(f"doi:{doi_ref.normalized_doi}")
    for identity in record.identity_refs:
        refs = snapshot.workspace_id_index.lookup(
            provider=identity.provider, keyword_id=identity.keyword_id,
            page_id=identity.page_id, candidate_id=identity.candidate_id,
            normalized_doi=identity.normalized_doi)
        others = [ref for ref in refs if ref.paper_number != number]
        for ref in others:
            duplicate_numbers.add(ref.paper_number)
        if others:
            reasons.add("identity:" + "|".join((
                identity.provider, identity.keyword_id, identity.page_id,
                identity.candidate_id, identity.normalized_doi)))
    if not duplicate_numbers:
        return "", [], []

    def priority(paper_number: str) -> tuple[int, str]:
        candidate = snapshot.records_by_number.get(paper_number)
        if candidate is None:
            return (0, paper_number)
        state = candidate.lifecycle.ledger_state
        rank = 3 if candidate.scope == "papers" and state == "active" else (
            2 if state == "metadata_staged" else 1)
        return (-rank, paper_number)

    participants = duplicate_numbers | {number}
    primary = min(participants, key=priority)
    return (primary if primary != number else "",
            sorted(duplicate_numbers), sorted(reasons))


def run(*, paper_raw: Path, papers: Path, ledger_path: Path, apply: bool,
        limit: int, paper_number: str = "") -> dict[str, object]:
    ledger = PaperNumberLedger(ledger_path)
    with FileLock(str(paper_raw / ".paper_raw_write.lock")) if apply else _NoopLock():
        data = ledger.load()
        selected = [(number, item) for number, item in sorted(data.get("items", {}).items())
                    if (not paper_number or number == paper_number)
                    and isinstance(item, dict)
                    and item.get("state") in {"reserved", "metadata_staged", "active"}]
        if paper_number and not selected:
            raise KeyError(f"paper_number not found or not repairable: {paper_number}")
        built = build_workspace_registry(
            paper_raw_dir=paper_raw, papers_dir=papers, ledger=ledger)
        registry_ready = bool(built.complete and built.registry is not None)
        registry_usable = built.registry is not None
        snapshot = built.registry
        rows: list[dict[str, object]] = []
        for number, item in selected[:limit]:
            folder = resolve_stored_path(str(item.get("folder_path") or ""))
            state = str(item.get("state") or "")
            expected_root = papers if state == "active" else paper_raw
            try:
                folder_in_scope = (
                    not folder.is_symlink()
                    and folder.resolve().parent == expected_root.resolve()
                    and folder.resolve().is_dir()
                )
            except OSError:
                folder_in_scope = False
            if folder_in_scope:
                evidence = inspect_workspace_evidence(
                    folder, ledger_item=item, expected_paper_number=number)
                readiness = evaluate_metadata_staged(evidence)
                issue_text = [str(issue) for issue in evidence.issues]
                corrupt = any(issue.category in {
                    "metadata_unreadable", "metadata_paper_number_mismatch",
                    "source_record_unreadable", "receipt_unreadable",
                    "receipt_paper_number_mismatch", "marker_mismatch",
                    "stage_manifest_unreadable", "stage_manifest_paper_number_mismatch",
                    "asset_manifest_unreadable", "import_status_unreadable",
                    "ledger_folder_path_mismatch",
                } for issue in evidence.issues)
            else:
                evidence = None
                readiness = None
                issue_text = ["workspace_outside_expected_root_or_missing"]
                corrupt = False
            duplicate_of = ""
            duplicate_numbers: list[str] = []
            duplicate_reasons: list[str] = []
            if snapshot is not None and number in snapshot.records_by_number:
                duplicate_of, duplicate_numbers, duplicate_reasons = _duplicate_plan(
                    snapshot, number)
            if not folder_in_scope:
                action = "repair_required_workspace_mismatch"
            elif corrupt:
                action = "repair_required_corrupt_json"
            elif state == "active":
                action = "formal_report_only"
            elif state == "metadata_staged" and readiness is not None and not readiness.ready:
                if readiness.profile is None and evidence is not None and evidence.stage_manifest_present:
                    action = "repair_required_unknown_workflow"
                elif corrupt:
                    action = "repair_required_corrupt_json"
                else:
                    action = "demote_metadata_staged_to_reserved"
            elif not registry_usable:
                action = "repair_required_registry"
            elif duplicate_of:
                action = "quarantine_duplicate"
            elif state == "reserved" and readiness is not None and readiness.ready:
                action = "promote_metadata_staged"
            elif state == "reserved":
                action = "keep_reserved"
            elif readiness is not None and not readiness.ready:
                action = "repair_required"
            else:
                action = "report_only"
            rows.append({"paper_number": number, "state": state, "folder": str(folder),
                         "ready": bool(readiness and readiness.ready),
                         "missing": list(readiness.missing) if readiness else [],
                         "issues": issue_text, "action": action,
                         "duplicate_of": duplicate_of,
                         "duplicate_numbers": duplicate_numbers,
                         "duplicate_reasons": duplicate_reasons,
                         "applied": False})
        promoted = 0
        quarantined = 0
        if apply and (registry_usable or any(
            row["action"] == "demote_metadata_staged_to_reserved" for row in rows
        )):
            for row in rows:
                action = str(row["action"])
                number = str(row["paper_number"])
                folder = Path(str(row["folder"]))
                if action == "promote_metadata_staged":
                    ledger.mark_metadata_staged(number, folder)
                    promoted += 1
                    row["applied"] = True
                elif action == "quarantine_duplicate":
                    ledger.quarantine_reserved_duplicate(
                        number, folder, duplicate_of=str(row["duplicate_of"]),
                        duplicate_reasons=list(row["duplicate_reasons"]))
                    quarantined += 1
                    row["applied"] = True
                elif action == "demote_metadata_staged_to_reserved":
                    ledger.demote_metadata_staged_to_reserved(
                        number, folder,
                        reason="metadata_staged_workspace_incomplete:" +
                        ",".join(str(value) for value in row["missing"]),
                    )
                    write_import_status(
                        folder, METADATA_INCOMPLETE,
                        reason="metadata_staged_workspace_incomplete",
                        warnings=list(row["missing"]),
                    )
                    row["applied"] = True
    repair_usable = registry_usable or any(
        row["action"] == "demote_metadata_staged_to_reserved" for row in rows
    )
    return {"audit_only": not apply, "selected": len(rows), "promoted": promoted,
            "quarantined": quarantined, "paper_raw": str(paper_raw),
            "papers": str(papers), "registry_complete": registry_ready,
            "registry_usable_for_repair": repair_usable,
            "registry_issues": [str(issue) for issue in built.issues], "items": rows}


class _NoopLock:
    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb): return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--audit-only", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--paper-number", default="")
    parser.add_argument("--paper-raw", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--papers", type=Path, default=PAPERS_DIR)
    parser.add_argument("--ledger", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    parser.add_argument("--json-report", type=Path)
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be positive")
    report = run(paper_raw=args.paper_raw, papers=args.papers, ledger_path=args.ledger,
                 apply=args.apply, limit=args.limit, paper_number=args.paper_number)
    if args.json_report:
        args.json_report.parent.mkdir(parents=True, exist_ok=True)
        args.json_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["registry_usable_for_repair"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

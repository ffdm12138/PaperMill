#!/usr/bin/env python
"""Read-only audit of raw/formal discovery identity coverage."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import PAPER_NUMBER_LEDGER_PATH
from src.discovery.workspace_index import DiscoveryWorkspaceIndex
from src.discovery.workspace_registry import (
    WorkspaceScanRecord,
    build_workspace_registry,
    classify_record_issues,
    scan_workspace_record,
)
from src.library.paper_number_ledger import PaperNumberLedger
from src.library.paper_number_state import ALL_LEDGER_STATES, TERMINAL_LEDGER_STATES
from src.path_utils import resolve_stored_path
from src.services.duplicate_index import DuplicateIndex
from src.utils.identifiers import PAPER_NUMBER_RE


def _conflicts(index: Any) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for doi, refs in index.doi_to_refs.items():
        for ref in refs:
            grouped[doi].append({"paper_number": ref.paper_number, "scope": ref.scope,
                                 "folder": ref.folder})
    return [{"doi": doi, "refs": refs} for doi, refs in sorted(grouped.items())
            if len({(ref["scope"], ref["paper_number"]) for ref in refs}) > 1]


def _healthy_records_for_partial_audit(
    *, data: dict[str, Any], paper_raw: Path, papers: Path,
    excluded_numbers: set[str],
) -> tuple[dict[str, WorkspaceScanRecord], list[str]]:
    """Recover only independently valid records for diagnostic statistics.

    The authoritative Registry remains incomplete.  This second, audit-only
    pass merely prevents one damaged workspace from erasing conflicts among
    unrelated healthy workspaces.
    """
    records: dict[str, WorkspaceScanRecord] = {}
    issues: list[str] = []
    items = data.get("items") if isinstance(data.get("items"), dict) else {}
    raw_root = paper_raw.resolve()
    formal_root = papers.resolve()
    for number, item in sorted(items.items()):
        if number in excluded_numbers or not PAPER_NUMBER_RE.fullmatch(str(number)):
            continue
        if not isinstance(item, dict):
            continue
        state = str(item.get("state") or "")
        if state not in ALL_LEDGER_STATES or state in TERMINAL_LEDGER_STATES:
            continue
        scope = "papers" if state == "active" else "paper_raw"
        expected_root = formal_root if scope == "papers" else raw_root
        folder = resolve_stored_path(str(item.get("folder_path") or ""))
        try:
            resolved = folder.resolve()
            if resolved.parent != expected_root or not resolved.is_dir():
                continue
            record = scan_workspace_record(resolved, str(number), scope, item)
            record_issues = classify_record_issues(
                ledger_state=state, evidence=record.evidence,
                readiness=record.readiness)
        except Exception as exc:
            issues.append(f"partial_audit_scan_failed:{number}:{type(exc).__name__}:{exc}")
            continue
        if record_issues:
            issues.extend(str(issue) for issue in record_issues)
            continue
        records[str(number)] = record
    return records, issues


def _indexes_from_records(
    records: dict[str, WorkspaceScanRecord],
) -> tuple[DuplicateIndex, DiscoveryWorkspaceIndex]:
    doi_index = DuplicateIndex()
    workspace_ids = DiscoveryWorkspaceIndex()
    for record in records.values():
        for ref in record.doi_refs:
            doi_index.add_doi_ref(ref.as_duplicate_ref())
        for ref in record.identity_refs:
            workspace_ids.add_or_merge(ref)
    return doi_index.freeze(), workspace_ids.freeze()


def _identity_conflicts(index: DiscoveryWorkspaceIndex) -> list[dict[str, Any]]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for ref in index.refs:
        key = "|".join((ref.provider, ref.keyword_id, ref.page_id,
                        ref.candidate_id, ref.normalized_doi))
        grouped[key].append(ref)
    conflicts = []
    for key, refs in grouped.items():
        numbers = {ref.paper_number for ref in refs}
        if len(numbers) > 1:
            conflicts.append({"identity": key, "paper_numbers": sorted(numbers)})
    return sorted(conflicts, key=lambda item: item["identity"])


def audit(*, paper_raw: Path, papers: Path, ledger_path: Path) -> dict[str, Any]:
    ledger = PaperNumberLedger(ledger_path)
    try:
        data = ledger.load()
        ledger_error = ""
    except Exception as exc:
        data = ledger.empty_data()
        ledger_error = f"{type(exc).__name__}:{exc}"
    items = data.get("items") if isinstance(data.get("items"), dict) else {}
    states = Counter(str((item or {}).get("state") or "") for item in items.values())
    raw_dirs = {path.name: path for path in paper_raw.iterdir()
                if path.is_dir() and PAPER_NUMBER_RE.fullmatch(path.name)} if paper_raw.exists() else {}
    formal_dirs = [path for path in papers.iterdir()
                   if path.is_dir() and not path.name.startswith(".")] if papers.exists() else []
    active_paths = {resolve_stored_path(str(item.get("folder_path") or "")).resolve()
                    for item in items.values() if isinstance(item, dict)
                    and item.get("state") == "active" and item.get("folder_path")}
    try:
        result = build_workspace_registry(
            paper_raw_dir=paper_raw, papers_dir=papers, ledger=ledger)
        build_error = ""
    except Exception as exc:
        result = None
        build_error = f"{type(exc).__name__}:{exc}"
    snapshot = result.registry if result else None
    authoritative_issues = list(result.issues) if result else []
    if snapshot is not None:
        records = dict(snapshot.records_by_number)
        doi_index = snapshot.doi_index
        workspace_ids = snapshot.workspace_id_index
        partial_issues: list[str] = []
    else:
        excluded = {issue.paper_number for issue in authoritative_issues if issue.paper_number}
        records, partial_issues = _healthy_records_for_partial_audit(
            data=data, paper_raw=paper_raw, papers=papers,
            excluded_numbers=excluded)
        doi_index, workspace_ids = _indexes_from_records(records)
    doi_conflicts = _conflicts(doi_index)
    cross = [entry for entry in doi_conflicts
             if {ref["scope"] for ref in entry["refs"]} == {"paper_raw", "papers"}]
    raw_conflicts = [entry for entry in doi_conflicts
                     if {ref["scope"] for ref in entry["refs"]} == {"paper_raw"}]
    formal_conflicts = [entry for entry in doi_conflicts
                        if {ref["scope"] for ref in entry["refs"]} == {"papers"}]
    ledger_paths = {resolve_stored_path(str(item.get("folder_path") or "")).resolve()
                    for item in items.values() if isinstance(item, dict) and item.get("folder_path")}
    identity_conflicts = _identity_conflicts(workspace_ids)
    active_numbers = {str(number) for number, item in items.items()
                      if isinstance(item, dict) and item.get("state") == "active"}
    stable_active = {number for number, record in records.items()
                     if record.scope == "papers" and (record.doi_refs or record.identity_refs)}
    invalid_active = sorted(active_numbers - stable_active)
    workspace_issues = [str(issue) for issue in authoritative_issues]
    workspace_issues.extend(partial_issues)
    if build_error:
        workspace_issues.append(f"registry_build_failed:{build_error}")
    registry_complete = bool(result and result.complete and snapshot is not None)
    return {
        "ledger_path": str(ledger_path), "ledger_error": ledger_error,
        "registry_build_error": build_error,
        "ledger_total_entries": len(items), "ledger_state_counts": dict(sorted(states.items())),
        "active_ledger_entries": states.get("active", 0),
        "metadata_staged": states.get("metadata_staged", 0),
        "reserved": states.get("reserved", 0), "abandoned": states.get("abandoned", 0),
        "illegal_quarantined_duplicate": states.get("quarantined_duplicate", 0),
        "paper_raw_workspace_count": len(raw_dirs), "formal_workspace_count": len(formal_dirs),
        "orphan_paper_raw": sorted(set(raw_dirs) - set(items)),
        "orphan_papers": sorted(str(path) for path in formal_dirs if path.resolve() not in ledger_paths),
        "ledger_folder_path_mismatches": [str(issue) for issue in authoritative_issues
                                            if issue.code.value == "ledger_folder_mismatch"],
        "workspace_issues": workspace_issues,
        "paper_raw_doi_conflicts": raw_conflicts, "papers_doi_conflicts": formal_conflicts,
        "cross_scope_doi_conflicts": cross, "identity_conflicts": identity_conflicts,
        "repair_backlog_count": len(result.unsettled_numbers) if result else 0,
        "repair_backlog_numbers": list(result.unsettled_numbers) if result else [],
        "formal_publication_generation": snapshot.formal_generation if snapshot else None,
        "unindexable_active_formal_numbers": invalid_active,
        "active_formal_paths_missing_from_disk": sorted(
            str(path) for path in active_paths if not path.is_dir()),
        "registry_complete": registry_complete,
        "conflict_analysis_complete": registry_complete,
        "partial_healthy_record_count": len(records) if not registry_complete else 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paper-raw", type=Path, required=True)
    parser.add_argument("--papers", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    parser.add_argument("--json-report", type=Path, required=True)
    args = parser.parse_args(argv)
    report = audit(paper_raw=args.paper_raw, papers=args.papers, ledger_path=args.ledger)
    args.json_report.parent.mkdir(parents=True, exist_ok=True)
    args.json_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["ledger_error"] or not report["registry_complete"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

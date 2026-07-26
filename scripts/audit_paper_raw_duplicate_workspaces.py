"""Audit paper_raw workspaces for PDF/DOI duplicates and optionally clean them up.

This is a workspace-level audit: it groups physical ``data/paper_raw/<workspaces>``
that share a PDF sha256 / md5 / DOI and decides, per group, which workspace to
KEEP and which to drop (quarantine). The dedup index itself
(``src.ingest.duplicate_guard.build_ingest_duplicate_index``) covers both
16-digit numbered workspaces and legacy/untitled workspaces; this script consumes
that index and resolves a keep-rule per duplicate group.

Default mode is a DRY RUN: prints a report, exits 0 (no drops) or 1 (drops
pending). ``--apply-cleanup`` moves the ruled-out workspaces into
``data/paper_raw/quarantine/duplicate_workspaces/<name>/`` (never deletes),
rewrites their ``.import_status.json`` to ``quarantined_duplicate``, and marks
the matching ledger entry ``state=abandoned`` with canonical quarantine
provenance. Paper numbers are NEVER recycled and ``max_number``
is NEVER decremented.

Idempotent: a repeated ``--apply-cleanup`` finds nothing because quarantined
duplicates are excluded from the candidate pool.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

from config.settings import PAPER_NUMBER_LEDGER_PATH, PAPER_RAW_DIR
from src.ingest.duplicate_guard import (
    DuplicateIndex,
    build_ingest_duplicate_index,
)
from src.ingest.import_status import now_iso, read_import_status, write_import_status
from src.library.paper_number_ledger import PaperNumberLedger
from src.utils.atomic_io import atomic_write_json


# Workspace stage ranking. A workspace that has progressed further through the
# ingest pipeline (or has human-curated metadata/catalog on a legacy folder) is
# always kept over a freshly-restaged duplicate. The ledger ``state`` and the
# ``.import_status.json`` ``status`` are two views of the same state machine; the
# effective rank is the max of the two. Legacy untitled folders often have no
# ledger entry for their marker paper_number but DO carry an import_status, so
# import_status must be a complete ranking (not a sparse bump).
_STAGE_RANK = {
    # commit
    "committed": 60,
    "ready_for_commit": 50,
    "imported": 55,
    # curate / catalog
    "catalog_ready": 40,
    # convert
    "converted": 35,
    # metadata
    "metadata_matched": 30,
    "metadata_manual_review_required": 28,
    "metadata_candidates_found": 25,
    "metadata_resolve_failed": 22,  # curated far enough to ATTEMPT resolution
    "metadata_candidate_conflict": 22,
    "metadata_invalid": 18,
    "metadata_unmatched": 18,
    "metadata_incomplete": 18,
    # staging
    "ready_for_convert": 15,
    "catalog_generation_failed": 14,
    "catalog_invalid": 14,
    "formalize_failed": 14,
    # ledger baseline
    "reserved": 10,
    "active": 12,
    "": 5,
}


def _state_rank(value: str) -> int:
    return _STAGE_RANK.get(str(value or ""), 5)


_QUARANTINE_DIR_NAME = "quarantine"
_DUPLICATE_HOLDING_DIR = "duplicate_workspaces"


@dataclass
class WorkspaceInfo:
    folder: Path
    paper_number: str
    paper_raw_id: str
    state: str
    import_status: str
    has_paper_number_marker: bool
    asset_count: int

    @property
    def rank(self) -> int:
        return max(_state_rank(self.state), _state_rank(self.import_status))


def _workspace_state(ledger: PaperNumberLedger, info: WorkspaceInfo) -> None:
    if info.paper_number:
        item = ledger.load().get("items", {}).get(info.paper_number) or {}
        info.state = str(item.get("state") or "")


def _asset_count(folder: Path) -> int:
    count = 0
    for pattern in ("*.pdf", "*.md", "*.metadata.json", "*.catalog.json", "*.paper.number"):
        count += sum(1 for _ in folder.glob(pattern))
    if (folder / "images").is_dir():
        count += 1
    if (folder / "output").is_dir():
        count += 1
    return count


def _gather_workspaces(paper_raw_dir: Path, ledger: PaperNumberLedger) -> list[WorkspaceInfo]:
    """Collect candidate workspaces, excluding quarantined duplicate holding dir."""
    from src.ingest.duplicate_guard import is_paper_raw_workspace, resolve_paper_raw_identity

    out: list[WorkspaceInfo] = []
    if not paper_raw_dir.exists():
        return out
    for folder in sorted(p for p in paper_raw_dir.iterdir() if p.is_dir()):
        if folder.name == _QUARANTINE_DIR_NAME:
            continue  # quarantined workspaces are never cleanup candidates
        if not is_paper_raw_workspace(folder):
            continue
        paper_number, paper_raw_id = resolve_paper_raw_identity(folder)
        import_status = str(read_import_status(folder).get("status") or "")
        info = WorkspaceInfo(
            folder=folder,
            paper_number=paper_number,
            paper_raw_id=paper_raw_id,
            state="",
            import_status=import_status,
            has_paper_number_marker=any(folder.glob("*.paper.number")),
            asset_count=_asset_count(folder),
        )
        _workspace_state(ledger, info)
        out.append(info)
    return out


def _ref_to_workspace(refs_by_folder: dict[str, WorkspaceInfo], folder_str: str) -> WorkspaceInfo | None:
    for info in refs_by_folder.values():
        if Path(info.folder).name == Path(folder_str).name:
            return info
    return None


def _decide_keep(workspaces: list[WorkspaceInfo]) -> WorkspaceInfo:
    """Pick the workspace to KEEP per the keep-rule. Deterministic, highest-rank wins."""
    def sort_key(info: WorkspaceInfo):
        # higher rank, then more assets, then legacy-with-marker wins (marker
        # present => marker_rank 0 sorts first), then lexicographically lowest name.
        marker_rank = 0 if info.has_paper_number_marker else 1
        return (-info.rank, -info.asset_count, marker_rank, info.folder.name)
    return sorted(workspaces, key=sort_key)[0]


def _build_groups(index: DuplicateIndex, workspaces: list[WorkspaceInfo]) -> list[dict[str, Any]]:
    refs_by_folder: dict[str, WorkspaceInfo] = {Path(w.folder).name: w for w in workspaces}

    groups: list[dict[str, Any]] = []
    seen_drops: set[str] = set()

    def _group(key_name: str, mapping: dict[str, list], reason: str) -> None:
        for value, refs in sorted(mapping.items()):
            if not value or len(refs) < 2:
                continue
            members = []
            for ref in refs:
                ws = _ref_to_workspace(refs_by_folder, ref.folder)
                if ws is None:
                    continue
                members.append(ws)
            if len(members) < 2:
                continue
            keep = _decide_keep(members)
            drops = [m for m in members if m is not keep]
            drop_keys = {Path(d.folder).name for d in drops}
            # If a drop already ruled out by an earlier (stronger sha) group, skip
            new_drops = {k for k in drop_keys if k not in seen_drops}
            if not new_drops:
                continue
            groups.append({
                "evidence": {key_name: value},
                "duplicate_reason": reason,
                "keep": _ws_dict(keep),
                "drop": [_ws_dict(d) for d in drops if Path(d.folder).name in new_drops],
            })
            seen_drops.update(new_drops)

    _group("pdf_sha256", index.pdf_sha256_to_refs, "pdf_sha256_duplicate")
    _group("pdf_md5", index.pdf_md5_to_refs, "pdf_md5_duplicate")
    _group("doi", index.doi_to_refs, "doi_duplicate")
    return groups


def _ws_dict(info: WorkspaceInfo) -> dict[str, Any]:
    return {
        "folder": str(info.folder),
        "paper_number": info.paper_number,
        "paper_raw_id": info.paper_raw_id,
        "state": info.state,
        "import_status": info.import_status,
        "asset_count": info.asset_count,
        "has_paper_number_marker": info.has_paper_number_marker,
    }


def build_report(*, paper_raw_dir: Path, ledger_path: Path) -> dict[str, Any]:
    ledger = PaperNumberLedger(ledger_path)
    workspaces = _gather_workspaces(paper_raw_dir, ledger)
    index = build_ingest_duplicate_index(paper_raw_dir=paper_raw_dir, include_quarantine=False)
    groups = _build_groups(index, workspaces)
    return {
        "schema": "paper_raw_duplicate_workspace_audit",
        "generated_at": now_iso(),
        "paper_raw_dir": str(paper_raw_dir),
        "ledger_path": str(ledger_path),
        "workspace_count": len(workspaces),
        "duplicate_group_count": len(groups),
        "groups": groups,
        "pending_drop_count": sum(len(g["drop"]) for g in groups),
    }


def _apply_cleanup(report: dict[str, Any], *, paper_raw_dir: Path, ledger_path: Path) -> dict[str, Any]:
    ledger = PaperNumberLedger(ledger_path)
    quarantine_root = paper_raw_dir / _QUARANTINE_DIR_NAME / _DUPLICATE_HOLDING_DIR
    quarantine_root.mkdir(parents=True, exist_ok=True)

    moved: list[dict[str, Any]] = []
    for group in report["groups"]:
        keep = group["keep"]
        keep_folder = Path(keep["folder"])
        keep_rank = _rank_from_dict(keep)
        for drop in group["drop"]:
            drop_folder = Path(drop["folder"])
            drop_rank = _rank_from_dict(drop)
            # Hard veto: never drop a workspace whose rank is strictly HIGHER
            # than the kept one. Same-rank duplicates are resolved by the
            # tie-break (assets -> marker -> name) and cleared normally.
            if drop_rank > keep_rank:
                raise RuntimeError(
                    f"refusing to drop {drop_folder.name} (rank {drop_rank}) "
                    f"> keep {keep_folder.name} (rank {keep_rank}); aborting cleanup"
                )
            target = quarantine_root / drop_folder.name
            if target.exists():
                # already quarantined (idempotent re-run)
                continue
            shutil.move(str(drop_folder), str(target))
            previous_status = str(read_import_status(target).get("status") or "")
            write_import_status(
                target,
                "quarantined_duplicate",
                reason=f"duplicate of {keep_folder.name} ({keep.get('paper_number', '')})",
                extra={
                    "duplicate_of": keep_folder.name,
                    "duplicate_of_paper_number": keep.get("paper_number", ""),
                    "duplicate_reason": group["duplicate_reason"],
                    "quarantined_at": now_iso(),
                    "previous_status": previous_status,
                },
            )
            moved.append({
                "from": str(drop_folder),
                "to": str(target),
                "paper_number": drop["paper_number"],
                "previous_status": previous_status,
            })

            # Ledger update: mark quarantined, repoint folder_path via the
            # canonical quarantine method. Never recycles numbers.
            if drop["paper_number"]:
                try:
                    ledger.quarantine_reserved_duplicate(
                        drop["paper_number"],
                        target,
                        duplicate_of=str(keep.get("paper_number", "")),
                        duplicate_reasons=[group.get("duplicate_reason", "")],
                    )
                except ValueError:
                    # Already-quarantined or incompatible state; skip silently.
                    pass

    manifest_path = paper_raw_dir / _QUARANTINE_DIR_NAME / f"duplicate_cleanup_{now_iso().replace(':', '')}.json"
    manifest = {**report, "applied": True, "moved": moved}
    atomic_write_json(manifest_path, manifest, indent=2)
    return {"manifest_path": str(manifest_path), "moved_count": len(moved)}


def _rank_from_dict(ws_dict: dict[str, Any]) -> int:
    return max(_state_rank(ws_dict.get("state")), _state_rank(ws_dict.get("import_status")))


def _print_human_report(report: dict[str, Any]) -> None:
    print(f"paper_raw duplicate workspace audit — {report['workspace_count']} workspaces, "
          f"{report['duplicate_group_count']} duplicate group(s), "
          f"{report['pending_drop_count']} pending drop(s)")
    for group in report["groups"]:
        keep = group["keep"]
        print(f"\n[group {group['evidence']} → keep {keep['folder']} (pn={keep['paper_number']}, state={keep['state']})]")
        for drop in group["drop"]:
            print(f"    DROP {drop['folder']} (pn={drop['paper_number']}, state={drop['state']})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit/clean duplicate paper_raw workspaces.")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON report")
    parser.add_argument("--apply-cleanup", action="store_true", help="move ruled-out workspaces to quarantine")
    parser.add_argument("--strict", action="store_true", help="exit 1 if any drops are pending")
    args = parser.parse_args(argv)

    if args.apply_cleanup and not args.ledger_path:
        parser.error("--ledger-path is required with --apply-cleanup")

    report = build_report(paper_raw_dir=args.paper_raw_dir, ledger_path=args.ledger_path)

    if args.apply_cleanup:
        result = _apply_cleanup(report, paper_raw_dir=args.paper_raw_dir, ledger_path=args.ledger_path)
        if args.json:
            print(json.dumps({"applied": True, **result}, ensure_ascii=False, indent=2))
        else:
            print(f"cleanup applied: {result['moved_count']} workspace(s) moved -> {result['manifest_path']}")
        return 0

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human_report(report)

    if args.strict and report["pending_drop_count"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

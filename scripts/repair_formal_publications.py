#!/usr/bin/env python
"""Audit and explicitly repair legacy formal publication identity sidecars.

The writer is intentionally narrow.  It may only canonicalize the marker and
asset-manifest identity aliases when a temporary copy passes the current
formal-paper validator.  Any closure/hash/freeze defect is reported as a
rollback/recommit requirement and is never rewritten in place.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

from config.settings import PAPER_NUMBER_LEDGER_PATH, PAPERS_DIR, TRANSACTION_ROOT
from src.library.formal_publication import (
    publish_formal_publication_state_unlocked,
    publication_state_path,
)
from src.utils.file_fingerprint import compute_sha256
from src.ingest.locking import (
    INDEX_PUBLISH_RANK,
    LEDGER_RANK,
    PAPERS_INSTALL_RANK,
    LockRequest,
    acquire_locks,
    transaction_requests,
)
from src.library.paper_number_ledger import PaperNumberLedger
from src.library.validation import validate_formal_paper
from src.utils.path_utils import resolve_stored_path
from src.utils.atomic_io import atomic_write_json


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must be a JSON object")
    return value


def _identity_patch(folder: Path, number: str, item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    paper_name = str(item.get("paper_name") or folder.name)
    if paper_name != folder.name:
        raise ValueError("ledger paper_name/folder mismatch")
    marker_paths = sorted(folder.glob("*.paper.number"))
    manifests = sorted(folder.glob("*.asset_manifest.json"))
    if len(marker_paths) != 1 or marker_paths[0].name != f"{number}.paper.number":
        raise ValueError("formal marker identity/path is not canonical")
    if len(manifests) != 1 or manifests[0].name != f"{paper_name}.asset_manifest.json":
        raise ValueError("formal asset manifest path is not canonical")
    marker = _read_json(marker_paths[0])
    manifest = _read_json(manifests[0])
    if marker.get("paper_number") != number or marker.get("folder_name") != paper_name:
        raise ValueError("formal marker identity mismatch")
    if marker.get("state") != "active":
        raise ValueError("formal marker is not active")
    if manifest.get("paper_number") != number or manifest.get("stage") != "papers":
        raise ValueError("formal manifest identity/stage mismatch")
    marker_name = marker.get("planned_paper_name")
    marker_alias = marker.get("planned_paper_id")
    if marker_name not in (None, "", paper_name):
        raise ValueError("formal marker has conflicting planned_paper_name")
    if marker_alias not in (None, "", paper_name):
        raise ValueError("formal marker has conflicting planned_paper_id")
    manifest_name = manifest.get("paper_name")
    manifest_alias = manifest.get("paper_id")
    if manifest_name not in (None, "", paper_name):
        raise ValueError("formal manifest has conflicting paper_name")
    if manifest_alias not in (None, "", paper_name):
        raise ValueError("formal manifest has conflicting paper_id")
    patched_marker = dict(marker)
    patched_marker["planned_paper_name"] = paper_name
    patched_marker.pop("planned_paper_id", None)
    patched_manifest = dict(manifest)
    patched_manifest["paper_name"] = paper_name
    patched_manifest.pop("paper_id", None)
    return patched_marker, patched_manifest, []


def _validate_prospective(
    folder: Path, number: str, item: dict[str, Any]
) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    """Return action and prospective sidecars after validation in temp space."""
    try:
        marker, manifest, _ = _identity_patch(folder, number, item)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return "rollback_recommit_required", None, None, [str(exc)]
    with tempfile.TemporaryDirectory(prefix="mineru-formal-repair-") as temp:
        candidate = Path(temp) / folder.name
        shutil.copytree(folder, candidate, symlinks=True)
        marker_path = candidate / f"{number}.paper.number"
        manifest_path = candidate / f"{folder.name}.asset_manifest.json"
        atomic_write_json(marker_path, marker, indent=2)
        manifest = dict(manifest)
        hashes = dict(manifest.get("asset_hashes") or {})
        hashes["paper_number_marker"] = compute_sha256(marker_path)
        manifest["asset_hashes"] = hashes
        atomic_write_json(manifest_path, manifest, indent=2)
        try:
            validate_formal_paper(candidate, expected_paper_name=folder.name)
        except (OSError, ValueError) as exc:
            return "rollback_recommit_required", None, None, [str(exc)]
    return "repair_identity", marker, manifest, []


def _rows(*, papers: Path, ledger: PaperNumberLedger, limit: int,
          paper_number: str = "") -> list[dict[str, Any]]:
    data = ledger.load()
    selected = [
        (str(number), item) for number, item in sorted((data.get("items") or {}).items())
        if isinstance(item, dict) and item.get("state") == "active"
        and (not paper_number or str(number) == paper_number)
    ]
    if paper_number and not selected:
        raise KeyError(f"active paper_number not found: {paper_number}")
    rows: list[dict[str, Any]] = []
    for number, item in selected[:limit]:
        folder = resolve_stored_path(str(item.get("folder_path") or ""))
        row: dict[str, Any] = {
            "paper_number": number, "folder": str(folder),
            "paper_name": str(item.get("paper_name") or ""),
            "action": "repair_required", "applied": False,
            "issues": [],
        }
        if folder.is_symlink() or not folder.is_dir() or folder.parent.resolve() != papers.resolve():
            row["action"] = "rollback_recommit_required"
            row["issues"] = ["formal path is missing or outside papers root"]
        else:
            action, marker, manifest, issues = _validate_prospective(folder, number, item)
            row["action"] = action
            if action == "repair_identity":
                row["marker"] = marker
                row["manifest"] = manifest
            else:
                row["issues"] = issues or ["formal closure is not repairable"]
        rows.append(row)
    return rows


def run(*, papers: Path, ledger_path: Path, transactions: Path,
        apply: bool, limit: int, paper_number: str = "") -> dict[str, Any]:
    if limit < 1:
        raise ValueError("limit must be positive")
    ledger = PaperNumberLedger(ledger_path)
    rows = _rows(papers=papers, ledger=ledger, limit=limit, paper_number=paper_number)
    repairable = [row for row in rows if row["action"] == "repair_identity"]
    if apply and any(row["action"] == "rollback_recommit_required" for row in rows):
        return {
            "audit_only": False, "applied": 0, "rows": rows,
            "blocked": True, "papers": str(papers),
        }
    if apply and repairable:
        numbers = [str(row["paper_number"]) for row in repairable]
        state_lock = Path(str(publication_state_path(papers)) + ".lock")
        with acquire_locks(
            *transaction_requests(transactions / "locks", numbers),
            LockRequest.path_lock(LEDGER_RANK, ledger.lock_path),
            LockRequest.path_lock(PAPERS_INSTALL_RANK, papers / ".papers_install.lock"),
            LockRequest.path_lock(INDEX_PUBLISH_RANK, state_lock),
        ):
            current = ledger.load()
            for row in repairable:
                number = str(row["paper_number"])
                item = (current.get("items") or {}).get(number)
                if not isinstance(item, dict) or item.get("state") != "active":
                    raise RuntimeError(f"formal ledger changed during repair: {number}")
                folder = resolve_stored_path(str(item.get("folder_path") or ""))
                action, marker, manifest, _ = _validate_prospective(folder, number, item)
                if action != "repair_identity" or marker is None or manifest is None:
                    raise RuntimeError(f"formal repair became unsafe: {number}")
                atomic_write_json(folder / f"{number}.paper.number", marker, indent=2)
                atomic_write_json(
                    folder / f"{folder.name}.asset_manifest.json", manifest, indent=2,
                )
                validate_formal_paper(folder, expected_paper_name=folder.name)
                row["applied"] = True
            publish_formal_publication_state_unlocked(
                papers_dir=papers, ledger_items=current.get("items") or {},
                allow_initialize=True,
            )
    return {
        "audit_only": not apply, "applied": sum(bool(row["applied"]) for row in rows),
        "rows": rows, "blocked": False, "papers": str(papers),
        "publication_state": str(publication_state_path(papers)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--audit-only", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--paper-number", default="")
    parser.add_argument("--papers", type=Path, default=PAPERS_DIR)
    parser.add_argument("--ledger", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    parser.add_argument("--transactions", type=Path, default=TRANSACTION_ROOT)
    parser.add_argument("--json-report", type=Path)
    args = parser.parse_args(argv)
    report = run(
        papers=args.papers, ledger_path=args.ledger, transactions=args.transactions,
        apply=args.apply, limit=args.limit, paper_number=args.paper_number,
    )
    if args.json_report:
        args.json_report.parent.mkdir(parents=True, exist_ok=True)
        args.json_report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    unsafe = any(
        row.get("action") == "rollback_recommit_required"
        for row in report.get("rows", [])
    )
    return 1 if report.get("blocked") or unsafe else 0


if __name__ == "__main__":
    raise SystemExit(main())

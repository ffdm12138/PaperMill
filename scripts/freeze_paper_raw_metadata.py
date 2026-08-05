"""Freeze citation-ready metadata after a verified PDF identity match.

The identity receipt is written by fetch/convert/resolve; this script is the
standalone freeze phase.  Freeze order is: write the valid freeze receipt
first, then update ``.import_status.json`` to ``frozen`` — a crash between
the two is repaired by the next run (status fixed only, no revision bump,
no timestamp change).

``--all-eligible`` freezes every workspace whose receipt final decision is
``matched`` and whose closure validates; single-workspace failures never
abort the batch.  Maintenance mode (identity migration in progress) is
enforced at entry and again inside the global write lock.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

from loguru import logger

from config.settings import PAPER_RAW_DIR
from src.metadata.freeze import assert_metadata_frozen, freeze_metadata
from src.ingest.status import read_status, update_status
from src.ingest.workspace import PaperRawWorkspace


def _eligibility(folder: Path, paper_number: str) -> tuple[bool, str]:
    """Return (eligible, reason).  Already-frozen workspaces must verify
    their closure; a valid freeze with a non-frozen status is repaired by
    the caller (crash-recovery semantics)."""
    from src.metadata.pdf_match import validate_metadata_match_receipt

    receipt_path = folder / f"{paper_number}.metadata_match.json"
    freeze_path = folder / f"{paper_number}.metadata_freeze.json"
    if not receipt_path.is_file():
        return False, "no match receipt"
    if freeze_path.is_file():
        try:
            assert_metadata_frozen(folder, paper_number)
            return True, "already_frozen"
        except Exception as exc:
            return False, f"freeze closure invalid: {exc}"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"receipt unreadable: {exc}"
    if receipt.get("match_status") != "matched":
        return False, f"not matched: {receipt.get('match_status')}"
    errors = validate_metadata_match_receipt(
        receipt,
        metadata_path=folder / f"{paper_number}.metadata.json",
        pdf_path=folder / f"{paper_number}.pdf",
        workspace=folder,
    )
    if errors:
        return False, "; ".join(errors)
    return True, ""


def _workspace_numbers(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(
        p.name for p in root.iterdir() if p.is_dir() and p.name.isdigit() and len(p.name) == 16
    )


def _revision(folder: Path, paper_number: str) -> int:
    freeze_path = folder / f"{paper_number}.metadata_freeze.json"
    try:
        return int(json.loads(freeze_path.read_text(encoding="utf-8")).get("revision") or 1)
    except Exception:
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-number", default=None)
    parser.add_argument("--all-eligible", action="store_true")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    from src.ingest.locking import (
        assert_no_active_identity_migration,
        paper_raw_write_lock,
    )

    assert_no_active_identity_migration(args.paper_raw_dir)

    if args.paper_number:
        numbers = [args.paper_number]
    elif args.all_eligible:
        numbers = _workspace_numbers(args.paper_raw_dir)
    else:
        parser.error("--paper-number or --all-eligible is required")

    results: dict[str, str] = {}
    for number in numbers:
        folder = args.paper_raw_dir / number
        if not (folder / f"{number}.pdf").is_file() or not (
            folder / f"{number}.metadata.json"
        ).is_file():
            results[number] = "skipped_no_assets"
            continue
        eligible, reason = _eligibility(folder, number)
        if not eligible:
            results[number] = f"blocked: {reason}"
            continue
        if reason == "already_frozen":
            # Crash repair: a valid freeze with a non-frozen status gets
            # the status fixed without touching the freeze (H rule).
            try:
                workspace = PaperRawWorkspace.from_path(folder)
                state = (read_status(workspace).get("metadata") or {}).get("state")
                if state != "frozen":
                    with paper_raw_write_lock(args.paper_raw_dir):
                        assert_no_active_identity_migration(args.paper_raw_dir)
                        update_status(
                            workspace, "metadata", "frozen", revision=_revision(folder, number)
                        )
                    results[number] = "status_repaired"
                else:
                    results[number] = "already_frozen"
            except Exception as exc:
                results[number] = f"status_repair_failed: {exc}"
            continue
        if not args.apply:
            results[number] = "planned"
            continue
        try:
            with paper_raw_write_lock(args.paper_raw_dir):
                assert_no_active_identity_migration(args.paper_raw_dir)
                # Order matters: valid freeze first, status second.
                frozen = freeze_metadata(folder, number)
                update_status(
                    PaperRawWorkspace.from_path(folder),
                    "metadata",
                    "frozen",
                    revision=frozen["revision"],
                )
            results[number] = "frozen"
        except Exception as exc:
            results[number] = f"failed: {exc}"

    report = {
        "mode": "apply" if args.apply else "dry-run",
        "results": results,
        "summary": {
            status: sum(1 for value in results.values() if value == status)
            for status in sorted(set(results.values()))
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

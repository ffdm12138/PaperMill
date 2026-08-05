"""Manually confirm a paper_raw PDF identity (automatic/final receipt model).

A human may confirm the PDF identity for workspaces whose automatic
decision is ``ambiguous``, ``unverifiable``, or ``related_version`` — the
confirmation records ``manual_confirmation`` on the receipt, sets the final
decision to ``matched`` / ``manual_confirmed``, and moves the workspace
state to ``matched`` (freeze eligibility follows).

``identifier_conflict`` can never be overridden by manual confirmation
(hard rule).

Protocol (enforced here):
- run under the global paper_raw write lock, with the migration maintenance
  marker re-checked inside the lock;
- ``--expected-receipt-sha256`` must match the current receipt (the review
  window is closed if the receipt changed in the meantime);
- re-running with the same ``--confirmed-by`` is a no-op (crash-safe).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

from config.settings import PAPER_RAW_DIR
from src.utils.file_fingerprint import compute_sha256
from src.utils.timestamps import now_iso
from src.ingest.status import update_status
from src.ingest.workspace import PaperRawWorkspace
from src.metadata.pdf_match import (
    MANUAL_OVERRIDABLE_STATUSES,
    write_match_receipt,
)
from src.utils.identifiers import validate_paper_raw_id

OVERRIDE_LABEL = ", ".join(sorted(MANUAL_OVERRIDABLE_STATUSES))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-number", required=True)
    parser.add_argument("--confirmed-by", required=True, help="stable operator identity")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--expected-receipt-sha256", default="")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    paper_number = validate_paper_raw_id(args.paper_number)
    folder = args.paper_raw_dir / paper_number
    receipt_path = folder / f"{paper_number}.metadata_match.json"

    from src.ingest.locking import (
        assert_no_active_identity_migration,
        paper_raw_write_lock,
    )

    assert_no_active_identity_migration(args.paper_raw_dir)
    if not receipt_path.is_file():
        print(f"ERROR: no match receipt for {paper_number}", file=sys.stderr)
        return 1

    if not args.apply:
        print(json.dumps({
            "planned": "confirm",
            "paper_number": paper_number,
            "confirmed_by": args.confirmed_by,
            "reason": args.reason,
            "expected_receipt_sha256": args.expected_receipt_sha256 or "(any)",
            "overrideable_statuses": sorted(MANUAL_OVERRIDABLE_STATUSES),
        }, ensure_ascii=False, indent=2))
        return 0

    with paper_raw_write_lock(args.paper_raw_dir):
        assert_no_active_identity_migration(args.paper_raw_dir)
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"ERROR: receipt unreadable: {exc}", file=sys.stderr)
            return 1
        if receipt.get("schema_version") != "2.0":
            print("ERROR: schema 1.0 receipt requires pdf_identity migration", file=sys.stderr)
            return 1
        current_sha = compute_sha256(receipt_path)
        if args.expected_receipt_sha256 and current_sha != args.expected_receipt_sha256:
            print(
                "ERROR: receipt changed since review (expected "
                f"{args.expected_receipt_sha256}, current {current_sha})",
                file=sys.stderr,
            )
            return 1
        automatic_status = str(
            (receipt.get("automatic_decision") or {}).get("match_status") or ""
        )
        if automatic_status not in MANUAL_OVERRIDABLE_STATUSES:
            print(
                f"ERROR: automatic decision {automatic_status!r} is not "
                f"overridable (allowed: {OVERRIDE_LABEL})",
                file=sys.stderr,
            )
            return 1
        existing_manual = receipt.get("manual_confirmation") or {}
        if (receipt.get("final_decision") or {}).get("match_method") == "manual_confirmed":
            if existing_manual.get("confirmed_by") == args.confirmed_by:
                print(json.dumps({"status": "already_confirmed",
                                  "paper_number": paper_number}, ensure_ascii=False))
                return 0
            print(
                "ERROR: already confirmed by "
                f"{existing_manual.get('confirmed_by')!r}; a different operator "
                "must reconcile first",
                file=sys.stderr,
            )
            return 1
        manual = {
            "confirmed_by": args.confirmed_by,
            "confirmed_at": now_iso(),
            "reason": args.reason,
            "metadata_sha256": receipt.get("metadata_sha256", ""),
            "pdf_sha256": receipt.get("pdf_sha256", ""),
            "accepted_pdf_doi": receipt.get("pdf_primary_doi"),
            "accepted_relation": (receipt.get("automatic_decision") or {}).get("relation"),
        }
        receipt["manual_confirmation"] = manual
        receipt["manual_errors"] = []
        receipt["final_decision"] = {
            "match_status": "matched",
            "match_method": "manual_confirmed",
        }
        receipt["match_status"] = "matched"
        receipt["match_method"] = "manual_confirmed"
        write_match_receipt(folder, receipt)
        update_status(
            PaperRawWorkspace.from_path(folder),
            "metadata",
            "matched",
            match_method="manual_confirmed",
            match_status="matched",
        )
    print(json.dumps({
        "status": "confirmed",
        "paper_number": paper_number,
        "confirmed_by": args.confirmed_by,
        "receipt_sha256": compute_sha256(receipt_path),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

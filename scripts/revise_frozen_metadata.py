"""Admin-only revision of frozen metadata in a numeric raw workspace.

Uses ranked transaction locks (PAPER_RAW_GLOBAL_RANK + WORKSPACE_RANK)
to guard the revision while ensuring no active commit transaction is in
progress.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import PAPER_RAW_DIR
from src.ingest.locking import (
    PAPER_RAW_GLOBAL_RANK,
    WORKSPACE_RANK,
    LockRequest,
    acquire_locks,
)
from src.ingest.status import update_status
from src.ingest.transactions import CommitJournalStore
from src.ingest.workspace import PaperRawWorkspace
from src.metadata.citation_readiness import validate_citation_ready
from src.metadata.freeze import assert_metadata_frozen, freeze_metadata
from src.metadata.pdf_identity import extract_pdf_identity_evidence
from src.metadata.pdf_match import build_match_receipt, write_match_receipt
from src.utils.atomic_io import atomic_write_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paper-number", required=True)
    parser.add_argument("--metadata-file", type=Path, required=True)
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--transactions-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    workspace = PaperRawWorkspace.open(args.paper_raw_dir, args.paper_number)
    transactions = args.transactions_dir or args.paper_raw_dir.parent / "transactions"

    try:
        old_freeze = assert_metadata_frozen(workspace.root, workspace.paper_number)
        new = json.loads(args.metadata_file.read_text(encoding="utf-8"))
        if new.get("paper_number") != workspace.paper_number:
            raise ValueError("revised metadata paper_number mismatch")
        readiness = validate_citation_ready(new)
        if not readiness.ready:
            raise ValueError(
                "revised metadata is not citation-ready: "
                + "; ".join(readiness.errors)
            )

        preview = {
            "paper_number": workspace.paper_number,
            "old_revision": old_freeze.get("revision"),
            "planned_revision": int(old_freeze.get("revision") or 0) + 1,
            "catalog_will_be_stale": True,
        }

        if not args.apply:
            print(json.dumps(preview, ensure_ascii=False, indent=2))
            return 0

        store = CommitJournalStore(transactions)
        paper_raw_lock = workspace.root.parent / ".paper_raw_write.lock"
        with acquire_locks(
            LockRequest.path_lock(PAPER_RAW_GLOBAL_RANK, paper_raw_lock),
            LockRequest.path_lock(WORKSPACE_RANK, workspace.lock),
        ):
            if store.find_active(workspace.paper_number):
                raise RuntimeError(
                    "metadata revision forbidden during active commit transaction"
                )

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            archive = (
                workspace.root
                / "metadata_revisions"
                / f"revision_{old_freeze.get('revision', 1)}_{stamp}"
            )
            archive.mkdir(parents=True)
            for path in (
                workspace.metadata,
                workspace.metadata_match,
                workspace.metadata_freeze,
            ):
                shutil.copy2(path, archive / path.name)

            try:
                atomic_write_json(workspace.metadata, new, indent=2)
                evidence = extract_pdf_identity_evidence(
                    pdf_path=workspace.pdf,
                    markdown_path=(
                        workspace.markdown if workspace.markdown.exists() else None
                    ),
                    conversion_manifest_path=(
                        workspace.conversion
                        if workspace.conversion.exists()
                        else None
                    ),
                )
                receipt = build_match_receipt(
                    workspace.root,
                    workspace.paper_number,
                    new,
                    evidence,
                    provider_records=list(
                        (new.get("source") or {}).get("raw_record_path")
                        and [(new.get("source") or {})["raw_record_path"]]
                        or []
                    ),
                )
                write_match_receipt(workspace.root, receipt)
                frozen = freeze_metadata(workspace.root, workspace.paper_number)
                frozen["revision"] = (
                    int(old_freeze.get("revision") or 0) + 1
                )
                atomic_write_json(workspace.metadata_freeze, frozen, indent=2)
            except Exception:
                for path in (
                    workspace.metadata,
                    workspace.metadata_match,
                    workspace.metadata_freeze,
                ):
                    shutil.copy2(archive / path.name, path)
                raise

            for path in (
                workspace.catalog_task,
                workspace.catalog_freeze,
                workspace.formalization,
            ):
                if path.exists():
                    os.replace(
                        path,
                        path.with_name(
                            path.name + f".stale_revision_{frozen['revision']}"
                        ),
                    )

            update_status(
                workspace,
                "catalog",
                "stale",
                reason="metadata revision invalidated catalog closure",
            )
            update_status(
                workspace,
                "formalization",
                "stale",
                reason="metadata revision invalidated formalization",
            )

        print(
            json.dumps(
                {
                    **preview,
                    "applied": True,
                    "revision": frozen["revision"],
                    "archive": str(archive),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

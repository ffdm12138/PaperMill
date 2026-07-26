"""Audit DOI/PDF duplicate groups across paper_raw and formal papers."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

from config.settings import PAPER_RAW_DIR, PAPERS_DIR
from src.ingest.duplicate_guard import DuplicateIndex, build_ingest_duplicate_index


def _ref_dicts(refs) -> list[dict[str, str]]:
    return [ref.to_dict() for ref in refs]


def _groups(mapping: dict[str, list]) -> list[dict[str, Any]]:
    return [
        {"value": value, "refs": _ref_dicts(refs)}
        for value, refs in sorted(mapping.items())
        if value and len(refs) > 1
    ]


def _md5_sha_conflicts(index: DuplicateIndex) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for md5, refs in sorted(index.pdf_md5_to_refs.items()):
        shas = {ref.pdf_sha256 for ref in refs if ref.pdf_sha256}
        if len(refs) > 1 and len(shas) > 1:
            out.append({"pdf_md5": md5, "pdf_sha256_values": sorted(shas), "refs": _ref_dicts(refs)})
    return out


def build_report(*, paper_raw_dir: Path, papers_dir: Path) -> dict[str, Any]:
    index = build_ingest_duplicate_index(paper_raw_dir=paper_raw_dir, papers_dir=papers_dir)
    duplicate_doi_groups = _groups(index.doi_to_refs)
    duplicate_pdf_sha256_groups = _groups(index.pdf_sha256_to_refs)
    duplicate_pdf_md5_groups = _groups(index.pdf_md5_to_refs)
    md5_sha_conflict_groups = _md5_sha_conflicts(index)
    blocking_count = (
        len(duplicate_doi_groups)
        + len(duplicate_pdf_sha256_groups)
        + len(duplicate_pdf_md5_groups)
        + len(md5_sha_conflict_groups)
    )
    return {
        "valid": blocking_count == 0,
        "blocking_count": blocking_count,
        "duplicate_doi_groups": duplicate_doi_groups,
        "duplicate_pdf_sha256_groups": duplicate_pdf_sha256_groups,
        "duplicate_pdf_md5_groups": duplicate_pdf_md5_groups,
        "md5_sha_conflict_groups": md5_sha_conflict_groups,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit ingest duplicates across paper_raw and papers.")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args(argv)

    report = build_report(paper_raw_dir=args.paper_raw_dir, papers_dir=args.papers_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if args.strict and report["blocking_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

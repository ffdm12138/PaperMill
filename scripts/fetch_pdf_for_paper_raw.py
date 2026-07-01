"""Fetch a PDF for existing paper_raw metadata and attach it as <paper_number>.pdf."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

from config.settings import PAPER_RAW_DIR
from src.discovery.models import normalize_doi
from src.fetch.access_policy import AccessMode, AccessPolicy
from src.fetch.fetch_pipeline import fetch_pdf
from src.services.ingest_ids import validate_paper_raw_id
from src.services.metadata_quality import is_valid_normalized_doi
from src.services.v2_library import PaperRawAllocator
from src.utils.atomic_io import atomic_write_json


def _paper_numbers(root: Path, all_sources: bool, one: str | None) -> list[str]:
    if one:
        return [validate_paper_raw_id(one)]
    if all_sources:
        return sorted(p.name for p in root.iterdir() if p.is_dir() and p.name.isdigit() and len(p.name) == 16)
    raise ValueError("--paper-number or --all is required")


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch PDFs into v2 paper_raw folders.")
    parser.add_argument("--paper-number", default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--access-mode", choices=[m.value for m in AccessMode], default=AccessMode.OA_ONLY.value)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    write = args.apply and not args.dry_run
    policy = AccessPolicy(mode=AccessMode(args.access_mode))
    allocator = PaperRawAllocator(args.paper_raw_dir)
    report = []

    for source_id in _paper_numbers(args.paper_raw_dir, args.all, args.paper_number):
        folder = args.paper_raw_dir / source_id
        meta_path = folder / f"{source_id}.metadata.json"
        item = {"paper_number": source_id, "paper_raw_id": source_id, "status": "planned"}
        if not meta_path.exists():
            item.update({"status": "failed", "error": "metadata file missing"})
            report.append(item)
            continue
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        doi = normalize_doi(((metadata.get("identifiers") or {}).get("doi") or "").strip())
        title = ((metadata.get("title") or {}).get("original") or "").strip()
        year = metadata.get("year")
        if not doi:
            item.update({"status": "failed", "error": "metadata.identifiers.doi is required for fetch"})
            if write:
                atomic_write_json(folder / ".import_status.json", {
                    "status": "doi_invalid",
                    "reason": item["error"],
                    "paper_number": source_id,
                    "paper_raw_id": source_id,
                }, indent=2)
            report.append(item)
            continue
        if not is_valid_normalized_doi(doi):
            item.update({"status": "failed", "error": "metadata.identifiers.doi must be a valid DOI for fetch", "doi": doi})
            if write:
                atomic_write_json(folder / ".import_status.json", {
                    "status": "doi_invalid",
                    "reason": item["error"],
                    "paper_number": source_id,
                    "paper_raw_id": source_id,
                    "doi": doi,
                }, indent=2)
            report.append(item)
            continue
        metadata.setdefault("identifiers", {})["doi"] = doi
        logger.info("{} fetch {} for {}", "FETCH" if write else "DRY-RUN", doi, source_id)
        if write:
            fetch_root = folder / ".fetch"
            try:
                result = fetch_pdf(
                    doi,
                    domain_id="paper_raw",
                    output_root=fetch_root,
                    dry_run=False,
                    access_policy=policy,
                    title=title,
                    year=year if isinstance(year, int) else None,
                    metadata=metadata,
                )
                item["fetch_result"] = result.to_dict()
                if not result.success or not result.output_path:
                    item.update({"status": "failed", "error": result.error or "fetch failed"})
                else:
                    attached = allocator.attach_pdf(source_id, result.output_path, move=True)
                    item.update(attached)
                    item["status"] = "fetched"
                    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
                    metadata.setdefault("links", {})["pdf_url"] = result.pdf_url or metadata.get("links", {}).get("pdf_url", "")
                    metadata.setdefault("source", {}).setdefault("raw_record", {})["fetch_result"] = result.to_dict()
                    atomic_write_json(meta_path, metadata, indent=2)
            except Exception as exc:
                item.update({"status": "failed", "error": str(exc)})
            finally:
                shutil.rmtree(fetch_root, ignore_errors=True)
        report.append(item)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"applied": write, "items": report}, ensure_ascii=False, indent=2))
    return 1 if any(i["status"] == "failed" for i in report) else 0


if __name__ == "__main__":
    raise SystemExit(main())

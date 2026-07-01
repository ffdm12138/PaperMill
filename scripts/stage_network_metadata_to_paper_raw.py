"""Stage network/discovery metadata records into 16-digit paper_raw workspaces."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

from config.settings import PAPER_NUMBER_LEDGER_PATH, PAPER_RAW_DIR
from src.discovery.models import normalize_doi
from src.services.metadata_quality import is_valid_normalized_doi
from src.services.v2_library import PaperNumberLedger, PaperRawAllocator, empty_metadata, merge_missing_metadata


DOI_REQUIRED_ERROR = "network/search metadata import requires metadata.identifiers.doi"
DOI_INVALID_ERROR = "network_metadata_requires_valid_doi"


def _records(path: Path) -> list[dict]:
    if path.suffix.lower() == ".jsonl":
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data.get("items") or data.get("papers") or [data]


def _planned_paper_numbers(ledger_path: Path, count: int) -> list[str]:
    return PaperNumberLedger(ledger_path).peek_next_numbers(count)


def _metadata_from_record(source_id: str, record: dict[str, Any]) -> dict:
    base = empty_metadata(source_id, source_type="network_search")
    patch = empty_metadata(source_id, source_type="network_search")
    title = record.get("title") or record.get("title_original") or record.get("display_name") or ""
    patch["title"]["original"] = title
    patch["title"]["translated_zh"] = record.get("title_zh") or record.get("translated_zh") or ""
    patch["title"]["short_zh"] = record.get("short_zh") or record.get("short_name_zh") or ""
    patch["year"] = record.get("year") or record.get("publication_year")
    doi = _record_doi(record)
    patch["identifiers"]["doi"] = doi
    patch["identifiers"]["openalex_id"] = record.get("openalex_id") or record.get("id") or ""
    patch["identifiers"]["semantic_scholar_id"] = record.get("semantic_scholar_id") or ""
    patch["links"]["url"] = record.get("url") or record.get("landing_url") or ""
    patch["links"]["pdf_url"] = record.get("pdf_url") or record.get("url_for_pdf") or ""
    patch["abstract"] = record.get("abstract") or ""
    patch["keywords"] = record.get("keywords") or []
    venue = record.get("venue") or record.get("journal") or record.get("container_title") or ""
    patch["container"]["journal"] = venue
    volume = record.get("volume") or ""
    issue = record.get("issue") or ""
    number = record.get("number") or issue
    pages = record.get("page") or record.get("pages") or ""
    article_number = record.get("article-number") or record.get("article_number") or ""
    if number and not issue:
        issue = number
    if issue and not number:
        number = issue
    patch["publication"]["volume"] = str(volume) if volume else ""
    patch["publication"]["number"] = str(number) if number else ""
    patch["publication"]["issue"] = str(issue) if issue else ""
    patch["publication"]["pages"] = str(pages) if pages else ""
    patch["publication"]["article_number"] = str(article_number) if article_number else ""
    authors = record.get("authors") or []
    if authors:
        normalized = []
        for author in authors:
            if isinstance(author, dict):
                normalized.append({
                    "full_name": author.get("full_name") or author.get("name") or author.get("display_name") or "",
                    "family": author.get("family") or "",
                    "given": author.get("given") or "",
                    "orcid": author.get("orcid") or "",
                    "affiliation": author.get("affiliation") or "",
                })
            else:
                normalized.append({"full_name": str(author), "family": "", "given": "", "orcid": "", "affiliation": ""})
        patch["authors"] = normalized
        first = normalized[0]
        patch["first_author"] = {"family": first.get("family", ""), "display": first.get("full_name", "")}
    patch["source"].update({
        "kind": "network_search",
        "provider": record.get("provider") or record.get("source") or "",
        "query": record.get("query") or "",
        "retrieved_at": record.get("retrieved_at") or record.get("created_at") or "",
        "raw_record": record,
    })
    merged, _ = merge_missing_metadata(base, patch)
    return merged


def _record_doi(record: dict[str, Any]) -> str:
    identifiers = record.get("identifiers") if isinstance(record.get("identifiers"), dict) else {}
    return normalize_doi(record.get("doi") or record.get("DOI") or identifiers.get("doi") or identifiers.get("DOI") or "")


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage network metadata into v2 paper_raw workspaces.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--ledger-path", type=Path, default=PAPER_NUMBER_LEDGER_PATH)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    write = args.apply and not args.dry_run
    records = _records(args.input)
    ids = _planned_paper_numbers(args.ledger_path, len(records))
    planned_index = 0
    allocator = PaperRawAllocator(args.paper_raw_dir, ledger_path=args.ledger_path)
    report = []
    for record in records:
        item = {
            "status": "planned",
            "title": record.get("title", ""),
        }
        doi = _record_doi(record)
        if not doi:
            item.update({"status": "failed", "error": DOI_REQUIRED_ERROR})
            logger.error("network metadata stage rejected: {}", DOI_REQUIRED_ERROR)
            report.append(item)
            continue
        if not is_valid_normalized_doi(doi):
            item.update({"status": "failed", "error": DOI_INVALID_ERROR, "doi": doi})
            logger.error("network metadata stage rejected: invalid DOI {}", doi)
            report.append(item)
            continue
        planned_id = ids[planned_index]
        planned_index += 1
        item["planned_paper_number"] = planned_id
        item["planned_paper_raw_id"] = planned_id
        record = {**record, "doi": doi}
        metadata = _metadata_from_record(planned_id, record)
        if write:
            try:
                result = allocator.allocate_metadata(metadata, source_type="network_search")
                item.update(result)
                item["status"] = "staged"
            except Exception as exc:
                item.update({"status": "failed", "error": str(exc)})
                logger.error("network metadata stage failed: {}", exc)
        logger.info("{} metadata -> paper_raw/{}", "STAGE" if write else "DRY-RUN", planned_id)
        report.append(item)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"applied": write, "count": len(report), "items": report}, ensure_ascii=False, indent=2))
    return 1 if any(i["status"] == "failed" for i in report) else 0


if __name__ == "__main__":
    raise SystemExit(main())

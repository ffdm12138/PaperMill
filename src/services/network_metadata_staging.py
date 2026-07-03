"""Stage network/discovery metadata records into 16-digit paper_raw workspaces.

Shared service used by both ``scripts/stage_network_metadata_to_paper_raw.py``
(the CLI that ingests a JSONL/JSON file of discovery records) and
``scripts/discover_papers.py --stage-to-paper-raw`` (which stages a
``CandidateBatch`` directly after search).

Contract:
- Only records carrying a valid DOI are staged into paper_raw. No DOI → failed.
- Each valid record gets a fresh 16-digit paper_number via
  ``PaperRawAllocator.allocate_metadata`` (which also dedups against existing
  paper_raw/papers and writes the metadata.json + .import_status.json files).
- Duplicate DOIs (in-batch or against existing workspaces) never create a new
  workspace.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from src.discovery.models import normalize_doi
from src.services.ingest_duplicate_guard import DuplicateIngestError, check_doi_duplicate
from src.services.ingest_state import METADATA_MANUAL_REVIEW_REQUIRED
from src.services.metadata_quality import bibliographic_identity_gate, is_valid_normalized_doi
from src.services.v2_library import (
    PaperNumberLedger,
    PaperRawAllocator,
    empty_metadata,
    merge_missing_metadata,
    now_iso,
    validate_metadata_schema,
)


DOI_REQUIRED_ERROR = "network/search metadata import requires metadata.identifiers.doi"
DOI_INVALID_ERROR = "network_metadata_requires_valid_doi"


def _record_provider(record: dict[str, Any]) -> str:
    source = record.get("source")
    if isinstance(source, dict):
        provider = source.get("provider") or source.get("name")
    else:
        provider = source
    return str(record.get("provider") or provider or "").strip().lower()


def _record_providers(record: dict[str, Any], provider: str) -> list[str]:
    raw = record.get("providers")
    if raw is None:
        source = record.get("source")
        if isinstance(source, dict):
            raw = source.get("providers")
    providers: list[str] = []
    if isinstance(raw, list):
        providers = [str(item).strip().lower() for item in raw if str(item).strip()]
    elif isinstance(raw, str) and raw.strip():
        providers = [raw.strip().lower()]
    if provider and provider not in providers:
        providers.insert(0, provider)
    return list(dict.fromkeys(providers))


def _record_confidence(record: dict[str, Any], provider: str) -> float:
    value = record.get("confidence")
    try:
        if value is not None:
            return float(value)
    except (TypeError, ValueError):
        pass
    if provider == "crossref":
        return 0.85
    return 0.80


def _metadata_from_record(source_id: str, record: dict[str, Any]) -> dict:
    base = empty_metadata(source_id, source_type="network_search")
    patch = empty_metadata(source_id, source_type="network_search")
    provider = _record_provider(record)
    providers = _record_providers(record, provider)
    confidence = _record_confidence(record, provider)
    title = record.get("title") or record.get("title_original") or record.get("display_name") or ""
    patch["title"]["original"] = title
    patch["year"] = record.get("year") or record.get("publication_year")
    doi = _record_doi(record)
    patch["identifiers"]["doi"] = doi
    patch["identifiers"]["openalex_id"] = record.get("openalex_id") or record.get("id") or ""
    patch["identifiers"]["crossref_id"] = record.get("crossref_id") or ""
    patch["links"]["url"] = record.get("url") or record.get("landing_url") or ""
    patch["links"]["pdf_url"] = record.get("pdf_url") or record.get("url_for_pdf") or ""
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
        "provider": provider,
        "query": record.get("query") or "",
        "retrieved_at": record.get("retrieved_at") or record.get("created_at") or "",
        "raw_record_path": f"source_records/metadata_source.{provider or 'network_search'}.json",
    })
    merged, _ = merge_missing_metadata(base, patch)
    ready, reasons = bibliographic_identity_gate(merged)
    status = "matched" if ready else "unmatched"
    merged["metadata_match"] = {
        "status": status,
        "source": provider or "network_search",
        "confidence": confidence,
        "matched_at": now_iso() if ready else "",
        "warnings": reasons,
    }
    errors = validate_metadata_schema(merged)
    if errors:
        raise ValueError("invalid network metadata: " + "; ".join(errors))
    return merged


def _source_record_payload(metadata: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    source = metadata.get("source") if isinstance(metadata.get("source"), dict) else {}
    provider = str(source.get("provider") or _record_provider(record) or "network_search")
    return {
        "provider": provider,
        "providers": _record_providers(record, provider),
        "record": record,
    }


def _record_doi(record: dict[str, Any]) -> str:
    identifiers = record.get("identifiers") if isinstance(record.get("identifiers"), dict) else {}
    return normalize_doi(record.get("doi") or record.get("DOI") or identifiers.get("doi") or identifiers.get("DOI") or "")


def stage_network_metadata_records(
    records: list[dict[str, Any]],
    *,
    paper_raw_dir: Path,
    papers_dir: Path,
    ledger_path: Path,
    apply: bool = False,
    dry_run: bool = False,
    skip_duplicates: bool = False,
) -> dict:
    """Stage a list of already-parsed discovery records into paper_raw workspaces.

    Returns a report dict:
        {
            "applied": bool, "count": int,
            "items": list[dict],
            "failed": int, "duplicate": int, "staged": int, "planned": int,
            "exit_code": int,
        }
    """
    write = apply and not dry_run
    ids = PaperNumberLedger(ledger_path).peek_next_numbers(len(records))
    planned_index = 0
    allocator = PaperRawAllocator(paper_raw_dir, ledger_path=ledger_path, papers_dir=papers_dir)
    report: list[dict] = []
    seen_batch_dois: dict[str, int] = {}

    for record in records:
        item: dict[str, Any] = {
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
        duplicate_reasons: list[str] = []
        duplicate_refs: list[dict] = []
        if doi in seen_batch_dois:
            duplicate_reasons.extend(["batch_doi_duplicate", "doi_duplicate"])
            duplicate_refs.append({
                "scope": "batch",
                "paper_number": "",
                "paper_id": "",
                "folder": f"input[{seen_batch_dois[doi]}]",
                "source": "input_record",
                "doi": doi,
                "pdf_md5": "",
                "pdf_sha256": "",
            })
        dup = check_doi_duplicate(doi, paper_raw_dir=paper_raw_dir, papers_dir=papers_dir)
        duplicate_reasons.extend(dup.reasons)
        duplicate_refs.extend(ref.to_dict() for ref in dup.refs)
        duplicate_reasons = list(dict.fromkeys(duplicate_reasons))
        if duplicate_reasons:
            item.update({
                "status": "duplicate",
                "error": "doi_duplicate",
                "doi": doi,
                "duplicate_reasons": duplicate_reasons,
                "duplicate_refs": duplicate_refs,
            })
            logger.warning("network metadata duplicate DOI {}: {}", doi, ", ".join(duplicate_reasons))
            report.append(item)
            continue
        seen_batch_dois[doi] = len(report)
        planned_id = ids[planned_index]
        planned_index += 1
        item["planned_paper_number"] = planned_id
        item["planned_paper_raw_id"] = planned_id
        record = {**record, "doi": doi}
        metadata = _metadata_from_record(planned_id, record)
        if write:
            try:
                result = allocator.allocate_metadata(
                    metadata,
                    source_type="network_search",
                    raw_record=_source_record_payload(metadata, record),
                )
                item.update(result)
                item["status"] = "staged"
                match = metadata.get("metadata_match") if isinstance(metadata.get("metadata_match"), dict) else {}
                if match.get("status") != "matched":
                    item["import_status"] = METADATA_MANUAL_REVIEW_REQUIRED
                    item["warnings"] = list(match.get("warnings") or [])
            except DuplicateIngestError as exc:
                item.update({
                    "status": "duplicate",
                    "error": "doi_duplicate",
                    "duplicate_reasons": exc.result.reasons,
                    "duplicate_refs": [ref.to_dict() for ref in exc.result.refs],
                    "doi": exc.result.doi or doi,
                })
            except Exception as exc:
                item.update({"status": "failed", "error": str(exc)})
                logger.error("network metadata stage failed: {}", exc)
        logger.info("{} metadata -> paper_raw/{}", "STAGE" if write else "DRY-RUN", planned_id)
        report.append(item)

    failed = sum(1 for i in report if i.get("status") == "failed")
    duplicate = sum(1 for i in report if i.get("status") == "duplicate")
    staged = sum(1 for i in report if i.get("status") == "staged")
    planned = sum(1 for i in report if i.get("status") == "planned")
    exit_code = 0
    if failed:
        exit_code = 1
    elif duplicate and not skip_duplicates:
        exit_code = 1

    return {
        "applied": write,
        "count": len(report),
        "items": report,
        "failed": failed,
        "duplicate": duplicate,
        "staged": staged,
        "planned": planned,
        "exit_code": exit_code,
    }

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
from src.services.ingest_duplicate_guard import DuplicateIngestError, DuplicateRef, build_doi_duplicate_index, check_doi_duplicate
from src.services.ingest_state import METADATA_MANUAL_REVIEW_REQUIRED
from src.services.metadata_quality import bibliographic_identity_gate, is_valid_normalized_doi
from src.services.network_metadata_canonical import canonicalize_network_record
from src.library.paper_number_ledger import PaperNumberLedger
from src.metadata.schema import empty_metadata, validate_metadata_schema
from src.metadata.normalization import merge_missing_metadata
from src.ingest.models import now_iso
from src.ingest.paper_raw import PaperRawAllocator


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


def _metadata_from_record(record: dict[str, Any] | str, paper_number: str | dict[str, Any] | None = None) -> dict:
    if isinstance(record, str) and isinstance(paper_number, dict):
        record, paper_number = paper_number, record
    if not isinstance(record, dict):
        raise TypeError("record must be a dict")
    source_id = str(paper_number or "0000000000000000")
    base = empty_metadata(source_id, source_type="network_search")
    patch = empty_metadata(source_id, source_type="network_search")
    provider = _record_provider(record)
    providers = _record_providers(record, provider)
    confidence = _record_confidence(record, provider)
    canonical = canonicalize_network_record(record)
    patch["entry_type"] = canonical.entry_type
    patch["title"]["original"] = canonical.title
    patch["year"] = canonical.year
    doi = _record_doi(record)
    patch["identifiers"]["doi"] = doi
    patch["identifiers"]["openalex_id"] = record.get("openalex_id") or record.get("id") or ""
    patch["identifiers"]["crossref_id"] = record.get("crossref_id") or ""
    patch["identifiers"]["issn"] = ";".join(canonical.issn)
    patch["identifiers"]["isbn"] = ";".join(canonical.isbn)
    patch["links"]["url"] = canonical.url or record.get("landing_url") or ""
    patch["links"]["pdf_url"] = canonical.pdf_url or record.get("url_for_pdf") or ""
    patch["container"]["journal"] = canonical.venue
    patch["container"]["publisher"] = canonical.publisher
    patch["date"]["published"] = canonical.published
    patch["date"]["online"] = canonical.online
    volume = canonical.volume
    issue = canonical.issue
    number = canonical.number or issue
    pages = canonical.pages
    article_number = canonical.article_number
    if number and not issue:
        issue = number
    if issue and not number:
        number = issue
    patch["publication"]["volume"] = str(volume) if volume else ""
    patch["publication"]["number"] = str(number) if number else ""
    patch["publication"]["issue"] = str(issue) if issue else ""
    patch["publication"]["pages"] = str(pages) if pages else ""
    patch["publication"]["article_number"] = str(article_number) if article_number else ""
    authors = canonical.authors or record.get("authors") or []
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
    merged["entry_type"] = canonical.entry_type
    blocking = [warning for warning in canonical.warnings if "unknown provider type" in warning]
    ready, reasons = bibliographic_identity_gate(merged, blocking)
    merged.pop("metadata_match", None)
    errors = validate_metadata_schema(merged)
    if paper_number and errors:
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
    reuse_paper_number: str | None = None,
) -> dict:
    """Stage a list of already-parsed discovery records into paper_raw workspaces.

    Returns a report dict:
        {
            "applied": bool, "count": int,
            "items": list[dict],
            "failed": int, "duplicate": int, "staged": int, "planned": int,
            "exit_code": int,
        }

    ``reuse_paper_number`` is the crash-recovery hook: when set, the FIRST record
    completes staging into the EXISTING workspace previously reserved for the
    same candidate (discovery-context match). No new paper number is allocated,
    and the pre-staging duplicate guard skips the reused workspace because
    ownership was already established by reconciliation.
    """
    write = apply and not dry_run
    ids = PaperNumberLedger(ledger_path).peek_next_numbers(len(records)) if dry_run or not apply else []
    planned_index = 0
    allocator = PaperRawAllocator(paper_raw_dir, ledger_path=ledger_path, papers_dir=papers_dir)
    report: list[dict] = []
    seen_batch_dois: dict[str, int] = {}
    reuse_number = str(reuse_paper_number or "").strip()
    doi_index = build_doi_duplicate_index(
        paper_raw_dir=paper_raw_dir,
        papers_dir=papers_dir,
        skip_paper_number=reuse_number or None,
    )

    for record in records:
        discovery_context = record.get("discovery_context") if isinstance(record.get("discovery_context"), dict) else {}
        item: dict[str, Any] = {
            "status": "planned",
            "title": record.get("title", ""),
            "candidate_id": discovery_context.get("candidate_id") or record.get("candidate_id") or "",
        }
        doi = _record_doi(record)
        if not doi:
            item.update({"status": "failed_terminal", "error": DOI_REQUIRED_ERROR, "safe_error": DOI_REQUIRED_ERROR})
            logger.error("network metadata stage rejected: {}", DOI_REQUIRED_ERROR)
            report.append(item)
            continue
        if not is_valid_normalized_doi(doi):
            item.update({"status": "failed_terminal", "error": DOI_INVALID_ERROR, "safe_error": DOI_INVALID_ERROR, "doi": doi})
            logger.error("network metadata stage rejected: invalid DOI {}", doi)
            report.append(item)
            continue
        item["doi"] = doi
        # Reuse path: reconciliation already proved this candidate owns the
        # reused workspace, so the batch/existing duplicate guard would only
        # re-discover that same workspace. Skip it; allocate_metadata still
        # guards against the DOI appearing in any OTHER workspace.
        is_reuse_record = bool(reuse_number) and len(report) == 0
        duplicate_reasons: list[str] = []
        duplicate_refs: list[dict] = []
        if not is_reuse_record:
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
            dup = check_doi_duplicate(doi, paper_raw_dir=paper_raw_dir, papers_dir=papers_dir, index=doi_index)
            duplicate_reasons.extend(dup.reasons)
            duplicate_refs.extend(ref.to_dict() for ref in dup.refs)
            duplicate_reasons = list(dict.fromkeys(duplicate_reasons))
            if duplicate_reasons:
                item.update({
                    "status": "duplicate",
                    "error": "doi_duplicate",
                    "safe_error": "doi_duplicate",
                    "doi": doi,
                    "duplicate_reasons": duplicate_reasons,
                    "duplicate_refs": duplicate_refs,
                })
                logger.warning("network metadata duplicate DOI {}: {}", doi, ", ".join(duplicate_reasons))
                report.append(item)
                continue
        seen_batch_dois[doi] = len(report)
        planned_id = ""
        if not write:
            planned_id = ids[planned_index]
            planned_index += 1
            item["dry_run_planned_paper_number"] = planned_id
            item["dry_run_planned_paper_raw_id"] = planned_id
        record = {**record, "doi": doi}
        metadata = _metadata_from_record(record, paper_number=planned_id or reuse_number or None)
        if write:
            try:
                result = allocator.allocate_metadata(
                    metadata,
                    source_type="network_search",
                    raw_record=_source_record_payload(metadata, record),
                    discovery_receipt_context={
                        **discovery_context,
                        "normalized_doi": doi,
                    } if discovery_context else None,
                    reuse_paper_number=reuse_number or None,
                )
                item.update(result)
                item["actual_allocated"] = True
                item["status"] = "staged"
                item["paper_number"] = result.get("paper_number") or result.get("paper_raw_id") or ""
                if result.get("receipt_path"):
                    item["receipt_path"] = result.get("receipt_path")
                doi_index.add_doi_ref(DuplicateRef(
                    scope="paper_raw",
                    paper_number=item["paper_number"],
                    paper_id="",
                    folder=str(result.get("folder") or ""),
                    source="metadata",
                    doi=doi,
                ))
                item["safe_error"] = None
                item["import_status"] = "metadata_resolved"
            except DuplicateIngestError as exc:
                item.update({
                    "status": "duplicate",
                    "error": "doi_duplicate",
                    "safe_error": "doi_duplicate",
                    "duplicate_reasons": exc.result.reasons,
                    "duplicate_refs": [ref.to_dict() for ref in exc.result.refs],
                    "doi": exc.result.doi or doi,
                })
            except Exception as exc:
                error_type = "allocator_collision" if isinstance(exc, FileExistsError) else (
                    "metadata_validation_failed" if isinstance(exc, ValueError) else "allocation_transaction_failed"
                )
                status = "failed_terminal" if error_type == "metadata_validation_failed" else "failed_retryable"
                item.update({
                    "status": status,
                    "error": str(exc),
                    "safe_error": str(exc)[:500],
                    "error_type": error_type,
                    "retryable": status == "failed_retryable",
                })
                logger.error("network metadata stage failed: {}", exc)
        destination = item.get("paper_number") or planned_id
        logger.info("{} metadata -> paper_raw/{}", "STAGE" if write else "DRY-RUN", destination)
        report.append(item)

    failed = sum(1 for i in report if i.get("status") in {"failed", "failed_retryable", "failed_terminal"})
    for i in report:
        if i.get("status") in {"failed_retryable", "failed_terminal"}:
            i.setdefault("failed_legacy", True)
    duplicate = sum(1 for i in report if i.get("status") == "duplicate")
    staged = sum(1 for i in report if i.get("status") == "staged")
    planned = sum(1 for i in report if i.get("status") == "planned")
    failed_allocator = sum(1 for i in report if i.get("error_type") in {"allocator_collision", "allocation_transaction_failed"})
    failed_validation = sum(1 for i in report if i.get("error_type") == "metadata_validation_failed")
    failed_io = sum(1 for i in report if i.get("error_type") == "metadata_write_failed")
    failed_provider = sum(1 for i in report if i.get("error_type") == "provider_error")
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
        "failed_allocator": failed_allocator,
        "failed_validation": failed_validation,
        "failed_io": failed_io,
        "failed_provider": failed_provider,
        "exit_code": exit_code,
    }

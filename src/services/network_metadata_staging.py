"""Thin normalization/report adapter for discovery staging transactions."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from src.utils.identifiers import normalize_doi
from src.discovery.stage_transaction import (
    DiscoveryStageResult, DiscoveryStageTransaction, NormalizedDiscoveryCandidate, PreparedCandidate,
)
from src.discovery.staging_context import DiscoveryStagingContext
from src.library.paper_number_ledger import PaperNumberLedger
from src.metadata.normalization import merge_missing_metadata
from src.metadata.schema import empty_metadata, validate_metadata_schema
from src.services.metadata_quality import bibliographic_identity_gate, is_valid_normalized_doi
from src.services.network_metadata_canonical import canonicalize_network_record


DOI_REQUIRED_ERROR = "network/search metadata import requires metadata.identifiers.doi"
DOI_INVALID_ERROR = "network_metadata_requires_valid_doi"


def _record_provider(record: dict[str, Any]) -> str:
    source = record.get("source")
    nested = source.get("provider") or source.get("name") if isinstance(source, dict) else source
    return str(record.get("provider") or nested or "").strip().lower()


def _record_providers(record: dict[str, Any], provider: str) -> list[str]:
    raw = record.get("providers")
    if raw is None and isinstance(record.get("source"), dict):
        raw = record["source"].get("providers")
    values = ([str(v).strip().lower() for v in raw] if isinstance(raw, list)
              else [str(raw).strip().lower()] if raw else [])
    return list(dict.fromkeys(([provider] if provider else []) + [v for v in values if v]))


def _record_confidence(record: dict[str, Any], provider: str) -> float:
    try:
        return float(record["confidence"])
    except (KeyError, TypeError, ValueError):
        return 0.85 if provider == "crossref" else 0.80


def _record_doi(record: dict[str, Any]) -> str:
    ids = record.get("identifiers") if isinstance(record.get("identifiers"), dict) else {}
    return normalize_doi(record.get("doi") or record.get("DOI") or ids.get("doi") or ids.get("DOI") or "")


def _metadata_from_record(record: dict[str, Any] | str,
                          paper_number: str | dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(record, str) and isinstance(paper_number, dict):
        record, paper_number = paper_number, record
    if not isinstance(record, dict):
        raise TypeError("record must be a dict")
    number = str(paper_number or "0000000000000000")
    provider = _record_provider(record)
    canonical = canonicalize_network_record(record)
    base = empty_metadata(number, source_type="network_search")
    patch = empty_metadata(number, source_type="network_search")
    patch["entry_type"] = canonical.entry_type
    patch["title"]["original"] = canonical.title
    patch["year"] = canonical.year
    patch["identifiers"].update({
        "doi": _record_doi(record),
        "openalex_id": record.get("openalex_id") or record.get("id") or "",
        "crossref_id": record.get("crossref_id") or "",
        "issn": ";".join(canonical.issn), "isbn": ";".join(canonical.isbn),
    })
    patch["links"].update({"url": canonical.url or record.get("landing_url") or "",
                           "pdf_url": canonical.pdf_url or record.get("url_for_pdf") or ""})
    patch["container"].update({"journal": canonical.venue, "publisher": canonical.publisher})
    patch["date"].update({"published": canonical.published, "online": canonical.online})
    issue = canonical.issue or canonical.number
    patch["publication"].update({
        "volume": str(canonical.volume or ""), "number": str(canonical.number or issue or ""),
        "issue": str(issue or ""), "pages": str(canonical.pages or ""),
        "article_number": str(canonical.article_number or ""),
    })
    authors: list[dict[str, str]] = []
    for author in canonical.authors or record.get("authors") or []:
        if isinstance(author, dict):
            authors.append({
                "full_name": author.get("full_name") or author.get("name") or author.get("display_name") or "",
                "family": author.get("family") or "", "given": author.get("given") or "",
                "orcid": author.get("orcid") or "", "affiliation": author.get("affiliation") or "",
            })
        else:
            authors.append({"full_name": str(author), "family": "", "given": "", "orcid": "", "affiliation": ""})
    if authors:
        patch["authors"] = authors
        patch["first_author"] = {"family": authors[0]["family"], "display": authors[0]["full_name"]}
    patch["source"].update({
        "kind": "network_search", "provider": provider,
        "query": record.get("query") or "",
        "retrieved_at": record.get("retrieved_at") or record.get("created_at") or "",
        "raw_record_path": f"source_records/metadata_source.{provider or 'network_search'}.json",
    })
    merged, _ = merge_missing_metadata(base, patch)
    merged["entry_type"] = canonical.entry_type
    merged.pop("metadata_match", None)
    blocking = [w for w in canonical.warnings if "unknown provider type" in w]
    bibliographic_identity_gate(merged, blocking)
    errors = validate_metadata_schema(merged)
    if paper_number and errors:
        raise ValueError("invalid network metadata: " + "; ".join(errors))
    return merged


def _source_record_payload(metadata: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    source = metadata.get("source") if isinstance(metadata.get("source"), dict) else {}
    provider = str(source.get("provider") or _record_provider(record) or "network_search")
    return {"provider": provider, "providers": _record_providers(record, provider), "record": record}


def stage_network_metadata_records(
    records: list[dict[str, Any]], *, paper_raw_dir: Path, papers_dir: Path,
    ledger_path: Path, apply: bool = False, dry_run: bool = False,
    skip_duplicates: bool = False, reuse_paper_number: str | None = None,
    transaction: DiscoveryStageTransaction | None = None,
    max_lock_seconds: float = 2.0,
) -> dict[str, Any]:
    """Normalize a batch lock-free, then stage authoritative candidates in chunks."""
    write = apply and not dry_run
    planned_ids = PaperNumberLedger(ledger_path).peek_next_numbers(len(records)) if not write else []
    configuration_error = None
    if write and transaction is None:
        try:
            transaction = DiscoveryStagingContext.create(
                paper_raw_dir=paper_raw_dir, papers_dir=papers_dir,
                ledger_path=ledger_path, prepare_allocation=True,
            ).transaction
        except Exception as exc:
            configuration_error = f"{type(exc).__name__}:{exc}"
    items: list[dict[str, Any]] = [{} for _ in records]
    prepared: list[tuple[int, PreparedCandidate]] = []
    primary_by_key: dict[tuple[str, str], int] = {}
    followers: dict[int, list[int]] = {}
    for offset, raw in enumerate(records):
        context = raw.get("discovery_context") if isinstance(raw.get("discovery_context"), dict) else {}
        item: dict[str, Any] = {"title": raw.get("title", ""),
                                "candidate_id": context.get("candidate_id") or raw.get("candidate_id") or ""}
        items[offset] = item
        doi = _record_doi(raw)
        if not doi or not is_valid_normalized_doi(doi):
            error = DOI_REQUIRED_ERROR if not doi else DOI_INVALID_ERROR
            item.update(status="failed_terminal", error=error, safe_error=error, doi=doi)
            continue
        if not write:
            number = planned_ids[offset]
            item.update(status="planned", doi=doi, dry_run_planned_paper_number=number,
                        dry_run_planned_paper_raw_id=number)
            continue
        if configuration_error:
            item.update(status="repair_required", doi=doi, error=configuration_error,
                        safe_error="registry_configuration_failed")
            continue
        record = {**raw, "doi": doi}
        candidate_id = str(context.get("candidate_id") or raw.get("candidate_id") or f"network:{doi}")
        page_id = str(context.get("page_id") or f"input:{offset}")
        candidate = NormalizedDiscoveryCandidate(
            candidate_id=candidate_id, page_id=page_id, keyword_id=str(context.get("keyword_id") or ""),
            provider=str(context.get("provider") or _record_provider(raw) or "").lower(),
            normalized_doi=doi, metadata=_metadata_from_record(record, reuse_paper_number or None),
            requested_paper_number=str(reuse_paper_number or ""),
        )
        assert transaction is not None
        prepared_item = DiscoveryStageTransaction.prepare_candidate(
            candidate, source_record=_source_record_payload(candidate.metadata, record))
        if isinstance(prepared_item, DiscoveryStageResult):
            item.update(status=prepared_item.status, doi=doi,
                        safe_error=prepared_item.error.code if prepared_item.error else None)
            continue
        identity_key = "|".join((candidate.provider, candidate.keyword_id,
                                  candidate.page_id, candidate.candidate_id))
        # DOI is authoritative; identity is retained for the no-DOI extension
        # point but valid network staging currently always requires a DOI.
        key = (doi, "") if doi else ("", identity_key)
        primary = primary_by_key.get(key)
        if primary is not None:
            followers.setdefault(primary, []).append(offset)
            continue
        primary_by_key[key] = offset
        prepared.append((offset, prepared_item))

    for start in range(0, len(prepared), 16):
        remaining = prepared[start:start + 16]
        assert transaction is not None
        while remaining:
            if hasattr(transaction, "stage_candidates_batch"):
                results = transaction.stage_candidates_batch(
                    [entry for _, entry in remaining], apply=True, max_batch_size=16,
                    max_lock_seconds=max_lock_seconds)
            else:  # Narrow compatibility for injected transaction test doubles.
                results = tuple(transaction.stage_candidate(
                    entry.candidate, source_record=entry.source_record, apply=True)
                    for _, entry in remaining)
            retry_after_fair_release: list[tuple[int, PreparedCandidate]] = []
            for (offset, _prepared), result in zip(remaining, results, strict=True):
                if (result.status == "failed_retryable" and result.error is not None
                        and result.error.code == "lock_epoch_budget_exhausted"):
                    retry_after_fair_release.append((offset, _prepared))
                    continue
                item = items[offset]
                doi = _prepared.normalized_doi
                # Preserve the legacy outward "staged" label for reuse without
                # conflating reuse with a new paper-number allocation.
                status = "staged" if result.status == "reused" else result.status
                item.update(status=status, doi=doi, paper_number=result.paper_number,
                            paper_raw_id=result.paper_number, folder=str(result.workspace_path or ""),
                            receipt_path=result.receipt_path,
                            duplicate_refs=[ref.__dict__ | {"workspace_path": str(ref.workspace_path)} for ref in result.duplicate_refs],
                            safe_error=result.error.code if result.error else None,
                            actual_allocated=result.status == "staged",
                            reused_existing=result.status == "reused")
                if status == "staged":
                    item["import_status"] = "metadata_resolved"
                if status == "duplicate":
                    item["duplicate_reasons"] = ["batch_doi_duplicate", "doi_duplicate"]
                    item["error"] = "doi_duplicate"
                    item["safe_error"] = "doi_duplicate"
                if result.error:
                    item["error"] = result.error.detail or result.error.code
                for follower in followers.get(offset, []):
                    items[follower].update(
                        status="duplicate", doi=doi, paper_number=result.paper_number,
                        paper_raw_id=result.paper_number, folder=str(result.workspace_path or ""),
                        duplicate_reasons=["batch_doi_duplicate", "in_batch_duplicate"], safe_error="doi_duplicate",
                        error="doi_duplicate", actual_allocated=False,
                        reused_existing=False,
                    )
            if retry_after_fair_release == remaining:
                raise RuntimeError("staging lock epoch made no candidate progress")
            remaining = retry_after_fair_release
    failed = sum(i["status"] in {"failed_retryable", "failed_terminal", "repair_required"} for i in items)
    duplicate = sum(i["status"] == "duplicate" for i in items)
    return {
        "applied": write, "count": len(items), "items": items, "failed": failed,
        "duplicate": duplicate, "staged": sum(i["status"] == "staged" for i in items),
        "allocated": sum(bool(i.get("actual_allocated")) for i in items),
        "reused": sum(bool(i.get("reused_existing")) for i in items),
        "planned": sum(i["status"] == "planned" for i in items),
        "failed_allocator": 0, "failed_validation": 0, "failed_io": 0, "failed_provider": 0,
        "exit_code": 1 if failed or (duplicate and not skip_duplicates) else 0,
    }

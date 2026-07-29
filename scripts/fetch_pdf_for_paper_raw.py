"""Fetch PDFs for existing paper_raw metadata and attach as <paper_number>.pdf."""
from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

from loguru import logger

from config.settings import PAPER_RAW_DIR, PAPERS_DIR
from src.fetch.access_policy import (
    AccessMode,
    AccessPolicy,
    BLOCKED_FETCH_STATUSES,  # noqa: F401  (re-exported script surface)
    FetchCandidateStatus,
    FetchSelection,
    classify_pdf_fetch_candidate,
    select_fetch_candidates,
)
from src.fetch.host_policy import classify_failure
import src.fetch.fetch_pipeline as fetch_pipeline
from src.fetch.pdf_transport import TRANSPORT_POLICY, sanitize_for_persistence, sanitize_url_for_persistence
from src.ingest.duplicate_guard import DuplicateIngestError
from src.utils.identifiers import validate_paper_raw_id
from src.ingest.import_status import write_import_status
from src.metadata.source_records import (
    ensure_raw_record_path_is_metadata_source,
    fetch_result_rel_path,
    resolve_metadata_source_record_path,
)
from src.fetch.fetch_result_record import write_fetch_result
from src.ingest.stage_manifest import (
    doi_fetch_pdf_source,
    read_stage_manifest,
    update_stage_manifest,
)
from src.ingest.paper_raw import PaperRawAllocator
from src.utils.atomic_io import atomic_write_json
from src.utils.jsonio import read_json


def _paper_numbers(root: Path, all_sources: bool, one: str | None) -> list[str]:
    if one:
        return [validate_paper_raw_id(one)]
    if all_sources:
        if not root.exists():
            return []
        return sorted(p.name for p in root.iterdir() if p.is_dir() and p.name.isdigit() and len(p.name) == 16)
    raise ValueError("--paper-number or --all is required")


def _parse_header(value: str, *, ua_warn_only: bool = True) -> tuple[str, str]:
    if ":" not in value:
        raise ValueError(f"invalid --header value (expected 'Key: Value'): {value}")
    key, header_value = value.split(":", 1)
    key = key.strip()
    if not key:
        raise ValueError("header key must not be empty")
    if key.lower() == "user-agent":
        if ua_warn_only:
            # HeaderBasedDoiResolver pins the User-Agent; a user often copies one
            # from browser DevTools. Ignore it with a warning instead of erroring.
            logger.warning(
                "User-Agent is ignored: header_based resolver uses a fixed User-Agent. "
                "Use --strict-headers to override."
            )
            return "", ""
        raise ValueError("User-Agent is fixed in header_based resolver and cannot be overridden")
    return key, header_value.strip()


def _load_headers(headers_json: Path | None, cli_headers: list[str], *, ua_warn_only: bool = True) -> dict[str, str]:
    headers: dict[str, str] = {}
    if headers_json:
        data = json.loads(headers_json.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("headers"), dict):
            data = data["headers"]
        if not isinstance(data, dict):
            raise ValueError("--headers-json must contain a JSON object")
        for key, value in data.items():
            if str(key).lower() == "user-agent":
                if ua_warn_only:
                    logger.warning("User-Agent is ignored: header_based resolver uses a fixed User-Agent.")
                    continue
                raise ValueError("User-Agent is fixed in header_based resolver and cannot be overridden")
            headers[str(key)] = str(value)
    for value in cli_headers:
        key, header_value = _parse_header(value, ua_warn_only=ua_warn_only)
        if key:
            headers[key] = header_value
    return headers


def _header_keys(headers: dict[str, str], *, include_user_agent: bool) -> list[str]:
    keys = sorted(headers.keys())
    if include_user_agent and "User-Agent" not in keys:
        keys = ["User-Agent", *keys]
    return keys


def _build_policy(args: argparse.Namespace, headers: dict[str, str]) -> AccessPolicy:
    if args.resolver == "header-based":
        # header_based now defaults to https://doi.org/{doi} when no
        # --base-url/--url-template is given.  Both are optional overrides.
        return AccessPolicy(
            mode=AccessMode.CUSTOM,
            allow_custom_resolvers=True,
            allow_publisher_tdm=False,
            custom_resolvers=["header_based"],
            timeout_seconds=args.timeout,
            extra={
                "resolver_names": ["header_based"],
                "base_url": args.base_url or "",
                "url_template": args.url_template or "",
                "headers": headers,
                "timeout_seconds": args.timeout,
            },
        )
    if args.resolver == "oa":
        # original_link + OA resolvers only; no header_based fallback.
        return AccessPolicy(mode=AccessMode.OA_ONLY, timeout_seconds=args.timeout)
    # --resolver auto: original_link + OA + publisher + header_based fallback (always).
    # header_based now defaults to https://doi.org/{doi} when no --base-url
    # or --url-template is given, so it is always a viable DOI fallback.
    resolver_names = [
        "original_link",
        "unpaywall", "openalex", "semantic_scholar", "arxiv",
        "publisher_oa", "springer_direct",
        "sciengine_direct",
        "biorxiv", "pmc_oa",
        "header_based",
    ]
    if args.base_url or args.url_template:
        return AccessPolicy(
            mode=AccessMode.CUSTOM,
            allow_custom_resolvers=True,
            allow_publisher_tdm=False,
            custom_resolvers=["header_based"],
            timeout_seconds=args.timeout,
            extra={
                "resolver_names": list(resolver_names),
                "base_url": args.base_url or "",
                "url_template": args.url_template or "",
                "headers": headers,
                "timeout_seconds": args.timeout,
            },
        )
    # auto without header config: still include header_based as
    # DOI landing fallback (resolves from https://doi.org/{doi}).
    return AccessPolicy(
        mode=AccessMode.CUSTOM,
        allow_custom_resolvers=True,
        allow_publisher_tdm=False,
        custom_resolvers=["header_based"],
        timeout_seconds=args.timeout,
        extra={
            "resolver_names": list(resolver_names),
            "base_url": "",
            "url_template": "",
            "headers": headers,
            "timeout_seconds": args.timeout,
        },
    )


def _sanitized_fetch_record(
    result,
    attached: dict[str, Any] | None,
    header_keys: list[str],
    *,
    success: bool | None = None,
    final_reason: str = "",
) -> dict[str, Any]:
    attached = dict(attached or {})
    return sanitize_for_persistence({
        "success": bool(result.success) if success is None else bool(success),
        "final_reason": final_reason or result.error or "",
        "resolver": result.resolver,
        "resolver_chain": list(result.resolver_chain or []),
        "attempts": list(result.attempts or []),
        "transport_policy": TRANSPORT_POLICY,
        "transport_attempts": list(result.transport_attempts or []),
        "access_mode": result.access_mode,
        "fetched_at": result.fetched_at or result.downloaded_at,
        "pdf_url": result.pdf_url,
        "landing_url": result.landing_url,
        "is_direct_pdf": result.is_direct_pdf,
        "fixed_user_agent": result.resolver == "header_based",
        "header_keys": header_keys if result.resolver == "header_based" else [],
        "headers_masked": result.resolver == "header_based",
        "pdf_md5": attached.get("pdf_md5", ""),
        "pdf_sha256": attached.get("pdf_sha256", ""),
    })


def _load_source_record(folder: Path, metadata: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    """Return ``(source_record, status, error)`` for the workspace metadata."""
    # Load source record from source.raw_record_path (if present) via the
    # path-safe resolver — never persisted back to metadata.
    source_record: dict[str, Any] = {}
    source_record_status = "missing"
    source_record_error = ""
    raw_record_path = (metadata.get("source") or {}).get("raw_record_path", "").strip()
    if raw_record_path:
        sr_path, err = resolve_metadata_source_record_path(
            folder, raw_record_path,
        )
        if err:
            source_record_status = "invalid"
            source_record_error = err
        elif sr_path and sr_path.is_file():
            try:
                source_record = json.loads(sr_path.read_text(encoding="utf-8"))
                source_record_status = "loaded"
            except Exception as exc:
                source_record_status = "read_failed"
                source_record_error = str(exc)
        else:
            source_record_status = "missing"
            source_record_error = "source record file not found"
    return source_record, source_record_status, source_record_error


def _resolve_and_download(
    candidate: FetchCandidateStatus,
    *,
    policy: AccessPolicy,
    allocator: PaperRawAllocator,
    force_refetch: bool,
    header_keys: list[str],
    metadata: dict[str, Any],
    title: str,
    year: Any,
    source_record: dict[str, Any],
    fetch_root: Path,
    item: dict[str, Any],
) -> tuple[Any, dict[str, Any] | None]:
    """Run the fetch pipeline and attach the downloaded PDF.

    On fetch failure or duplicate PDF the terminal ``item`` state is
    recorded here and ``(result, None)`` is returned; on success returns
    ``(result, attached)`` for provenance recording.
    """
    folder = candidate.folder
    result = fetch_pipeline.fetch_pdf(
        candidate.doi,
        domain_id="paper_raw",
        output_root=fetch_root,
        dry_run=False,
        access_policy=policy,
        title=title,
        year=year if isinstance(year, int) else None,
        metadata=metadata,
        source_record=source_record,
    )
    if not result.success or not result.output_path:
        item.update({
            "status": "failed",
            "reason": result.error or "fetch failed",
            "resolver_chain": list(result.resolver_chain or []),
            "attempts": sanitize_for_persistence(list(result.attempts or [])),
            "transport_policy": TRANSPORT_POLICY,
            "transport_attempts": sanitize_for_persistence(list(result.transport_attempts or [])),
            "final_reason": result.error or "fetch failed",
        })
        if result.transport_attempts:
            write_fetch_result(
                folder,
                _sanitized_fetch_record(
                    result,
                    None,
                    header_keys,
                    success=False,
                    final_reason=result.error or "fetch failed",
                ),
            )
        return result, None
    try:
        attached = allocator.attach_pdf(
            candidate.paper_number,
            result.output_path,
            move=True,
            replace=force_refetch,
        )
    except DuplicateIngestError as exc:
        item.update({
            "status": "duplicate",
            "error": "pdf_duplicate",
            "reason": "fetched PDF duplicates an existing paper_raw/papers PDF",
            "duplicate_reasons": exc.result.reasons,
            "duplicate_refs": [ref.to_dict() for ref in exc.result.refs],
            "pdf_md5": exc.result.pdf_md5,
            "pdf_sha256": exc.result.pdf_sha256,
            "transport_policy": TRANSPORT_POLICY,
            "transport_attempts": sanitize_for_persistence(list(result.transport_attempts or [])),
        })
        write_import_status(
            folder,
            "duplicate",
            reason=item["reason"],
            extra={
                "duplicate_reasons": item["duplicate_reasons"],
                "duplicate_refs": item["duplicate_refs"],
                "paper_number": candidate.paper_number,
                "paper_raw_id": candidate.paper_number,
                "source_type": "network_search",
                "source_provider": ((candidate.metadata or {}).get("source") or {}).get("provider", "network_search") if candidate.metadata else "network_search",
                "doi": candidate.doi,
                "pdf_md5": exc.result.pdf_md5,
                "pdf_sha256": exc.result.pdf_sha256,
            },
        )
        if result.transport_attempts:
            write_fetch_result(
                folder,
                _sanitized_fetch_record(
                    result,
                    {
                        "pdf_md5": exc.result.pdf_md5,
                        "pdf_sha256": exc.result.pdf_sha256,
                    },
                    header_keys,
                    success=False,
                    final_reason="duplicate PDF",
                ),
            )
        return result, None
    return result, attached


def _record_result(
    candidate: FetchCandidateStatus,
    result: Any,
    attached: dict[str, Any],
    *,
    force_refetch: bool,
    header_keys: list[str],
    item: dict[str, Any],
) -> None:
    """Persist provenance for an attached PDF and finalize ``item``:
    fetch record, stage_manifest pdf_source, match receipt, and (when
    matched) the metadata freeze — in that order."""
    folder = candidate.folder
    meta_path = folder / f"{candidate.paper_number}.metadata.json"

    metadata = read_json(meta_path, {})
    fetch_record = _sanitized_fetch_record(result, attached, header_keys)
    # fetch_result.json is a SEPARATE file from metadata source records.
    # Never write the fetch result to metadata.source.raw_record_path.
    write_fetch_result(folder, fetch_record)
    # Enrich the stage_manifest pdf_source with fetch-specific details.
    existing_manifest = read_stage_manifest(folder)
    pdf_source = existing_manifest.get("pdf_source") if isinstance(existing_manifest.get("pdf_source"), dict) else None
    if pdf_source is None:
        pdf_source = doi_fetch_pdf_source(operation="attach")
    pdf_source.update(doi_fetch_pdf_source(
        operation="replace" if force_refetch else "attach",
        fetch_record_path=fetch_result_rel_path(),
        resolver=result.resolver,
        pdf_url=sanitize_url_for_persistence(result.pdf_url or ""),
        doi=candidate.doi,
    ))
    update_stage_manifest(folder, updates={"pdf_source": pdf_source})
    # Build the sole authoritative match sidecar from independent PDF
    # bytes. If the PDF text layer exposes the DOI, network ingest can
    # freeze immediately; otherwise conversion will regenerate richer
    # evidence from Markdown before Catalog generation.
    from src.metadata.pdf_identity import extract_pdf_identity_evidence
    from src.metadata.pdf_match import build_match_receipt, write_match_receipt
    from src.metadata.freeze import freeze_metadata
    from src.ingest.status import update_status
    from src.ingest.workspace import PaperRawWorkspace
    evidence=extract_pdf_identity_evidence(pdf_path=folder/f"{candidate.paper_number}.pdf")
    provider_record=str((metadata.get("source") or {}).get("raw_record_path") or "")
    match=build_match_receipt(folder,candidate.paper_number,metadata,evidence,requested_doi=candidate.doi,provider_records=[provider_record] if provider_record else [])
    write_match_receipt(folder,match)
    if match["match_status"] in {"matched","manual_confirmed"}:
        frozen=freeze_metadata(folder,candidate.paper_number)
        update_status(PaperRawWorkspace.from_path(folder),"metadata","frozen",revision=frozen["revision"])
        item["metadata_frozen"]=True
    else:
        update_status(PaperRawWorkspace.from_path(folder),"metadata","mismatch",match_method=match["match_method"])
    item.update({
        **attached,
        "status": "attached",
        "reason": "",
        "resolver": result.resolver,
        "pdf_path": attached.get("pdf", ""),
        "pdf_md5": attached.get("pdf_md5", ""),
        "pdf_sha256": attached.get("pdf_sha256", ""),
        "transport_policy": TRANSPORT_POLICY,
        "transport_attempts": sanitize_for_persistence(list(result.transport_attempts or [])),
    })


def _fetch_one(
    candidate: FetchCandidateStatus,
    *,
    policy: AccessPolicy,
    allocator: PaperRawAllocator,
    force_refetch: bool,
    header_keys: list[str],
) -> dict[str, Any]:
    start = time.monotonic()
    item = candidate.to_item()
    item["transport_policy"] = TRANSPORT_POLICY
    item["transport_attempts"] = []
    folder = candidate.folder
    metadata = dict(candidate.metadata or {})
    metadata.setdefault("identifiers", {})["doi"] = candidate.doi
    title = ((metadata.get("title") or {}).get("original") or "").strip()
    year = metadata.get("year")
    fetch_root = folder / ".fetch"
    source_record, source_record_status, source_record_error = _load_source_record(folder, metadata)
    item["source_record_status"] = source_record_status
    item["source_record_error"] = source_record_error
    try:
        result, attached = _resolve_and_download(
            candidate,
            policy=policy,
            allocator=allocator,
            force_refetch=force_refetch,
            header_keys=header_keys,
            metadata=metadata,
            title=title,
            year=year,
            source_record=source_record,
            fetch_root=fetch_root,
            item=item,
        )
        if attached is None:
            return item
        _record_result(
            candidate,
            result,
            attached,
            force_refetch=force_refetch,
            header_keys=header_keys,
            item=item,
        )
        return item
    except Exception as exc:
        item.update({"status": "failed", "reason": str(exc)})
        return item
    finally:
        item["duration_seconds"] = round(time.monotonic() - start, 3)
        shutil.rmtree(fetch_root, ignore_errors=True)


def _merge(
    candidates: list[FetchCandidateStatus],
    items: list[dict[str, Any]],
    completed: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Overlay finished results onto the planned items, preserving order."""
    return [
        completed.get(candidate.paper_number, item)
        for candidate, item in zip(candidates, items)
    ]


def _payload(
    args: argparse.Namespace,
    policy: AccessPolicy,
    header_keys: list[str],
    write: bool,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "applied": write,
        "paper_raw_dir": str(args.paper_raw_dir),
        "resolver": "header_based" if args.resolver == "header-based" else args.resolver,
        "access_mode": policy.mode.value,
        "selection": {
            "skip_attempted": bool(args.skip_attempted),
            "retry_after_days": args.retry_after_days,
            "doi_prefixes": list(args.doi_prefix),
            "limit": args.limit,
        },
        "summary": _summary(items),
        "headers": {
            "keys": header_keys,
            "masked": bool(header_keys),
        },
        "items": sanitize_for_persistence(items),
    }


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload, indent=2)


def _write_blocked_worklist(path: Path, items: list[dict[str, Any]]) -> int:
    """Write failures that hit a publisher refusing this network.

    These papers cannot be rescued by retrying; they need institutional
    access or a different egress, so they belong on an operator worklist
    rather than in the next run's queue.
    """
    rows = [
        {
            "paper_number": item.get("paper_number", ""),
            "doi": item.get("doi", ""),
            "doi_url": f"https://doi.org/{item.get('doi', '')}" if item.get("doi") else "",
            "reason": item.get("final_reason") or item.get("reason") or "",
        }
        for item in items
        if classify_failure(item) == "blocked_publisher"
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["paper_number", "doi", "doi_url", "reason"])
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def _summary(items: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "scanned": len(items),
        "eligible": sum(1 for item in items if item["status"] in {"planned", "attached", "failed", "duplicate"}),
        "planned": sum(1 for item in items if item["status"] == "planned"),
        "attached": sum(1 for item in items if item["status"] == "attached"),
        "skipped": sum(1 for item in items if item["status"] == "skipped"),
        "failed": sum(1 for item in items if item["status"] == "failed"),
        "duplicate": sum(1 for item in items if item["status"] == "duplicate"),
        # Of the failures, how many are unreachable from this network rather
        # than merely unlucky -- they need institutional access, not a retry.
        "blocked_publisher": sum(1 for item in items if classify_failure(item) == "blocked_publisher"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch PDFs into v2 paper_raw folders.")
    parser.add_argument("--paper-number", default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument(
        "--resolver",
        choices=["auto", "oa", "header-based"],
        default="auto",
        help="auto: original_link → OA → publisher-specific → header_based "
             "(always included; defaults to doi.org without --base-url); "
             "oa: original_link + OA resolvers only; "
             "header-based: explicit header-based resolver only.",
    )
    parser.add_argument(
        "--only-missing-pdf",
        action="store_true",
        help="Affected only when --force-refetch is absent (the default): a workspace that "
             "already has <paper_number>.pdf is skipped. Provided for SOP readability; this "
             "behavior is already the default -- use --force-refetch to re-fetch existing PDFs.",
    )
    parser.add_argument("--base-url", default="")
    parser.add_argument("--url-template", default="")
    parser.add_argument("--header", action="append", default=[])
    parser.add_argument("--headers-json", type=Path, default=None)
    parser.add_argument(
        "--strict-headers",
        action="store_true",
        help="Fail if User-Agent is supplied via --header/--headers-json. By default a "
             "supplied User-Agent is ignored with a warning because the header_based resolver "
             "pins a fixed User-Agent; Cookie/Authorization remain accepted and masked.",
    )
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--max-workers", type=int, default=2)
    parser.add_argument("--force-refetch", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    # Batch controls: the eligible backlog is far larger than one run, so a
    # run must be able to reach fresh work without replaying every known-hard
    # failure first, and must survive interruption.
    parser.add_argument(
        "--skip-attempted",
        action="store_true",
        help="Skip workspaces that already have a fetch_result sidecar, so a run "
             "spends its time on workspaces never tried before.",
    )
    parser.add_argument(
        "--retry-after-days",
        type=float,
        default=None,
        help="Only retry a previously attempted workspace when its last attempt is "
             "older than this many days. Ignored for never-attempted workspaces.",
    )
    parser.add_argument(
        "--doi-prefix",
        action="append",
        default=[],
        metavar="10.5194",
        help="Only fetch DOIs with this registrant prefix. Repeatable; use it to run "
             "high-yield publishers first.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Attempt at most N workspaces in this run (after all other filters).",
    )
    parser.add_argument(
        "--report-blocked",
        type=Path,
        default=None,
        help="Write a CSV worklist of failures that hit a publisher which refuses this "
             "network, for institutional-access or manual download.",
    )
    args = parser.parse_args()

    try:
        headers = _load_headers(args.headers_json, args.header, ua_warn_only=not args.strict_headers)
        policy = _build_policy(args, headers)
        paper_numbers = _paper_numbers(args.paper_raw_dir, args.all, args.paper_number)
    except Exception as exc:
        parser.error(str(exc))

    write = args.apply and not args.dry_run
    header_keys = _header_keys(headers, include_user_agent=args.resolver == "header-based")
    candidates = select_fetch_candidates(
        [
            classify_pdf_fetch_candidate(
                args.paper_raw_dir / paper_number,
                paper_number,
                force_refetch=args.force_refetch,
            )
            for paper_number in paper_numbers
        ],
        FetchSelection(
            skip_attempted=args.skip_attempted,
            retry_after_days=args.retry_after_days,
            doi_prefixes=tuple(args.doi_prefix),
            limit=args.limit,
        ),
    )
    items = [candidate.to_item() for candidate in candidates]
    for item in items:
        item.setdefault("transport_policy", TRANSPORT_POLICY)
        item.setdefault("transport_attempts", [])

    eligible = [candidate for candidate in candidates if candidate.eligible]
    if write and eligible:
        allocator = PaperRawAllocator(args.paper_raw_dir, papers_dir=args.papers_dir)
        completed: dict[str, dict[str, Any]] = {}
        workers = max(1, int(args.max_workers or 1))
        logger.info("fetching {} of {} workspaces with {} workers", len(eligible), len(candidates), workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    _fetch_one,
                    candidate,
                    policy=policy,
                    allocator=allocator,
                    force_refetch=args.force_refetch,
                    header_keys=header_keys,
                ): candidate
                for candidate in eligible
            }
            done = 0
            for future in as_completed(futures):
                candidate = futures[future]
                completed[candidate.paper_number] = future.result()
                done += 1
                # Flush after every result: a long backlog run WILL be
                # interrupted, and an all-or-nothing report throws away every
                # completed item when it is.
                if args.report:
                    _write_report(
                        args.report,
                        _payload(args, policy, header_keys, write,
                                 _merge(candidates, items, completed)),
                    )
                if done % 10 == 0 or done == len(eligible):
                    logger.info("fetched {}/{}", done, len(eligible))
        items = _merge(candidates, items, completed)
    elif not write:
        for item in items:
            if item["status"] == "planned":
                logger.info("DRY-RUN fetch {} for {}", item["doi"], item["paper_number"])

    payload = _payload(args, policy, header_keys, write, items)
    if args.report:
        _write_report(args.report, payload)
    if args.report_blocked:
        written = _write_blocked_worklist(args.report_blocked, items)
        logger.info("blocked-publisher worklist: {} rows -> {}", written, args.report_blocked)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if any(item["status"] in {"failed", "duplicate"} for item in items) else 0


if __name__ == "__main__":
    raise SystemExit(main())

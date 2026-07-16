"""In-process DOI discovery coordinator.

This is the single active engine used by both single-keyword and multi-keyword
CLI entrypoints. It coordinates journal-first provider paging, pending drains,
global lane concurrency, provider limiters, and report aggregation.
"""
from __future__ import annotations

import threading
import time
import uuid
import queue
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Literal

from filelock import FileLock, Timeout as FileLockTimeout

from config.settings import (
    DISCOVERY_DIR,
    DISCOVERY_EXPORTS_DIR,
    DISCOVERY_KEYWORD_NOTEBOOK_DIR,
    DISCOVERY_LOCKS_DIR,
    DISCOVERY_PENDING_PAGES_DIR,
    PAPER_NUMBER_LEDGER_PATH,
    PAPER_RAW_DIR,
    PAPERS_DIR,
)
from src.discovery.backfill_transaction import (
    BackfillTransactionResult,
    StateLockTimeout,
    run_backfill_page_transaction,
)
from src.discovery.constants import INITIAL_CURSOR
from src.discovery.batch_runtime import (
    ActiveRelevanceProfiles,
    DiscoveryBatchRuntime,
    DiscoveryPipelineMetrics,
)
from src.discovery.keyword_notebook import (
    PROVIDERS,
    KeywordNotebookStore,
    LegacyNotebookSchemaError,
    NotebookCorruptError,
    UnsupportedNotebookSchemaError,
    _active_queries,
    keyword_id as make_keyword_id,
    validate_discovery_readiness,
)
from src.discovery.page_journal import (
    PageJournalStore,
    parse_iso,
    refresh_page_id,
    request_signature,
)
from src.discovery.relevance_profiles import (
    list_applying_relevance_profile_transactions,
)
from src.discovery.relevance import (
    OpenAlexDoiVerifier,
    RawOpenAlexWorkCache,
    evaluate_page_candidates,
    is_legacy_unbound_profile,
    openalex_topic_filter,
)
from src.discovery.pending_queue import DrainReport, drain_pending_candidates
from src.discovery.provider_models import DiscoveryPage, failed_page
from src.discovery.resolve_crossref import search_crossref_page
from src.discovery.search_openalex import search_openalex_page
from src.services.rate_limit import ProviderRateLimiter, default_config


DiscoveryMode = Literal["refresh", "backfill", "hybrid"]
STAGING_QUEUE_CAPACITY = 500


@dataclass
class PageBudget:
    limit: int | None = None
    used: int = 0
    exhausted: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def try_acquire(self) -> bool:
        with self._lock:
            if self.limit is not None and self.used >= self.limit:
                self.exhausted = True
                return False
            self.used += 1
            return True


@dataclass
class DiscoveryOptions:
    mode: DiscoveryMode = "hybrid"
    refresh_pages: int = 2
    backfill_pages: int = 5
    page_size: int = 50
    max_candidates: int = 50
    stage_to_paper_raw: bool = False
    apply: bool = False
    skip_duplicates: bool = False
    hide_existing: bool = False
    until_exhausted: bool = False
    max_pages_total: int | None = None
    doi_resolution_budget: int = 10
    max_pending_candidates: int = 1000
    resume_pending_candidates: int = 700
    staging_no_progress_timeout_seconds: float = 300.0
    notebook_dir: Path = DISCOVERY_KEYWORD_NOTEBOOK_DIR
    pending_pages_dir: Path = DISCOVERY_PENDING_PAGES_DIR
    locks_dir: Path = DISCOVERY_LOCKS_DIR
    exports_dir: Path = DISCOVERY_EXPORTS_DIR
    output_dir: Path = DISCOVERY_DIR / "doi_candidates"
    paper_raw_dir: Path = PAPER_RAW_DIR
    papers_dir: Path = PAPERS_DIR
    ledger_path: Path = PAPER_NUMBER_LEDGER_PATH
    relevance_cache_dir: Path = DISCOVERY_DIR / "relevance_raw_work_cache"
    relevance_profiles_lock_path: Path | None = None
    relevance_profiles_transaction_root: Path | None = None
    # Tests and isolated callers may inject a DOI verifier.  Production uses
    # the raw-Work cache backed OpenAlex verifier created by the coordinator.
    crossref_scope_verifier: Any | None = None


@dataclass
class LaneReport:
    status: str = "skipped"
    pages_requested: int = 0
    pages_recovered: int = 0
    pages_persisted: int = 0
    pages_committed: int = 0
    journals_recovered: int = 0
    items_returned: int = 0
    provider_failures: int = 0
    states_exhausted: int = 0
    cursor_conflicts: int = 0
    stop_reason: str | None = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "pages_requested": self.pages_requested,
            "pages_recovered": self.pages_recovered,
            "pages_persisted": self.pages_persisted,
            "pages_committed": self.pages_committed,
            "journals_recovered": self.journals_recovered,
            "items_returned": self.items_returned,
            "provider_failures": self.provider_failures,
            "states_exhausted": self.states_exhausted,
            "cursor_conflicts": self.cursor_conflicts,
            "stop_reason": self.stop_reason,
            "errors": list(self.errors),
        }


@dataclass
class KeywordDiscoveryReport:
    keyword_zh: str
    keyword_id: str
    status: str
    refresh: LaneReport
    backfill: LaneReport
    pending: DrainReport
    final_pending: DrainReport
    candidates: dict[str, int]
    budget: dict[str, Any]
    mode: str
    queries_total: int = 0
    queries_zh: int = 0
    queries_en: int = 0
    queries_executed: list[dict[str, str]] = field(default_factory=list)
    backpressure: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "3.0",
            "keyword_zh": self.keyword_zh,
            "keyword_id": self.keyword_id,
            "status": self.status,
            "mode": self.mode,
            "queries_total": self.queries_total,
            "queries_zh": self.queries_zh,
            "queries_en": self.queries_en,
            "queries_executed": [dict(item) for item in self.queries_executed],
            "refresh": self.refresh.to_dict(),
            "backfill": self.backfill.to_dict(),
            "pending": self.pending.to_dict(),
            "final_pending": self.final_pending.to_dict(),
            "candidates": dict(self.candidates),
            "budget": dict(self.budget),
            "backpressure": self.backpressure,
            "errors": list(self.errors),
        }


@dataclass
class BatchDiscoveryReport:
    status: str
    keywords: list[KeywordDiscoveryReport]
    aggregate: dict[str, Any]
    exit_code: int
    pipeline_metrics: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "3.0",
            "status": self.status,
            "exit_code": self.exit_code,
            "keywords": [kw.to_dict() for kw in self.keywords],
            "aggregate": self.aggregate,
            "pipeline_metrics": dict(self.pipeline_metrics),
        }


def _profile_sort(nb: dict[str, Any], provider: str, lane: str, options: DiscoveryOptions) -> str | None:
    profile = nb.get("relevance_profile")
    if isinstance(profile, dict):
        if is_legacy_unbound_profile(profile):
            return None
        if provider == "openalex":
            return str(profile["openalex"]["refresh_sort" if lane == "refresh" else "backfill_sort"])
        return str(profile["crossref"]["refresh_sort" if lane == "refresh" else "backfill_sort"])
    return None


def _profile_order(nb: dict[str, Any], lane: str) -> str | None:
    profile = nb.get("relevance_profile")
    if isinstance(profile, dict):
        if is_legacy_unbound_profile(profile):
            return None
        return str(profile["crossref"]["refresh_order" if lane == "refresh" else "backfill_order"])
    return None


def _profile_filters(nb: dict[str, Any], provider: str, lane: str, sort: str | None, order: str | None) -> dict[str, Any]:
    profile = nb.get("relevance_profile")
    if not isinstance(profile, dict):
        return {"provider": provider, "lane": lane, "sort": sort or "", "order": order or ""}
    if is_legacy_unbound_profile(profile):
        return {}
    return {
        "provider": provider,
        "lane": lane,
        "profile_hash": profile["profile_hash"],
        "openalex_filter": "" if is_legacy_unbound_profile(profile) else openalex_topic_filter(profile),
        "scope_policy": profile["crossref"]["scope_policy"],
        "sort": sort or "",
        "order": order or "",
    }


_OPENALEX_SORT_VALUES = frozenset({
    "relevance" + "_score:desc", "relevance" + "_score:asc",
    "cited_by_count:desc", "cited_by_count:asc",
    "publication_date:desc", "publication_date:asc",
})
_CROSSREF_SORT_VALUES = frozenset({"relevance", "published", "cited"})
_CROSSREF_ORDER_VALUES = frozenset({"asc", "desc"})


def _validate_openalex_sort(sort: str | None) -> str | None:
    """Validate an OpenAlex sort; invalid values fail closed."""
    if not sort:
        return None
    tokens = [t.strip() for t in sort.split(",")]
    for token in tokens:
        if token not in _OPENALEX_SORT_VALUES:
            raise ValueError(f"invalid OpenAlex sort: {sort!r}")
    return ",".join(tokens)


def _validate_crossref_sort_order(sort: str | None, order: str | None) -> tuple[str | None, str | None]:
    """Validate Crossref sort/order; invalid values fail closed."""
    if sort and sort.strip().lower() not in _CROSSREF_SORT_VALUES:
        raise ValueError(f"invalid Crossref sort: {sort!r}")
    if order and order.strip().lower() not in _CROSSREF_ORDER_VALUES:
        raise ValueError(f"invalid Crossref order: {order!r}")
    return (sort.strip().lower() if sort else None,
            order.strip().lower() if order else None)


def _default_fetch_page(
    provider: str,
    query: str,
    *,
    keyword_zh: str,
    query_id: str = "",
    query_language: str = "",
    lane: str,
    page_size: int,
    cursor: str,
    sort: str | None = None,
    order: str | None = None,
    topic_filter: str = "",
    rate_limiter: Any | None = None,
    limiter_lock: threading.Lock | None = None,
) -> Any:
    if provider == "openalex":
        if sort and "relevance" + "_score:" in sort and not query.strip():
            raise ValueError("OpenAlex relevance" + "_score sort requires a non-empty search query")
        return search_openalex_page(
            query,
            keyword_zh=keyword_zh,
            query_id=query_id,
            query_language=query_language,
            lane=lane,
            page_size=page_size,
            cursor=cursor,
            sort=_validate_openalex_sort(sort),
            topic_filter=topic_filter,
            rate_limiter=rate_limiter,
            limiter_lock=limiter_lock,
        )
    if provider == "crossref":
        crossref_sort, crossref_order = _validate_crossref_sort_order(sort, order)
        return search_crossref_page(
            query,
            keyword_zh=keyword_zh,
            query_id=query_id,
            query_language=query_language,
            lane=lane,
            page_size=page_size,
            cursor=cursor,
            sort=crossref_sort,
            order=crossref_order,
            rate_limiter=rate_limiter,
            limiter_lock=limiter_lock,
        )
    raise ValueError(f"unknown provider: {provider}")


def _status_from_failures(ran: bool, failures: int, items: int, skipped_reason: str = "") -> str:
    if not ran:
        return skipped_reason or "skipped"
    if failures == 0:
        return "success"
    return "partial_success" if items > 0 else "failed"


def _batch_status(keyword_reports: list[KeywordDiscoveryReport]) -> tuple[str, int]:
    statuses = [r.status for r in keyword_reports]
    if any(s == "failed" for s in statuses):
        return "failed", 1
    if any(s == "partial_success" for s in statuses):
        return "partial_success", 2
    return "success", 0


def _validate_discovery_options(
    options: DiscoveryOptions,
    keywords: list[str],
    *,
    max_workers: int,
) -> None:
    if max_workers < 1:
        raise ValueError(f"max_workers must be >= 1; got {max_workers!r}")
    if not keywords or not any(str(keyword or "").strip() for keyword in keywords):
        raise ValueError("keywords must contain at least one non-blank query")
    if options.mode not in {"refresh", "backfill", "hybrid"}:
        raise ValueError(f"mode must be one of refresh/backfill/hybrid; got {options.mode!r}")
    if options.page_size < 1:
        raise ValueError(f"page_size must be >= 1; got {options.page_size!r}")
    if options.refresh_pages < 0:
        raise ValueError(f"refresh_pages must be >= 0; got {options.refresh_pages!r}")
    if options.backfill_pages < 1:
        raise ValueError(f"backfill_pages must be >= 1; got {options.backfill_pages!r}")
    if options.max_candidates < 0:
        raise ValueError(f"max_candidates must be >= 0; got {options.max_candidates!r}")
    if options.max_pending_candidates < 1:
        raise ValueError(f"max_pending_candidates must be >= 1; got {options.max_pending_candidates!r}")
    if options.resume_pending_candidates < 0 or options.resume_pending_candidates >= options.max_pending_candidates:
        raise ValueError(
            "resume_pending_candidates must satisfy 0 <= resume < max_pending_candidates; "
            f"got resume={options.resume_pending_candidates!r}, max={options.max_pending_candidates!r}"
        )
    if options.staging_no_progress_timeout_seconds <= 0:
        raise ValueError("staging_no_progress_timeout_seconds must be positive")
    if options.max_pages_total is not None and options.max_pages_total < 1:
        raise ValueError(f"max_pages_total must be a positive integer or None; got {options.max_pages_total!r}")
    if options.until_exhausted and options.max_pages_total is None:
        raise ValueError("max_pages_total must be a positive integer when until_exhausted=True; got None")
    if options.until_exhausted and options.mode not in {"backfill", "hybrid"}:
        raise ValueError(f"until_exhausted requires mode backfill or hybrid; got {options.mode!r}")
    if options.apply and not options.stage_to_paper_raw:
        raise ValueError("apply=True requires stage_to_paper_raw=True")
    if options.skip_duplicates and not options.stage_to_paper_raw:
        raise ValueError("skip_duplicates=True requires stage_to_paper_raw=True")


def _aggregate(keyword_reports: list[KeywordDiscoveryReport], budget: PageBudget) -> dict[str, Any]:
    agg = {
        "keywords": {
            "total": len(keyword_reports),
            "success": 0,
            "partial_success": 0,
            "failed": 0,
            "skipped": 0,
            "exhausted": 0,
        },
        "refresh": {
            "pages_requested": 0,
            "pages_recovered": 0,
            "pages_persisted": 0,
            "items_returned": 0,
            "provider_failures": 0,
            "stop_reasons": {},
        },
        "backfill": {
            "pages_requested": 0,
            "pages_recovered": 0,
            "pages_persisted": 0,
            "pages_committed": 0,
            "journals_recovered": 0,
            "states_exhausted": 0,
            "provider_failures": 0,
        },
        "pending": {"processed": 0, "remaining": 0, "backpressure": 0},
        "candidates": {
            "staged": 0,
            "emitted": 0,
            "existing_duplicates": 0,
            "duplicate_observations": 0,
            "invalid": 0,
            "unresolved": 0,
            "retryable_failures": 0,
        },
        "budget": {"page_limit": budget.limit, "pages_used": budget.used, "page_budget_exhausted": budget.exhausted},
    }
    for report in keyword_reports:
        agg["keywords"][report.status] = agg["keywords"].get(report.status, 0) + 1
        for section_name in ("refresh", "backfill"):
            section = getattr(report, section_name)
            for field_name in agg[section_name]:
                if field_name == "stop_reasons":
                    continue
                if hasattr(section, field_name):
                    agg[section_name][field_name] += int(getattr(section, field_name))
            if section.stop_reason:
                reasons = agg[section_name].setdefault("stop_reasons", {})
                reasons[section.stop_reason] = int(reasons.get(section.stop_reason, 0)) + 1
        for drain in (report.pending, report.final_pending):
            agg["pending"]["processed"] += drain.processed
            agg["candidates"]["staged"] += drain.staged
            agg["candidates"]["emitted"] += drain.emitted
            agg["candidates"]["existing_duplicates"] += drain.existing_duplicate
            agg["candidates"]["duplicate_observations"] += drain.duplicate_observation
            agg["candidates"]["invalid"] += drain.invalid
            agg["candidates"]["unresolved"] += drain.unresolved
            agg["candidates"]["retryable_failures"] += drain.retryable_failures
        agg["pending"]["remaining"] += report.final_pending.remaining
        if report.backpressure:
            agg["pending"]["backpressure"] += 1
    return agg


def _run_discovery_batch_unlocked(
    keywords: list[str],
    *,
    options: DiscoveryOptions | None = None,
    max_workers: int = 4,
    fetch_page: Callable[..., Any] | None = None,
    rate_limiters: dict[str, ProviderRateLimiter] | None = None,
) -> BatchDiscoveryReport:
    options = options or DiscoveryOptions()
    _validate_discovery_options(options, keywords, max_workers=max_workers)
    fetch_page = fetch_page or _default_fetch_page
    notebook = KeywordNotebookStore(options.notebook_dir)
    journal = PageJournalStore(options.pending_pages_dir)
    enabled_notebooks: dict[str, dict[str, Any]] = {}
    active_profile_hashes: dict[str, str] = {}
    global_binding_error = ""
    try:
        for summary in notebook.list_keywords():
            if not summary["enabled"]:
                continue
            current = notebook.require_v3(str(summary["keyword_zh"]))
            readiness = validate_discovery_readiness(current)
            if not readiness:
                raise RuntimeError(
                    f"enabled notebook {current['keyword_zh']!r} is not discovery-ready: "
                    + "; ".join(readiness.errors)
                )
            enabled_notebooks[str(current["keyword_zh"])] = current
            active_profile_hashes[str(current["keyword_id"])] = str(
                current["relevance_profile"]["profile_hash"]
            )
    except (
        RuntimeError, NotebookCorruptError, LegacyNotebookSchemaError,
        UnsupportedNotebookSchemaError,
    ) as exc:
        global_binding_error = str(exc)
    if global_binding_error:
        reports = [KeywordDiscoveryReport(
            keyword_zh=keyword, keyword_id=make_keyword_id(keyword), status="failed",
            refresh=LaneReport(status="failed", errors=[global_binding_error]),
            backfill=LaneReport(status="failed", errors=[global_binding_error]),
            pending=DrainReport(), final_pending=DrainReport(), candidates={},
            budget={"page_limit": options.max_pages_total, "pages_used": 0},
            mode=options.mode, errors=[global_binding_error],
        ) for keyword in keywords]
        preflight_budget = PageBudget(options.max_pages_total)
        return BatchDiscoveryReport(
            status="failed", keywords=reports,
            aggregate=_aggregate(reports, preflight_budget), exit_code=1,
            pipeline_metrics=DiscoveryPipelineMetrics().to_dict(),
        )
    active_profiles = ActiveRelevanceProfiles.build(active_profile_hashes)
    runtime = DiscoveryBatchRuntime.create(
        journal=journal, paper_raw_dir=options.paper_raw_dir,
        papers_dir=options.papers_dir, ledger_path=options.ledger_path,
        needs_staging=bool(options.stage_to_paper_raw or options.hide_existing),
        active_relevance_profiles=active_profiles,
        persist_repair_cursor=bool(options.apply and options.stage_to_paper_raw),
    )
    staging_notifications: queue.Queue[tuple[str, int] | None] = queue.Queue(
        maxsize=STAGING_QUEUE_CAPACITY)
    staging_candidate_slots = threading.BoundedSemaphore(STAGING_QUEUE_CAPACITY)
    state_lock = threading.RLock()
    progress_lock = threading.Lock()
    last_staging_progress = [time.monotonic()]
    dynamically_backpressured: set[str] = set()
    staging_budget_exhausted: set[str] = set()
    budget = PageBudget(options.max_pages_total)
    limiters = rate_limiters or {
        "openalex": ProviderRateLimiter(default_config()),
        "crossref": ProviderRateLimiter(default_config()),
    }
    limiter_locks = {provider: threading.Lock() for provider in PROVIDERS}
    cache_dir = options.relevance_cache_dir
    if (
        cache_dir == DiscoveryOptions().relevance_cache_dir
        and options.notebook_dir != DISCOVERY_KEYWORD_NOTEBOOK_DIR
    ):
        cache_dir = Path(options.notebook_dir).parent / ".relevance_raw_work_cache"
    raw_work_cache = RawOpenAlexWorkCache(cache_dir)
    default_scope_verifier = OpenAlexDoiVerifier(
        cache=raw_work_cache,
        rate_limiter=limiters.get("openalex"),
        limiter_lock=limiter_locks["openalex"],
    )
    scope_verifier = options.crossref_scope_verifier or default_scope_verifier
    worker_id = f"worker-{uuid.uuid4().hex[:12]}"
    keyword_reports: dict[str, KeywordDiscoveryReport] = {}
    executed_queries: dict[str, set[tuple[str, str]]] = {}
    executed_queries_lock = threading.Lock()

    def finalize_page_relevance(page_path: Path, profile: dict[str, Any], provider: str) -> dict[str, dict[str, Any]]:
        page_data = journal.read(page_path)
        return evaluate_page_candidates(
            page_data.get("candidates") or [],
            profile,
            provider=provider,
            scope_verifier=scope_verifier,
        )

    def retry_due_deferred(keyword_id: str, profile: dict[str, Any]) -> None:
        now_dt = datetime.now(timezone.utc)
        changed = False
        for ref in journal.list_pages([keyword_id]):
            if ref.state not in {"cursor_committed", "draining"}:
                continue
            page = journal.read(ref.path)
            due = False
            for item in page.get("candidates", []):
                relevance = item.get("relevance") if isinstance(item.get("relevance"), dict) else {}
                if (
                    relevance.get("state") == "verification_deferred"
                    and relevance.get("profile_hash") == profile.get("profile_hash")
                    and (not relevance.get("next_retry_at") or (parse_iso(relevance.get("next_retry_at")) or now_dt) <= now_dt)
                ):
                    due = True
                    break
            if not due:
                continue
            due_items = [
                item for item in page.get("candidates", [])
                if isinstance(item.get("relevance"), dict)
                and item["relevance"].get("state") == "verification_deferred"
                and item["relevance"].get("profile_hash") == profile.get("profile_hash")
                and (
                    not item["relevance"].get("next_retry_at")
                    or (parse_iso(item["relevance"].get("next_retry_at")) or now_dt) <= now_dt
                )
            ]
            decisions = evaluate_page_candidates(
                due_items,
                profile,
                provider=str(page["provider"]),
                scope_verifier=scope_verifier,
            )
            updated_page = journal.retry_deferred_relevance(ref.path, decisions)
            updated_candidates = [
                candidate for candidate in updated_page["candidates"]
                if str(candidate.get("candidate_id") or "") in decisions
            ]
            runtime.journal_index.apply_relevance_updates(
                ref.path, updated_candidates,
            )
            runtime.metrics.relevance_incremental_updates += len(updated_candidates)
            changed = True
        if changed:
            try:
                runtime.journal_index.assert_active_bindings(
                    runtime.active_relevance_profiles.by_keyword_id
                )
            except RuntimeError:
                runtime.metrics.relevance_binding_invariant_failures += 1
                raise
            runtime.metrics.sync_journal(runtime.journal_index)

    def notify_staging(keyword_id: str, candidate_count: int) -> None:
        """Publish weighted candidate work with an exact 500-candidate bound."""
        remaining = max(0, candidate_count)
        while remaining:
            weight = min(STAGING_QUEUE_CAPACITY, remaining)
            for _ in range(weight):
                if not staging_candidate_slots.acquire(blocking=False):
                    with state_lock:
                        dynamically_backpressured.add(keyword_id)
                    staging_candidate_slots.acquire()
            staging_notifications.put((keyword_id, weight))
            remaining -= weight

    def candidate_budget_is_exhausted(keyword_id: str) -> bool:
        with state_lock:
            return keyword_id in staging_budget_exhausted

    def fetch_with_budget(provider: str, query: str, **kwargs: Any) -> Any:
        query_id_value = str(kwargs.pop("query_id", ""))
        query_language = str(kwargs.pop("query_language", ""))
        keyword_zh = str(kwargs.get("keyword_zh", ""))
        if query_language:
            with executed_queries_lock:
                executed_queries.setdefault(keyword_zh, set()).add(
                    (query, query_language)
                )
        if not budget.try_acquire():
            return failed_page(
                provider=provider,
                keyword_zh=kwargs.get("keyword_zh", ""),
                query=query,
                lane=kwargs.get("lane", "backfill"),
                request_cursor=kwargs.get("cursor"),
                page_size=int(kwargs.get("page_size") or options.page_size),
                error_type="page_budget_exhausted",
                safe_error="global page budget exhausted",
                query_id=query_id_value,
                query_language=query_language,
            )
        call_kwargs = dict(kwargs)
        if provider == "openalex":
            call_kwargs.setdefault("topic_filter", "")
            call_kwargs.pop("order", None)
        else:
            call_kwargs.pop("topic_filter", None)
        call_kwargs["rate_limiter"] = limiters.get(provider)
        call_kwargs["limiter_lock"] = limiter_locks.setdefault(provider, threading.Lock())
        try:
            page = fetch_page(provider, query, **call_kwargs)
        except TypeError as exc:
            # Keep injected test/fake providers written against the pre-
            # profile signature usable; production provider adapters accept
            # every optional keyword above and never take this path.
            message = str(exc)
            if "unexpected keyword argument" not in message:
                raise
            retry_kwargs = dict(call_kwargs)
            for optional in ("topic_filter", "order", "rate_limiter", "limiter_lock"):
                retry_kwargs.pop(optional, None)
                try:
                    page = fetch_page(provider, query, **retry_kwargs)
                    break
                except TypeError as retry_exc:
                    if "unexpected keyword argument" not in str(retry_exc):
                        raise
            else:
                raise
        if isinstance(page, DiscoveryPage):
            page = replace(
                page,
                query_id=page.query_id or query_id_value,
                query_language=page.query_language or query_language,
            )
        return page

    def run_refresh(keyword: str, nb: dict[str, Any], refresh_run_id: str, backpressure: bool) -> LaneReport:
        report = LaneReport(status="skipped" if backpressure else "success")
        if backpressure or options.mode not in {"refresh", "hybrid"}:
            return report
        kid = nb["keyword_id"]
        active_qs = _active_queries(nb)
        for aq in active_qs:
            query = aq["query"]
            query_id_value = aq["query_id"]
            query_language = aq["language"]
            for provider in PROVIDERS:
                sort = _profile_sort(nb, provider, "refresh", options)
                order = _profile_order(nb, "refresh") if provider == "crossref" else None
                sig = request_signature(
                    sort=sort,
                    filters=_profile_filters(nb, provider, "refresh", sort, order),
                    page_size=options.page_size,
                )
                topic_filter = (
                    "" if is_legacy_unbound_profile(nb["relevance_profile"])
                    else openalex_topic_filter(nb["relevance_profile"])
                ) if provider == "openalex" else ""
                cursor = INITIAL_CURSOR
                for seq in range(options.refresh_pages):
                    if candidate_budget_is_exhausted(kid):
                        report.stop_reason = "candidate_budget_exhausted"
                        report.status = "partial_success" if report.items_returned else "skipped"
                        return report
                    page = fetch_with_budget(
                        provider,
                        query,
                        keyword_zh=keyword,
                        lane="refresh",
                        page_size=options.page_size,
                        cursor=cursor,
                        sort=sort,
                        order=order,
                        topic_filter=topic_filter,
                        query_id=query_id_value,
                        query_language=query_language,
                    )
                    if page.status == "failed":
                        if page.error_type == "page_budget_exhausted":
                            report.status = "partial_success" if report.items_returned else "skipped"
                            return report
                        report.provider_failures += 1
                        report.errors.append(page.safe_error or page.error_type or "provider failed")
                        break
                    pid = refresh_page_id(
                        keyword_id=kid,
                        query_id=query_id_value,
                        provider=provider,
                        request_signature_hash=sig["hash"],
                        refresh_run_id=refresh_run_id,
                        page_sequence=seq,
                    )
                    page_data = journal.make_page(
                        page_id=pid,
                        keyword_id=kid,
                        keyword_zh=keyword,
                        query_id=query_id_value,
                        query=query,
                        query_language=query_language,
                        provider=provider,
                        lane="refresh",
                        generation=max(1, int(nb.get("relevance_generation") or 1)),
                        request_signature_value=sig,
                        request_cursor=cursor,
                        next_cursor=page.next_cursor,
                        provider_exhausted=page.exhausted,
                        candidates=page.candidates,
                        relevance_profile_hash=nb["relevance_profile"]["profile_hash"],
                        refresh_run_id=refresh_run_id,
                        page_sequence=seq,
                        state="fetched",
                    )
                    page_path = journal.write_page(page_data)
                    decisions = finalize_page_relevance(page_path, nb["relevance_profile"], provider)
                    page_data = journal.finalize_relevance(page_path, decisions)
                    page_data = journal.mark_cursor_committed(page_path)
                    with state_lock:
                        runtime.metrics.journal_pages_written += 1
                        runtime.metrics.page_fsyncs += 1
                    runtime.journal_index.add_page(page_path, page_data)
                    # Bounded notification queue is the provider backpressure
                    # boundary; the single consumer owns staging mutations.
                    notify_staging(
                        str(page_data["keyword_id"]), len(page_data.get("candidates") or []))
                    report.pages_requested += 1
                    report.pages_persisted += 1
                    report.items_returned += int(page.returned_count)
                    if page.exhausted or not page.next_cursor:
                        break
                    cursor = page.next_cursor
        report.status = _status_from_failures(True, report.provider_failures, report.items_returned)
        return report

    def run_backfill(keyword: str, nb: dict[str, Any], backpressure: bool) -> LaneReport:
        report = LaneReport(
            status="skipped" if backpressure else "success",
            stop_reason="backpressure_active" if backpressure else None,
        )
        if backpressure or options.mode not in {"backfill", "hybrid"}:
            return report
        kid = nb["keyword_id"]
        active_qs = _active_queries(nb)
        for aq in active_qs:
            query = aq["query"]
            query_id_value = aq["query_id"]
            query_language = aq["language"]
            for provider in PROVIDERS:
                sort = _profile_sort(nb, provider, "backfill", options)
                order = _profile_order(nb, "backfill") if provider == "crossref" else None
                sig = request_signature(
                    sort=sort,
                    filters=_profile_filters(nb, provider, "backfill", sort, order),
                    page_size=options.page_size,
                )
                topic_filter = (
                    "" if is_legacy_unbound_profile(nb["relevance_profile"])
                    else openalex_topic_filter(nb["relevance_profile"])
                ) if provider == "openalex" else ""
                pages_left = options.max_pages_total if options.until_exhausted else options.backfill_pages
                pages_done = 0
                while pages_done < pages_left:
                    if candidate_budget_is_exhausted(kid):
                        report.stop_reason = "candidate_budget_exhausted"
                        report.status = "partial_success" if report.items_returned else "skipped"
                        return report
                    try:
                        result: BackfillTransactionResult = run_backfill_page_transaction(
                            keyword_zh=keyword,
                            keyword_id=kid,
                            query_id=query_id_value,
                            query=query,
                            query_language=query_language,
                            provider=provider,
                            notebook_store=notebook,
                            journal_store=journal,
                            locks_dir=options.locks_dir,
                            request_signature=sig,
                            page_size=options.page_size,
                            relevance_profile_hash=nb["relevance_profile"]["profile_hash"],
                            finalize_page=lambda path, _profile=nb["relevance_profile"], _provider=provider: finalize_page_relevance(
                                path, _profile, _provider
                            ),
                            fetch_page=lambda p, q, **kw: fetch_with_budget(
                                p, q, query_id=query_id_value, sort=sort, order=order,
                                topic_filter=topic_filter, **kw,
                            ),
                        )
                    except StateLockTimeout as exc:
                        report.provider_failures += 1
                        report.errors.append(str(exc))
                        break
                    # Recovery statistics must reach the report regardless of
                    # the transaction's final status — a journal recovered this
                    # run is real work even if the same call then stops/exhausts.
                    report.pages_recovered += result.pages_recovered
                    report.journals_recovered += result.journals_recovered
                    if result.status == "stopped" and result.stop_reason == "page_budget_exhausted":
                        report.stop_reason = "page_budget_exhausted"
                        break
                    if result.status == "exhausted":
                        report.states_exhausted += 1
                        report.stop_reason = result.stop_reason or "provider_exhausted"
                        break
                    if result.status != "success":
                        report.provider_failures += 1
                        report.stop_reason = result.stop_reason
                        if result.safe_error:
                            report.errors.append(result.safe_error)
                        break
                    report.pages_requested += result.pages_requested
                    report.pages_persisted += result.pages_persisted
                    report.pages_committed += result.pages_committed
                    report.items_returned += result.candidates_returned
                    with state_lock:
                        journal_writes = result.pages_persisted + result.pages_committed
                        runtime.metrics.journal_pages_written += journal_writes
                        runtime.metrics.page_fsyncs += journal_writes
                    if result.page_path is not None:
                        if result.page_path not in runtime.journal_index.page_cache:
                            committed_page = journal.read(result.page_path)
                            with state_lock:
                                runtime.journal_index.pages_read += 1
                            runtime.journal_index.add_page(result.page_path, committed_page)
                        notify_staging(kid, result.candidates_returned)
                    pages_done += 1
                    if result.provider_exhausted:
                        report.states_exhausted += 1
                        report.stop_reason = result.stop_reason or "provider_exhausted"
                        break
                    if not options.until_exhausted and pages_done >= options.backfill_pages:
                        break
        report.status = _status_from_failures(True, report.provider_failures, report.items_returned)
        return report

    def terminal_keyword_report(
        keyword: str,
        *,
        status: str,
        error: str = "",
        keyword_id: str | None = None,
    ) -> None:
        empty_drain = DrainReport()
        lane_errors = [error] if error else []
        keyword_reports[keyword] = KeywordDiscoveryReport(
            keyword_zh=keyword,
            keyword_id=keyword_id or make_keyword_id(keyword),
            status=status,
            refresh=LaneReport(status=status, errors=lane_errors),
            backfill=LaneReport(status=status, errors=lane_errors),
            pending=empty_drain,
            final_pending=empty_drain,
            candidates={},
            budget={"page_limit": budget.limit, "pages_used": budget.used},
            mode=options.mode,
            errors=lane_errors,
        )

    def prepare_keyword(
        keyword: str,
    ) -> tuple[str, dict[str, Any], DrainReport, bool] | None:
        # Notebook must already have bilingual queries configured.
        # Discovery never auto-seeds queries; it only reads from search_queries.
        try:
            nb = notebook.require_v3(keyword)
        except (
            FileNotFoundError,
            NotebookCorruptError,
            LegacyNotebookSchemaError,
            UnsupportedNotebookSchemaError,
        ) as exc:
            terminal_keyword_report(keyword, status="failed", error=str(exc))
            return None
        if nb["enabled"] is False:
            terminal_keyword_report(
                keyword,
                status="skipped",
                keyword_id=nb["keyword_id"],
            )
            return None
        readiness = validate_discovery_readiness(nb)
        if not readiness:
            error = (
                f"notebook {keyword!r} is not discovery-ready:\n  "
                + "\n  ".join(readiness.errors)
            )
            terminal_keyword_report(
                keyword,
                status="failed",
                error=error,
                keyword_id=nb["keyword_id"],
            )
            return None
        kid = nb["keyword_id"]
        retry_due_deferred(kid, nb["relevance_profile"])
        initial_drain = drain_pending_candidates(
            journal=journal,
            keyword_ids=[kid],
            candidate_budget=min(16, options.max_candidates),
            stage_to_paper_raw=options.stage_to_paper_raw,
            apply=options.apply,
            paper_raw_dir=options.paper_raw_dir,
            papers_dir=options.papers_dir,
            ledger_path=options.ledger_path,
            locks_dir=options.locks_dir,
            exports_dir=options.exports_dir,
            worker_id=worker_id,
            doi_resolution_budget=options.doi_resolution_budget,
            skip_duplicates=options.skip_duplicates,
            hide_existing=options.hide_existing,
            runtime=runtime,
        )
        pending_after_drain = runtime.journal_index.pending_count([kid])
        bp_state = notebook.update_backpressure(
            keyword,
            pending_count=pending_after_drain,
            max_threshold=options.max_pending_candidates,
            resume_threshold=options.resume_pending_candidates,
        )
        backpressure = bool(bp_state.get("active"))
        if backpressure:
            initial_drain.backpressure = True
        return keyword, nb, initial_drain, backpressure

    prepared: list[tuple[str, dict[str, Any], DrainReport, bool]] = []
    for keyword in keywords:
        prepared_item = prepare_keyword(keyword)
        if prepared_item is not None:
            prepared.append(prepared_item)

    concurrent_drains: dict[str, DrainReport] = {}
    initial_by_id = {nb["keyword_id"]: initial for _, nb, initial, _ in prepared}

    def staging_consumer() -> None:
        while True:
            notification = staging_notifications.get()
            try:
                if notification is None:
                    return
                kid, candidate_count = notification
                prior = concurrent_drains.setdefault(kid, DrainReport())
                try:
                    remaining = max(0, options.max_candidates
                                    - initial_by_id.get(kid, DrainReport()).processed
                                    - prior.processed)
                    if remaining and candidate_count:
                        current = drain_pending_candidates(
                            journal=journal, keyword_ids=[kid],
                            candidate_budget=min(candidate_count, remaining),
                            stage_to_paper_raw=options.stage_to_paper_raw, apply=options.apply,
                            paper_raw_dir=options.paper_raw_dir, papers_dir=options.papers_dir,
                            ledger_path=options.ledger_path, locks_dir=options.locks_dir,
                            exports_dir=options.exports_dir, worker_id=worker_id,
                            doi_resolution_budget=options.doi_resolution_budget,
                            skip_duplicates=options.skip_duplicates,
                            hide_existing=options.hide_existing, runtime=runtime)
                        for field_name in ("processed", "staged", "reused_existing", "emitted", "existing_duplicate",
                                           "duplicate_observation", "invalid", "unresolved",
                                           "retryable_failures", "terminal_failures", "planned"):
                            setattr(prior, field_name, getattr(prior, field_name) + getattr(current, field_name))
                        prior.before = max(prior.before, current.before)
                        prior.remaining = current.remaining
                        prior.errors.extend(current.errors)
                    if (not options.until_exhausted and options.max_candidates > 0
                            and initial_by_id.get(kid, DrainReport()).processed
                            + prior.processed >= options.max_candidates):
                        with state_lock:
                            staging_budget_exhausted.add(kid)
                except Exception as exc:
                    prior.errors.append(
                        f"staging_consumer_failed:{type(exc).__name__}:{exc}")
            finally:
                if notification is not None:
                    for _ in range(notification[1]):
                        staging_candidate_slots.release()
                staging_notifications.task_done()
                with progress_lock:
                    last_staging_progress[0] = time.monotonic()

    consumer = threading.Thread(target=staging_consumer, name="discovery-staging-consumer", daemon=False)
    consumer.start()

    refresh_run_id = uuid.uuid4().hex
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for keyword, nb, _initial, backpressure in prepared:
            futures[pool.submit(run_refresh, keyword, nb, refresh_run_id, backpressure)] = (keyword, "refresh")
            futures[pool.submit(run_backfill, keyword, nb, backpressure)] = (keyword, "backfill")
        lane_results: dict[tuple[str, str], LaneReport] = {}
        for future in as_completed(futures):
            key = futures[future]
            try:
                lane_results[key] = future.result()
            except Exception as exc:
                lane_results[key] = LaneReport(status="failed", provider_failures=1, errors=[str(exc)])

    no_progress_timeout = options.staging_no_progress_timeout_seconds

    def assert_consumer_progress() -> None:
        with progress_lock:
            idle = time.monotonic() - last_staging_progress[0]
        if idle >= no_progress_timeout:
            raise RuntimeError(
                f"staging consumer made no progress for {idle:.1f}s "
                f"({staging_notifications.unfinished_tasks} unfinished notifications)")

    while True:
        try:
            staging_notifications.put(None, timeout=min(1.0, no_progress_timeout))
            break
        except queue.Full:
            assert_consumer_progress()
    # Queue.join semantics with a progress watchdog: total drain time is
    # unbounded while work keeps completing; only an idle consumer times out.
    with staging_notifications.all_tasks_done:
        while staging_notifications.unfinished_tasks:
            assert_consumer_progress()
            staging_notifications.all_tasks_done.wait(
                timeout=min(1.0, no_progress_timeout))
    consumer.join()
    if consumer.is_alive():
        raise RuntimeError("staging consumer stopped queue progress but did not exit")

    def merge_drain(target: DrainReport, source: DrainReport) -> None:
        for field_name in ("processed", "staged", "reused_existing", "emitted", "existing_duplicate",
                           "duplicate_observation", "invalid", "unresolved",
                           "retryable_failures", "terminal_failures", "planned"):
            setattr(target, field_name,
                    getattr(target, field_name) + getattr(source, field_name))
        target.before = max(target.before, source.before)
        target.remaining = source.remaining
        target.errors.extend(source.errors)

    for keyword, nb, initial_drain, backpressure in prepared:
        kid = nb["keyword_id"]
        concurrent_drain = concurrent_drains.get(kid, DrainReport())
        final_drain = DrainReport(before=runtime.journal_index.pending_count([kid]))
        if options.until_exhausted:
            while True:
                claimable = runtime.journal_index.pending_count([kid])
                if claimable <= 0:
                    break
                current = drain_pending_candidates(
                    journal=journal, keyword_ids=[kid], candidate_budget=min(16, claimable),
                    stage_to_paper_raw=options.stage_to_paper_raw, apply=options.apply,
                    paper_raw_dir=options.paper_raw_dir, papers_dir=options.papers_dir,
                    ledger_path=options.ledger_path, locks_dir=options.locks_dir,
                    exports_dir=options.exports_dir, worker_id=worker_id,
                    doi_resolution_budget=options.doi_resolution_budget,
                    skip_duplicates=options.skip_duplicates, hide_existing=options.hide_existing,
                    runtime=runtime)
                merge_drain(final_drain, current)
                if current.processed <= 0:
                    break
            remaining_claimable = runtime.journal_index.pending_count([kid])
            final_drain.remaining = remaining_claimable
            if remaining_claimable:
                final_drain.errors.append(
                    f"until_exhausted_journal_not_empty:{remaining_claimable}")
        else:
            final_drain = drain_pending_candidates(
                journal=journal,
                keyword_ids=[kid],
                candidate_budget=max(0, options.max_candidates - initial_drain.processed
                                     - concurrent_drain.processed),
                stage_to_paper_raw=options.stage_to_paper_raw,
                apply=options.apply,
                paper_raw_dir=options.paper_raw_dir,
                papers_dir=options.papers_dir,
                ledger_path=options.ledger_path,
                locks_dir=options.locks_dir,
                exports_dir=options.exports_dir,
                worker_id=worker_id,
                doi_resolution_budget=options.doi_resolution_budget,
                skip_duplicates=options.skip_duplicates,
                hide_existing=options.hide_existing,
                runtime=runtime,
            )
        merge_drain(initial_drain, concurrent_drain)
        refresh = lane_results.get((keyword, "refresh"), LaneReport(status="skipped"))
        backfill = lane_results.get((keyword, "backfill"), LaneReport(status="skipped"))
        if options.until_exhausted:
            expected_lanes = len(_active_queries(nb)) * len(PROVIDERS)
            if backfill.states_exhausted < expected_lanes:
                backfill.errors.append(
                    f"until_exhausted_provider_lanes_incomplete:"
                    f"{backfill.states_exhausted}/{expected_lanes}")
                backfill.status = "partial_success" if backfill.items_returned else "failed"
        errors = list(refresh.errors) + list(backfill.errors) + list(initial_drain.errors) + list(final_drain.errors)
        statuses = {refresh.status, backfill.status, initial_drain.status, final_drain.status}
        if "failed" in statuses:
            made_progress = (
                refresh.status in {"success", "partial_success"}
                or backfill.status in {"success", "partial_success"}
                or initial_drain.processed > 0
                or final_drain.processed > 0
            )
            status = "partial_success" if made_progress else "failed"
        elif "partial_success" in statuses:
            status = "partial_success"
        elif backpressure and not errors:
            status = "success"
        else:
            status = "success"
        dynamic_backpressure = kid in dynamically_backpressured
        report = KeywordDiscoveryReport(
            keyword_zh=keyword,
            keyword_id=kid,
            status=status,
            refresh=refresh,
            backfill=backfill,
            pending=initial_drain,
            final_pending=final_drain,
            candidates={
                "staged": initial_drain.staged + final_drain.staged,
                "reused_existing": initial_drain.reused_existing + final_drain.reused_existing,
                "emitted": initial_drain.emitted + final_drain.emitted,
                "existing_duplicates": initial_drain.existing_duplicate + final_drain.existing_duplicate,
                "duplicate_observations": initial_drain.duplicate_observation + final_drain.duplicate_observation,
                "invalid": initial_drain.invalid + final_drain.invalid,
                "unresolved": initial_drain.unresolved + final_drain.unresolved,
                "retryable_failures": initial_drain.retryable_failures + final_drain.retryable_failures,
            },
            budget={"page_limit": budget.limit, "pages_used": budget.used, "page_budget_exhausted": budget.exhausted},
            mode=options.mode,
            queries_total=len(_active_queries(nb)),
            queries_zh=sum(1 for item in _active_queries(nb) if item["language"] == "zh"),
            queries_en=sum(1 for item in _active_queries(nb) if item["language"] == "en"),
            queries_executed=[
                {"query": item["query"], "query_language": item["language"]}
                for item in _active_queries(nb)
                if (item["query"], item["language"])
                in executed_queries.get(keyword, set())
            ],
            backpressure=backpressure or dynamic_backpressure,
            errors=errors,
        )
        notebook.update_pending_counts(
            keyword,
            pages=sum(1 for page in runtime.journal_index.page_cache.values()
                      if page.get("keyword_id") == kid),
            candidates=runtime.journal_index.pending_count([kid]),
        )
        keyword_reports[keyword] = report

    ordered = [keyword_reports[k] for k in keywords if k in keyword_reports]
    status, exit_code = _batch_status(ordered)
    aggregate = _aggregate(ordered, budget)
    runtime.metrics.sync_journal(runtime.journal_index)
    return BatchDiscoveryReport(
        status=status, keywords=ordered, aggregate=aggregate, exit_code=exit_code,
        pipeline_metrics=runtime.metrics.to_dict(),
    )


def run_discovery_batch(
    keywords: list[str],
    *,
    options: DiscoveryOptions | None = None,
    max_workers: int = 4,
    fetch_page: Callable[..., Any] | None = None,
    rate_limiters: dict[str, ProviderRateLimiter] | None = None,
) -> BatchDiscoveryReport:
    """Run one batch while excluding the plan-bound profile apply transaction."""
    effective = options or DiscoveryOptions()
    lock_path = effective.relevance_profiles_lock_path
    transaction_root = effective.relevance_profiles_transaction_root
    if lock_path is None:
        if effective.notebook_dir == DISCOVERY_KEYWORD_NOTEBOOK_DIR:
            lock_path = DISCOVERY_DIR.parent / "transactions" / "locks" / "relevance_profiles.lock"
            transaction_root = transaction_root or (
                DISCOVERY_DIR.parent / "transactions" / "relevance_profiles"
            )
        else:
            # Isolated tests/comparison runs must not touch production runtime
            # state merely to acquire the shared mutex.
            lock_path = Path(effective.notebook_dir).parent / ".relevance_profiles.lock"
            transaction_root = transaction_root or (
                Path(effective.notebook_dir).parent / ".relevance_profile_transactions"
            )
    elif transaction_root is None:
        transaction_root = Path(lock_path).parent.parent / "relevance_profiles"
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_path), timeout=0)
    try:
        lock.acquire()
    except FileLockTimeout as exc:
        raise RuntimeError("relevance profile transaction is applying; discovery is fail-closed") from exc
    try:
        applying = list_applying_relevance_profile_transactions(Path(transaction_root))
        if applying:
            raise RuntimeError(
                "relevance profile transaction is durably applying; discovery is fail-closed: "
                + ",".join(map(str, applying))
            )
        return _run_discovery_batch_unlocked(
            keywords,
            options=effective,
            max_workers=max_workers,
            fetch_page=fetch_page,
            rate_limiters=rate_limiters,
        )
    finally:
        lock.release()

"""In-process DOI discovery coordinator (v100).

v101: dead code removed, single-source report model from report_builder.

This is the single active engine used by both single-keyword and multi-keyword
CLI entrypoints. It coordinates journal-first provider paging, pending drains,
global lane concurrency, provider limiters, and report aggregation.

Architecture (v100)::

    run_discovery_batch
    → _run_discovery_batch_unlocked
    → with DiscoveryBatchRuntime factory as runtime:
    → with CandidateDrainCoordinator(...) as drain:
    → schedule_lanes(active_specs, ...)
    → ReportBuilder.build(...)

v100: Phase 0-8 complete.  LaneScheduler extracted, drain context-manager,
telemetry typed, durable_progress field, architecture verifier updated.

Deliverable: 12 files changed, 406 tests passing, verifier [OK].
Snapshot: mineru_snapshot.zip (549 files, 1.2 MB, runtime_files_included=0).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

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
from src.discovery.workspace import (
    DiscoveryWorkspace,
    WorkspaceResolver,
)
from src.discovery.runtime.batch_runtime import (
    ActiveRelevanceProfiles,
    DiscoveryBatchRuntime,
    DiscoveryPipelineMetrics,
    ShutdownReason,
)
from src.discovery.runtime.candidate_drain import CandidateDrainCoordinator
from src.discovery.execution.lane_executor import execute_refresh_lane, execute_backfill_lane
from src.discovery.reporting.report_builder import ReportBuilder  # module-level for test monkeypatching
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
from src.discovery.relevance_runtime import RelevanceRuntimePaths
from src.discovery.relevance import (
    OpenAlexDoiVerifier,
    RawOpenAlexWorkCache,
    evaluate_page_candidates,
    is_legacy_unbound_profile,
    openalex_topic_filter,
)
from src.discovery.pending_queue import DrainReport, drain_pending_candidates
from src.discovery.providers.provider_models import DiscoveryPage, failed_page
DiscoveryMode = Literal["refresh", "backfill", "hybrid"]
STAGING_QUEUE_CAPACITY = 500


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
    max_provider_requests_total: int | None = None
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
    title_resolution_cache_dir: Path = DISCOVERY_DIR / "title_resolution_cache"
    relevance_runtime_paths: RelevanceRuntimePaths | None = None
    # Tests and isolated callers may inject a DOI verifier.  Production uses
    # the raw-Work cache backed OpenAlex verifier created by the coordinator.
    crossref_scope_verifier: Any | None = None
    # v4 workspace (primary path source).  When set, all directory attributes
    # derive from the workspace.  Production CLI auto-resolves the active
    # workspace via WorkspaceResolver.  Flat-path attributes remain as
    # properties for backward-compatible test injection.
    workspace: DiscoveryWorkspace | None = None

    def __post_init__(self) -> None:
        # Resolve flat paths from workspace when available
        if self.workspace is not None:
            if self.notebook_dir == DISCOVERY_KEYWORD_NOTEBOOK_DIR:
                object.__setattr__(self, "notebook_dir", self.workspace.keyword_notebook_dir)
            if self.pending_pages_dir == DISCOVERY_PENDING_PAGES_DIR:
                object.__setattr__(self, "pending_pages_dir", self.workspace.page_journals_dir)
            if self.locks_dir == DISCOVERY_LOCKS_DIR:
                object.__setattr__(self, "locks_dir", self.workspace.locks_dir)
            if self.exports_dir == DISCOVERY_EXPORTS_DIR:
                object.__setattr__(self, "exports_dir", self.workspace.exports_dir)
            object.__setattr__(self, "output_dir", self.workspace.exports_dir)


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
def _validate_provider_request_shape(
    provider: str, sort: str | None, order: str | None,
) -> tuple[str | None, str | None]:
    """Validate and normalize sort/order for a provider."""
    if provider == "openalex":
        return _validate_openalex_sort(sort), None
    return _validate_crossref_sort_order(sort, order)



def _validate_discovery_options(
    options: DiscoveryOptions,
    keywords: list[str],
    *,
    max_workers: int,
) -> None:
    if max_workers < 1:
        raise ValueError(f"max_workers must be >= 1; got {max_workers!r}")
    if not keywords or not any(str(kw or "").strip() for kw in keywords):
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
    if options.max_provider_requests_total is not None and options.max_provider_requests_total < 1:
        raise ValueError(f"max_provider_requests_total must be a positive integer or None; got {options.max_provider_requests_total!r}")
    if options.until_exhausted and options.max_pages_total is None and options.max_provider_requests_total is None:
        raise ValueError("until_exhausted requires max_pages_total or max_provider_requests_total as safety valve")
    if options.until_exhausted and options.mode not in {"backfill", "hybrid"}:
        raise ValueError(f"until_exhausted requires mode backfill or hybrid; got {options.mode!r}")
    if options.apply and not options.stage_to_paper_raw:
        raise ValueError("apply=True requires stage_to_paper_raw=True")
    if options.skip_duplicates and not options.stage_to_paper_raw:
        raise ValueError("skip_duplicates=True requires stage_to_paper_raw=True")


def _run_discovery_batch_unlocked(
    keywords: list[str],
    *,
    options: "DiscoveryOptions | None" = None,
    max_workers: int = 4,
    page_fetcher: "Any" = None,
) -> "BatchDiscoveryReport":
    from src.discovery.runtime.budgets import (
        BatchDoiResolutionBudget, DualScopePageBudget, ProviderRequestBudget,
    )
    from src.discovery.execution.lane_models import (
        DiscoveryLaneKey, LaneCounters, LaneExecutionSpec, LaneOutcome,
        LaneState, RequestSignature, StopReason,
    )
    from src.discovery.page_journal import JournalCorruptError, PAGE_SCHEMA_VERSION
    from src.discovery.providers.provider_page_fetcher import ProviderPageFetcher
    from src.discovery.reporting.report_builder import KeywordReportInput
    from src.discovery.execution.lane_services import RefreshStateService
    from src.discovery.title_resolution import TitleResolutionService, DurableTitleCache

    options = options or DiscoveryOptions()
    _validate_discovery_options(options, keywords, max_workers=max_workers)
    page_fetcher = page_fetcher or ProviderPageFetcher()
    notebook = KeywordNotebookStore(options.notebook_dir)
    journal = PageJournalStore(options.pending_pages_dir)
    builder = ReportBuilder()
    page_budget = DualScopePageBudget(
        per_lane_limit=None if options.until_exhausted else options.backfill_pages,
        total_limit=options.max_pages_total,
    )

    active_profiles: dict[str, str] = {}
    global_error = ""
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
            active_profiles[str(current["keyword_id"])] = str(
                current["relevance_profile"]["profile_hash"]
            )
    except (RuntimeError, NotebookCorruptError, LegacyNotebookSchemaError,
            UnsupportedNotebookSchemaError) as exc:
        global_error = str(exc)
    if global_error:
        return builder.build(
            keyword_inputs=[KeywordReportInput(
                keyword_zh=keyword,
                keyword_id=make_keyword_id(keyword),
                mode=options.mode,
                errors=(global_error,),
                terminal_status="failed",
            ) for keyword in keywords],
            lane_outcomes=(),
            page_budget_snapshot=page_budget.snapshot(),
            telemetry_snapshot={"attempted": 0, "retried": 0, "succeeded": 0, "failed": 0,
                                "by_provider_purpose": {}},
            pipeline_metrics=DiscoveryPipelineMetrics().to_dict(),
        )

    request_budget = (
        ProviderRequestBudget(limit=options.max_provider_requests_total)
        if options.max_provider_requests_total is not None else None
    )
    doi_budget = BatchDoiResolutionBudget(limit=int(options.doi_resolution_budget))
    try:
        runtime = DiscoveryBatchRuntime.create(
            journal=journal,
            paper_raw_dir=options.paper_raw_dir,
            papers_dir=options.papers_dir,
            ledger_path=options.ledger_path,
            needs_staging=bool(options.stage_to_paper_raw or options.hide_existing),
            active_relevance_profiles=ActiveRelevanceProfiles.build(active_profiles),
            persist_repair_cursor=bool(options.apply and options.stage_to_paper_raw),
            request_budget=request_budget,
            doi_resolution_budget=doi_budget,
            page_budget=page_budget,
        )
    except JournalCorruptError as exc:
        # v4: per-keyword isolation — map v2/v3 journals to their keyword_ids
        # and only those keywords receive repair_required.  Unaffected keywords
        # continue normally.  When keyword attribution is impossible, every
        # keyword receives the error (existing behaviour).
        error_msg = f"provider_page_journal_repair_required:{exc}"
        affected_keywords: dict[str, set[str]] = {}
        try:
            # Best-effort attribution: read the offending journal path from
            # the error context if available
            for path in options.pending_pages_dir.rglob("*.json"):
                try:
                    raw = path.read_text(encoding="utf-8")
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        schema_ver = data.get("schema_version", "")
                        if schema_ver not in ("", "3.0") and schema_ver != PAGE_SCHEMA_VERSION:
                            kw = str(data.get("keyword_zh", "") or "")
                            if kw:
                                affected_keywords.setdefault(kw, set()).add("legacy_journal")
                except Exception:
                    pass
        except Exception:
            pass

        keyword_inputs: list[KeywordReportInput] = []
        for keyword in keywords:
            is_affected = bool(keyword in affected_keywords)
            keyword_inputs.append(KeywordReportInput(
                keyword_zh=keyword,
                keyword_id=make_keyword_id(keyword),
                mode=options.mode,
                errors=(error_msg,) if is_affected else (),
                terminal_status="repair_required" if is_affected else None,
            ))
        # If no keywords were attributed (or attribution failed), fail all
        if not affected_keywords:
            keyword_inputs = [KeywordReportInput(
                keyword_zh=keyword,
                keyword_id=make_keyword_id(keyword),
                mode=options.mode,
                errors=(error_msg,),
                terminal_status="repair_required",
            ) for keyword in keywords]

        return builder.build(
            keyword_inputs=keyword_inputs,
            lane_outcomes=(),
            page_budget_snapshot=page_budget.snapshot(),
            telemetry_snapshot={"attempted": 0, "retried": 0, "succeeded": 0, "failed": 0,
                                "by_provider_purpose": {}},
            pipeline_metrics=DiscoveryPipelineMetrics().to_dict(),
        )
    with runtime:
        runtime.title_resolution_service = TitleResolutionService(
            client=runtime.provider_client("crossref"),
            budget=doi_budget,
            cache=DurableTitleCache(options.title_resolution_cache_dir),
            runtime_guard=runtime.guard,
        )
        cache_dir = options.relevance_cache_dir
        if cache_dir == DiscoveryOptions().relevance_cache_dir and options.notebook_dir != DISCOVERY_KEYWORD_NOTEBOOK_DIR:
            cache_dir = Path(options.notebook_dir).parent / ".relevance_raw_work_cache"
        default_scope_verifier = OpenAlexDoiVerifier(
            cache=RawOpenAlexWorkCache(cache_dir),
            client=runtime.provider_client("openalex"),
        )
        scope_verifier = options.crossref_scope_verifier or default_scope_verifier
        refresh_state = RefreshStateService(notebook)

        with CandidateDrainCoordinator(
            runtime=runtime,
            journal=journal,
            options=options,
            worker_id=f"worker-{uuid.uuid4().hex[:12]}",
            paper_raw_dir=options.paper_raw_dir,
            papers_dir=options.papers_dir,
            ledger_path=options.ledger_path,
            locks_dir=options.locks_dir,
            exports_dir=options.exports_dir,
            skip_duplicates=options.skip_duplicates,
            hide_existing=options.hide_existing,
            max_candidates=options.max_candidates,
            max_pending_candidates=options.max_pending_candidates,
            resume_pending_candidates=options.resume_pending_candidates,
            stage_to_paper_raw=options.stage_to_paper_raw,
            apply=options.apply,
            doi_resolution_budget=options.doi_resolution_budget,
            until_exhausted=options.until_exhausted,
        ) as drain:
            # deferred relevance retry helper
            def retry_due_deferred(keyword_id: str, profile: dict) -> None:
                now = datetime.now(timezone.utc)
                for ref in journal.list_pages([keyword_id]):
                    if ref.state not in {"cursor_committed", "draining"}:
                        continue
                    page = journal.read(ref.path)
                    due = [
                        c for c in page.get("candidates", [])
                        if isinstance(c.get("relevance"), dict)
                        and c["relevance"].get("state") == "verification_deferred"
                        and c["relevance"].get("profile_hash") == profile.get("profile_hash")
                        and (
                            not c["relevance"].get("next_retry_at")
                            or (parse_iso(c["relevance"].get("next_retry_at")) or now) <= now
                        )
                    ]
                    if not due:
                        continue
                    decisions = evaluate_page_candidates(
                        due, profile,
                        provider=str(page["provider"]),
                        scope_verifier=scope_verifier,
                    )
                    updated = journal.retry_deferred_relevance(ref.path, decisions)
                    updated_candidates = [
                        c for c in updated["candidates"]
                        if str(c.get("candidate_id") or "") in decisions
                    ]
                    runtime.journal_index.apply_relevance_updates(ref.path, updated_candidates)
                    runtime.metrics.relevance_incremental_updates += len(updated_candidates)
                runtime.journal_index.assert_active_bindings(
                    runtime.active_relevance_profiles.by_keyword_id)
                runtime.metrics.sync_journal(runtime.journal_index)

            def candidate_budget_is_exhausted(keyword_id: str) -> bool:
                return drain.budget_exhausted(keyword_id)

            def finalize_page_relevance(page_path: Path, profile: dict, provider: str) -> dict:
                page = journal.read(page_path)
                return evaluate_page_candidates(
                    page.get("candidates") or [], profile,
                    provider=provider, scope_verifier=scope_verifier,
                )

            def on_refresh_page_persisted(page_path: Path, page_data: dict, *, profile: dict, provider: str) -> None:
                decisions = finalize_page_relevance(page_path, profile, provider)
                journal.finalize_relevance(page_path, decisions)
                committed = journal.mark_cursor_committed(page_path)
                runtime.journal_index.add_page(page_path, committed)
                runtime.metrics.journal_pages_written += 1
                runtime.metrics.page_fsyncs += 1

            # per-keyword records
            records: list[dict] = []
            for keyword in keywords:
                record: dict = {
                    "keyword": keyword, "keyword_id": make_keyword_id(keyword),
                    "nb": None, "initial": DrainReport(), "final": DrainReport(),
                    "backpressure": False, "errors": [], "terminal_status": None,
                }
                try:
                    nb = notebook.require_v3(keyword)
                    record["keyword_id"] = nb["keyword_id"]
                    if not nb["enabled"]:
                        record["terminal_status"] = "skipped"
                    else:
                        readiness = validate_discovery_readiness(nb)
                        if not readiness:
                            record["terminal_status"] = "failed"
                            record["errors"].append("; ".join(readiness.errors))
                        else:
                            retry_due_deferred(nb["keyword_id"], nb["relevance_profile"])
                            initial = drain.drain(nb["keyword_id"], min(16, options.max_candidates), phase="initial")
                            record["initial"] = initial
                            pending_count = runtime.journal_index.pending_count([nb["keyword_id"]])
                            state = notebook.update_backpressure(
                                keyword, pending_count=pending_count,
                                max_threshold=options.max_pending_candidates,
                                resume_threshold=options.resume_pending_candidates,
                            )
                            record["backpressure"] = bool(state.get("active"))
                            if record["backpressure"]:
                                initial.backpressure = True
                            record["nb"] = nb
                except (FileNotFoundError, NotebookCorruptError, LegacyNotebookSchemaError,
                        UnsupportedNotebookSchemaError, RuntimeError) as exc:
                    record["terminal_status"] = "failed"
                    record["errors"].append(str(exc))
                records.append(record)

            # build lane execution specs
            refresh_run_id = uuid.uuid4().hex
            profiles_by_keyword = {
                r["keyword_id"]: r["nb"]["relevance_profile"]
                for r in records if isinstance(r["nb"], dict)
            }
            specs: list[LaneExecutionSpec] = []
            for record in records:
                nb = record["nb"]
                if not isinstance(nb, dict):
                    continue
                for active_query in _active_queries(nb):
                    for provider in PROVIDERS:
                        for mode in ("refresh", "backfill"):
                            if options.mode != "hybrid" and options.mode != mode:
                                continue
                            sort, order = _validate_provider_request_shape(
                                provider,
                                _profile_sort(nb, provider, mode, options),
                                _profile_order(nb, mode),
                            )
                            signature = RequestSignature.create(
                                sort=sort,
                                filters=_profile_filters(nb, provider, mode, sort, order),
                                page_size=options.page_size,
                                pagination_schema_version="2.0",
                            )
                            if mode == "backfill":
                                bound = notebook.ensure_backfill_generation(
                                    record["keyword"], active_query["query_id"],
                                    provider, request_signature_hash=signature.hash,
                                )
                                generation = int(bound["generation"])
                            else:
                                generation = max(1, int(nb.get("relevance_generation") or 1))
                            key = DiscoveryLaneKey(
                                keyword_id=nb["keyword_id"],
                                query_id=active_query["query_id"],
                                provider=provider, mode=mode,
                                generation=generation,
                                request_signature=signature.hash,
                            )
                            # Preserve original query language — never coerce mixed to zh.
                            # The QueryLanguage enum accepts zh, en, and mixed.
                            query_lang = active_query.get("language", "zh")
                            specs.append(LaneExecutionSpec(
                                key=key, request_signature=signature,
                                keyword_zh=record["keyword"],
                                query=active_query["query"],
                                query_language=query_lang,
                                relevance_profile_hash=nb["relevance_profile"]["profile_hash"],
                                order=order,
                                topic_filter=(
                                    openalex_topic_filter(nb["relevance_profile"])
                                    if provider == "openalex" else ""
                                ),
                                refresh_run_id=refresh_run_id if mode == "refresh" else None,
                            ))

            # planned lane inventory
            planned_lane_ids: list[str] = [spec.key.stable_id() for spec in specs]
            backpressured_keyword_ids: set[str] = {
                r["keyword_id"] for r in records
                if r["backpressure"] and isinstance(r["nb"], dict)
            }
            active_specs = [s for s in specs if s.key.keyword_id not in backpressured_keyword_ids]
            skipped_outcomes: list[LaneOutcome] = [
                LaneOutcome(key=s.key, state=LaneState.SKIPPED,
                            stop_reason=StopReason.CANDIDATE_BACKPRESSURE,
                            counters=LaneCounters(), exhaustion_evidence=None)
                for s in specs if s.key.keyword_id in backpressured_keyword_ids
            ]

            def execute_spec(spec: LaneExecutionSpec) -> LaneOutcome:
                if spec.key.mode == "refresh":
                    return execute_refresh_lane(
                        spec, runtime=runtime, notebook=notebook,
                        journal=journal, options=options,
                        page_fetcher=page_fetcher, refresh_state=refresh_state,
                        candidate_budget_exhausted=candidate_budget_is_exhausted,
                        notify_staging=drain.notify,
                        on_page_persisted=lambda p, pg, _s=spec: on_refresh_page_persisted(
                            p, pg, profile=profiles_by_keyword[_s.key.keyword_id],
                            provider=_s.key.provider,
                        ),
                    )
                return execute_backfill_lane(
                    spec, runtime=runtime, notebook=notebook,
                    journal=journal, options=options,
                    page_fetcher=page_fetcher,
                    candidate_budget_exhausted=candidate_budget_is_exhausted,
                    notify_staging=drain.notify,
                    finalize_page=lambda p, _s=spec: finalize_page_relevance(
                        p, profiles_by_keyword[_s.key.keyword_id], _s.key.provider,
                    ),
                )

            # ── incremental bounded scheduler (Phase 3: extracted to lane_scheduler) ──
            from src.discovery.execution.lane_scheduler import schedule_lanes

            def _backpressure_provider() -> frozenset[str]:
                return drain.dynamically_backpressured

            active_outcomes, sched_snapshot = schedule_lanes(
                active_specs,
                max_workers=max_workers,
                execute_lane=execute_spec,
                backpressure_provider=_backpressure_provider,
                cancellation_token=runtime.cancellation_token,
            )
            outcomes: list[LaneOutcome] = list(skipped_outcomes) + active_outcomes
            # Surface scheduler-level interruption to the runtime
            interrupted = sched_snapshot.error in ("keyboard_interrupt", "cancelled")
            if interrupted:
                runtime.cancel(ShutdownReason.INTERRUPTED)

            # ── final drain ──
            # Skip final drain after interrupt — runtime is no longer OPEN.
            # Phase 11: no final drain after KeyboardInterrupt.
            if not interrupted:
                for record in records:
                    nb = record["nb"]
                    if not isinstance(nb, dict):
                        continue
                    keyword_id = nb["keyword_id"]
                    concurrent = list(drain.drain_reports.get(keyword_id, []))
                    if options.until_exhausted:
                        fragments: list[DrainReport] = []
                        while True:
                            remaining = runtime.journal_index.pending_count([keyword_id])
                            if remaining <= 0:
                                break
                            current = drain.drain(keyword_id, min(16, remaining), phase="final_until_exhausted")
                            fragments.append(current)
                            if current.processed <= 0:
                                break
                        record["final_fragments"] = fragments
                    else:
                        remaining = max(0,
                            options.max_candidates - record["initial"].processed
                            - sum(r.processed for r in concurrent))
                        record["final_fragments"] = [drain.drain(keyword_id, remaining, phase="final")]
                    try:
                        notebook.update_pending_counts(
                            record["keyword"],
                            pages=runtime.journal_index.page_count_for_keyword(keyword_id),
                            candidates=runtime.journal_index.pending_count([keyword_id]),
                        )
                    except Exception as exc:
                        record["errors"].append(f"pending_count_update_failed:{type(exc).__name__}:{exc}")
            else:
                for record in records:
                    record.setdefault("final_fragments", [])

            # build report
            inputs: list[KeywordReportInput] = []
            for record in records:
                nb = record["nb"]
                queries: tuple = ()
                if isinstance(nb, dict):
                    queries = tuple({"query": i["query"], "query_language": i["language"]} for i in _active_queries(nb))
                keyword_id = record["keyword_id"]
                inputs.append(KeywordReportInput(
                    keyword_zh=record["keyword"], keyword_id=keyword_id, mode=options.mode,
                    queries=queries,
                    pending_reports=(record["initial"], *drain.drain_reports.get(keyword_id, [])),
                    final_pending_reports=tuple(record.get("final_fragments", (record["final"],))),
                    backpressure=bool(record["backpressure"]) or keyword_id in drain.dynamically_backpressured,
                    initial_backpressure=bool(record["backpressure"]),
                    dynamic_backpressure=keyword_id in drain.dynamically_backpressured,
                    errors=tuple(record["errors"]),
                    terminal_status=record["terminal_status"],
                ))
            runtime.metrics.sync_journal(runtime.journal_index)
            return builder.build(
                keyword_inputs=inputs, lane_outcomes=outcomes,
                page_budget_snapshot=page_budget.snapshot(),
                telemetry_snapshot=runtime.snapshot_telemetry(),
                pipeline_metrics=runtime.metrics.to_dict(),
                planned_lane_ids=planned_lane_ids,
            )


def run_discovery_batch(
    keywords: list[str],
    *,
    options: "DiscoveryOptions | None" = None,
    max_workers: int = 4,
    page_fetcher: "Any" = None,
) -> "BatchDiscoveryReport":
    """Run an isolated batch while excluding profile-apply transactions."""
    from src.discovery.relevance_runtime import RelevanceRuntimePaths
    from src.discovery.relevance_profiles import list_applying_relevance_profile_transactions
    from filelock import FileLock, Timeout as FileLockTimeout

    effective = options or DiscoveryOptions()
    # v4: auto-resolve active workspace when no explicit workspace set
    if effective.workspace is None:
        resolver = WorkspaceResolver()
        effective.workspace = resolver.resolve_active()
        # Re-run post_init to derive flat paths from workspace
        effective.__post_init__()
    runtime_paths = effective.relevance_runtime_paths or RelevanceRuntimePaths.resolve_default(
        notebook_root=effective.notebook_dir, journal_root=effective.pending_pages_dir,
    )
    Path(runtime_paths.lock_path).parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(runtime_paths.lock_path), timeout=0)
    try:
        lock.acquire()
    except FileLockTimeout as exc:
        raise RuntimeError("relevance profile transaction is applying; discovery is fail-closed") from exc
    try:
        applying = list_applying_relevance_profile_transactions(Path(runtime_paths.transaction_root))
        if applying:
            raise RuntimeError(
                "relevance profile transaction is durably applying; discovery is fail-closed: "
                + ",".join(map(str, applying))
            )
        try:
            return _run_discovery_batch_unlocked(
                keywords, options=effective, max_workers=max_workers, page_fetcher=page_fetcher,
            )
        except KeyboardInterrupt:
            from src.discovery.reporting.report_builder import BatchDiscoveryReport
            return BatchDiscoveryReport(
                status="interrupted",
                exit_code=130,
                keywords=[],
                aggregate={},
                pipeline_metrics={},
            )
    finally:
        lock.release()

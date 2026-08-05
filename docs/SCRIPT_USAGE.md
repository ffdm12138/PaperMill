# Script usage and risk index

| Script | Role | Mutates state |
|---|---|---|
| `_bootstrap.py` | (library) entry-point runtime init imported by operational scripts: validate settings, create runtime dirs, configure logging | creates runtime dirs |
| `audit_discovery_workspace_registry.py` | read-only raw/formal Registry, conflict, generation, and repair-backlog audit | no |
| `verify_discovery_final_architecture.py` | strict static and dynamic verification of the single-path Discovery execution architecture | no |
| `repair_discovery_workspaces.py` | explicit reserved-workspace repair planning/promotion | `--apply` |
| `reseal_discovery_manifest.py` | one-time re-seal of the active generation `workspace.json` + pointer under the strict final-freeze manifest contract (dry run by default) | `--apply` |
| `repair_formal_publications.py` | audit or identity-only repair of legacy active formal sidecars; unsafe closures emit rollback/recommit plans | `--apply` |
| `benchmark_discovery_pipeline.py` | synthetic raw+formal+candidate batch I/O benchmark | no real runtime state |
| `audit_discovery_reset_state.py` | read-only full discovery reset-state audit (paper_raw, ledger, formal, journals, cursors, locks) | no |

`repair_discovery_workspaces.py` fails on a globally unreadable/invalid ledger,
but can repair a selected independently healthy raw workspace when unrelated
workspaces have typed local issues. `registry_complete=false` remains visible in
the report; `registry_usable_for_repair=true` authorizes only the explicit repair
actions, never discovery staging.
| `stage_raw_pdfs_to_paper_raw.py` | allocate numeric raw workspace and attach local PDF | `--apply` |
| `stage_network_metadata_to_paper_raw.py` | stage DOI-backed citation Metadata | `--apply` |
| `fetch_pdf_for_paper_raw.py` | duplicate-guarded PDF attach; batch selection below | `--apply` |
| `convert_paper_raw_gpu.py` | MinerU conversion; Metadata freeze not required | `--apply` |
| `resolve_paper_raw_metadata.py` | deterministic Metadata candidate resolution | `--apply` |
| `rematch_paper_raw_pdf_identity.py` | transactional v2 identity migration: plan → receipts-only → freeze-eligible (journaled, abort/resume) | `--plan`/`--receipts-only`/`--freeze-eligible` |
| `confirm_paper_raw_pdf_identity.py` | manual identity confirmation for ambiguous/unverifiable/related_version (identifier_conflict never overridable) | `--apply` |
| `freeze_paper_raw_metadata.py` | standalone freeze phase for matched receipts; `--all-eligible` batch | `--apply` |
| `prepare_paper_raw_catalog_task.py` | write read-only Catalog task envelope | `--apply` |
| `validate_paper_raw_catalog.py` | validate/freeze complete Catalog v3.2 | `--apply` |
| `formalize_paper_raw.py` | write numeric installation plan only | `--apply` |
| `commit_paper_raw_to_papers.py` | recoverable hidden-staging install | `--apply` |
| `sync_catalog_categories.py` | synchronize DOI-notebook Chinese categories | `--apply` |
| `plan_catalog_classification.py` | plan missing per-paper LLM decisions | `--apply` |
| `run_catalog_classification.py` | execute pending tasks via injectable backend | `--apply` |
| `apply_catalog_classification_result.py` | validate and apply one LLM result | `--apply` |
| `audit_discovery_keyword_index_sources.py` | read-only audit of DOI keyword identity and cursor state | no |
| `claim_catalog_classification_tasks.py` | list next unapplied tasks for a worker | no |
| `reconcile_catalog_folders.py` | rebuild folder links from authoritative state | `--apply` |
| `doctor_catalog_folders.py` | audit writer safety and folder integrity | no |
| `show_catalog_classification_progress.py` | report classification completion status | no |
| `rollback_formal_papers_to_paper_raw.py` | recoverable formal-to-numeric-raw rollback | `--apply` |
| `validate_v2_library.py` | validate formal Catalog v3.2 library/index closure | no |
| `agent_acceptance.py` | compile, tests, hygiene, pack, ZIP verification | creates snapshot |
| `cleanup_test_caches.py` | safe cleanup of stale test workspaces and legacy caches | `--apply` |
| `test_runtime_workspace.py` | isolated test workspace context manager (library) | no |
| `pack_repo.py` | runtime-zero source audit snapshot | creates ZIP |
| `benchmark_discovery_staging.py` | performance benchmark for discovery staging (synthetic temp data only) | no |

The five-notebook production set was migrated from v3 by a one-time reviewed,
plan-bound transaction; the migration finalized on 2026-07-25 and its tooling
has been removed from the working tree (git history preserves it). Operator
mapping and transaction plans remain local/runtime state, never source
snapshot artifacts. The strict audit command is read-only.

## Platform notes

On Windows, any PowerShell commands shown in this repository should be run
with **PowerShell 7** (`pwsh`) rather than Windows PowerShell 5.1.  PowerShell 7
provides consistent cross-platform behavior and avoids legacy 5.1 parsing
issues.  If only `powershell.exe` is available, prefer the equivalent `cmd` or
`bash` commands instead.

## Discovery / Metadata discovery

`discover_papers.py`, `discover_papers_concurrent.py`,
`manage_discovery_keywords.py`, and `init_discovery_workspace.py` manage the
schema-v4 concurrent Refresh/Backfill discovery queue. Each notebook has one
Chinese `keyword_zh` identity and curated Chinese/English `search_queries`.
An enabled notebook must be bilingual-ready; a disabled draft may be
incomplete. The concurrent wrapper executes every active query in both
providers while Catalog classification reads only `keyword_zh`. The strict
audit is read-only. A fresh runtime-zero install has no active discovery
generation; create the first one once with
`python scripts/init_discovery_workspace.py` (idempotent, imports no legacy
data, enables no keywords), then add notebooks with
`manage_discovery_keywords.py`. Bootstrap, keyword notebook mutations,
relevance-profile apply/resume/abort, and workspace repair all run under the
global discovery maintenance lock (`.maintenance.lock`; renamed from its
migration-era name on 2026-07-27, see the historical ADRs); both discovery writers
refuse to start while the lock is held, and the one-time v3→v4 migration
toolchain is removed (see `docs/ADR_DISCOVERY_V4_MIGRATION_FINAL.md`). Only
v4 schema notebooks are accepted in production; v1/v2/v3 notebooks are
unsupported inputs and must be regenerated outside the active pipeline.

Each enabled notebook also carries its own strict relevance profile. Resolve
the complete OpenAlex subfield taxonomy and create a plan before applying it:

```text
python scripts/configure_relevance_profiles.py --plan --profiles <source.json> --notebook-root <isolated-notebooks> --journal-root <isolated-pages> --transaction-root <isolated-transactions> --json-report <plan.json>
python scripts/configure_relevance_profiles.py --apply --plan <plan.json> --expected-plan-hash <sha256>
python scripts/configure_relevance_profiles.py --resume <transaction.json>
python scripts/configure_relevance_profiles.py --inspect-transaction <transaction.json>
python scripts/configure_relevance_profiles.py --abort <transaction.json>
```

An enabled notebook with a missing or legacy/profile-unbound relevance profile
fails before provider I/O. The apply transaction closes every old-profile
nonterminal relevance verdict (`profile_unbound`, `passed`, and
`verification_deferred`) by candidate/request profile hash, never by the
provider lane's generation integer. `processing` and `failed_retryable` require
normal Discovery recovery and block the whole plan; completed terminal history
is immutable. Unknown lifecycle values also block the whole plan. Phase A binds
exact expected-after bytes and performs a complete zero-write preflight before
creating the durable `applying` journal. Apply/resume use only plan-bound
metadata. It records page before/after hashes and commits notebook profiles and
generations as its final point. The drain index and
final claim independently require the candidate profile hash to equal the
notebook's active profile hash.

Actual A/B/C subject-screening comparison uses OpenAlex only because its
responses directly contain Topics/Subfields evidence. Crossref remains part of
production Discovery, but is rejected before file generation for this frozen
comparison corpus. Synthetic fixtures may exercise the Crossref matcher and are
explicitly marked synthetic. `config/relevance_profiles.example.json` is an
unresolved source definition (`resolved=false`, empty IDs), never an active
profile. Comparison requires explicit isolated roots: fetch freezes one shared
wide-recall corpus and both sampling/replay configurations; replay verifies file
hash/size/count and never uses the network. A/B/C evaluate identical candidate
IDs and ranks. Human labels and Precision@50 remain null until manually supplied.

The comparison CLI has two strict, mutually exclusive modes. Fetch does not
load profiles, notebooks, or known DOIs:

```text
python scripts/compare_discovery_relevance.py --fetch --sampling-config <sampling.json> --output-root <corpus-root> --allow-network-fetch
python scripts/compare_discovery_relevance.py --replay --profiles <profiles.json> --notebook-root <isolated-notebooks> --sampling-config <sampling.json> --output-root <corpus-root> [--known-dois <known-dois.txt>]
```

Replay publishes only the committed run under
`<corpus-root>/replay_runs/<run_id>/`, containing `manifest.json`,
`report.json`, and `COMMITTED`. The former root-level
`relevance_comparison.json` is a legacy artifact: it is not authoritative and
is no longer created, updated, or read.

For a final no-network plan check, run:

```text
python scripts/discover_papers_concurrent.py --from-enabled-notebooks --dry-run
```

The output must list, per notebook and provider lane, the active Chinese and
English queries, `query_id`, lane, generation, request signature, cursor,
`exhausted` state, refresh pages, backfill pages, worker count, and page
budget (including `max_provider_requests_total`). Dry-run does not contact a
provider, advance cursors, write page journals, allocate paper numbers, modify
notebooks, or modify Catalog state.

Discovery execution contract: refresh and backfill page budgets are
independent (refresh is bounded by `--refresh-pages` and never consumes the
backfill `--max-pages-total`). `--until-exhausted` is decoupled from
`--max-pages-total`: it requires at least one safety valve
(`--max-pages-total` OR `--max-provider-requests-total`); giant-integer
simulation of unbounded runs is rejected. Reaching a valve is a clean,
resumable stop with one exact reason: `lane_page_budget_reached`,
`batch_page_budget_reached`, or `provider_request_budget_reached`. It is never
a provider failure or `exhausted`. Lane stop reasons use the frozen
`STOP_REASONS` vocabulary (`provider_exhausted`, `refresh_window_complete`,
the three budget reasons, `candidate_backpressure`, `skipped_by_mode`, etc.);
`exhausted=True` requires
`exhaustion_evidence` and is never written on transient failure, timeout, 429,
5xx, SSL, budget, or interrupt.

Provider-page journals use the complete v3 evidence record only: full lane key,
complete request signature, response metadata, and (when exhausted) matching
exhaustion evidence. A legacy/hash-only page is `repair_required`; discovery
does not invent evidence, migrate it during execution, advance its cursor, or
contact a provider first. A 429 registers the shared provider gate and the next
attempt waits through that gate; non-429 failures use normal retry backoff.

Use `manage_discovery_keywords.py --add-query-zh --query-zh ...` or
`--add-query-en --query-en ...` for one-language query edits. The removed
`--add-queries` and ambiguous discovery topic option are not accepted.

## PDF fetch: batch selection and unreachable publishers

The eligible backlog is far larger than a single run, so `fetch_pdf_for_paper_raw.py`
separates *eligibility* (can this workspace be fetched at all) from *selection*
(should this run spend time on it now):

| Flag | Effect |
| --- | --- |
| `--skip-attempted` | skip workspaces that already have `source_records/fetch_result.json`, so a run reaches never-attempted work instead of replaying known-hard failures |
| `--retry-after-days N` | retry a previously attempted workspace only when its last attempt is older than N days; never-attempted workspaces are always selected |
| `--doi-prefix 10.5194` | restrict to a registrant prefix; repeatable, so high-yield publishers can run first |
| `--limit N` | attempt at most N workspaces after all other filters |
| `--report-blocked FILE.csv` | write the operator worklist described below |

`--report` is flushed after every completed item, so an interrupted run keeps
the results it already has. Selection is echoed back in the report under
`selection`.

Recommended order for working through a backlog: highest-yield prefixes first
with `--skip-attempted`, then the remainder.

```bash
python scripts/fetch_pdf_for_paper_raw.py --all --doi-prefix 10.5194 --skip-attempted --limit 50 --apply --report reports/fetch_copernicus.json
```

Some publishers run bot management that scores requests by originating network.
From a datacenter egress they answer `403`/`202` to every request regardless of
User-Agent, TLS fingerprint, cookies, referer, or proxy. `src/fetch/host_policy.py`
holds the empirically derived host set and is used three ways: it demotes such
URLs during OA candidate ranking, it skips the proxy retry after a `403` from
one of them (which cannot succeed), and it marks the failure `blocked_publisher`
in the report and `--report-blocked` worklist. Those papers need institutional
access or a different network, not another run.

Because a paper's reachable repository copy is only known after the OA lookups
run, blocked publishers are classified *after* the fetch, never pre-filtered —
pre-filtering would discard exactly the papers the repository lookups rescue.

## Complete root script inventory

The remaining root entry points are documented here by risk class so the index
cannot silently omit an executable:

```text
_bootstrap.py
agent_acceptance.py
attach_pdf_to_paper_raw.py
audit_discovery_keyword_index_sources.py
cleanup_test_caches.py
test_runtime_workspace.py
audit_ingest_duplicates.py
audit_metadata_quality.py
audit_paper_raw_duplicate_workspaces.py
audit_source_provenance.py
audit_third_party_licenses.py
benchmark_mineru.py
check_directory_hygiene.py
check_mineru_processes.py
check_write_planning_docs.py
check_write_quality_text.py
check_write_tex_project.py
commit_paper_raw_to_papers.py
convert_paper_raw_batch.py
convert_paper_raw_gpu.py
create_write_job.py
curate_paper_raw.py
discover_papers.py
discover_papers_concurrent.py
init_discovery_workspace.py
configure_relevance_profiles.py
compare_discovery_relevance.py
doctor_ingest_pipeline.py
doctor_write_pipeline.py
export_job_bib.py
export_write_job_bib.py
fetch_pdf_for_paper_raw.py
formalize_paper_raw.py
freeze_paper_raw_metadata.py
manage_discovery_keywords.py
pack_repo.py
preflight_paper_raw_import.py
prepare_paper_raw_catalog_task.py
prepare_write_article_workdir.py
apply_catalog_classification_result.py
claim_catalog_classification_tasks.py
doctor_catalog_folders.py
reconcile_catalog_folders.py
run_catalog_classification.py
show_catalog_classification_progress.py
sync_catalog_categories.py
repair_formal_publications.py
repair_paper_raw_derived_files.py
repair_stale_formal_asset_manifests.py
reset_paper_number_ledger.py
resolve_paper_raw_metadata.py
rollback_formal_papers_to_paper_raw.py
run_paper_raw_gpu_conversion_then_resolve.py
smoke_mineru_conversion.py
stage_network_metadata_to_paper_raw.py
stage_raw_pdfs_to_paper_raw.py
start_mineru_services.py
stop_mineru_services.py
validate_paper_raw_catalog.py
validate_rolled_back_paper_raw.py
validate_v2_library.py
validate_write_job.py
write_catalog_tex_article.py
```

Audit/check/validate commands are read-only unless their own help explicitly
offers an apply flag. Repair/reset/rollback/commit/stage/fetch/convert
commands mutate only with their explicit apply/confirmation gates.

## Write jobs: workflow selection and the two bib exporters

`create_write_job.py --workflow` picks which downstream pipeline the job
belongs to; it is persisted as `workflow` in `write/jobs/<job_id>/job.json`
and every later gate reads it from there.

| `--workflow` | persisted value | skill | plan file |
| --- | --- | --- | --- |
| `article` (default) | `catalog_tex_article` | `catalog_tex_writer` | — |
| `review` | `catalog_review` | `catalog_review_writer` | `planning/review_plan.json` |
| `proposal` | `catalog_research_proposal` | `catalog_research_proposal_writer` | `planning/proposal_plan.json` |

```text
python scripts/create_write_job.py --workflow review --categories 风沙运动 --limit 12
```

`proposal` additionally seeds `input/research_input.md` from a placeholder
template. That file is user-authored: `check_write_planning_docs.py` fails
closed while any TODO marker remains, and no skill may fill it in.

`check_write_planning_docs.py --job-id <id>` validates the review/proposal
intermediates and plan JSON (read-only; writes only its own report under
`reports/`). It fails closed on an empty paper pool, a missing
`tex/references.bib`, unresolvable `bib_key`s, evidence outside the pool, and
any `results_plan` item whose status is not `planned`.

Two bib exporters exist and are not interchangeable:

- `export_write_job_bib.py --job-id <id>` — the review/proposal planning
  pipeline. Reads only job-local `article/<paper_number>/*.metadata.json` and
  writes `tex/references.bib`. Fails closed on a missing/ambiguous metadata
  file, a non-citation-ready record, or a `--paper-numbers` value that is not
  in `selected_catalog.json`.
- `export_job_bib.py --job <id>` — the older tex-project path used by
  `write_catalog_tex_article.py` via `src.writer.bib_manager`.

### Discovery staging benchmark

`scripts/benchmark_discovery_staging.py` builds only synthetic temporary
workspaces and sends every measured candidate through the complete staging
transaction. Required arguments are `--existing-workspaces`, `--new-records`,
`--unsettled-workspaces`, `--repeat`, and `--json-report`. Its observer reports
full/incremental Registry operations, workspace reads, ledger loads/saves,
allocations, staged records, and cold/warm latency. Benchmark JSON under
`reports/` is runtime output and is excluded from source snapshots.

Release benchmark tiers are 100/20, 1000/50, 3000/100, and 10000/100
existing/new records. Reports include Registry pre-refresh, post-refresh, and
direct-publish counters in addition to ledger loads/saves. A valid warm run has
zero successful-stage post-refreshes and retains both durable saves for each
new record.

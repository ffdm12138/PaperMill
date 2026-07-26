# Script usage and risk index

| Script | Role | Mutates state |
|---|---|---|
| `_bootstrap.py` | (library) entry-point runtime init imported by operational scripts: validate settings, create runtime dirs, configure logging | creates runtime dirs |
| `audit_discovery_workspace_registry.py` | read-only raw/formal Registry, conflict, generation, and repair-backlog audit | no |
| `verify_discovery_final_architecture.py` | strict static and dynamic verification of the single-path Discovery execution architecture | no |
| `repair_discovery_workspaces.py` | explicit reserved-workspace repair planning/promotion | `--apply` |
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
| `fetch_pdf_for_paper_raw.py` | duplicate-guarded PDF attach | `--apply` |
| `convert_paper_raw_gpu.py` | MinerU conversion; Metadata freeze not required | `--apply` |
| `resolve_paper_raw_metadata.py` | deterministic Metadata candidate resolution | `--apply` |
| `freeze_paper_raw_metadata.py` | strict match replay and Metadata freeze | `--apply` |
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
`manage_discovery_keywords.py`, and `migrate_discovery_v4.py` manage the
schema-v4 concurrent Refresh/Backfill discovery queue. Each notebook has one
Chinese `keyword_zh` identity and curated Chinese/English `search_queries`.
An enabled notebook must be bilingual-ready; a disabled draft may be
incomplete. The concurrent wrapper executes every active query in both
providers while Catalog classification reads only `keyword_zh`. The strict
audit is read-only. `migrate_discovery_v4.py` handles one-time migration from
v2/v3 discovery state to v4; it supports `--plan`, `--apply`, `--resume`,
`--inspect`, `--dry-run`, `--cutover`, and `--abort`, plus the post-cutover
chain `--post-cutover-validate`, `--rollback`, `--clean-legacy`, and
`--finalize`. Cutover holds the global `.migration.lock`, snapshots the
superseded pointer, self-heals from crash windows on rerun, and is allowed
only from `smoke_passed`; the smoke run targets isolated paper_raw/papers/
ledger directories inside the staging workspace. Only v4 schema notebooks are
accepted in production; v1/v2/v3 notebooks must be migrated first.

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
migrate_discovery_v4.py
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

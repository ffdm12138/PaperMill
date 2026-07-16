# Script usage and risk index

| Script | Role | Mutates state |
|---|---|---|
| `audit_discovery_workspace_registry.py` | read-only raw/formal Registry, conflict, generation, and repair-backlog audit | no |
| `repair_discovery_workspaces.py` | explicit reserved-workspace repair planning/promotion | `--apply` |
| `repair_formal_publications.py` | audit or identity-only repair of legacy active formal sidecars; unsafe closures emit rollback/recommit plans | `--apply` |
| `migrate_quarantined_duplicate_ledger_state.py` | explicit retired-state migration to abandoned quarantine facts | `--apply` |
| `benchmark_discovery_pipeline.py` | synthetic raw+formal+candidate batch I/O benchmark | no real runtime state |

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
| `migrate_keyword_notebooks_v3.py` | inventory and plan-bound transactional migration to strict notebook v3 | `--write-plan`; `--apply` requires transaction id and plan hash |
| `claim_catalog_classification_tasks.py` | list next unapplied tasks for a worker | no |
| `reconcile_catalog_folders.py` | rebuild folder links from authoritative state | `--apply` |
| `doctor_catalog_folders.py` | audit writer safety and folder integrity | no |
| `recover_discovery_keyword_notebooks.py` | inspect strict-v3 recovery candidates and emit an identity-bound plan | inspect-only; no v3 apply entry point |
| `show_catalog_classification_progress.py` | report classification completion status | no |
| `rollback_formal_papers_to_paper_raw.py` | recoverable formal-to-numeric-raw rollback | `--apply` |
| `revise_frozen_metadata.py` | admin-only raw Metadata revision | dry-run by default |
| `validate_v2_library.py` | validate formal Catalog v3.2 library/index closure | no |
| `agent_acceptance.py` | compile, tests, hygiene, pack, ZIP verification | creates snapshot |
| `cleanup_test_caches.py` | safe cleanup of stale test workspaces and legacy caches | `--apply` |
| `test_runtime_workspace.py` | isolated test workspace context manager (library) | no |
| `pack_repo.py` | runtime-zero source audit snapshot | creates ZIP |
| `benchmark_discovery_staging.py` | performance benchmark for discovery staging (synthetic temp data only) | no |

Real migration apply requires explicit authorization after inventory, backup,
dry-run reports, and fixture acceptance. Agent tests always use temporary roots.
For the current five-notebook production migration, review the inventory and
fixed mapping/plan first, then apply only that same plan-bound transaction.
Operator mapping and transaction plans belong under local/runtime state and are
not source snapshot artifacts. The migration inventory, strict audit, and
`recover_discovery_keyword_notebooks.py --inspect` commands are read-only.

## Discovery / Metadata discovery

`discover_papers.py`, `discover_papers_concurrent.py`, and
`manage_discovery_keywords.py` manage the schema-v3 concurrent Refresh/Backfill
discovery queue. Each notebook has one Chinese `keyword_zh` identity and
curated Chinese/English `search_queries`. An enabled notebook must be
bilingual-ready; a disabled draft may be incomplete. The concurrent wrapper
executes every active query in both providers while Catalog classification
reads only `keyword_zh`. The strict audit is read-only, and v3 recovery is
currently inspect-only because no unsafe legacy write path is retained. The
retired v2 notebook migration script is not an active entry point.

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

Crossref scope verification reuses raw OpenAlex Work evidence only; profile
verdicts remain notebook-local. `config/relevance_profiles.example.json` is an
unresolved source definition (`resolved=false`, empty IDs), never an active
profile. Comparison requires explicit isolated roots: fetch freezes one shared
wide-recall corpus and both sampling/replay configurations; replay verifies file
hash/size/count and never uses the network. A/B/C evaluate identical candidate
IDs and ranks. Human labels and Precision@50 remain null until manually supplied.

For a final no-network plan check, run:

```text
python scripts/discover_papers_concurrent.py --from-enabled-notebooks --dry-run
```

The output must list, per notebook and provider lane, the active Chinese and
English queries, `query_id`, lane, generation, request signature, refresh pages,
backfill pages, worker count, and page budget. Dry-run does not contact a
provider, advance cursors, write page journals, allocate paper numbers, modify
notebooks, or modify Catalog state.

Use `manage_discovery_keywords.py --add-query-zh --query-zh ...` or
`--add-query-en --query-en ...` for one-language query edits. The removed
`--add-queries` and ambiguous discovery topic option are not accepted.

## Complete root script inventory

The remaining root entry points are documented here by risk class so the index
cannot silently omit an executable:

```text
agent_acceptance.py
attach_pdf_to_paper_raw.py
audit_discovery_keyword_index_sources.py
cleanup_test_caches.py
test_runtime_workspace.py
audit_ingest_duplicates.py
audit_metadata_quality.py
audit_paper_number_ledger.py
audit_paper_raw_duplicate_workspaces.py
audit_raw_vs_paper_raw.py
audit_source_provenance.py
audit_third_party_licenses.py
benchmark_mineru.py
check_directory_hygiene.py
check_mineru_processes.py
check_write_quality_text.py
check_write_tex_project.py
commit_paper_raw_to_papers.py
convert_paper_raw_batch.py
convert_paper_raw_gpu.py
create_write_job.py
curate_paper_raw.py
discover_papers.py
discover_papers_concurrent.py
configure_relevance_profiles.py
compare_discovery_relevance.py
doctor_ingest_pipeline.py
doctor_write_pipeline.py
export_job_bib.py
fetch_pdf_for_paper_raw.py
formalize_paper_raw.py
freeze_paper_raw_metadata.py
manage_discovery_keywords.py
migrate_keyword_notebooks_v3.py
migrate_import_status_v2.py
pack_repo.py
preflight_paper_raw_import.py
prepare_paper_raw_catalog_task.py
prepare_write_article_workdir.py
quarantine_unreferenced_workspaces.py
apply_catalog_classification_result.py
claim_catalog_classification_tasks.py
doctor_catalog_folders.py
reconcile_catalog_folders.py
recover_discovery_keyword_notebooks.py
run_catalog_classification.py
show_catalog_classification_progress.py
sync_catalog_categories.py
reconcile_paper_raw_non_destructive.py
repair_corrupted_markers.py
repair_formal_publications.py
repair_ledger_folder_names.py
repair_paper_raw_derived_files.py
repair_stale_formal_asset_manifests.py
reset_paper_number_ledger.py
resolve_paper_raw_metadata.py
restore_paper_raw_from_mineru_output_cache.py
revise_frozen_metadata.py
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

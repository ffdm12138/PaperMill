# Script usage and risk index

| Script | Role | Mutates state |
|---|---|---|
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
| `apply_catalog_classification_result.py` | validate and apply one LLM result | `--apply` |
| `reconcile_catalog_folders.py` | rebuild folder links from authoritative state | `--apply` |
| `doctor_catalog_folders.py` | audit writer safety and folder integrity | no |
| `manage_catalog_categories.py` | list or explicitly retire categories | `--apply` |
| `rebuild_catalog_folder_system.py` | one-time no-import replacement of retired indexes | `--apply` |
| `rollback_formal_papers_to_paper_raw.py` | recoverable formal-to-numeric-raw rollback | `--apply` |
| `revise_frozen_metadata.py` | admin-only raw Metadata revision | dry-run by default |
| `validate_v2_library.py` | validate formal Catalog v3.2 library/index closure | no |
| `agent_acceptance.py` | compile, tests, hygiene, pack, ZIP verification | creates snapshot |
| `pack_repo.py` | runtime-zero source audit snapshot | creates ZIP |

Real migration apply requires explicit authorization after inventory, backup,
dry-run reports, and fixture acceptance. Agent tests always use temporary roots.

## Discovery / Metadata discovery

`discover_papers.py`, `discover_papers_concurrent.py`,
`manage_discovery_keywords.py`, and `migrate_discovery_notebooks_v2.py` manage
the concurrent Refresh/Backfill discovery queue. The concurrent wrapper is the
normal broad-search entry point.

## Complete root script inventory

The remaining root entry points are documented here by risk class so the index
cannot silently omit an executable:

```text
agent_acceptance.py
attach_pdf_to_paper_raw.py
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
doctor_ingest_pipeline.py
doctor_write_pipeline.py
export_job_bib.py
fetch_pdf_for_paper_raw.py
formalize_paper_raw.py
freeze_paper_raw_metadata.py
manage_discovery_keywords.py
match_paper_raw_metadata.py
migrate_discovery_notebooks_v2.py
migrate_import_status_v2.py
pack_repo.py
preflight_paper_raw_import.py
prepare_paper_raw_catalog_task.py
prepare_write_article_workdir.py
quarantine_unreferenced_workspaces.py
apply_catalog_classification_result.py
doctor_catalog_folders.py
manage_catalog_categories.py
plan_catalog_classification.py
rebuild_catalog_folder_system.py
reconcile_catalog_folders.py
sync_catalog_categories.py
reconcile_paper_raw_non_destructive.py
repair_catalog_asset_refs.py
repair_corrupted_markers.py
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
write_review.py
```

Audit/check/validate commands are read-only unless their own help explicitly
offers an apply flag. Repair/reset/rollback/commit/stage/fetch/convert
commands mutate only with their explicit apply/confirmation gates.

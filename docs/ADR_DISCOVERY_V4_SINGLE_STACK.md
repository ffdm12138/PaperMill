# ADR: Discovery v4 Single-Stack Architecture

**Status:** Accepted (target architecture for discovery runtime)  
**Date:** 2026-07-24  
**Supersedes:** Any earlier document that claims discovery runtime supports multiple concurrent notebook/page-journal schemas or a v3 fallback path.

## 1. Context

The discovery subsystem had evolved into a dual-stack:

- A new v4 shell (`DiscoveryWorkspace`, `DiscoveryStoreBundleV4`, `src/discovery/contracts`, `src/discovery/stores`) was built and tested in isolation.
- The production coordinator, CLI, and several legacy tests still constructed the old `KeywordNotebookStore`, old `PageJournalStore`, and old flat discovery directories (`data/discovery/keyword_notebooks`, `data/discovery/pending_pages`, …).
- Two incompatible protocols both claimed schema version `"4.0"`: the old `src/discovery/page_journal.py` (no `checksum`) and the strict `src/discovery/contracts/page_journal.py` (mandatory `checksum`).
- CLI entry points silently fell back to the old flat paths when an active workspace pointer could not be resolved.

This ADR freezes the single v4 stack and the order in which the remaining migration/cleanup work must happen.

## 2. Decision

There is exactly one production discovery runtime stack: **Discovery v4**.  All other notebook, page-journal, lane-state, candidate, and report representations are either migration inputs or obsolete.

The single stack is:

```text
active generation pointer (data/discovery/active_generation.json)
        │
        ▼
DiscoveryWorkspace + workspace.json manifest
        │
        ▼
DiscoveryStoreBundleV4
  ├── notebooks        → NotebookStoreV4
  ├── lane_states      → LaneStateStoreV4
  ├── pages            → PageJournalStoreV4
  ├── candidates       → PendingCandidateStoreV4
  ├── indexes          → JournalIndexV4
  └── reports          → ReportStoreV4
        │
        ▼
DiscoveryRuntimeDependencies (injected, never constructed inside the coordinator)
  ├── stores
  ├── metadata_gateway → MetadataStagingGateway
  ├── provider_factory → ProviderClientFactory
  └── clock
        │
        ▼
run_discovery_batch_with_dependencies(keywords, deps, ...) → BatchDiscoveryReportV4
        │
        ▼
MetadataStagingGateway
        │
        ▼
DiscoveryStageTransaction → metadata v2.0 → paper_raw
```

Metadata v2.0 and Catalog v3.2 are **out of scope** for this migration: they remain the formal citation and content layers and are not modified by discovery work.

## 3. Frozen v4 protocol

### 3.1 Notebook

- One notebook file = one Chinese classification concept (`keyword_zh`).
- `keyword_id` is derived **only** from `keyword_zh` (NFC-normalized, whitespace-folded, casefolded).
- English queries participate in search but never create categories or affect `keyword_id`/`query_id`.
- Schema version is `"4.0"`.
- The active production contract is `NotebookStoreV4` / `KeywordNotebookV4`.  v1/v2/v3 notebooks are rejected with `UnsupportedNotebookSchemaError`; any v2/v3 reader must live only in the migration package.

### 3.2 Lane

- A lane is `(keyword_id, query_id, provider, mode)` where `provider ∈ {openalex, crossref}` and `mode ∈ {refresh, backfill}`.
- `LaneStateV4` is the only durable cursor state.
- Cursor advancement is a **CAS transaction**: `(expected_revision, expected_cursor) → (new_cursor, new_revision)` and is recorded only after a durable v4 page journal exists.
- `generation` belongs to the provider lane; it is not a version negotiation field.

### 3.3 Provider page journal

There is **only one** `ProviderPageJournalV4` definition and one `PAGE_SCHEMA_VERSION_V4`:

```python
PAGE_SCHEMA_VERSION_V4 = "4.0"
PAGE_V4_FIELDS = frozenset({
    "schema_version", "page_id", "keyword_id", "keyword_zh",
    "query_id", "query", "query_language", "provider", "lane",
    "generation", "lane_key", "request_signature",
    "request_cursor", "next_cursor",
    "provider_exhausted", "returned_count",
    "response_metadata", "exhaustion_evidence", "state",
    "fetched_at", "cursor_committed_at", "drained_at",
    "candidates", "statistics",
    "refresh_run_id", "page_sequence", "checksum",
})
```

- `checksum` is **mandatory** for every persisted journal.  It is a SHA-256 digest over the canonical JSON payload excluding the `checksum` field itself.
- Every writer that mutates a page must recompute `checksum` before atomic persistence.
- Readers validate `checksum` on every load and raise `JournalCorruptError` on mismatch.
- `refresh_run_id` and `page_sequence` are required fields even when they are sentinels (`None`, `0`).

### 3.4 Candidate

- A candidate is identified by `candidate_id` = `stable_hash(page_id, provider_record_id)`.
- The durable record lives inside the page journal.
- Lifecycle states are the `CandidateState` literals; terminal states are final.
- `PendingCandidateV4` is the transport object used by the drain loop and the metadata staging gateway.

### 3.5 Report

- `BatchDiscoveryReportV4` is the only batch report schema.
- `schema_version` emitted by the CLI and report store must be `"4.0"`, never `"3.0"`.

### 3.6 Workspace manifest and active pointer

- `DiscoveryWorkspaceManifestV4` (`workspace.json`) records the complete hash-bound state of one generation at creation time.
- `ActiveGenerationPointerV4` (`data/discovery/active_generation.json`) is the single atomic cutover point.  It must carry `generation_id`, `workspace_manifest_sha256`, `activated_at`, and `migration_id`.
- A missing or invalid active pointer is a **hard failure** for production; CLI and coordinator must not fall back to the old flat directories.

### 3.7 Migration journal

- `MigrationJournalV4` records the state machine for legacy discovery data migration.
- Allowed states are adjacent: `planned → inventory_complete → archive_prepared → workspace_built → notebooks_staged → candidates_extracted → preflight_validated → smoke_passed → cutover_committed → legacy_cleaned → finalized`.  `smoke_failed` is a recoverable side state that retries the smoke step; `aborted` is terminal and reachable from any pre-cutover state, and from `cutover_committed` via `--rollback` (the only post-cutover escape).
- Cross-state jumps are forbidden.
- Smoke failure is a hard stop: nonzero exit code must transition to `SMOKE_FAILED`, not `SMOKE_PASSED`.  `--skip-real-smoke` leaves the journal at `PREFLIGHT_VALIDATED` and never grants cutover eligibility; `allowed_cutover_states` is exactly `{SMOKE_PASSED}`.

## 4. Store / repository layer

`DiscoveryStoreBundleV4` is the only store injection point.  Requirements:

- All stores are constructed from one `DiscoveryWorkspace`.
- No store reads `config.settings` discovery constants or the old flat paths.
- All writes are atomic (tmp → fsync → `os.replace`) and hold the workspace lock.
- Corrupt files fail closed with a typed exception, never treated as "missing".
- Path traversal is rejected for all identifiers.
- Stores exchange typed objects, not raw dicts, except where the v4 contract intentionally provides a `to_dict()`/`from_dict_strict()` pair.

The old `KeywordNotebookStore`, `PageJournalStore`, and old flat-directory stores are removed from the production namespace; if the migration tool still needs them, they are renamed to `Legacy…Reader` and live only under `src/migrations/discovery_v4/`.

## 5. Composition root and coordinator boundary

- **CLI** owns workspace resolution: parse `--workspace-root`, load the active generation pointer, build `DiscoveryWorkspace`, and construct `DiscoveryStoreBundleV4.from_workspace()`.
- **Coordinator** receives `DiscoveryRuntimeDependencies` through `run_discovery_batch_with_dependencies(...)` and never calls `WorkspaceResolver()` or `resolve_active()` inside the core path.
- `DiscoveryOptions` is a pure knob holder: it carries no flat discovery paths (`notebook_dir`, `pending_pages_dir`, `locks_dir`, `exports_dir`).  The transitional `DiscoveryRuntimeDependencies.from_options()` helper and its `flat-<uuid>` pseudo-generation fallback are removed.  Production callers (scripts) construct `DiscoveryRuntimeDependencies` explicitly and call `run_discovery_batch_with_dependencies(...)`; tests create a `DiscoveryWorkspace` under `tmp_path` (via `tests/helpers/discovery_workspace.make_test_workspace`) and pass it as `options.workspace` or pass a ready bundle to `run_discovery_batch`, which fails closed when neither is supplied.

## 6. Metadata staging gateway

- `MetadataStagingGateway` is the stable interface between discovery and `paper_raw`.
- Discovery emits `PendingCandidateV4` objects; the gateway converts them to metadata v2.0 inputs.
- `DiscoveryStageTransaction` remains the sole transaction coordinator under `paper_raw/.paper_raw_write.lock`.
- Results must distinguish `staged_new`, `reused_existing`, `duplicate_observation`, `invalid`, `failed_retryable`, `failed_terminal`.
- The following aliases are removed: `StageTransactionResult = DiscoveryStageResult`, `inspect_doi`, `paper_raw_id` dual naming, and `hasattr(transaction, "stage_candidates_batch")` fallback branches.

## 7. Migration state machine

The migration CLI must support exactly these subcommands:

```bash
python scripts/migrate_discovery_v4.py --plan
python scripts/migrate_discovery_v4.py --apply
python scripts/migrate_discovery_v4.py --resume <migration_id>
python scripts/migrate_discovery_v4.py --inspect <migration_id>
python scripts/migrate_discovery_v4.py --cutover <migration_id>
python scripts/migrate_discovery_v4.py --post-cutover-validate <migration_id>
python scripts/migrate_discovery_v4.py --rollback <migration_id>
python scripts/migrate_discovery_v4.py --clean-legacy <migration_id>
python scripts/migrate_discovery_v4.py --finalize <migration_id>
python scripts/migrate_discovery_v4.py --abort <migration_id>
python scripts/migrate_discovery_v4.py --dry-run
```

Rules:

- `--apply` performs the full migration but does **not** cut over.  Cutover is an explicit `--cutover` step.
- `--resume` restarts from the last recorded state, verifies artifact hashes, and skips completed phases.
- `--abort` is safe only before cutover; after cutover only `--rollback` is allowed.
- Smoke must be executed against the **staging** workspace, not the active pointer, and its paper_raw/papers/ledger targets are isolated directories inside the staging workspace (`--paper-raw-dir` / `--papers-dir` / `--ledger-path`).
- Smoke failure blocks cutover.
- `--cutover` holds the global `.migration.lock`, snapshots the superseded pointer, records `previous_generation_id`, and self-heals from every crash window (rename / pointer write / journal save) on rerun.
- The post-cutover chain is `--post-cutover-validate` (read-only; gates on a fully drained pending store and, when no legacy candidates were imported, verifies the activation-time tree hash) → `--clean-legacy` (re-verifies archive hashes, removes the drained transitional `pending_candidates/` directory, then deletes the legacy archive) → `--finalize`.  `--rollback` restores the previous pointer and moves the promoted generation back to staging.  The pending-store lifecycle and the exact tree-hash conditions are frozen in `docs/ADR_DISCOVERY_V4_MIGRATION_FINAL.md`.

## 8. Deletion of old runtime compatibility

The following must disappear from the production `src/discovery/**` tree and tests:

- [x] `LegacyNotebookSchemaError`, `LegacyNotebookError` as production APIs.
- [x] `PAGE_V3_FIELDS` and any second definition of `PAGE_SCHEMA_VERSION = "4.0"`.
- [x] `schema_version = "3.0"` in reports.
- [x] `LegacyCandidateSeedV4` in `src/discovery/contracts/candidate.py` (move to migration package).
- [x] Flat-directory fallbacks: `data/discovery/keyword_notebooks`, `data/discovery/pending_pages`, etc.
- Silent `except Exception: pass` and "fall back to defaults for backward compat".
- Duplicate modules: [x] `src/discovery/batch_runtime.py`, [x] `src/discovery/provider_models.py`, [x] `src/discovery/page_journal.py`, [x] `src/discovery/keyword_notebook.py` (retired alias shells deleted; `src/discovery/contracts/*` and `src/discovery/stores/*` are the sole implementations).

Hygiene tests must enforce the above by grepping the production tree.

## 9. Verification and acceptance

The final gate must include:

```bash
python -m compileall -q src scripts tests
pytest -q tests/unit/discovery
pytest -q tests/contract
pytest -q tests/integration
pytest -q tests/hygiene
pytest -q
python scripts/verify_discovery_final_architecture.py --json
python scripts/check_directory_hygiene.py
python scripts/pack_repo.py
```

Additional gate conditions:

- `DiscoveryStoreBundleV4` is imported and used by the production coordinator.
- There is only one `page_journal.py` schema definition with mandatory `checksum`.
- CLI fails closed when the active pointer is missing.
- Production code does not import `src.migrations.discovery_v4`.
- Metadata v2.0 and Catalog v3.2 are unchanged.
- `scripts/verify_discovery_final_architecture.py` also enforces the migration-hardening structural gates: the retired alias shells stay deleted with no `src.discovery.keyword_notebook` / `src.discovery.page_journal` references anywhere under `src/` or `scripts/`; the smoke run passes the three isolated targets; `commit_workspace` is lock-guarded with previous-pointer snapshot and crash reconciliation; `resolve_active(verify_tree=True)` performs a real tree-hash content check; `allowed_cutover_states` is exactly `{SMOKE_PASSED}`; no `legacy_candidate_seeds` references remain; archive copies are destination-rehashed; `pending_queue` drains `PendingCandidateStoreV4` with the coordinator injecting `bundle.pending`; and the CLI exposes `--post-cutover-validate` / `--rollback` / `--clean-legacy` / `--finalize`.

## 10. Execution environment note

On Windows, migration scripts and CLI commands that require PowerShell must be run with **PowerShell 7** (`pwsh`), not Windows PowerShell 5 (`powershell`).  PowerShell 7 handles UTF-8, long paths, and JSON output consistently with the rest of the pipeline.

## 11. Consequences

- Positive: the production and test paths are the same; there is no hidden fallback to an old stack.
- Positive: every persisted page journal is checksum-protected, so mutations that forget to recompute the digest are caught immediately.
- Negative: legacy tests and helpers that rely on manual dict mutation or raw `write_text()` must be updated to use the v4 store or recompute the checksum.
- Negative: the migration of existing legacy discovery data (≈3.36 GB of schema-2.0 page journals) must run through the formal migration state machine before any cleanup.

## 12. Related documents

- `docs/ADR_DISCOVERY_STAGING_BOUNDARIES.md`
- `docs/ADR_DISCOVERY_V4_MIGRATION_FINAL.md`
- `docs/PROJECT_CONTRACT.md`
- `docs/ARCHITECTURE.md`
- `artifacts/discovery_v4_repair/baseline.json`

# Project status

The discovery pipeline now uses a shared batch runtime: formal and raw identities
share one Registry, durable formal publication state controls reloads, a single
`JournalDrainIndex` replaces per-candidate page scans, and one bounded consumer
stages groups of at most 16. Historical repair backlog is isolated from candidate
refresh while exact matches remain live-validated. Retryable candidates remain
scheduled in the same runtime, repair probes rotate through a durable cursor,
and a consumer watchdog measures lack of progress rather than total drain time.

Notebook-scoped relevance filtering is fail-closed. Legacy/profile-unbound
sentinels cannot query providers or evaluate candidates. Profile replacement
closes only pre-staging `pending/resolution_pending/ready` observations whose
old relevance is `profile_unbound`, `passed`, or `verification_deferred`.
`processing/failed_retryable` require Discovery recovery; completed terminal
history and durable DOI projections are immutable. Unknown lifecycle values
globally block planning. The transaction uses deterministic expected-after bytes,
zero-write Phase A, crash-resumable page publication, and notebook-last commit.
The drain index owns a batch-frozen active-profile mapping. The five-profile
source is deliberately unresolved and requires an exact frozen-taxonomy plan.

The active ingest architecture is the frozen Metadata / complete Catalog v3.2
pipeline. There is one active implementation.

## Current flow

```text
network Metadata or local PDF
-> permanent 16-digit paper_number
-> data/paper_raw/<paper_number>/
-> PDF attach and optional early conversion
-> citation-ready Metadata + strict PDF match receipt
-> Metadata freeze
-> current Markdown/images conversion
-> Catalog v3.2 task, generation, validation, freeze
-> plan-only formalize
-> external-journal hidden-staging commit
-> atomic formal install + ledger activation
-> all and pending directory links + per-paper classification task
```

Local PDF conversion may precede Metadata resolution. Catalog requires both
Metadata frozen and conversion complete. Formalize never renames. Commit alone
renames copied staging assets to `paper_name`.

Only Metadata v2.0 and Catalog v3.2 are supported. Older Metadata, Catalog, and
non-numeric workspace layouts must be regenerated outside the active pipeline;
the repository contains no runtime migration path.

Each formal paper owns its Catalog. DOI-notebook Chinese keywords define the
available categories; an LLM reads each independent Catalog and records positive
or negative decisions. Category members link to complete formal directories.
Writing browses category folders and citations still come only from Metadata.
Default and source snapshots are runtime-zero.

Discovery keyword notebooks use schema v4. One notebook owns one Chinese
`keyword_zh` classification identity plus multiple enabled Chinese and English
`search_queries`; all queries participate in OpenAlex/Crossref discovery with
independent provider progress. Only `keyword_zh` enters the Catalog registry.
English queries never create categories or classification tasks, and query
changes do not invalidate existing classification decisions.

Enabled notebooks are required to be bilingual-ready. `ensure_notebook` creates
an incomplete notebook as a disabled draft; `set_enabled(True)` and every
definition mutation enforce readiness atomically. The audit is strict and
read-only. Recovery currently supports only `--inspect`, because legacy v3
apply is not exposed until a plan-bound writer can be independently verified.

## Catalog status

- Pending: 0
- Missing decisions: 0
- Stale decisions: 0
- Unapplied results: 0
- Classification complete: true
- Writer category safe: true

## Notebook v4 delivery status

The five production notebooks are enabled, schema v4, bilingual-ready, and
migrated from v3 via `scripts/migrate_discovery_v4.py`.  All cursors, exhausted
states, and generation counters were reset on migration.  Provider page journals
now use the strict v4 schema (`"4.0"`) with complete lane key, response metadata,
and exhaustion evidence records.

The active v4 workspace lives under `data/discovery/generations/<id>/` and is
activated by `data/discovery/active_generation.json`.  Legacy v2/v3 journals
remain in `data/discovery/pending_pages/` (not read by v4 production code) and
may be archived to `data/discovery/legacy_archive/`.

## Test suite status

- **Tests collected:** 1951 (run `pytest --collect-only -q` for current count)
- **Acceptance:** pre-flight clean, syntax gate 464/464 passed, runtime-zero snapshot verified
- **Snapshot:** 548 payload files, 549 members, 0 runtime files, secret scan passed

### Cleanup principles

Duplicate layered-coverage tests and field-exhaustive cross-module contracts were
removed. Security boundaries, transaction/rollback/recovery, locking, TOCTOU,
duplicate guards, and snapshot roundtrip tests are preserved. Field-level
exhaustive coverage moved to the unit layer; contract tests use a representative
matrix.

Catalog synchronization is limited to the same five Chinese categories. The
folder-integrity projection is safe; 4 formal papers classified, all decisions
current. Classification was completed through the validated manual backend.
The final discovery check is `--from-enabled-notebooks --dry-run`; it does not
start real network search or mutate discovery, ingest, or Catalog state.

Runtime formal readiness is guarded by
`data/papers/.formal_publication_state.json` and a full metadata/catalog/
manifest hash closure at each staging batch boundary. Use
`repair_formal_publications.py` for identity-only legacy sidecars; any closure
or freeze drift is reported as `rollback_recommit_required`. Use
`repair_discovery_workspaces.py` to demote historical incomplete
`metadata_staged` entries to `reserved` while preserving permanent numbers.

Discovery staging is single-tracked through `DiscoveryStageTransaction` and
the authoritative `WorkspaceRegistry`. Evidence and profile readiness are
separate; missing/unknown workflow profiles fail closed. Incremental Registry
publication is copy-on-write, while the in-memory DOI and identity indexes
have no disk refresh methods. The allocator is generic and `pending_queue.py`
is journal orchestration only.

Snapshot 53 closes the remaining integrity and warm-path gaps: incomplete
`reserved` remains unsettled, incomplete `metadata_staged` fails closed,
`identity_key + paper_number` preserves multiple providers per paper, refresh
publishes atomically, and each candidate reuses one locked ledger load. New
records retain two crash-safe saves and publish directly with no post-refresh.

Snapshot 54 closes stale-cache decisions without restoring settled full scans.
Refresh compares state/folder/scope lifecycle projections from the locked
ledger and target-rescans only changed numbers. Matched DOI/identity records are
live-revalidated, damaged settled evidence fails `repair_required`, rollback
replacement removes old formal refs, and reuse no longer counts as allocation.

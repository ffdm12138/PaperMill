# Project status

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

Discovery keyword notebooks use schema v3. One notebook owns one Chinese
`keyword_zh` classification identity plus multiple enabled Chinese and English
`search_queries`; all queries participate in OpenAlex/Crossref discovery with
independent provider progress. Only `keyword_zh` enters the Catalog registry.
English queries never create categories or classification tasks, and query
changes do not invalidate existing classification decisions.

Enabled notebooks are required to be bilingual-ready. `ensure_notebook` creates
an incomplete notebook as a disabled draft; `set_enabled(True)` and every
definition mutation enforce readiness atomically. The v3 audit is strict and
read-only. Recovery currently supports only `--inspect`, because v3 apply is
not exposed until a plan-bound writer can be independently verified.

## Catalog status

- Pending: 0
- Missing decisions: 0
- Stale decisions: 0
- Unapplied results: 0
- Classification complete: true
- Writer category safe: true

## Notebook v3 delivery status

The five production notebooks are enabled, schema v3, bilingual-ready, and
covered by the reviewed migration mapping and the committed v3 transaction.
Provider generation, request signatures, cursors, generation history, and page
journals remain attached to their query/provider identities. The mapping and
fixed-plan evidence is kept in ignored operator state; it is not part of the
active source tree or runtime-zero snapshot.

## Test suite status

- **Current test count: 1763** (reduced from 1869 in July 2026 cleanup)
- **Full pytest:** 1752 passed, 8 skipped, 0 failed
- **Acceptance:** pre-flight clean, syntax gate 393/393 passed, runtime-zero snapshot verified
- **Snapshot:** 481 payload files, 0 runtime files, secret scan passed

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

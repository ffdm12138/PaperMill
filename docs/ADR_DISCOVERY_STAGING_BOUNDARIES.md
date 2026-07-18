# ADR: Discovery Staging Architecture Boundaries

## 1. Ledger owns only number allocation and raw/formal lifecycle

`PaperNumberLedger` and `paper_number_state.py` answer exactly two questions:

- Is this `paper_number` allocated, and to which workspace?
- Is the paper in `raw` or `formal` stage?

Ledger states are strictly lifecycle positions, never operation results:

```
allocating → reserved → metadata_staged → active
                 ↓              ↓
              abandoned      abandoned
```

`abandoned` is a permanent terminal state — once abandoned, a number is never
recycled or revived for any candidate, under any recovery path.

`stage_failed` does NOT belong in the ledger. Staging failure is an operation
result recorded in `.import_status.json` and the candidate journal. A workspace
whose staging failed is either `reserved` (partial artifacts exist, re-scan
needed) or `abandoned` (no recoverable artifacts).

## 2. Workspace inspector returns only file facts

`WorkspaceEvidence` is a frozen dataclass of file-existence and parse-success
booleans. It contains zero readiness judgments. It never computes
`readiness.ready`, `formalize_allowed`, or any composite gate.

## 3. Readiness evaluator uses ingest profile

Different ingest sources require different evidence:

| Profile | Requires discovery receipt? | Requires PDF? |
|---------|---------------------------|---------------|
| `manual_pdf` | No | Yes |
| `network_metadata` | Yes | No |
| `network_metadata_pdf_fetch` | Yes | Yes |

Profile is determined from `stage_manifest.workflow_path`. Unknown workflow
paths are rejected (`repair_required`), never guessed.

`WorkspaceReadiness` answers one question: "does this workspace's evidence
satisfy the `metadata_staged` contract for its ingest profile?"

## 4. WorkspaceRegistry is the sole scan entry point

`WorkspaceRegistry` performs one scan of `paper_raw/` and `papers/` that reads
each workspace's JSON artifacts once and produces both the DOI duplicate index
and the discovery identity index. No other module may scan source records,
receipts, or metadata directories to build these indices.

- `DuplicateIndex` becomes a pure in-memory lookup structure.
- `DiscoveryWorkspaceIndex` becomes a pure in-memory lookup structure.
- `workspace_refresh.py` is deleted; its logic lives in the registry.

## 5. DiscoveryStageTransaction is the sole write-lock coordinator

Network discovery staging has exactly one transaction coordinator that, under
`paper_raw/.paper_raw_write.lock`, executes in fixed order:

1. Refresh registry (scan new + unsettled)
2. Identity reconciliation against registry
3. DOI duplicate check against registry
4. Choose: reuse existing OR allocate new
5. Write all durable artifacts (source record, metadata, receipt, manifest, status)
6. Transition ledger: `reserved → metadata_staged`
7. Update registry in memory

Registry publication is copy-on-write: refresh scans only new and unsettled
raw workspaces plus newly active formal workspaces, builds a temporary
replacement, and returns no snapshot on any ledger/evidence/readiness error.
The transaction never mutates the last known-good snapshot while durable
writes are in progress.

The snapshot is not permanent truth. Every refresh compares the cached
lifecycle projection (ledger state, folder, and scope) with the already-loaded
ledger view and target-rescans only changed paper numbers. DOI/identity matches
are live-revalidated before reuse or duplicate decisions, so missing settled
evidence fails with `repair_required`. Replacing a record first removes all of
that paper number's old DOI/identity and formal projections; active rollback
therefore cannot leave refs to the old formal folder.

`pending_queue.py` calls the transaction; it never scans workspaces, allocates
numbers, or writes metadata directly. `PaperRawAllocator` provides generic
workspace primitives but does not perform discovery-specific reconciliation.
There is no disk-refresh fallback in either in-memory index.

An incomplete `reserved` workspace remains unsettled. `metadata_staged` is a
complete-closure assertion: missing metadata, source record, discovery receipt,
stage manifest, import status, or marker is `repair_required` before allocation.
The identity index has one mutable fact source keyed by
`(identity_key, paper_number)`, so a paper may keep multiple provider identities
through freeze. Refresh consumes the transaction's locked ledger view and
publishes atomically. Each candidate loads the ledger once; new staging saves
reservation before artifact writes and saves `metadata_staged` after readiness
validation, then directly publishes the validated record without post-refresh.
External adapters may preserve a legacy `staged` display status for reuse, but
`actual_allocated` means only a newly allocated number and `reused_existing`
means only reuse; `DrainReport.staged` counts only new allocations.
# Batch runtime performance boundary

The coordinator creates exactly one `DiscoveryBatchRuntime` per batch. It owns
the `DiscoveryStagingContext`, generation-bound raw/formal Registry snapshot,
`JournalDrainIndex`, metrics, and a repair backlog with a default probe budget of
20. Provider lanes publish durable pages and notify a bounded queue; one consumer
claims and stages at most 16 candidates per write-lock epoch. Journal processing
maps are scheduling hints only; DOI file locks remain the concurrency authority.
No pure TTL cache participates in a duplicate or repair decision.

The formal view is built from active ledger publication revisions and valid
formal closures. Active assets are immutable: the published asset manifest's
Metadata hash is authoritative, and an in-place mutation is a typed Registry
issue. A partial Registry is never usable by discovery, but explicit repair may
use its independently healthy records so unrelated damage cannot deadlock
recovery. Applied batches persist a round-robin repair cursor; retryable Journal
items remain claimable or enter the delayed schedule. Provider publication uses
the Journal index's own lock and consumer shutdown waits while progress
continues, with a configurable no-progress watchdog.

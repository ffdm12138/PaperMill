# ADR: Discovery v4 Active Runtime Contract

**Status:** Accepted (current runtime contract for discovery)
**Date:** 2026-07-27
**Supersedes:** The migration-era operational sections of
`docs/ADR_DISCOVERY_V4_SINGLE_STACK.md` and
`docs/ADR_DISCOVERY_V4_MIGRATION_FINAL.md` (both historical; the v3→v4
migration finalized on 2026-07-25 and its toolchain was removed on
2026-07-26).  Where those ADRs and this one disagree about the *current*
runtime, this ADR is authoritative.

## 1. Context

The v3→v4 migration is finalized and the one-time migration toolchain is
deleted.  What remains is a single production runtime whose structure was
previously documented only inside migration-era ADRs mixed with dead
commands and dead stores.  This ADR freezes the current runtime contract
in one place so maintainers never have to consult historical tooling docs
to understand the live system.

## 2. Decision — the active chain

There is exactly one production candidate flow:

```text
active v4 notebook (NotebookStoreV4, keyword_notebooks/)
        │
        ▼
provider fetch (ProviderClient: OpenAlex + Crossref, dual lane
refresh/backfill, shared per-provider limiter)
        │
        ▼
ProviderPageJournalV4 (page_journals/, mandatory per-record checksum)
        │
        ▼
JournalDrainIndex
        │
        ▼
MetadataStagingGateway
        │
        ▼
DiscoveryStageTransaction (sole coordinator under
paper_raw/.paper_raw_write.lock)
        │
        ▼
metadata v2.0 → paper_raw
```

- The provider page journal is the **sole** candidate carrier; there is no
  secondary candidate store.
- `DiscoveryStoreBundleV4` is the only store injection point and contains
  exactly two stores: `notebooks` (`NotebookStoreV4`) and `pages`
  (`PageJournalStoreV4`).
- Durable cursor state lives inside the v4 notebook itself; reports are
  plain files under `reports/` (`BatchDiscoveryReportV4`, schema `"4.0"`).

## 3. Active pointer and workspace manifest strict contract

`ActiveGenerationPointerV4` (`data/discovery/active_generation.json`) and
`DiscoveryWorkspaceManifestV4` (`workspace.json` inside each generation)
are parsed only through `from_dict_strict`:

- Exact key sets — unknown or missing keys are a hard failure; no
  coercion of any field.
- Timestamps must be strict ISO-8601 with an explicit timezone.
- All hashes must be 64-character lowercase hex SHA-256 digests.
- `generation_id` rejects `.`, `..`, path separators, and Windows
  reserved device names.
- An empty content set is represented by `EMPTY_SET_SHA256`
  (`sha256(b"[]")`), never by an empty string.
- `STORE_SCHEMA_VERSIONS_V4` is exactly
  `{"notebooks": "4.0", "page_journals": "4.0"}`; any other version map
  fails validation.
- A missing or invalid active pointer is a hard failure for production;
  no caller may fall back to retired flat discovery directories.

## 4. Error taxonomy and the fresh-install distinction

Runtime resolution failures keep their types across the
`src/discovery/runtime_context.py` boundary:

- `DiscoveryRuntimeNotInitialized` — no active generation pointer exists.
  This is the **normal fresh-install state** and the only state a caller
  may degrade on.
- `DiscoveryRuntimeCorrupt` — the pointer, manifest, or generation exists
  but is damaged.  Fail closed; never treat as a fresh install.
- `DiscoveryRuntimeIncomplete` — the generation is missing required
  structural pieces.
- `DiscoveryRuntimeMaintenance` — resolution is blocked by an active
  maintenance window.

All four subclass `DiscoveryRuntimeUnavailableError`.

The server exposes this taxonomy directly:

- `GET /status` reports
  `discovery: ready|uninitialized|corrupt|incomplete|maintenance`.
- `GET /status/discovery` returns `200 {"discovery": "ready"}` or a typed
  `503` whose `detail.state` is one of the four states above.

The server itself is layered: middleware performs auth and headers only,
and services are constructed lazily per domain
(`_get_catalog` / `_get_library` / `_get_prompt_builder` /
`_get_job_manager`), so an unresolved discovery runtime never blocks
unrelated endpoints.

## 5. Workspace layout

```text
data/discovery/
├── active_generation.json          ← single atomic cutover point
├── generations/
│   └── <generation_id>/
│       ├── workspace.json
│       ├── keyword_notebooks/
│       ├── page_journals/
│       ├── exports/
│       ├── reports/
│       └── locks/
└── migrations/
    ├── .maintenance.lock           ← exclusive maintenance lock
    └── writer_leases/              ← shared writer lease files
```

A generation contains exactly five subdirectories.  Workspace roots,
generation directories, and every required subdirectory must be real
directories: symlinks and junctions are rejected (`_is_reparse_point`
checks) at resolution, bootstrap, and commit time.

## 6. Bootstrap and crash-window recovery

`bootstrap_initial_workspace()` creates and activates the first v4
generation on a fresh install.  It is idempotent and resumes a crashed
previous attempt deterministically across six windows:

1. crash after staging creation → the staging tree is reused and the
   manifest is (re)written into it;
2. crash after the manifest write → the existing staged manifest is
   strictly validated and committed as-is;
3. crash after the rename but before the pointer write → the existing
   generation is strictly re-validated (manifest, tree hash, directory
   closure) and the pointer is rebuilt from **its original manifest
   hash**, never a new manifest;
4. crash after the pointer write → plain idempotent return;
5. commit reconciliation where the target generation exists and the
   pointer already names it → the commit already finished; only the
   caller's journal needs fixing;
6. neither staging nor target exists → fail closed.

Ambiguous states (multiple unpointed generations, multiple staging dirs,
hash mismatches) raise `CommitReconciliationError` instead of guessing.
`commit_workspace` holds the maintenance lock, snapshots the superseded
pointer, and records `previous_generation_id` in the new pointer.

## 7. Maintenance read/write exclusion

Two cooperating mechanisms under `data/discovery/migrations/`
(`src/discovery/maintenance_gate.py`):

- `DiscoveryWriterLease` — **shared**, held by every production discovery
  batch for its whole run: `writer_leases/<nonce>.lock` plus a JSON
  sidecar binding `pid` + process `start_time` + random `nonce`, so PID
  reuse never authenticates a stale owner.  Many writers may coexist.
- `DiscoveryMaintenanceLock` — **exclusive**, required for fresh-install
  bootstrap, keyword notebook mutations, relevance-profile
  apply/resume/abort, and workspace repair; acquisition is non-blocking
  and only succeeds while **zero** writer leases are held.

Ordering rule (both sides; lock before resolve, never
resolve-before-lock):

1. Writer: publish the lease first, **then** assert no maintenance
   window; on conflict the lease is released and the writer fails closed.
2. Maintenance: acquire the exclusive mutex first, **then** scan writer
   leases; any held lease releases the mutex and fails the command
   closed.

Because each side re-checks after acquiring, neither can slip through the
other's window.  Lock probes that raise `OSError` and unreadable sidecars
are fail-closed everywhere.  The mutex file is the same one
`commit_workspace` uses, so workspace commits stay serialized with
maintenance commands.  The lock file is `.maintenance.lock`; it was
renamed from its migration-era name on 2026-07-27 (the old name is
recorded in the historical ADRs).

## 8. What was removed

Deleted with the dead-store cleanup and the migration finalization
(2026-07-25/26), with reintroduction tombstones enforced by
`scripts/verify_discovery_final_architecture.py`:

- Dead stores: `LaneStateStoreV4`, `JournalIndexV4`, `ReportStoreV4`
  (zero production readers) and the transitional pending-candidate store
  drain channel.
- Dead contracts: `LaneStateV4`, `CursorTransactionV4`.
- Workspace directories `lane_states/`, `indexes/`, and
  `pending_candidates/`.
- The one-time migration toolchain: the migration CLI with all its
  subcommands, the reconciliation script, and `src/migrations/discovery_v4/`
  (exact names are recorded in the historical ADRs).

History and rationale live in the two historical ADRs:
`docs/ADR_DISCOVERY_V4_SINGLE_STACK.md` and
`docs/ADR_DISCOVERY_V4_MIGRATION_FINAL.md` (sections describing the
removed tooling carry inline `> **Removed:**` markers).

## 9. Related documents

- `docs/ADR_DISCOVERY_V4_SINGLE_STACK.md` (historical)
- `docs/ADR_DISCOVERY_V4_MIGRATION_FINAL.md` (historical)
- `docs/ADR_DISCOVERY_STAGING_BOUNDARIES.md`
- `src/discovery/workspace.py` (`WorkspaceResolver`,
  `bootstrap_initial_workspace`, `commit_workspace`)
- `src/discovery/contracts/manifest.py`
- `src/discovery/runtime_context.py`
- `src/discovery/maintenance_gate.py`
- `scripts/verify_discovery_final_architecture.py`

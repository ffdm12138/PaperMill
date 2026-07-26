# ADR: Discovery v4 Migration Final Decisions

**Status:** Accepted  
**Date:** 2026-07-25  
**Update 2026-07-26:** the migration finalized on 2026-07-25 (see
`artifacts/discovery_v4_post_cutover/final_acceptance.json`). The one-time
toolchain (`scripts/migrate_discovery_v4.py`,
`scripts/reconcile_discovery_v4_migration.py`, `src/migrations/`,
`PendingCandidateStoreV4` and its drain channel) has been removed from the
working tree; git history preserves it. Body text below is historical record.  
**Complements:** `docs/ADR_DISCOVERY_V4_SINGLE_STACK.md` (which freezes the
single v4 stack and the migration state machine).  Where the older ADR
describes the post-cutover chain without the pending-store lifecycle, this
ADR is authoritative.

## 1. Context

The real v3→v4 migration has completed in production (migration
`v4-20260723-080858` is `finalized`).  The repair program that reviewed the
migration surfaced three questions that the earlier ADRs left implicit:

1. Two candidate-carrying structures exist in a v4 workspace — the provider
   page journals and `PendingCandidateStoreV4` (`pending_candidates/`).
   Which one is the production carrier, and what happens to the other?
2. `WorkspaceResolver.resolve_active()` defaults to `verify_tree=False`.
   Is that an oversight, and should the workspace be split into immutable
   and runtime directory sets so the tree hash can always be enforced?
3. Legacy (schema 1/2/3) parsing knowledge exists in the repository.  Where
   may it live, and what must production code do when it meets a non-4.0
   schema?

This ADR freezes the answers and records the migration-repair invariants
that future verifiers and audits must check.

## 2. Decision 1 — Page journal is the sole candidate carrier; the pending store is a transitional migration channel

- The provider page journal (`ProviderPageJournalV4`) is the **only**
  production candidate carrier.  It owns the candidate state machine,
  leases, and claims; candidates flow
  `JournalDrainIndex` → `MetadataStagingGateway` → `paper_raw`.
- `PendingCandidateStoreV4` has **no state machine**.  It is a transitional
  drain channel for migrated legacy candidates only:
  - **Writer:** exactly one — the v4 migrator
    (`scripts/migrate_discovery_v4.py` `_step_extract_candidates`), which
    converts extracted legacy seeds into strict `PendingCandidateV4`
    records.
  - **Reader/deleter:** normal discovery runs, via
    `src/discovery/pending_queue.py` `_drain_pending_store_candidates`
    (read → stage through the gateway → delete on durable result).  Normal
    discovery never writes to it.
  - **End of life:** after cutover the store must be drained to zero files
    and then removed:
    - `--post-cutover-validate` fails while any candidate file remains and
      directs the operator to run a normal discovery batch to drain it.
    - `--clean-legacy` refuses to run while the store is non-empty; once
      drained, it deletes the active generation's `pending_candidates/`
      directory as part of the cleanup.
- Consequently `DiscoveryWorkspace.verify_dirs()` no longer requires
  `pending_candidates`: the active generation must keep resolving (and
  `--finalize` must keep working) after the directory is removed.  Staging
  workspaces still create the directory via `ensure_dirs()`, so every
  future migration gets the channel; the store and the drain loop both
  tolerate its absence.

There is no dual pipeline: the pending store never competes with the page
journal as a candidate source.  It exists only to carry legacy candidates
across the cutover, and its removal is part of the migration's definition
of done.

## 3. Decision 2 — `resolve_active(verify_tree=False)` is deliberate; the immutable/runtime split is deferred

The manifest's `workspace_tree_sha256` binds the **activation-time**
closure: the exact bytes the migrator staged.  Normal discovery runs
intentionally mutate `keyword_notebooks` (curation), `lane_states`,
`page_journals`, `pending_candidates` (drain), `indexes`, `exports`,
`reports`, and `locks` (see the comment in
`src/discovery/workspace.py` `resolve_active`).  Recomputing the whole-tree
hash on every resolve would reject the first ordinary production run, so
the production path verifies identity, manifest hash, and required
directories only.

Runtime-store integrity is **not** derived from the tree hash.  It rests on:

- per-record checksums (e.g. the mandatory page-journal `checksum`,
  validated on every load),
- atomic writes (tmp → fsync → `os.replace`),
- store-level indexes rebuilt from content, and
- recovery scans that fail closed on corrupt records.

`verify_tree=True` remains available and is used exactly once, inside
`--post-cutover-validate`, and only when the migration staged **zero**
legacy candidates (`candidate_stats.imported == 0`): in that case no
production drain run is required before validation, so the activation-time
closure must still match.  When candidates were imported, a production
drain run must precede validation, that run legitimately mutates the
runtime tree, and the tree check is superseded by identity + manifest +
drained-store evidence.

**We do not split the workspace into immutable vs runtime directory sets.**
The split is deferred, not rejected.  It should be re-evaluated when any of
the following becomes true:

- an audit or incident shows tampering that per-record checksums and
  recovery scans cannot detect or attribute;
- a second writer class (beyond the known runtime mutations) appears, so
  "expected drift" is no longer enumerable;
- runtime directories stabilize enough that a meaningful per-generation
  content hash can be maintained incrementally; or
- whole-tree attestation of a live generation becomes a hard compliance
  requirement.

Until then, the operational cost of maintaining two directory sets (plus
the resolver and migrator changes to match) buys no integrity the
per-record mechanisms do not already provide.

## 4. Decision 3 — Legacy contracts live only in the migration package; production fails closed on non-4.0

- Legacy schema knowledge (notebook v3, page-journal v2/v3, legacy
  candidate seeds) exists **only** under
  `src/migrations/discovery_v4/legacy_contracts/`
  (`notebook_v3.py`, `page_journal_v3.py`, `candidate.py`).
- Production `src/discovery/**` contains no schema 1/2/3 parsing: no legacy
  sentinels, no old-schema whitelists, no compatibility re-exports.  Any
  non-`"4.0"` schema met by production code is a hard, typed failure
  (fail closed), never a silent fallback.
- The migration layer converts legacy input into strict v4 artifacts; the
  production validators then validate the **converted** artifacts.

## 5. Migration-repair invariants (for future verifiers and audits)

These invariants held at every point of the repair and must continue to
hold:

1. **No production validators on legacy input.**  The migration layer never
   runs production v4 validators against legacy bytes; validators act only
   on the converted v4 artifacts.
2. **Smoke is side-effect free.**  The smoke run executes against an
   ephemeral full clone of the staging workspace (lock files stripped,
   paper_raw/papers/ledger targets isolated inside the clone), and the
   staging tree hash before the run must equal the hash after it.  Any
   drift blocks cutover.
3. **Candidate conservation.**  For candidate extraction:
   `candidates_observed == invalid_doi + already_existing +
   duplicate_seeds + imported + terminal + quarantined + unresolved`.
   Every unresolved candidate must land in the quarantine JSONL with full
   evidence; if the quarantine write fails or conservation does not hold,
   the step blocks and the journal does not advance.
4. **Maintenance lock discipline.**  `--apply`, `--resume`, `--cutover`,
   and `--rollback` all run under the global migration maintenance lock.
   The production discovery writer
   (`scripts/discover_papers_concurrent.py`) checks the maintenance lock at
   startup and fails closed while a migration window is active.
5. **No legacy residue in production.**  `src/discovery/**` carries no
   legacy sentinel values, no old-schema whitelists, and no compatibility
   re-exports; retired alias modules stay deleted.

## 6. Consequences

- Positive: the pending store's role is unambiguous — one writer, one
  drainer, defined removal — so no second candidate pipeline can accrete.
- Positive: the post-cutover chain now proves the migration carry-over was
  actually staged into `paper_raw` (drained store) before the legacy
  archive and the transitional directory are deleted.
- Negative: `--post-cutover-validate` can no longer pass immediately after
  cutover when legacy candidates were imported; a real discovery drain run
  must happen first.  This is intentional: validation now certifies the
  end state, not the intermediate one.
- Neutral: `pending_candidates/` may be absent from a cleaned active
  generation; tooling must not assume the directory exists (use the store,
  which tolerates its absence).

## 7. Related documents

- `docs/ADR_DISCOVERY_V4_SINGLE_STACK.md`
- `docs/ADR_DISCOVERY_STAGING_BOUNDARIES.md`
- `scripts/migrate_discovery_v4.py`
- `src/discovery/workspace.py` (`resolve_active`, `verify_dirs`)
- `src/discovery/pending_queue.py` (`_drain_pending_store_candidates`)
- `src/migrations/discovery_v4/legacy_contracts/`

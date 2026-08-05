# Project status

## Post-refactor review sweep (2026-07-27)

A three-way read-only audit of the consolidation series (structural diffs,
ingest/mineru safety paths, cross-cutting integrity) found the moves themselves
faithful — zero stale module references, decomposed functions line-for-line
equivalent, CLI surfaces byte-compatible — and concentrated the real defects in
the newly written write-job scripts. Fixed in this sweep:

- `check_write_planning_docs.py` was fail-open: an empty `selected_catalog.json`
  paper pool skipped every deep check (references.bib presence, matrix rows,
  bib_key resolution, evidence-in-pool, `results_plan.status == planned`) and
  still exited 0. The pool is now an error in its own right, all early exits
  persist their report, malformed JSON becomes an error instead of a traceback,
  and `input/research_input.md` is scanned with the full `TODO_MARKERS` set plus
  the same length floor as every other intermediate.
- `export_write_job_bib.py` silently ignored unknown `--paper-numbers` and
  silently picked the first of several `*.metadata.json`. Both now fail closed;
  `references.bib` is written atomically.
- `src/discovery/drain_locks.py` used `Any` without importing it (latent
  `NameError` under any runtime annotation evaluation); the dead
  `DISCOVERY_LOCK_TIMEOUT` copy left in `pending_queue.py` after the split is
  gone, along with 14 unused imports there.
- Process enumeration no longer depends on `wmic` alone (absent from default
  Windows 11 24H2+ installs): the probe falls back to `pwsh Get-CimInstance
  Win32_Process` and raises `ProcessProbeError` when no backend works, so an
  unverifiable PID is refused instead of being treated as stale — the pid file
  survives and nothing gets killed on an unproven identity.
- Layering guard blind spots closed: relative imports, `from src import X`, and
  string imports (`__import__`/`importlib.import_module`) are now visible;
  `ROOT_LEAVES` pins root membership so a new `src/*.py` cannot inherit
  import-anything rights; sanctioned seams are covered by the no-root invariant;
  the duplicated `SANCTIONED_LATE` entry that made its lazy-only constraint
  unenforceable was removed; every src subpackage must carry `__init__.py`.
- `src/staging/` and `config/` were missing `__init__.py`, so setuptools'
  `find` would have dropped them from any wheel; both now have one.
- Single-sourcing finished rather than half-done: five byte-identical
  canonical-JSON encoders now call `src/utils/canonical_json.py`, and the
  same-shape inline timestamps call `now_iso()`. Two sites keep a written
  do-not-unify reason — `src/mineru/lock.py` (naive by necessity: lock age is
  computed against a naive `datetime.now()`) and `scripts/test_runtime_workspace.py`
  (standalone test infrastructure that must import without `src`).
- `resume_commit` phase helpers take a frozen `_ResumeContext` instead of
  eleven threaded keyword arguments, and an unknown journal phase now raises
  instead of spinning the resume loop forever.

Two record-keeping corrections to the commit history: the `_REFERENCES_HEADING_RE`
definition added in 38478e0 was a real behavior fix, not part of the "pure
split" — the pre-split code referenced an undefined name and raised `NameError`
on every PDF DOI scan when PyMuPDF was installed. And the MinerU `except`
narrowing that a04da78 claims happened in 95edec4, its parent.

## Architecture consolidation + writing skills (2026-07-27)

Full-scope structural cleanup executed as gated stages on `main`, each stage
passing the fast acceptance gate:

- Package homes finalized: MinerU runtime modules form `src/mineru/`
  (converter/cleaner/runtime/lock/service_manager/smoke), Path-A staging forms
  `src/staging/` (network metadata staging + canonicalization), and
  `src/services/` is fully dissolved (admin → `src/ingest/`, paper_library →
  `src/catalog_folders/`, repository_hygiene → `src/utils/`, bib →
  `src/writer/`). Root leaves (naming, path_utils, file_fingerprint,
  logging_setup) joined `src/utils/`; `src/` root now holds only `server.py`
  and `prompt_builder.py`.
- The layering guard (`tests/hygiene/test_layering.py`) is strict: the old
  bidirectional `root` wildcard is gone, function-body (lazy) imports are
  scanned, every `ALLOWED` edge must point strictly downward, and all cycles
  exist only as reasoned `SANCTIONED`/`SANCTIONED_LATE` seams. Nothing may
  import root.
- Single-sourcing extended: canonical-JSON hashing (`src/utils/canonical_json.py`,
  Family-A sites only — persisted non-canonical encodings carry explicit
  do-not-unify comments), process-liveness (`src/utils/process.py`), placeholder
  markers (`src/writer/safe_write.TODO_MARKERS`), and six same-shape inline
  timestamp implementations.
- The seven largest functions were decomposed behavior-preserving
  (coordinator batch run, both pending-queue drains, resume_commit per journal
  phase, relevance-profile planning, service start, batch-convert main), and
  script-embedded engines sank into `src/` (reset-state audit →
  `src/discovery/audits/`, conversion gates → `src/ingest/conversion_gates.py`,
  fetch candidate policy → `src/fetch/access_policy.py`, rollback target
  discovery → `src/ingest/rollback.py`) with byte-compatible CLI surfaces.
- Change governance now lives in Chinese under `md/` (indexed by
  `md/README.md`, roadmap in `md/06_refactor_roadmap.md`); the hygiene suite
  scans `md/` like `docs/` and the snapshot packs it.
- Two writing skills landed on the write-job pipeline:
  `catalog_review_writer` (topic review + research gaps + directions) and
  `catalog_research_proposal_writer` (review → methods → planned
  results/data-analysis), with `create_write_job.py --workflow`,
  `export_write_job_bib.py`, `check_write_planning_docs.py`, JSON-schema'd
  plans, and companion hygiene tests. Citations remain Metadata-only.

## Productionization (2026-07-26)

Single-day production hardening pass, executed as nine gated stages on
`main` (local and both remotes carry only `main`):

- Acceptance is parallel by default: the fast gate runs its 9 groups as
  concurrent isolated subprocesses (~52s wall, previously ~3m) and `--full`
  runs a pytest-xdist chunk plus a sequential process/slow residue. Routine
  changes require the fast gate only; `--full` gates releases and refactors.
- ~17k lines of finished-mission code removed (v4 migration toolchain after
  finalization, one-shot repair scripts, the duplicate Gradio UI, orphan
  configs) with reintroduction tombstones in the verifier and contract tests.
- Single-sourced utilities (`src/utils/`: identifiers, timestamps, jsonio,
  atomic_io, fs, rate_limit); timezone-aware persistence is the contract
  (the writer-family naive stamps were converted in the 2026-07-27 review
  sweep; the two remaining naive sites carry a written reason).
- Layered packages enforced by `tests/hygiene/test_layering.py` (see
  `docs/ARCHITECTURE.md`); the four historical import cycles are gone; the
  paper_raw write lock has a single ranked acquisition point; the resolver's
  double rate-limit wait bug is fixed (one pacing layer per request).
- `config/settings.py` imports are side-effect free; operational scripts
  initialize through `scripts/_bootstrap`; logging is one loguru sink; the
  project carries `pyproject.toml` + split runtime/dev requirements.

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
-> citation-ready Metadata + evidence-tiered PDF identity match receipt (v2)
-> standalone freeze phase (freeze_paper_raw_metadata.py --all-eligible)
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

## PDF identity v2 migration (2026-07-31)

Receipt schema 2.0 replaced the flat-set DOI matcher with an evidence-tiered
decision (`identity_match.py`): structured DoiEvidence (XMP / Document Info /
first page / front matter / body / reference list / raw bytes), split
match/conflict title thresholds (0.85 / 0.60), four-state bibliographic
strength, and the automatic/final decision model for manual confirmation.
The 1067 PDFs in `data/paper_raw` were migrated transactionally
(`rematch_paper_raw_pdf_identity.py`: plan → receipts-only → freeze-eligible,
journaled, abortable, idempotent); the maintenance marker closed all other
paper_raw writers between phases, and the legacy `mismatch` metadata state
was readable only by the migration tool during the window (removed
afterwards).

### Final strategy audit (2026-07-31, second pass)

A read-only corpus audit of the 585 matched papers showed 405 resting on
year-only or author-only corroboration (title similarities as low as
0.10-0.28, no author overlap).  The strong-evidence rule was tightened to
the contract form — labeled first-page DOI → strong only on
`title match` OR `reliable author overlap AND compatible year` — and the
corpus was re-migrated transactionally: matched 585 → 180, ambiguous 224 →
629, unverifiable 258 (unchanged), identifier_conflict 0 (unchanged);
179 freezes rebuilt, 179/179 closures verified, zero dangling freezes.
The 405 downgraded papers keep their labeled-DOI + year evidence in the
receipts and are the manual-review list (`confirm_paper_raw_pdf_identity.py`).

### Corpus-wide PDF fetch sweep (2026-08-01)

The remaining 2643 no-PDF workspaces were attempted to 100% coverage
(every workspace now has either an attached PDF or a `fetch_result.json`
sidecar).  Two fetch-path defects found and fixed during the sweep:

- `semantic_scholar` was removed from the `auto` resolver chain: the API is
  unreachable from this egress (ProviderPermanentError on every lookup),
  yet each paper still paid ~9 serialized 3s-paced lookups/retries for it,
  capping the whole batch at ~2 papers/min.  After removal the batch runs
  ~5x faster (11-14 papers/min); `--resolver oa` keeps the full OA list.
- Mid-read connection breaks (urllib3 `InvalidChunkLength` on mangled
  chunked streams — 108 papers, 73 of them AMS) escaped the transport and
  landing-page resolver as bare exceptions, killing the paper with no
  attempt records.  Root cause: `ChunkedEncodingError` is NOT a
  `ConnectionError` subclass in requests 2.34 (MRO: RequestException →
  OSError).  The transport prefix read, `limit_content`, and the
  header_based/TDM call sites now type the break (`connection_broken` /
  ValueError) so the direct attempt falls back through the proxy and every
  failure records properly.

Outcome: 14 new PDFs (2 matched via `doi_exact` — 0200/header_based and
0857/original_link — 1 ambiguous, 11 unverifiable), 180 frozen (+1 new,
closure verified), 36 duplicate-PDF guard hits, 0 escapes in the final
pass.  The 2 newly matched papers 0857 (techreport) and 1377 (phdthesis)
are blocked from freezing on citation-readiness (institution/publisher
missing) until their Metadata is repaired.  Fast gate green
(562 files, runtime-zero snapshot, no pollution).

### Citation-readiness repair for 0857/1377 (2026-08-01)

The two matched-but-unfreezable papers were repaired as an operator
intervention: their OpenAlex records genuinely lack venue data (both
`primary_location` are null — re-resolution via the canonical resolver
lands on `manual_review`, never auto-apply), so the missing
`container.publisher`/`container.institution` (Illinois Center for
Transportation; San José State University) and the missing
`source_records/metadata_source.openalex.json` provenance file were
written under the paper_raw write lock with schema validation, then the
match receipts were rebuilt (metadata hash changed → the closure requires
a fresh receipt; same evidence, `doi_exact` preserved) and both papers
froze.  Frozen 180 → 182; closures verified (source record now inside the
hash closure).

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
read-only. Legacy v3 notebook recovery tooling was retired after the migration
finalized; v3 inputs are rejected by production before provider I/O.

## Catalog status

- Pending: 0
- Missing decisions: 0
- Stale decisions: 0
- Unapplied results: 0
- Classification complete: true
- Writer category safe: true

## Notebook v4 delivery status

The five production notebooks are enabled, schema v4, bilingual-ready, and
were migrated from v3 by the one-time v4 migration toolchain (finalized
2026-07-25 and since removed from the working tree; git history preserves
it).  All cursors, exhausted states, and generation counters were reset on
migration.  Provider page journals use the strict v4 schema (`"4.0"`) with
complete lane key, response metadata, and exhaustion evidence records.

The active v4 workspace lives under `data/discovery/generations/<id>/` and is
activated by `data/discovery/active_generation.json`.  Legacy flat directories
were retired to `data/discovery/legacy_retained/<id>/`;
`data/discovery/keyword_notebooks` and `data/discovery/pending_pages` are
tombstone files.

## Test suite status

- **Tests collected:** run `pytest --collect-only -q` for the current count
  (2026-07-26: ~2,700; see `docs/TESTING.md` for verified gate timings)
- **Acceptance:** pre-flight clean, syntax gate passed, runtime-zero snapshot
  verified; fast gate runs its groups in parallel (~52s wall)
- **Snapshot:** runtime-zero, 0 runtime files, secret scan passed

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

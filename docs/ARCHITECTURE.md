# Architecture

## Package layering (enforced by tests/hygiene/test_layering.py)

`src/` packages import strictly downward — module-level and function-body
imports are both scanned; cycles exist only as sanctioned per-module seams:

```text
utils                     leaf: atomic_io, identifiers, timestamps, jsonio,
                          fs, rate_limit, naming, path_utils, file_fingerprint,
                          logging_setup, process, canonical_json,
                          repository_hygiene — no src imports at all
mineru                    MinerU runtime: converter, cleaner, runtime, lock,
                          service_manager, smoke
metadata                  schema v2.0, freeze, citation, source_records,
                          quality, pdf identity/match; identity_match.py is
                          the evidence-tiered decision policy (DoiEvidence
                          tiers, four-state bibliographic strength, version
                          families, automatic/final manual model)
catalog ~ workspace       Catalog v3.2 schema/task/freeze + asset_refs;
                          workspace facts (evidence/readiness/lifecycle/receipt)
library                   PaperNumberLedger + formal_publication + marker
                          (declared 2-cycle with workspace, sanctioned)
ingest                    allocator, conversion, commit/rollback,
                          transaction_paths, import_status (sole writer
                          facade), stage/asset manifests, duplicate guard,
                          conversion gates, admin, locking (ranked;
                          paper_raw_write_lock helper)
catalog_folders           category registry/links/reader/doctor + paper_library
fetch                     PDF resolvers + transport + fetch_result_record
discovery                 notebooks/journals/lanes/providers/drain/staging
metadata_resolve          Path-B resolver package (scoring/evidence/apply)
staging                   Path-A app layer: network metadata staging +
                          canonicalization
writer                    write-job pipeline + bib
root                      src/server.py + src/prompt_builder.py only; may
                          import anything, nothing may import root
scripts                   entry points (init via scripts/_bootstrap)
```

The sanctioned cross-edges (candidate value object, unified ProviderClient
HTTP mandate, staging gateway) are enumerated with reasons in the layering
guard test — extend them consciously, never casually.

## Domain ownership

- `paper_number` is the permanent 16-digit identity managed by the ledger.
- Metadata v2.0 is the immutable bibliographic record and only citation source.
- Catalog v3.2 is each paper's independent content dossier and owns `paper_name`.
- `paper_raw` is the numeric mutable pre-commit workspace.
- `papers` contains complete immutable formal assets.
- `catalog` is a folder-backed browsing view, never a merged document.

## Formal identity and classification

The live formal registry verifies active ledger rows against every formal
directory, marker, Metadata, Catalog and asset manifest. The durable
`data/papers/.formal_publication_state.json` sidecar binds the active set and
all formal identity/content hashes; a missing or drifted sidecar is a
fail-closed repair condition. DOI keyword notebooks contribute stable Chinese
category definitions.
An LLM reads one paper Catalog and records an independent positive or negative
decision for every active category.

Every active discovery notebook uses schema v4: `keyword_zh` is its sole
Chinese classification identity, while `search_queries` contains the enabled
Chinese and English strings sent to OpenAlex/Crossref. Discovery runs both
languages and stores refresh/backfill progress independently per query and
provider. Search queries never become categories, directory names, or
classification-task inputs, and query changes do not alter the category
definition hash.

An enabled notebook is always bilingual-ready. Notebook definition mutations
are rejected if they would leave an enabled notebook unready; new incomplete
notebooks are disabled drafts until an explicit readiness-checked enable. The
strict discovery audit performs only validation and cross-identity checks.
Discovery recovery is inspect-only in v3 and emits a plan with generation,
request-signature, cursor-chain, and source-hash evidence; it has no write
entry point.

The five current production notebooks are the only real migration scope.
Migration mappings and fixed plans are operator/runtime evidence under the
ignored local state area and are excluded from source snapshots; a safe example
mapping may remain under `migrations/` for tests and documentation. The
read-only discovery dry-run reports each active query/provider lane's current
generation and request signature plus its execution page limits, worker count,
and page budget, and never performs provider I/O or advances notebook state.

`data/catalog/all/` links every active paper. `_pending/` links papers with
missing or stale decisions. Chinese category folders contain controlled links
to complete formal directories and may overlap. Assignment files preserve both
positive and negative decisions; Catalog content is never rewritten.

## Transactions

Commit installs and validates hidden staging, activates the ledger, requests
category reconciliation, then removes numeric raw data. Ledger activation is
the formal commit point. Classification does not run inside the transaction;
a failure leaves a repairable `DIRTY` marker and never rolls back a valid paper.
Rollback restores numeric raw assets, reserves the ledger entry, reconciles all
category links, removes the assignment and then deletes quarantine.

Category updates hold one category lock, create `DIRTY`, reconcile from the
ledger, formal directories, registry and assignments, validate, then clear the
marker. Writers fail closed while dirty and read Catalogs in batches of 10–20.

For the manual PDF path, stage the PDF, convert it first, then resolve Metadata
from converted Markdown front matter. `data/raw/` is a queue; manual PDF staging uses
`stage_raw_pdfs_to_paper_raw.py --move --apply` before conversion. Network ingest may stage Metadata before fetching
and converting the PDF.

## Discovery staging control plane

The production call graph is `pending_queue → stage_network_metadata_records
→ DiscoveryStageTransaction.stage_candidate → WorkspaceRegistry`. The ledger
owns only numbering/lifecycle; Evidence reads facts once; Readiness accepts
only an explicit known workflow profile. Registry is the only DOI/identity
scanner and publishes validated copy-on-write snapshots. Transaction owns the
raw write-lock decision sequence. Allocator and pending queue contain no
discovery reconciliation, and the old index disk-refresh paths are deleted.

Registry keeps incomplete `reserved` workspaces unsettled and rejects an
incomplete `metadata_staged` closure. Schema-incomplete reserved Metadata is
kept in the repair backlog without blocking a complete Registry; structural
JSON/path/identity damage still fails closed. Identity entities are keyed by
`identity_key + paper_number`, permitting multiple provider identities per
paper. Transaction loads one locked ledger view per lock epoch of at most 16
candidates, uses it for one atomic pre-refresh, persists reservation and final transition as separate
checkpoints, and directly publishes success without a post-refresh.

Registry snapshots are caches rather than durable facts. A discovery batch
performs one full publication-sidecar/hash-closure validation; each staging
lock epoch then reads only the sidecar revision/generation. A supported
commit/rollback generation change triggers a full formal reload before the
next candidate decision. Unsupported direct disk edits are found by the next
batch-boundary or explicit periodic audit, while a DOI/identity hit always
gets targeted live revalidation. Refresh compares the in-memory ledger
lifecycle projection and target-rescans only numbers whose state, folder, or
expected scope changed. Atomic record replacement removes old formal
DOI/identity refs after rollback. Reporting distinguishes a new allocation
(`actual_allocated`) from an existing-workspace reuse (`reused_existing`).
# Discovery batch runtime

Relevance is a notebook-local gate orthogonal to candidate lifecycle. Enabled
notebooks require a taxonomy-resolved non-sentinel profile before provider I/O.
Profile transactions identify stale page observations by keyword, request and
candidate profile hashes plus query/provider/lane identity; provider cursor
generation is not a relevance identity. A single lifecycle classifier permits
closure only before staging, blocks recovery-required states, and never rewrites
completed terminal history. Phase A validates exact transformed bytes without
creating a journal; Phase B publishes pages and Phase C commits notebooks last.
Discovery scans the durable transaction root after taking the shared lock, so a
crash-left `applying` journal remains a provider-I/O barrier. The drain projection
is built with one immutable full active-profile mapping, and the locked claim
independently checks that mapping.

Discovery uses one context/full Registry build and one journal scan per batch.
The Registry indexes `metadata_staged` raw workspaces and valid active formal
papers, reloads the formal view on publication-generation change, refreshes only
new/lifecycle-dirty numbers, and live-revalidates matched records. Repair backlog
is not a per-candidate scan source. Provider producers and the single staging
consumer overlap through a candidate-weighted bounded queue. `JournalDrainIndex`
has its own short critical sections, so providers can publish a page while the
consumer stages earlier work. Shutdown has a configurable no-progress watchdog,
not a fixed total-duration deadline; page and staging mutations are batched.
Retryable candidates remain claimable in this runtime or enter its delayed
schedule. Applied repair probes use a durable round-robin cursor. Formal
generation is the active-ledger publication revision; committed formal assets
are immutable and their Metadata hash is checked against the asset manifest.

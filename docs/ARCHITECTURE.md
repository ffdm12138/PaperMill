# Architecture

## Domain ownership

- `paper_number` is the permanent 16-digit identity managed by the ledger.
- Metadata v2.0 is the immutable bibliographic record and only citation source.
- Catalog v3.2 is each paper's independent content dossier and owns `paper_name`.
- `paper_raw` is the numeric mutable pre-commit workspace.
- `papers` contains complete immutable formal assets.
- `catalog` is a folder-backed browsing view, never a merged document.

## Formal identity and classification

The live formal registry verifies active ledger rows against every formal
directory, marker, Metadata, Catalog and asset manifest. It persists no path
map. DOI keyword notebooks contribute stable Chinese category definitions.
An LLM reads one paper Catalog and records an independent positive or negative
decision for every active category.

Every active discovery notebook uses schema v3: `keyword_zh` is its sole
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

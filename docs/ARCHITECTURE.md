# Architecture

## Domain ownership

- `paper_number` is the permanent 16-digit identity managed by the ledger.
- Metadata v2.0 is the immutable bibliographic record and only citation source.
- Catalog v3.2 is each paper's independent content dossier and owns `paper_id`.
- `paper_raw` is the numeric mutable pre-commit workspace.
- `papers` contains complete immutable formal assets.
- `catalog` is a folder-backed browsing view, never a merged document.

## Formal identity and classification

The live formal registry verifies active ledger rows against every formal
directory, marker, Metadata, Catalog and asset manifest. It persists no path
map. DOI keyword notebooks contribute stable Chinese category definitions.
An LLM reads one paper Catalog and records an independent positive or negative
decision for every active category.

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
from converted front matter. Network ingest may stage Metadata before fetching
and converting the PDF.

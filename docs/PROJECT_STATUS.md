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
renames copied staging assets to `paper_id`.

Only Metadata v2.0 and Catalog v3.2 are supported. Older Metadata, Catalog, and
non-numeric workspace layouts must be regenerated outside the active pipeline;
the repository contains no runtime migration path.

Each formal paper owns its Catalog. DOI-notebook Chinese keywords define the
available categories; an LLM reads each independent Catalog and records positive
or negative decisions. Category members link to complete formal directories.
Writing browses category folders and citations still come only from Metadata.
Default and source snapshots are runtime-zero.

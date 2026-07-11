# Project contract

1. Metadata v2.0 contains no `paper_id`, LLM content, or match state. It must
   independently generate CSL-JSON, BibTeX, and styled references.
2. Journal articles require valid DOI; conference/chapter/thesis/report records
   require their type-specific stable identifier or URL.
3. PDF matching writes an independent receipt and never edits Metadata. DOI
   conflict cannot fall back to title matching or manual override.
4. Freeze validates schema, citation artifacts, PDF/match hashes, provider
   provenance, raw records, year, and first author. Normal services cannot edit
   any frozen closure asset.
5. Catalog v3.2 is the only active Catalog. It stores complete content
   understanding, trusted abstract provenance, logical evidence references, and
   `paper_id`, but no authoritative bibliographic record.
6. `paper_id` equals the frozen Metadata year/author prefix plus the LLM Chinese
   content title. Conflicts or path-budget failures require Catalog repair; no
   suffix, hash, number, truncation, or Python translation is allowed.
7. Every active raw workspace and main asset uses the 16-digit `paper_number`.
8. Conversion requires attached PDF; Catalog requires Metadata frozen and
   conversion complete; formalize requires Catalog frozen; commit requires a
   current formalization plan.
9. Formalize writes only a plan/status. Commit copies immutable bytes into
   hidden staging, renames there, validates, atomically installs, activates the
   ledger, publishes indexes, and deletes raw only after durable evidence.
10. Commit and rollback journals live outside data they delete. Their public
    coordinators automatically resume the sole active journal; conflicting or
    ambiguous transactions fail closed, and durable phase evidence is checked
    against the filesystem, ledger, and published index before each mutation.
11. Ledger activation plus a validated formal directory is the formal commit
    point. Catalog folders are repairable browsing state and writers fail closed
    when `DIRTY` exists or `all` differs from the active formal registry.
12. Writer citation output and citation keys read only Metadata. Catalog cannot
    substitute for missing Metadata.
13. Runtime data, secrets, local tool state (``.workbuddy/``, ``.reasonix/``),
    runtime reports (``data/cleanup_report.json``), and paper workspaces
    (``data/paper_raw/``, ``data/papers/``) never enter source snapshots.
    Audit fixtures must be synthetic and live under
    ``tests/fixtures/synthetic_library``. Source snapshot packaging follows a
    strict runtime-zero policy defined in
    ``src/services/repository_hygiene.py``.
14. Source-record provider names must be normalized through
    ``normalize_provider_slug()`` and validated by resolved containment before
    any filesystem write. Metadata ``raw_record_path`` must be a POSIX-relative
    path under ``source_records/``.
15. ``PaperLibrary`` is defined only at ``src.services.paper_library``. No
    compatibility wrapper or alternate import path should exist.
16. Formalization application entry is ``scripts/formalize_paper_raw.py``
    calling ``src.ingest.formalization`` directly — no facade layer between
    CLI and domain logic.
17. Only Metadata v2.0 and Catalog v3.2 are accepted. No schema conversion,
    old-layout reader, or compatibility fallback may be added to the active
    repository; unsupported inputs are regenerated outside this pipeline.

## 关键词 discovery 双通道契约

Refresh starts a new first-page scan; Backfill resumes the durable notebook
cursor. Provider pages are journaled before cursor CAS. Pending candidates use
leases and DOI/title-resolution locks, and the allocator's final duplicate gate
remains authoritative.

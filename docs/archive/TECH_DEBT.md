# Technical debt

The retired `src/services/v2_library.py`, V2 commit service, AllCatalogBuilder,
old Catalog validators, formalize rename/repoint path, non-numeric active raw
path, and Metadata/Catalog double writes have been removed.

Remaining work is operational, not architectural:

- Unsupported historical Metadata, Catalog, and workspace layouts must be
  regenerated outside this repository; no runtime conversion path is present.
- Completed transaction journals are retained for audit. Long-term archive or
  compression is admin-only and intentionally outside normal ingest.
- Historical frozen tags and audit documents may describe retired behavior;
  active code and active docs must not import or recommend it.

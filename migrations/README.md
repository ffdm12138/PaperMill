# One-time state migrations

This directory documents explicit, operator-controlled migrations that are
outside the active runtime path.  Active readers never perform implicit
migration or compatibility fallback.

The current one-time migration is the discovery keyword notebook v3
transaction implemented by `scripts/migrate_keyword_notebooks_v3.py` and
`src/discovery/notebook_v3_migration.py`.  It inventories and validates legacy
notebooks, writes through a durable external journal, and requires explicit
operator authorization for apply or recovery.

Metadata schema v2.0 and Catalog schema v3.2 are the only supported manuscript
schemas.  This directory must not contain runtime readers or migrations for
older Metadata, Catalog, workspace, or formal-paper layouts; unsupported
artifacts are regenerated outside the active pipeline.

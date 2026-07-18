# One-time state migrations

This directory documents explicit, operator-controlled migrations that are
outside the active runtime path.  Active readers never perform implicit
migration or compatibility fallback.

Metadata schema v2.0 and Catalog schema v3.2 are the only supported manuscript
schemas.  This directory must not contain runtime readers or migrations for
older Metadata, Catalog, workspace, or formal-paper layouts; unsupported
artifacts are regenerated outside the active pipeline.

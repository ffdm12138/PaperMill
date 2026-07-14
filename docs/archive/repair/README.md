# Catalog Repair Archive — Historical Summary

This directory documents the historical catalog-repair effort that was
performed before the Notebook v3 migration. The detailed per-paper JSON
mapping files have been moved to `.local/archive/repair/` (never committed
or packed) because they contain real paper names, author lists, and
per-paper directory-mapping decisions.

## What was repaired

Three categories of structural issues were addressed across approximately
80 paper-workspaces:

1. **Asset-manifest drift** — Stale `asset_manifest.json` entries referencing
   files that had been rotated or renamed outside the manifest's knowledge.
   Fixed by reconciling against the actual filesystem and regenerating the
   manifest.

2. **Stale-formal-asset** references — Formal paper directories whose
   `asset_manifest.json` listed paths that had been moved, renamed, or deleted
   in the corresponding `paper_raw` workspace.  Resolved by regenerating
   formal manifests from the source record.

3. **Catalog-folder assignment mismatch** — A small number of papers whose
   `paper_name` or Catalog category did not match the actual content topic
   (e.g. a wind-blown-sand paper miscategorised under atmospheric boundary
   layer).  Resolved via manual review and re-assignment.

## Procedure

1. `scripts/doctor_catalog_folders.py` — detect stale/corrupted folder entries.
2. `repair_stale_formal_asset_manifests.py` — regenerate formal manifests.
3. Manual round-by-round mapping review using the JSON reports now stored in
   `.local/archive/repair/`.
4. `rebuild_catalog_folder_system.py` — apply corrected assignments.
   (This script has since been deleted; it is listed here only as a historical
   step and must not be executed.)

## Files moved to `.local/archive/repair/`

The following JSON files contained real paper-level mapping data and have
been relocated outside the source tree:

- `repair_final.json`
- `repair_mapping_after_enrich.json`
- `repair_mapping_full_dryrun.json`
- `repair_round2.json` through `repair_round5.json`
- `temp_authors.json`

## Verification

- Catalog folder integrity and writer category readiness both pass.
  - Broken links = 0
  - Escaping links = 0
  - Unknown directories = 0
  - Writer category safe = true
- The Notebook v3 migration was applied over the repaired state.
- Audit, recovery inspect, and discovery dry-run all pass as of the final
  delivery snapshot.

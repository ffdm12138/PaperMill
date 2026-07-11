# paper_raw_catalog_curator

Active contract: Catalog v3.2. Prepare the immutable task first with `scripts/prepare_paper_raw_catalog_task.py`, let the skill write the complete numeric-prefix Catalog, then validate/freeze with `scripts/validate_paper_raw_catalog.py` or `scripts/curate_paper_raw.py --apply`.

Metadata is read-only citation truth. Catalog is the full content-understanding archive and the source of `content_title_zh`/`paper_id`. Formalize only writes an installation plan; commit performs final renaming in hidden staging.

The authoritative shape is `catalog_schema.json`; `examples/example_catalog.json` demonstrates content only and uses placeholder provenance hashes that a real task must replace exactly.

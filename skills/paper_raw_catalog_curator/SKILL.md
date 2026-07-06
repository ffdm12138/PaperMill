# Paper Raw Catalog Curator Skill

Use this skill to curate one `data/paper_raw/<paper_number>/` workspace after MinerU conversion. It must generate a **catalog v3.1** content-only JSON file from Markdown, PDF-derived text, and images.

## Boundaries

- `metadata.json` is the bibliographic source of truth: DOI, authors, year, journal, venue, pages, identifiers, and BibTeX never belong in catalog.
- `catalog.json` is a content index for screening, evidence lookup, terminology, and writing reuse.
- Bibliographic keys are forbidden in catalog at every nesting level.
- Initial catalog generation must keep `screening.read_decision` as `"pending"`.
- Do not emit v3.0 fields: no top-level `paper_number`, `paper_id`, `asset_refs`, no `naming`, and no `content_notes`.
- If a score is not actually judged, use `null`; do not write default `5` values.
- Reject or remove fixture text such as `测试夹具` and `test_fixture`.
- `content_title_original_candidates` must never contain a paper-number-like value.

## Output

Write `<paper_number>.catalog.json` in the same workspace. The file must follow `catalog_schema.json` and include only these top-level groups:

`schema_version`, `library_locator`, `content_identity`, `classification`, `screening`, `research_card`, `writing_value`, `evidence_profile`, `figure_inventory`, `terminology`, `quality_control`, `provenance`.

## Required Content

- `library_locator`: `paper_number`, `paper_id`, `paper_dir`, and same-folder `asset_refs`.
- `content_identity`: Chinese content title, original title, source/confidence, language, document type.
- `classification`: primary domain, secondary domains, topic/method/phenomenon/material/model/application tags.
- `research_card`: problem, question, objective, study object, method, data/experiment, findings, mechanisms, limitations, usefulness.
- `writing_value`: summaries, possible writing uses, supported arguments, contrasts, best sections, citation context, open questions.
- `evidence_profile`: key claims, equations, figures, tables, quoted terms, section/page evidence.
- `figure_inventory`: extraction status plus image/figure items.
- `terminology`: Chinese-English technical terms.
- `quality_control`: completeness, missing fields, warnings, fixture flag, fallback flag.
- `provenance`: source paths, generated time, generator, generator version, hashes, generation mode, model if used.

## Naming Rule

Formalize derives `paper_id` from:

`metadata.year + metadata.first_author.family + catalog.content_identity.content_title_zh`

The curator does not rename folders and does not output final `paper_id`; it only supplies the Chinese content title used later by formalize.

## Validation

The generated catalog must pass `src.services.v2_library.validate_catalog_schema()`. A catalog is invalid if it contains bibliographic fields, v3.0 keys, fixture text, empty `classification.primary_domain`, empty `terminology`, empty `provenance.generated_at`, empty `provenance.generator`, or fake default screening scores.

---
name: paper_raw_catalog_curator
description: Read one frozen numeric paper_raw workspace and produce a complete Catalog v3.2 content archive and semantic paper_id.
---

# Paper Raw Catalog Curator v3.2

Use this skill only for `data/paper_raw/<16-digit paper_number>/` after both strict Metadata freeze and MinerU conversion are complete. The authoritative task is `<paper_number>.catalog_task.json`; do not infer paths or identity outside it.

## Read-only inputs

- frozen `<paper_number>.metadata.json` for bibliographic context and the fixed `paper_id_prefix` only;
- `<paper_number>.md`, `images/`, conversion manifest;
- trusted abstract candidates and hashes listed by the task envelope;
- `catalog_schema.json`.

Never write Metadata, match/freeze receipts, status, ledger, formalization, transactions, indexes, or `data/papers`.

## Output

Write exactly `<paper_number>.catalog.json`, schema `3.2`. It is a complete content-understanding archive, not a label set. It must contain:

- top-level `paper_number` and `paper_id`;
- Chinese content title, research domains, document language;
- trusted original abstract source when present, a Chinese synthesis, and a one-sentence summary;
- background, knowledge gap, research question and objectives;
- methods, data/study design, findings, mechanisms and limitations;
- terminology or a meaningful not-applicable reason;
- figures/tables with interpretations and machine-readable evidence refs;
- writing value and initial screening with `read_decision="pending"`;
- the exact task hashes and skill version in provenance.

`paper_id` must equal the task’s fixed `<year>_<first_author>_` prefix plus the model-written `content_title_zh`. Do not change the prefix, truncate the Chinese title, or add `_2`, paper numbers, or hashes.

## Abstract integrity

`abstract.source.origin` is one of `paper_explicit_abstract`, `provider_author_abstract`, `provider_unspecified_abstract`, or `not_found`. Copy source text only from a task candidate. If no trusted candidate exists, set `status=not_found`, `origin=not_found`, `text=null`, `source_ref=null`. Generated prose belongs only in `summary_zh` and `one_sentence_zh`.

## Evidence refs

Every evidence ref has exactly `asset`, `locator_type`, `locator`, `quote_hint`, `figure_label`, and `image_ref`. Use logical Markdown sections, figure/table labels, pages, or `images/...` paths. Never use absolute paths, `..`, raw/formal filenames, or invented sections.

## Forbidden output

Do not create structured citation fields anywhere in Catalog: `doi`, `authors`, `publication_year`, `journal`, `volume`, `issue`, `pages`, `publisher`, `bibtex`, `csl`, or `citation_key`. Natural-language discussion of dates, named researchers, datasets, regions, or theories remains valid paper content.

After output, Python performs JSON Schema validation, semantic/evidence validation, task closure replay, duplicate/path-budget checks, and Catalog freeze. If validation reports `catalog_repair_required`, revise only the Catalog output; never modify Metadata or task inputs.

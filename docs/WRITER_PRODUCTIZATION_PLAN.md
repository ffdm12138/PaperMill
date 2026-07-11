# Writer Productization Plan

## Summary

`ingest v2.2` has been frozen and tagged as `ingest-v2.2` (v2.1 was superseded
by the curate→formalize→commit state-machine refactor; schema unchanged). `writing v0.1`
has also been frozen and tagged as `writing-v0.1` after both the mechanical
writing loop and the quality sample passed.

This review does not propose changes to ingest code or to the catalog/metadata
schema. The next writer phase should productize the already validated
catalog-first writing path into stable, low-friction, diagnosable entrypoints.
It should not produce another sample article as its primary goal.

Implementation status: P0 entrypoints are implemented for writer v0.2. P1/P2
items remain planned only.

## 1. Current Writer v0.1 Capability Boundary

- `catalog-folder selection`: uses one or more Chinese category folders as a
  content-only screening index. Job preparation can select by `paper_number` or
  catalog filters.
- Initial generated catalog entries use `screening.read_decision = "pending"`.
  `must_read` / `maybe_read` / `skip` filters are for post-triage /
  writing-stage catalog annotations, not paper_raw catalog generation.
- `article copy workspace`: copies formal paper assets into
  `write/jobs/<job_id>/article/<paper_number>/`; writing reads from this
  job-local copy, not directly from formal paper storage.
- `metadata -> references.bib`: per-job bibliography is generated from copied
  metadata only. Catalog and Markdown are not bibliographic truth.
- `TeX generation`: creates `tex/main.tex`, `tex/references.bib`, and
  `tex/sections/*.tex` for the job.
- `compile/check`: `scripts/check_write_tex_project.py` checks project shape,
  BibTeX entries, restricted paths, citation consistency, image paths, and
  optional LaTeX compilation.
- `quality text check`: `scripts/check_write_quality_text.py` checks template
  wording, placeholder acceptance prose, bib-key usage, introduction/conclusion
  presence, and uncertainty/limitation coverage.
- `runtime job ignored`: `write/jobs/*` is local runtime state and is excluded
  from Git and snapshots except for `.gitkeep`.

## 2. Current Command-Chain Pain Points

- `prepare / write / check / quality check` are separate commands, so users must
  remember ordering and repeated arguments.
- `job_id`, `selected_catalog.json`, and 16-digit `paper_number` are powerful
  but still high-friction for new writing sessions.
- The mechanical writer creates scaffold/smoke output. The proven quality sample
  still depends on human or agent refinement using copied Markdown.
- `write/jobs/*` must not be committed, but there is not yet a clear policy for
  preserving or sharing job acceptance summaries.
- TeX must not read directly from `data/papers`; this is the correct boundary,
  but v0.2 needs clearer command guidance and error diagnostics.
- The repository currently contains both the older multi-stage `write_review.py`
  workflow and the newer catalog-to-TeX mini loop. v0.2 should first productize
  the validated mini loop rather than trying to merge both paths. Both already
  work under `write/jobs/<job_id>/` with job-local BibTeX/cite-key; the
  **recommended main entry is the catalog-to-TeX mini loop**
  (`scripts/create_write_job.py` → `write_catalog_tex_article.py` →
  `check_write_tex_project.py`), while the multi-stage `write_review.py` is kept
  as the lower-level / experimental entry. This is a product-boundary note, not
  legacy cleanup.

## 3. Writer v0.2 Candidate Features

### P0: `scripts/doctor_write_pipeline.py` (implemented)

Provide one diagnostic entrypoint for the writer environment and existing jobs.
It should check:

- the selected category folders exist, are clean, and contain valid links.
- Formal paper assets referenced by selected jobs are complete.
- `write/jobs/*` remains ignored and only `.gitkeep` is tracked.
- TeX compiler availability is detected.
- Required writer scripts exist.
- `check_write_tex_project.py` and `check_write_quality_text.py` can be invoked.
- If `--job-id` is supplied, summarize existing prepare/write/check/quality
  reports and classify the job as missing, scaffold-only, mechanically valid,
  or quality accepted.

### P0: `scripts/create_write_job.py` (implemented)

Provide one low-friction job creation entrypoint on top of the existing
catalog-first preparation flow.

- Accept either `--paper-numbers ...` or catalog filters such as
  `--domain`, `--read-decision`,
  and `--limit`.
  `--read-decision` is meaningful only after post-triage / writing-stage or
  human screening has annotated values beyond the initial `pending`.
- Run the existing prepare step and create the ignored job workspace.
- Generate a job-local README and selected-paper summary that explains the next
  commands.
- Do not write final prose or claim quality acceptance.

### P1: Job-Local Literature Matrix (planned)

Generate `write/jobs/<job_id>/reports/literature_matrix.md` from copied
catalog, copied metadata, and copied Markdown where needed.

Each selected paper should have one row covering:

- study object
- method/data
- key conclusion
- role in the review
- limitation/uncertainty
- key quantitative claims
- bib key

The matrix is a job-local writing aid. It must not update global catalog,
metadata, or formal paper assets.

### P1: Mechanism, Table, and Figure Outlines (planned)

Generate job-local outline drafts:

- `reports/mechanism_outline.md`
- `reports/table_outline.md`
- `reports/figure_outline.md`

These files should help move from selected literature to review structure.
They should be clearly marked as planning aids, not final scientific claims.

### P1: `scripts/check_write_job_acceptance.py` (planned)

Provide one acceptance command for an existing job.

- Run TeX project check.
- Run quality text check.
- Optionally compile via `--compile`.
- Write `reports/write_acceptance_report.json`.
- Return nonzero if any hard gate fails.

### P2: Stronger Quality Checks (planned)

Future quality gates can add:

- quantitative-claim traceability to copied Markdown
- stronger detection that every selected paper is substantively used
- mechanism-chain or problem-chain structure checks
- warnings for paper-by-paper list structure

These should remain deterministic checks, not an embedded LLM client.

## 4. Things Writer v0.2 Should Not Do

- Do not introduce RAG, embeddings, vector databases, or ChromaDB.
- Do not introduce an LLM client.
- Do not commit or pack `write/jobs/*` runtime products.
- Do not let TeX directly reference `data/papers`, `data/raw`,
  `data/paper_raw`, or other non-job-local data paths.
- Do not modify ingest code.
- Do not modify catalog or metadata schema.
- Do not turn writer v0.2 into an automatic paper-writing system. Its job is to
  reduce setup, diagnosis, report generation, and acceptance-check friction.

## 5. Recommended Priority

- P0: `doctor_write_pipeline.py` to unify environment and existing-job checks.
- P0: `create_write_job.py` to lower the startup barrier for catalog-first jobs.
- P1: `literature_matrix.md` generation to formalize the five-element review
  extraction.
- P1: mechanism/table/figure outlines to bridge the matrix and article plan.
- P2: stronger deterministic quality checks after v0.2 entrypoints stabilize.

## 6. Acceptance Method

- Do not mutate real ingest data or formal paper assets.
- Do not change catalog or metadata schema.
- Use ignored `write/jobs/<job_id>/` runtime workspaces for job-level outputs.
- Tests must use fixtures or mocks and must not access the real network.
- `pytest -q` must pass.
- `python scripts/pack_repo.py` must produce a clean snapshot without real data
  or write job runtime products.
- `git status` should be clean after commit, and `git ls-files write/jobs`
  should list only `.gitkeep`.

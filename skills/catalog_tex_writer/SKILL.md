---
name: catalog_tex_writer
description: Write a small TeX article from a prepared MinerU v2 write/jobs article workspace using selected_catalog.json, copied catalog files, and metadata-derived BibTeX only.
---

# Catalog TeX Writer

Use this skill when writing from `write/jobs/<job_id>/` after
`scripts/prepare_write_article_workdir.py` has copied formal papers into the
job-local `article/` directory.

## Required Reading Order

权威边界见 `docs/PROJECT_CONTRACT.md`；本 skill 不与之冲突。

1. Read `write/jobs/<job_id>/selected_catalog.json` first.
2. For each selected `paper_number`, read
   `write/jobs/<job_id>/article/<paper_number>/<paper_name>.catalog.json`.
3. Read `write/jobs/<job_id>/article/<paper_number>/<paper_name>.metadata.json`
   for citation facts and BibTeX.
4. Read copied Markdown only when a claim needs full-text confirmation.

Do not read `data/papers` directly. The job-local `article/` copy is the only
paper source for this writing pass.
Do not read `data/paper_raw`, `data/raw`, or non-job-local data paths directly.

## Writing Rules

- Output TeX only under `write/jobs/<job_id>/tex/`.
- Generate `references.bib` only from metadata, preferably through
  `bibtex_from_metadata()` or `src.writer.bib.bibtex_for_entry()`.
- Every `\cite{}` key in TeX must exist in `references.bib`.
- Every `references.bib` entry must have a DOI. If evidence is insufficient,
  write a TODO or report the shortage instead of inventing facts.
- Do not guess DOI, authors, year, venue, journal, volume, issue, pages, or
  article number.
- Images may only be referenced from copied `article/<paper_number>/images/`
  or files copied into the job TeX project.
- Keep the mini article compact: Abstract, Introduction, Literature Basis, Key
  Methods or Mechanisms, Discussion, Conclusion; roughly 1000-2000 Chinese
  characters or equivalent English; at least three citations.

## Quality Acceptance

- Do not use template sentences such as `X指出：X`,
  `指出：...指出`, smoke-test prose, or acceptance-demo prose as article text.
- Before writing, extract five elements for every selected paper: study object,
  method/data, key conclusion, role in the review, and limitation/uncertainty.
- Organize the review around a problem chain or mechanism chain. Do not write a
  paper-by-paper list with one paragraph per citation.
- Every selected paper must be used substantively at least once in the body,
  not only parked in `references.bib`.
- Quantitative claims must preserve the number, unit, direction/magnitude, and
  uncertainty where available, and must be traceable to copied Markdown under
  `article/<paper_number>/`.
- `references.bib` remains metadata-derived only. Never create citation facts
  from catalog text, Markdown prose, or memory.
- TeX must not reference `data/papers`, `data/raw`, `data/paper_raw`, or
  non-job-local data paths directly.
- `write/jobs/*` runtime products are never committed.
- Run `scripts/check_write_quality_text.py --job-id <job_id>` after the article
  has been human/agent polished. A mechanical smoke article is not quality
  accepted until this check passes.

## Output Checklist

- `tex/main.tex`
- `tex/references.bib`
- `tex/sections/introduction.tex`
- `tex/sections/literature_basis.tex`
- `tex/sections/discussion.tex`
- `reports/write_article_report.json`
- Run `scripts/check_write_tex_project.py --job-id <job_id>` before handoff.
- Run `scripts/check_write_quality_text.py --job-id <job_id>` before quality
  acceptance.

# Claude Notes

This is the topic-review writing skill for prepared write jobs. It is:

- not the mini-article skill (`catalog_tex_writer`)
- not the research-proposal skill (`catalog_research_proposal_writer`)
- not an ingest, metadata resolver, catalog curator, or classifier skill
- not a legacy llm work workflow

Read only:

- `write/jobs/<job_id>/job.json`
- `write/jobs/<job_id>/selected_catalog.json`
- `write/jobs/<job_id>/article/<paper_number>/`
- `write/jobs/<job_id>/reports/` (own outputs, for re-runs)

Write only:

- `write/jobs/<job_id>/planning/`
- `write/jobs/<job_id>/reports/`
- `write/jobs/<job_id>/tex/`

Never touch `data/papers`, `data/paper_raw`, `data/raw`, `data/catalog`.
Never create `planning/selected_papers.json` or `planning/workset_manifest.json`
(JobManager-owned names). Citations only from job-local metadata via
`scripts/export_write_job_bib.py` / `src.writer.bib`. After writing, run
`scripts/check_write_planning_docs.py`, `scripts/check_write_tex_project.py`,
and `scripts/check_write_quality_text.py`.

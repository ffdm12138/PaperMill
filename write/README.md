# Write Workspace

`write/` is the local writing workspace for MinerU v2.

Committed files in this directory are only documentation and `.gitkeep`
placeholders. Runtime writing jobs belong under `write/jobs/<job_id>/` and are
ignored by git because they may contain copied PDFs, Markdown, images, TeX
outputs, and reports. The article workspace is
`write/jobs/<job_id>/article/<paper_number>/`.

Use (mini article):

```bash
python scripts/prepare_write_article_workdir.py --job-id demo --paper-numbers 0000000000000001 0000000000000002 0000000000000003 --apply
python scripts/write_catalog_tex_article.py --job-id demo --title "Mini Review" --language zh --apply
python scripts/check_write_tex_project.py --job-id demo --compile
```

Topic review (`catalog_review_writer` skill) and research proposal
(`catalog_research_proposal_writer` skill, 先射箭后画靶) share the same job
workspace via `create_write_job.py --workflow review|proposal`; a proposal job
additionally carries a user-authored `input/research_input.md`:

```bash
python scripts/create_write_job.py --workflow review --categories 风沙动力学 --limit 8
python scripts/export_write_job_bib.py --job-id <job_id>
python scripts/check_write_planning_docs.py --job-id <job_id>
python scripts/check_write_tex_project.py --job-id <job_id> --compile
python scripts/check_write_quality_text.py --job-id <job_id>
```

The writing workflow reads paper Catalogs through `data/catalog/<category>/`
category folders. Each member link points to a complete formal paper directory.
`selected_catalog.json` is a per-job working snapshot, not the global catalog.
TeX and BibTeX generation must use the copied `article/` metadata, not
direct `data/papers` paths.

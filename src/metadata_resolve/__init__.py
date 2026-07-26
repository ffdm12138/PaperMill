"""paper_raw metadata resolution package.

Single-responsibility submodules: ``names`` (conservative CJK/name helpers),
``evidence`` (local-evidence extraction), ``scoring`` (candidate scoring and
the auto-match gate), ``candidates`` (candidate/report dataclasses, patch and
candidate builders, duplicate-reason helpers), ``resolver`` (orchestrator and
network helpers), ``apply`` (metadata.json apply step), ``sidecars``
(side-file writers and ``.import_status.json`` status vocabulary),
``enrichment`` (DOI extraction + Crossref enrichment), ``markdown_extract``
(MinerU Markdown candidate extraction), and ``checkpoint`` (resume
checkpoint). Callers import submodules directly; this package intentionally
re-exports nothing.
"""

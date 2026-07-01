# paper_raw_metadata_resolver

This skill resolves metadata for a `data/paper_raw/<source_id>/` folder whose
metadata is unmatched or incomplete. It is the PDF-after-MinerU bibliographic
metadata resolver: it reads the **converted MinerU Markdown first**, extracts
DOI/title/authors/year/venue candidates, **verifies them online** (Crossref /
OpenAlex / Semantic Scholar / Unpaywall), and **searches online when the Markdown
lacks enough candidates**. When network lookup is unavailable it fails closed
(status stays `unmatched`, candidates preserved, manual review requested) — it
never fabricates matched metadata.

For title/author/affiliation/abstract/keyword/DOI candidates, read the converted
Markdown physical first 100 lines as front-matter evidence before PDF title
fallback. DOI extraction remains strict and must not use reference-list DOI
values as the paper DOI.

It is metadata-first, not catalog-first. It must NOT read `data/papers` or the
catalog, must NOT invent DOI/author/year/venue (an LLM-guessed DOI is invalid),
and must NOT set `metadata_match.status` — the final `matched`/`manual_confirmed`
status is assigned only by the existing validation / human-confirmation / commit
path. Output is a candidates file plus a patch that fills only empty fields; the
patch uses the **same metadata schema as network-fetched metadata** (an
`empty_metadata` subset merged via `merge_missing_metadata`) — there is no
simplified旁路 format. The patch is applied by
`scripts/resolve_paper_raw_metadata.py --apply`, which re-checks DOI validity,
duplicates, conflicts, and completeness.

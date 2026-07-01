# Third-Party Integration

Third-party tools may provide candidate metadata or PDF URLs, but formal import remains v2-only.

Accepted handoff formats:

- JSON or JSONL candidate metadata for `stage_network_metadata_to_paper_raw.py`.
- Local PDF files placed in `data/raw/` for `stage_raw_pdfs_to_paper_raw.py`.
- DOI or URL metadata consumed by `fetch_pdf_for_paper_raw.py`.

No integration may write directly to `data/papers/`.

Manual PDF metadata resolution still starts after MinerU conversion. Title/author/
affiliation/abstract/keyword/DOI candidates for search or review must prefer the
converted Markdown first 100 lines as front-matter evidence before any PDF title
fallback, and DOI requirements are unchanged. Large MinerU
PDF conversion has no process-level timeout; service startup/shutdown should use
`python scripts/start_mineru_services.py --wait` and
`python scripts/stop_mineru_services.py`.

# Literature Library Manager

Pure v2 paper_raw workflow only.

Use `data/raw/` or network metadata as input, stage into `data/paper_raw/`, then match, fetch or attach PDF, convert, curate, formalize, commit, rebuild all catalog and validate.

For batch conversion, use `python scripts/start_mineru_services.py --wait`,
`python scripts/convert_paper_raw_gpu.py --all --apply`, then
`python scripts/stop_mineru_services.py`. MinerU conversion has no process-level
timeout; title/author/affiliation/abstract/keyword/DOI metadata candidates come
from converted Markdown first 100 lines as front-matter evidence before PDF
title fallback.

Catalog natural-language values default to Chinese for Chinese search,
classification, paper selection, and writing workflows. JSON keys/schema enums
stay English, technical terms may remain English, and metadata remains the
original/canonical bibliographic source.

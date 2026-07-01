# PDF Resolver Design

PDF resolution is a helper for v2 `paper_raw` folders.

## Rules

- The default access policy is `oa_only`.
- Resolver results are downloaded into a caller-owned temporary folder.
- `fetch_pdf_for_paper_raw.py` attaches the downloaded PDF to `data/paper_raw/<paper_number>/<paper_number>.pdf`.
- Manual download hints must tell users to place files in `data/raw/` and run
  `stage_raw_pdfs_to_paper_raw.py --move --apply`; `data/raw/` is a queue and
  successful normal staging consumes the PDF from raw.
- No resolver may write directly to `data/papers/`.

## Flow

```text
metadata DOI
-> resolver chain
-> temporary PDF
-> PaperRawAllocator.attach_pdf()
-> paper_raw source folder
```

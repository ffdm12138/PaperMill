# PDF Resolver Integration

The resolver chain is integrated only through `fetch_pdf_for_paper_raw.py`.

## Resolver chain (auto mode)

1. original links from metadata
2. legal OA resolvers (unpaywall, openalex, semantic_scholar, arxiv, …)
3. publisher-specific resolvers (sciengine_direct for 10.1360/ DOIs, biorxiv, pmc_oa)
4. header_based DOI landing fallback — always runs, defaults to https://doi.org/{doi}
5. rich failure report

All downloads check initial and final URLs for unsafe hosts (sci-hub, libgen,
etc.). Every PDF written to the final target must be non-empty and start with
`%PDF` (streaming buffer in `_download_pdf`, probe-read in `_copy_pdf`,
`validate_pdf_bytes` in `_write_bytes_pdf`). Network calls use
`FETCH_PROXY` / `get_fetch_proxies()`.

## v2 Integration

1. Create metadata sources with `stage_network_metadata_to_paper_raw.py`.
2. Run `fetch_pdf_for_paper_raw.py --all --only-missing-pdf --apply`.
3. Run metadata match, conversion, curation and commit.

Resolvers return URLs or temporary local files. The v2 fetch CLI performs the final attachment into the matching `paper_raw` source folder.

Header-based fetch is a fallback for explicitly authorized sessions:

```bash
python scripts/fetch_pdf_for_paper_raw.py --all --only-missing-pdf \
  --resolver header-based \
  --url-template "https://authorized.example.edu/fetch?doi={doi}" \
  --header "Cookie: <paste manually>" \
  --apply
```

The resolver uses a fixed in-code User-Agent. Header values are never persisted;
reports and metadata store only masked header keys.

# PDF Resolver Design

PDF resolution is a helper for v2 `paper_raw` folders.

## Resolution order

The resolver chain is built per ``AccessPolicy`` from ``resolver_registry.py``.
Default ``--resolver auto`` execution order:

1. **original links** already present in metadata
   (``metadata.links.pdf_url`` / ``url`` / ``publisher_url`` / ``repository_url``)
2. **legal OA resolvers** (unpaywall, openalex, semantic_scholar, arxiv,
   publisher_oa, springer_direct, biorxiv, pmc_oa)
3. **publisher-specific resolvers** — e.g. ``sciengine_direct`` for
   ``10.1360/`` Science China DOIs, biorxiv, pmc_oa
4. **header_based DOI landing fallback** — always runs for any DOI,
   defaulting to ``https://doi.org/{doi}``.  ``--base-url`` and
   ``--url-template`` are optional overrides.
5. **rich failure report** — each resolver attempt records ``candidate_url``,
   ``final_url``, ``status_code``, ``content_type``, and ``reason``.

``--resolver oa`` uses steps 1-2 only; ``--resolver header-based`` uses only
the header_based resolver (defaulting to https://doi.org/{doi}).

## Unsafe host blocking

Every URL in the pipeline — initial request URL, redirect final URL
(``response.url`` after ``allow_redirects=True``), and any ``pdf_url``
extracted from HTML — is checked against ``url_safety.is_unsafe_url()``.
Permanently blocked host fragments: ``sci-hub``, ``libgen``, ``z-lib``,
``zlibrary``, ``annas-archive``. No CLI flag can override this blocking.

- ``_download_pdf`` checks the initial URL **and** the final URL after redirect.
- Every resolver (``original_link`` / ``header_based`` / TDM) checks the
  final URL of every ``requests.get(allow_redirects=True)`` call.
- Blocked URLs produce a ``FetchResult`` with an error message
  (``"unsafe source blocked"`` / ``"unsafe final URL blocked"``).

## PDF content validation

All PDFs written to the final target **must** be non-empty and start with
``%PDF`` magic bytes:

| Write path | Validation |
|---|---|
| ``_download_pdf()`` | Streaming 4-byte buffer across chunks; rejected if empty, non-``%PDF``, or incomplete. |
| ``_write_bytes_pdf()`` | ``validate_pdf_bytes(content)`` before writing. |
| ``_copy_pdf()`` | Probe-reads first 5 bytes via ``validate_pdf_bytes``; copies remainder after the check. |

## PDF Content Transport

PDF and landing-page content requests use `src/fetch/pdf_transport.py`.
The policy is direct-first with `requests.Session().trust_env = False`, followed
by one terminal explicit proxy attempt only for retryable direct failures.
Retryable direct failures are connect/read timeout, connection/DNS/TLS/SSL or
redirect transport errors, and HTTP 403, 408, 429, or 500-599. HTTP 451, 407,
401, 404, 410, unsafe URLs, invalid URLs, oversized bodies, and invalid PDF
content are terminal and do not trigger proxy fallback.

PDF transport configuration is PDF-specific:

- `MINERU_PDF_PROXY_URL` (default `http://127.0.0.1:7890`)
- `MINERU_PDF_DISABLE_PROXY_FALLBACK`
- `MINERU_PDF_DIRECT_TIMEOUT`
- `MINERU_PDF_PROXY_TIMEOUT`

Metadata/discovery API queries keep the existing `FETCH_PROXY` behavior through
`src.fetch.proxy.get_fetch_proxies()`. Do not move metadata APIs to
`pdf_transport.py`.

Fetch reports contain `transport_policy: "direct_then_proxy"` and sanitized
`transport_attempts`. Metadata JSON must not contain transport attempts, proxy
URLs, headers, cookies, authorization values, or TDM tokens.

## Rules

- The default access policy is `oa_only`.
- Resolver results are downloaded into a caller-owned temporary folder.
- `fetch_pdf_for_paper_raw.py` attaches the downloaded PDF to `data/paper_raw/<paper_number>/<paper_number>.pdf`.
- DOI comes only from `data/paper_raw/<paper_number>/<paper_number>.metadata.json`.
  The fetch flow does not read `doi.csv`, does not allocate a new paper_number,
  and does not name PDFs by title, DOI slug, or URL basename.
- `header_based` is explicit custom behavior (`--resolver header-based`), not part
  of `oa_only`. Its User-Agent is fixed in Python; user-supplied headers are
  per-run only and are persisted only as masked header keys.
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

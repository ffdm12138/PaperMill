# Third-Party Notices

This repository's original source code is licensed under the project root
license. Third-party packages, external tools, APIs, services, models, PDFs,
converted Markdown, extracted images, metadata records, and writing outputs are
not relicensed by this repository. They remain governed by their own licenses,
terms of service, publication terms, or copyright status.

Do not describe the entire stack as MIT licensed. Only original repository code
is covered by the repository license boundary.

## Direct Python Dependencies

| Component | Role | License / Terms Note |
| --- | --- | --- |
| MinerU | PDF/document conversion runtime | MinerU Open Source License, based on Apache License 2.0 with additional terms. Review upstream terms and commercial thresholds before commercial deployment or redistribution. |
| PyMuPDF / MuPDF | Lightweight PDF text and DOI extraction support | AGPL-or-commercial dual path. Projects that cannot comply with AGPL must use the commercial license path. |
| FastAPI | Optional local API server | MIT. |
| uvicorn | Optional local ASGI server for API serving | BSD-3-Clause. |
| python-multipart | Multipart form parsing for API uploads | Apache-2.0. |
| Gradio | Optional local UI | Apache-2.0. |
| requests | HTTP client | Apache-2.0. |
| pydantic | Data validation | MIT. |
| loguru | Logging | MIT. |
| filelock | File-based locking | MIT. |
| orjson | Fast ledger JSON encoding and validation | Apache-2.0 OR MIT. |
| pytest | Test runner | MIT. |
| jsonschema | Schema validation used by tests/checks | MIT. |

## External Integrations And Services

| Integration | Category | Notice |
| --- | --- | --- |
| Crossref | Metadata service/API | Subject to Crossref API terms and etiquette. Metadata is not project source code. |
| OpenAlex | Metadata service/API | Subject to OpenAlex terms and rate-limit policies. Optional `OPENALEX_EMAIL` / `OPENALEX_API_KEY` values must never be committed or logged. |
| Unpaywall | OA location service/API | Subject to Unpaywall terms and policies. |
| Semantic Scholar | DOI/PDF URL resolver input | Subject to service terms. |
| arXiv, bioRxiv, PMC | Preprint/OA sources | Source documents and metadata keep their original licenses and reuse terms. |
| Publisher TDM APIs (Wiley, Elsevier, Springer/Nature direct URLs) | PDF content access | Use only with the user's lawful access rights, API credentials, and publisher terms. |
| ref-downloader bridge (`src/fetch/resolvers/ref_downloader_bridge.py`) | External executable integration | Associated with the external `ref-downloader` MIT project. This repository only records the integration boundary; review the upstream project when installing or redistributing that tool. |

## Runtime And Generated Artifacts

PDFs, converted Markdown, extracted images, metadata JSON from external
providers, generated BibTeX, and writing outputs are runtime/library artifacts.
They are not project source code and are not covered by the repository license.
When sharing audit snapshots, keep using the repository snapshot rules that
exclude heavy/binary assets and secrets.

## Audit Practice

Use `scripts/audit_third_party_licenses.py` to compare installed direct
dependencies against the expected license table. Use
`scripts/audit_source_provenance.py` to scan for copied/adapted source evidence,
repository URLs, copyright notices, SPDX markers, and external integration
references. These scripts are read-only and must not rewrite this notice.

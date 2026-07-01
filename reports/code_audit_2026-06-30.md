# Code Audit Fix Report - 2026-06-30

## Accepted Findings

- Formal MinerU conversion could inherit stale environment variables such as
  `MINERU_REQUIRE_GPU=false`, `MINERU_ALLOW_CPU=true`, or `CUDA_VISIBLE_DEVICES=1`.
- Re-running the fast API helper could start another `mineru-api` before checking
  whether one was already healthy.
- Multi-source `MINERU_RUNNER=cli` conversion could cold-start MinerU repeatedly.
- `paper_raw` conversion was not idempotent and could merge stale `images/` files.
- Local FastAPI had no optional API key gate, broad CORS settings, and no basic
  response security headers.
- Custom external resolver DOI handling needed explicit input validation and a
  tighter executable boundary.
- `_write_bytes_pdf()` lacked the same tmp cleanup guard already present in
  `_download_pdf()` and `_copy_pdf()`.

## Re-scoped Findings

- `ExternalCommandResolver` was not treated as classic shell injection because it
  already uses `shell=False`; the fix is trust-boundary and DOI validation hardening.
- `_download_pdf()` cleanup was already protected by `finally`; no rewrite was needed.
- CORS risk is lower under the default localhost deployment, but configurable origins
  and API key support were still added for safer non-localhost use.

## Fixes Applied

- `start_fast_api_mode.bat` now checks `/health` before starting `mineru-api`, refuses
  to start when port 8000 is occupied but unhealthy, and sets `CUDA_VISIBLE_DEVICES=0`
  in parent and child shells.
- `scripts/convert_paper_raw_gpu.py` hard-overrides formal GPU environment and defaults
  to `cli_api_proxy` without auto-starting the API.
- `scripts/convert_paper_raw_batch.py` now hard-fails unsafe formal CPU fallback and
  multi-source cold CLI batches unless explicitly debug-overridden.
- `cli_api_proxy` now checks API health before batch conversion and before single
  `MinerUConverter.convert()` subprocess launch.
- `PaperRawConverter` now inspects conversion state, skips existing converted assets,
  writes `<source_id>.conversion.json`, updates `.import_status.json`, and supports
  explicit `--force-reconvert`.
- Formal commit removes `*.conversion.json` before installing assets into `data/papers`.
- Resolver action hints now use `stage_raw_pdfs_to_paper_raw.py --move --apply`.
- FastAPI gained optional `X-API-Key`, configured CORS origins, restricted methods and
  headers, basic security headers, non-localhost startup guard, and request size limits.
- Custom resolver DOI validation and executable placeholder restrictions were added.
- `SECURITY.md` documents default boundaries and unsafe override behavior.

## Not Done

- No OAuth/JWT, multi-user auth, database, or account system.
- No full penetration test.
- No catalog/metadata schema changes.
- No active writing workflow directory contract changes.
- No broad exception-handling or atomic-write rewrite outside the requested tmp cleanup.

## Validation

Commands run with `PYTHONIOENCODING=utf-8` and `conda run -n mineru`:

- `python -m compileall -q scripts src tests` - passed.
- `python -m pytest -q tests/test_convert_paper_raw_batch_runner_warning.py` - 8 passed.
- `python -m pytest -q tests/test_mineru_api_single_instance.py` - 5 passed.
- `python -m pytest -q tests/test_paper_raw_convert_idempotency.py` - 7 passed.
- `python -m pytest -q tests/test_api_security.py tests/test_external_command_resolver_security.py` - 15 passed.
- `python -m pytest -q tests/test_converter_runtime.py tests/test_mineru_runtime.py` - 26 passed.
- `python -m pytest -q tests/test_paper_raw_preflight.py tests/test_v2_library.py` - 24 passed.
- `python -m pytest -q tests/test_stage_raw_pdfs.py tests/test_ingest_path_ordering.py` - 7 passed.
- `python -m pytest -q tests/test_access_policy.py tests/test_no_legacy_writing_workflow.py tests/test_docs_consistency.py` - 25 passed.
- `python -m pytest -q tests/test_server_security.py tests/test_api_validation.py` - 34 passed.
- `python -m pytest -q` - 495 passed, 9 warnings.

Warnings were pre-existing runtime/dependency warnings from Gradio, config robustness
tests, and SWIG/PyMuPDF imports; they did not fail validation.

## 2026-07-01 Follow-up

- MinerU conversion subprocesses no longer receive a process-level timeout; large
  PDFs are allowed to run until the MinerU command exits. Health, preflight,
  HTTP, and `MinerULock` wait timeouts remain separate safeguards.
- `scripts/start_mineru_services.py` and `scripts/stop_mineru_services.py` are the
  recommended service control entry points. `start_fast_api_mode.bat` delegates
  startup to the Python helper.
- Metadata title/author search evidence now prefers converted Markdown first 100
  physical lines before PDF title fallback.

## 2026-07-01 v2.2 state-machine refactor

- ingest upgraded v2.1 → v2.2 (tag `ingest-v2.2`). Schema (metadata / catalog v2.0 /
  `FORBIDDEN_CATALOG_KEYS`) unchanged; only flow + ledger state semantics changed.
- `curate_paper_raw.py` no longer renames the folder/files or allocates a
  paper_number; it only validates metadata/catalog and writes `.import_status.json
  status=catalog_ready`.
- New `scripts/formalize_paper_raw.py` + `src/services/paper_raw_formalizer.py`:
  in `data/paper_raw` it derives canonical `paper_id`, renames folder/assets,
  reserves a 16-digit `paper_number` (ledger `state=reserved`), backfills catalog
  links, writes `<paper_id>.formalization.json` + `<16-digit>.paper.number`, and
  sets `status=ready_for_commit`.
- `commit_paper_raw_to_papers.py` is now a transactional install: gate on
  `ready_for_commit` + formalization.json + marker → staging copytree → self-check
  → `os.replace` → `activate_reserved` → rebuild all.catalog → postcheck → delete
  source. Any post-install failure rolls back (removes `data/papers/<paper_id>`,
  deactivates the ledger number back to reserved). Fixes the v2.1 bug where a
  postcheck failure left a half-installed formal folder.
- `PaperNumberLedger` gained `reserve_for_paper_raw` / `activate_reserved` /
  `deactivate_to_source` / `paper_number_from_marker` and an additive `state`
  field (no schema_version bump; legacy entries backfill `state=active`).
- `audit_paper_raw_formal_imports.py` folds in formal-state checks (marker,
  paper_number consistency, metadata completeness/match, catalog content-only,
  transient files, suspicious paper_id) and a `--quarantine --apply` mode.


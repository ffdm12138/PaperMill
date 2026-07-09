# Security Notes

## Default Boundary

- FastAPI defaults to `MINERU_API_HOST=127.0.0.1`.
- Local UI/server use is intended for trusted localhost workflows.
- Gradio or other local UI entrypoints should keep sharing disabled unless explicitly reviewed.

## Exposing the API

If binding outside localhost, set `MINERU_API_KEY` and send it as `X-API-Key`.
Recommended additional controls:

- reverse proxy with TLS;
- firewall rules or trusted IP allowlists;
- no public exposure of runtime data directories.

Relevant environment variables:

- `MINERU_API_HOST`
- `MINERU_API_PORT`
- `MINERU_API_KEY`
- `MINERU_API_CORS_ORIGINS`
- `MINERU_ALLOW_UNAUTHENTICATED_PUBLIC_API`

`MINERU_ALLOW_UNAUTHENTICATED_PUBLIC_API=true` is unsafe and should only be used
behind trusted local network controls.

## Custom Resolver

The custom external command resolver is disabled by default and is only for
trusted local configurations. Keep `command_argv` under operator control; do not
expose it to untrusted users. The resolver runs with `shell=False`, validates DOI
shape, and checks output PDF paths, but it still executes a local command.

## Formal MinerU Conversion

Formal ingest uses `scripts/convert_paper_raw_gpu.py`. CPU/no-GPU fallback is
debug-only and must be explicit via `--allow-cpu` on the lower-level batch entry.
`mineru-api` must be started with `CUDA_VISIBLE_DEVICES=0` in its own shell;
setting that variable only in a later client process cannot change an already
running service.

## API Credentials

OpenAlex credentials (`OPENALEX_EMAIL`, `OPENALEX_API_KEY`) are loaded from
process environment variables only. There is no `.env` file, no config file
fallback. Missing credentials are transparent — the caller falls back to
anonymous access instead of raising an error.

Credentials are loaded once per request via the centralized module
`src.services.openalex_credentials`. Internal helper methods accept
`OpenAlexCredentials` as a parameter; the credentials object is never stored
on the module, never serialised, and never persisted into data structures
returned to callers.

**No credential values must appear in:**
- log output (use `OpenAlexCredentials.safe_summary()` → `"email=yes api_key=yes"`)
- error messages (use `safe_request_error_summary(exc)` instead of `str(exc)`)
- returned `PaperCandidate` or `FetchResult` objects
- snapshot / archive / report outputs
- plaintext in source files (the secret scanner in `scripts/pack_repo.py`
  detects hardcoded assignments and blocks packaging)

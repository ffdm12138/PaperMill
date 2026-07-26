# Security Notes

## Default Boundary

- FastAPI defaults to `MINERU_API_HOST=127.0.0.1`.
- Local UI/server use is intended for trusted localhost workflows.
- Local UI entrypoints should keep sharing disabled unless explicitly reviewed.

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
`src.fetch.openalex_credentials`. Internal helper methods accept
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

## Source-Record Provider Path Security

A security boundary exists between the external provider identifier and the
filesystem path where source records are stored. Every provider name MUST be
normalized through ``normalize_provider_slug()`` in
``src/metadata/source_records.py`` before being used in any file path.

The slug function enforces:
- Strict character whitelist ``[a-z0-9][a-z0-9._-]{0,63}``
- Rejection of path separators (``/``, ``\\``), directory traversal (``..``),
  colons, control characters, NUL bytes, and Windows reserved names
- Unicode NFC normalization with length limit and stripping

The file writer additionally applies a **resolved containment check**:
the target path must be a direct child of the resolved ``source_records/``
directory of the paper workspace. Any escape raises
``SourceRecordPathEscapeError``.

Metadata ``raw_record_path`` values are validated at schema level: they must be
POSIX-relative, be under ``source_records/``, and contain no ``..`` or
backslash components.

**If you discover a path-escape vulnerability**: open an issue immediately; do
not commit code that uses string concatenation to build file paths from
external provider strings.

## Snapshot (Packaging) Security Boundary

Repository source snapshots produced by ``scripts/pack_repo.py`` follow a
strict **runtime-zero** policy:

- Local tool state directories (``.workbuddy/``, ``.reasonix/``) are always
  excluded regardless of profile or git-tracked status.
- Runtime reports (``data/cleanup_report.json``) are always excluded.
- Real ``data/paper_raw/`` and ``data/papers/`` workspaces are always excluded.
- No runtime transaction journals, logs, caches, temporary files, credentials
  files, or live data enter the snapshot.

The snapshot manifest declares ``runtime_files_included`` and undergoes a
self-check that verifies the archive matches the declared runtime-zero
invariant. If the check fails, the ZIP is deleted and the command exits
non-zero.

Runtime-zero exclusions are defined centrally in
``src/utils/repository_hygiene.py`` and consumed by the packer and verifier.

Transaction journals are untrusted persisted input. Recovery accepts trusted
roots from configuration or CLI arguments, validates all lexical and resolved
paths and symlink chains before mutation, and never derives a destructive root
from a journal path. Destructive helpers reject symlinks by default.

Fetch reports are sanitized recursively before persistence: credentials and
all URL query strings are removed even when a URL appears inside free-form
error text. Discovery exports are trusted only after identity, DOI, path,
record-count, byte-size, and SHA-256 validation. Snapshot creation builds an
immutable selection plan and fails closed if any selected file disappears,
changes, becomes a symlink, exceeds a limit, or is omitted from the archive.
Other file-type exclusions remain in ``scripts/pack_repo.py`` as the constants
The canonical runtime classification and placeholder allowlist live in
``src/utils/repository_hygiene.py``; archive-format deny rules remain in
``scripts/pack_repo.py``.

**If you discover a packaging leak**: open an issue immediately with the
snapshot type, profile, and the unexpected file path found in the archive.

# Literature Library Manager

This is a library / ingest management skill. It may describe how to create a
writing job, but it is not the article-writing skill.

Use only the v2 `paper_raw` ingest workflow. Do not change ingest schema from
this skill.

Manual PDF imports treat `data/raw/` as a queue / raw 是待处理队列. Normal staging
must use `stage_raw_pdfs_to_paper_raw.py --move --apply`, so successful staging
consumes PDFs from raw. Copy mode is only for debugging, backup, tests, or
explicit one-off inspection.

Metadata-only PDF fetch only fills existing 16-digit `paper_raw` workspaces:
DOI comes from metadata, not `doi.csv`; fetch never allocates paper_numbers.
Header-based fetch is explicit, uses a fixed User-Agent in code, and never
persists header values.

MinerU conversion requires GPU / MinerU 正式转换必须使用 GPU. Manual staging does
not need GPU; formal ingest uses `convert_paper_raw_gpu.py`. It defaults
`MINERU_REQUIRE_GPU=true`, `CUDA_VISIBLE_DEVICES=0`, and checks both `nvidia-smi`
and `torch.cuda.is_available()`. Use `MINERU_RUNNER=cli_api_proxy` and
`MINERU_API_URL=http://127.0.0.1:8000` for persistent mineru-api batch conversion.
Start/reuse managed mineru-api with `python scripts/start_mineru_services.py --wait --restart-if-stale`,
verify `check_mineru_processes.py` verdict is `READY_FOR_CONVERSION`, run one
`smoke_mineru_conversion.py` report, then run formal batch conversion. Stop it
with `python scripts/stop_mineru_services.py`. Start mineru-api with
`CUDA_VISIBLE_DEVICES=0` in its own shell.
MinerU PDF conversion has no process-level timeout; `/health` is liveness only,
not GPU conversion readiness. Health/preflight/HTTP and lock wait timeouts are
separate checks. Metadata title/author/affiliation/
abstract/keyword/DOI candidates come from converted Markdown first 100 lines as
front-matter evidence before PDF title fallback.
`MINERU_ALLOW_CPU=true` / `MINERU_REQUIRE_GPU=false` is debug-only.

## Ingest layered semantics

Ingest layered semantics (conversion does not require metadata; formalize/commit does):

Conversion layer:
- PDF conversion to Markdown/images does not require complete metadata.
- Missing DOI or unmatched metadata must not block MinerU conversion.
- Conversion output Markdown is a valid metadata-resolution source.

Formal library layer:
- Formalize/commit requires strict metadata.
- Metadata freeze closure must be replay-valid; journal articles require a valid DOI.
- BibTeX is generated from metadata, never from catalog.

Summary: convert first is allowed; commit requires metadata.

Writing starts by creating an ignored job workspace:

```bash
python scripts/create_write_job.py --job-id review_001 --paper-numbers 0000000000000001
```

The writing article copy lives at:

```text
write/jobs/<job_id>/article/<paper_number>/
```

BibTeX and citation facts come from per-paper `metadata.json`. Catalog files
remain content-only and must not receive DOI, authors, year, journal, venue, or
other bibliographic fields.
Catalog natural-language values default to Chinese; JSON keys/schema enums stay
English, technical terms may remain English, and metadata remains the
original/canonical bibliographic source.

## Ingest duplicate guard scope

`build_ingest_duplicate_index()` indexes **every** paper_raw workspace, not only
16-digit numbered folders. `data/paper_raw/` holds two workspace kinds: (1)
strict 16-digit `paper_number` staging workspaces, and (2) legacy / untitled /
formalized workspaces named by `paper_name` (e.g. `1979_sykest_untitled/`, which
carry a `*.paper.number` marker and `metadata.paper_number` despite the
non-16-digit folder name). A folder is a workspace via `is_paper_raw_workspace()`
(asset presence), never via the folder-name regex alone. Duplicate workspaces
are audited and cleaned by `scripts/audit_paper_raw_duplicate_workspaces.py`
(moves losers into `data/paper_raw/quarantine/duplicate_workspaces/`; never
deletes, never recycles paper_numbers, never lowers `max_number`).

## Discovery staging boundary

The ledger owns only paper-number lifecycle. Evidence reads workspace facts
once; Readiness uses only the explicit manifest workflow profile and unknown
profiles fail closed. Registry is the sole DOI/identity disk scanner and
publishes copy-on-write refreshes atomically. Network staging is coordinated
only by `DiscoveryStageTransaction` under the raw write lock. The allocator is
discovery-agnostic, the pending queue only orchestrates journals, and the old
DuplicateIndex/WorkspaceIndex disk refresh APIs have no fallback.

Incomplete `reserved` remains unsettled; incomplete `metadata_staged` is
`repair_required`. Identity facts use `identity_key + paper_number`, preserving
multiple providers per paper through freeze. Transaction reuses one locked
ledger load, retains reservation/final durable saves, and directly publishes a
validated success with no post-refresh; Registry refresh is atomic.

Registry snapshots are caches, not permanent facts. Ledger state/folder/scope
changes trigger a targeted rescan, and DOI/identity matches are live-validated
before reuse or duplicate decisions. Missing settled evidence is
`repair_required`; record replacement removes old formal refs after rollback.
`actual_allocated` counts only a new number, while `reused_existing` counts
reuse.
# Discovery batch boundary

Use one shared staging context and journal index per batch. DOI/identity lookup
must include both raw and generation-valid formal papers; formal/raw collisions
fail closed. Do not scan repair backlog per candidate or use pure TTL correctness
caches. Stage at most 16 candidates per lock epoch and retain both ledger saves.

Relevance profile replacement closes only page-journal pre-staging states;
processing/retryable recovery states and unknown lifecycles globally block it.
Historical terminal facts and durable DOI projections are immutable. Phase A is
zero-write except for fixed lock behavior and binds exact after bytes; resume
accepts exact before/after only. Runtime owns one immutable complete active
profile mapping. A/B/C fetches one shared frozen corpus and replays offline.

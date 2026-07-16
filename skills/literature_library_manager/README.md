# Literature Library Manager

Pure v2 paper_raw workflow only.

Use `data/raw/` or network metadata as input, stage into `data/paper_raw/`, then match, fetch or attach PDF, convert, curate, formalize, commit, reconcile catalog folders and validate.

Metadata-only PDF fetch scans existing 16-digit `paper_raw` workspaces and reads
DOI only from `<paper_number>.metadata.json`; it never reads `doi.csv` or
allocates a new paper_number. Header-based fetch is explicit, uses a fixed
in-code User-Agent, and stores only masked header keys.

For batch conversion, use `python scripts/start_mineru_services.py --wait --restart-if-stale`,
`python scripts/check_mineru_processes.py`, a successful
`python scripts/smoke_mineru_conversion.py --paper-number <id> --apply`, then
`python scripts/convert_paper_raw_gpu.py --all --apply`, then
`python scripts/stop_mineru_services.py`. `smoke_mineru_conversion.py` without
`--apply` is readiness-only and cannot unlock batch conversion. MinerU conversion has no process-level
timeout; `/health` is liveness only, not GPU conversion readiness.
Title/author/affiliation/abstract/keyword/DOI metadata candidates come
from converted Markdown first 100 lines as front-matter evidence before PDF
title fallback.

Catalog natural-language values default to Chinese for Chinese search,
classification, paper selection, and writing workflows. JSON keys/schema enums
stay English, technical terms may remain English, and metadata remains the
original/canonical bibliographic source.

Discovery imports use one staging path: the pending journal calls the thin
network adapter, which delegates workspace reconciliation, duplicate checks,
allocation, and durable writes to `DiscoveryStageTransaction`. Its validated
`WorkspaceRegistry` snapshot is the only DOI/identity scan result; unknown
workflow profiles and refresh errors fail closed before allocation.

Incomplete `reserved` remains unsettled, while incomplete `metadata_staged` is
`repair_required`. Identity entities use `identity_key + paper_number` and keep
multiple providers per paper through freeze. Warm staging uses one locked
ledger load, two durable new-record checkpoints, atomic refresh, and direct
record publication with no successful-stage post-refresh.
# Discovery batch runtime

Keyword discovery shares one raw/formal Registry and one journal index per batch,
uses generation-bound formal publication facts, bounded repair probes, live match
revalidation, page-level claims/commits, and a single staging consumer. Applied
repair probes rotate through a durable cursor; retryable candidates remain in
the current runtime. Formal assets are immutable and manifest-hash audited.

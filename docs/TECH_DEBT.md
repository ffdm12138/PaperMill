# Technical Debt

## Planned v2_library.py split

`src/services/v2_library.py` still combines ingest id allocation, ledger,
converter, curation, commit, readiness, and catalog build logic. The ingest
v2.3 paper_number workspace migration intentionally does not split it.

Future refactor should separate:

- `src/services/paper_number_ledger.py`
- `src/services/paper_raw_readiness.py`
- `src/services/paper_raw_contract.py`
- `src/services/all_catalog_builder.py`

## Duplicate paper_raw workspaces (pre-guard legacy staging)

`build_ingest_duplicate_index()` previously gated its `data/paper_raw/` scan on
the 16-digit folder regex, so legacy / untitled / formalized workspaces
(e.g. `1979_sykest_untitled/`, which carry `*.metadata.json`, `*.pdf`, and a
`*.paper.number` marker despite the non-16-digit folder name) were excluded from
the dedup index. Their PDFs were then re-staged from `data/raw/` into brand-new
16-digit numbered workspaces, producing ~38 duplicate workspaces on disk.

Fixed on 2026-07-02: the guard now admits workspaces via
`is_paper_raw_workspace()` (asset-based) and resolves the true paper_number via
`resolve_paper_raw_identity()` (metadata → `*.paper.number` marker → ...). The
existing duplicate workspaces are cleaned by
`scripts/audit_paper_raw_duplicate_workspaces.py --apply-cleanup`, which moves
the loser (keep-rule: higher ingest-stage → more assets → legacy-with-marker →
lowest name) into `data/paper_raw/quarantine/duplicate_workspaces/` and marks
the ledger entry `state=quarantined_duplicate`. Paper numbers are never recycled
and `max_number` is never lowered.

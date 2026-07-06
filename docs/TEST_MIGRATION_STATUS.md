# Test Migration Status

## Current status

- Root flat tests (`tests/test_*.py`): forbidden
- Temporary allowlist: none
- Tombstone tests (`*._deleted`): forbidden
- Active test layers:
  - `tests/contract/`
  - `tests/unit/`
  - `tests/integration/`
  - `tests/e2e/`
  - `tests/hygiene/`
  - `tests/legacy/`
  - `tests/slow/`

All 76 root flat tests were moved into layered directories. The migrations were
structural relocations with only minor adjustments (import paths, fixtures);
test logic was not rewritten. Path fixes applied: `parent.parent` →
`parent.parent.parent` (repo root hop) and 6-digit paper_number IDs expanded
to 16-digit equivalents.

## Active guards

- `tests/hygiene/test_no_root_flat_tests.py` — forbids `tests/test_*.py`
- `tests/hygiene/test_no_tombstone_tests.py` — forbids `*._deleted`
- `tests/hygiene/test_snapshot_hygiene.py` — snapshot must stay clean of
  runtime data, `.reasonix/`, output, write/jobs, and tombstones

## Migration result

- root flat tests: **0**
- tombstone tests: **0**
- default fast gate: `python scripts/agent_acceptance.py`
- full gate: `python scripts/agent_acceptance.py --full`
- diagnostic groups: `python scripts/agent_acceptance.py --full-groups`

All pytest subprocesses inside `agent_acceptance.py` run with
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` so third-party plugins cannot change
behavior or hang the process.

## Slimming policy

Delete:

- migration bookkeeping tests
- duplicated pack/docs guards
- brittle keyword scans
- obsolete legacy behavior tests

Keep:

- ingest contract
- metadata/source record contract
- MinerU runtime and GPU guards
- formalize/commit/rollback transaction tests
- output cache tests

See `docs/TESTING.md` for the full slimming policy.

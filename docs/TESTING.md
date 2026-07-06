# Testing Guide

This project uses contract-driven tests. Tests should protect stable external
behavior and project contracts, not incidental implementation details.

## Test Layers

- `tests/contract`: stable project contracts that must not break.
- `tests/unit`: isolated pure/local logic.
- `tests/integration`: multi-module workflows using `tmp_path` and fake external services.
- `tests/e2e`: minimal smoke tests for agent acceptance.
- `tests/hygiene`: repository, docs, packaging, encoding, and safety checks.
- `tests/legacy`: deprecated behavior guards kept as migration safety nets.
- `tests/slow`: broad or slow regression coverage.
- `external`: marker for tests requiring network, GPU, real MinerU, or external APIs.

## Test Slimming Policy

The test suite has completed structural consolidation (root flat tests and
tombstones are gone; all coverage lives in the layered directories above). The
current goal is to keep the layered gates small and stable.

Slimming principles:

- Delete migration bookkeeping tests, duplicated pack/docs guards, and brittle
  keyword-scan tests that pin specific terms in specific docs.
- Delete tests whose behavior is already covered by a newer layered test;
  when removing a duplicate, state which test covers the same risk.
- Keep the core ingest / metadata / MinerU / formalize / commit / rollback
  safety gates intact — never widen a production contract just to cut a test.
- Do not use `skip` / `xfail` to fake slimming, and do not relocate old tests
  to `tests/legacy/` to avoid deletion.

Plain `pytest` does not exclude `slow` or `external`; the default agent gate is
controlled by `scripts/agent_acceptance.py`. **Do not run bare `pytest` as final
acceptance** — use `python scripts/agent_acceptance.py` instead.

## Root Flat Tests

Root flat tests (`tests/test_*.py`) are **not allowed**. All coverage must live
under the layered directories above. The hygiene guard
`tests/hygiene/test_no_root_flat_tests.py` enforces this — any new test file
added directly under `tests/` will fail acceptance.

See `docs/TEST_MIGRATION_STATUS.md` for the full migration history.

## Pytest Markers

All test directories use pytest markers for filtering:

```ini
markers =
    smoke: fast acceptance tests required before packing
    contract: stable project contracts that must not break
    unit: isolated pure/local tests
    integration: multi-module workflow tests using tmp_path
    e2e: minimal end-to-end smoke tests
    hygiene: repository, docs, packaging, encoding, and safety checks
    legacy: legacy/deprecated behavior guards
    slow: slow regression tests not required for every agent turn
    external: tests requiring network, GPU, real MinerU, or external APIs
```

## Agent Acceptance

Every normal coding-agent change must run:

```powershell
conda run -n mineru python scripts/agent_acceptance.py
```

The default fast gate runs `compileall`, the layered test directories
(contract + hygiene + unit + integration + e2e), `verify_git_hygiene()`,
`pack_repo.py`, and snapshot verification. Successful output must include:

```text
[OK] agent acceptance passed
[OK] Packed: mineru_snapshot.zip
```

For large refactors, schema changes, ingest changes, or test-system changes, run:

```powershell
conda run -n mineru python scripts/agent_acceptance.py --full
```

`--full` runs full `pytest -q --durations=30` before packing. `--no-pack` is a
debug-only shortcut; it is not valid final acceptance for ordinary agent work.

For diagnostic output when a group of tests fails, run:

```powershell
conda run -n mineru python scripts/agent_acceptance.py --full-groups
```

`--full-groups` runs pytest in groups of at most 10 files, prints each group's
name, file list, and full pytest command, and streams output live. It then
continues to pack and snapshot verification (same as `--full`).

### Pytest Plugin Isolation

All pytest subprocesses inside `agent_acceptance.py` run with
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`. This prevents third-party pytest plugins
from changing warning/asyncio/coverage/tracing behavior or causing the process
to hang after tests pass. This only affects `agent_acceptance.py` internals;
running `python -m pytest` directly is unaffected.

## Fixtures And Runtime Isolation

Tests must use minimal generated fixtures from `tests/factories/` or existing
compatibility helpers under `tests/helpers/`. Do not copy large JSON objects into
individual tests unless the test is specifically about that schema shape.

Default test fixtures must use 16-digit v2.3 `paper_number` workspaces. Old
6-digit identifiers are for legacy migration only and belong under `tests/legacy/`.

Tests must not write project-root runtime directories such as:

```text
output/
data/paper_raw/
data/papers/
data/raw/
write/jobs/
reports/
```

Use `tmp_path` and pass isolated `--paper-raw-dir`, `--papers-dir`,
`--ledger-path`, and `--all-catalog-path` where relevant.

### Strict-library regression coverage

These regressions guard the strict formal-library contracts (see
`docs/PROJECT_CONTRACT.md`):

- `test_rebuild_fails_on_incomplete_formal_dir` / `test_build_partial_dir_appends_error`
  / `test_rebuild_clean_empty_papers_dir_ok`: `AllCatalogBuilder.build()` must flag
  partial formal dirs to `last_errors`, never write/overwrite `all.catalog.json`
  on error, and still allow genuinely empty `papers/` dirs.
- `test_commit_removes_stale_raw_asset_manifest`: commit must remove paper_raw
  `<paper_number>.asset_manifest.json` so only `<paper_id>.asset_manifest.json`
  survives in `data/papers`.
- `test_validate_v2_library_detects_extra_asset_manifest` /
  `test_validate_v2_library_accepts_single_correct_manifest`: validator globs
  `*.asset_manifest.json` and rejects extras.
- `test_doctor_pytest_uses_isolated_env_and_timeout` /
  `test_doctor_pytest_timeout_is_blocking`: doctor pytest step runs with
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` + 300s timeout and converts
  `TimeoutExpired` into a blocking result (`returncode=124`).

## Updating Tests

If a project contract changes, update the docs or schema first, then the
implementation, then the contract tests. If only internals change, contract tests
should normally stay unchanged.

When a test fails:

1. If it is a contract test, check `docs/PROJECT_CONTRACT.md`. If the contract did
   not change, fix the implementation.
2. If it is a unit test, prefer behavior assertions over private implementation
   details.
3. If it is integration/e2e, confirm whether the workflow actually changed.
4. If it is legacy, decide whether the historical risk is still relevant. If not,
   replace it with a forbidden-path guard or remove it after new coverage exists.

When removing duplicate old tests, state which new contract/unit/integration test
covers the same risk.

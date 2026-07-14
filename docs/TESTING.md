# Testing

Tests use `tmp_path`, fake providers, fake MinerU, mock transports, isolated
ledger/index roots, and no real runtime data or network. Set
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`.

## Layers and markers

The root `tests/conftest.py` applies directory markers during collection:

- `contract`, `unit`, `integration`, `security`, `hygiene`, and `e2e` identify
  the responsibility layer.
- `process` identifies real subprocess, process-tree, cross-process lock, or
  port behavior; process tests are also `slow`.
- `slow` identifies tests with materially higher routine cost.
- `stress` identifies high-iteration race amplification and is also `slow`.
- `external` requires a non-hermetic local or remote service and is excluded
  from normal gates.

Tests may add behavioral markers, but must not contradict their directory
layer. New contracts belong in one authoritative layer; do not repeat static
schema combinations in unit and integration tests.

## Gates

```text
python scripts/agent_acceptance.py
python scripts/agent_acceptance.py --full
python scripts/agent_acceptance.py --full-groups
python scripts/agent_acceptance.py --process
python scripts/agent_acceptance.py --stress
python scripts/agent_acceptance.py --area packaging
python scripts/agent_acceptance.py --area ingest
python scripts/agent_acceptance.py --area discovery
python scripts/agent_acceptance.py --area security
```

Fast uses `not process and not slow and not stress and not external` over
contract, security, hygiene, selected unit tests, and two integration smoke
workflows. Full uses `not stress and not external` over the complete suite.
Full-groups applies the same selection in isolated groups with separate
temporary roots, pytest caches, environments, process trees, and timeouts.
Process uses `process and not stress and not external`. Stress uses `stress`
and prints the fixed `MINERU_STRESS_SEED` (default `20260711`). Area gates are
explicit paths and work without `.git` metadata.

Real subprocess tests require a process marker, bounded timeout, and complete
process-tree cleanup. Prefer direct `main(argv)` or domain API calls for CLI
branch coverage. Keep only representative durable crash states; never remove
the installed-before-ledger, ledger-before-index, partial-index, source-delete,
or rollback recovery boundaries.

## Packaging and delivery

Repository runtime-zero classification lives only in
`src/services/repository_hygiene.py`. Every source/audit profile excludes real
paper workspaces, transactions, generated indexes/ledger, local tool state,
runtime reports, credentials, caches, and logs. After changes, run the packer,
extract the ZIP into a temporary directory, and verify manifest/member counts,
runtime-zero, and the secret scan.

Final reports give collected/passed/skipped counts, durations, anything not
run, and pack evidence. A timeout is a failure, never a pass.

## Current test suite

- **1763 tests collected** (down from 1869 after July 2026 cleanup)
- **Full gate** (`not stress and not external`): 1752 passed, 8 skipped, 0 failed
- **Contract/hygiene gate**: 360 passed
- **Fast gate** (`not process and not slow and not stress and not external`): 353 passed
- **Snapshot**: runtime-zero, 481 payload files, 0 runtime files, secret scan passed

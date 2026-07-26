# Testing

Discovery performance acceptance is counter-based. The compact contract uses
200 raw workspaces, 200 formal papers and 500 candidates and asserts one context,
one cold Registry/formal load, batch-bounded lock/ledger loads, two saves per new
paper, and fingerprint work proportional to dirty/matched/new records rather than
candidate×repair-backlog. Journal tests assert one initial full scan and page-level
batch claim/commit. Tests use only isolated temporary roots.
Publication-state tests cover the supported publication boundary: official
commit/rollback/repair changes generation/revision and the next warm refresh
reloads the formal closure, while an unsupported direct formal-asset edit is
rejected by the next batch cold build or an explicit audit. A DOI/identity hit
still performs targeted live revalidation. Tests also cover commit/rollback
sidecar publication, identity-only formal repair, closure-drift refusal, and
demotion of incomplete `metadata_staged` workspaces to permanent `reserved`
numbers. Real runtime repair commands remain audit-only during agent
acceptance; operator apply is a separate reviewed step.
The fixture setup is outside the measured pipeline interval. The contract also
includes 40 Journal pages, 40 repair-backlog members, retryable in-memory
rescheduling, rotating repair probes, and formal immutable-hash detection.

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
- `performance` identifies counter-based benchmarks with bulk fixtures and is
  also `slow`.
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

Routine changes require the fast gate. The full gate is required before
releases, broad refactors, and final delivery.

Fast uses `not process and not slow and not stress and not external` over
contract, security, hygiene, selected unit tests, and selected integration
smoke workflows. Full uses `not stress and not external` over the complete
suite. Full-groups applies the same selection in isolated groups with separate
temporary roots, pytest caches, environments, process trees, and timeouts.
Process uses `process and not stress and not external`. Stress uses `stress`
and prints the fixed `MINERU_STRESS_SEED` (default `20260711`). Area gates are
explicit paths and work without `.git` metadata.

Acceptance is parallel by default; `--jobs N` caps the parallelism (0 = auto,
`min(12, cpus - 2)`) and `--no-parallel` forces the legacy sequential paths.
The fast gate runs its groups as concurrent subprocesses — every group keeps
its own isolated workspace, `--basetemp`, environment, process tree, and
per-group timeout — and replays buffered output in declaration order.
`--full` splits into one pytest-xdist invocation over
`not process and not slow and not stress and not external` plus one
sequential invocation over `(process or slow) and not stress and not
external`; the two selections partition the full suite exactly. Because
plugin autoload is disabled, acceptance loads xdist explicitly via `-p xdist`;
if pytest-xdist is not installed, `--full` falls back to the sequential
single invocation. `--full-groups` remains a sequential hang-bisection
diagnostic. A group or invocation timeout is a failure, never a pass.

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

- **2693 tests collected** (run `pytest --collect-only -q`)
- **Full gate** (`not stress and not external`): last verified 2026-07-26 —
  parallel chunk 2611 passed + 1 skipped in ~57s (`-n 12`), sequential
  residue 74 passed + 4 skipped in ~6m; ~7m20s wall end to end, 0 failed
- **Fast gate**: all 9 groups passed concurrently in ~52s wall (9 workers)
  with clean pre/post pollution checks (sequential baseline: ~3m)
- **Snapshot**: runtime-zero, 683 ZIP members, 0 runtime files, secret scan passed

Discovery staging acceptance additionally covers strict ledger validation,
single-read evidence, unknown-profile failure, atomic copy-on-write refresh,
zero allocation after refresh failure, and real-process races for identical
DOI and identical discovery identity. Performance tests require one cold full
Registry build and prohibit a per-candidate full rebuild; the four release
benchmark tiers are 100, 1000, 3000, and 10000 existing workspaces.

Relevance acceptance covers the authoritative lifecycle classifier and staging
side-effect call graph, global unknown/recovery blocking, immutable historical
terminal candidates and durable DOI projections, exact-byte Phase-A zero-write
preflight, page/notebook crash-resume and abort boundaries, durable `applying`
Discovery exclusion, immutable active-profile bindings across incremental and
forced rebuilds, typed matcher reasons, and five resolved-profile contracts.
Frozen comparison tests use injected providers, assert one fetch per sampling
key, reject actual Crossref comparison before writing files, keep Crossref
coverage explicitly synthetic, verify manifest hash/size/count, and replay
identical IDs/ranks without staging or synthetic human Precision.

The lifecycle matrix proves every required artifact missing from
`metadata_staged` is `repair_required`, while `reserved` stays unsettled and
does not advance allocation. Identity tests prove multiple provider identities
for one paper survive freeze. At 3000 existing + 100 staged, ledger performance
requires one cold build, about one load and at most two saves per new candidate,
zero post-refreshes, and direct publication for each successful stage.

Stale-snapshot regression coverage creates a Context before damaging each
required `metadata_staged` artifact and requires `repair_required` with no
allocation. It also rolls an active formal workspace back to `paper_raw` and
asserts targeted lifecycle refresh removes all old formal DOI/identity refs.
Adapter tests independently assert new allocation, reuse, and duplicate count
semantics. The compact patch gate uses one 1000-existing + 10-new smoke only.

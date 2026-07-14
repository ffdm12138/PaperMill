# Discovery Notebook v3 Migration — Verification Summary

## Overview

This document summarizes the completed migration of keyword-discovery
notebooks from the legacy v2 (keyword + expansions) schema to the active
v3 (keyword_zh + bilingual search_queries) schema. The migration was
applied to the five production Chinese classification notebooks.

## Migration Metadata (Sanitized)

| Field | Value | Evidence |
|---|---|---|
| Migration date | 2026-07 (completed before this verification) | runtime audit |
| Transaction ID | Short hash available in operator runtime state | runtime audit |
| Plan SHA256 prefix | unavailable in runtime-zero evidence | — |
| Source notebooks | 5 legacy v2 notebooks (atmospheric boundary layer, wind-blown sand dynamics, wind-blown sand physics, wind-driven snow dynamics, wind-driven snow physics) | runtime audit |
| Target notebooks | 5 v3 bilingual notebooks | runtime audit |
| Standalone English notebooks merged | 0 (all integrated as multilingual queries) | runtime audit |
| Page journals scanned by final audit | 12 | final audit summary |
| Unmapped queries | 0 | migration transaction evidence |
| Ambiguous mapping | 0 | migration transaction evidence |
| Cursor conflicts | 0 | recovery inspect |

## Notebook State (Post-Migration)

| keyword_zh | Schema | Enabled | ZH queries | EN queries | Ready |
|---|---|---|---|---|---|
| 大气边界层 | 3.0 | yes | 2 | 3 | yes |
| 风沙动力学 | 3.0 | yes | 2 | 3 | yes |
| 风沙物理学 | 3.0 | yes | 2 | 3 | yes |
| 风雪动力学 | 3.0 | yes | 2 | 2 | yes |
| 风雪物理学 | 3.0 | yes | 2 | 2 | yes |

All notebooks have `bilingual-ready` status with at least one active Chinese
and one active English query.

## Provider State

- **OpenAlex**: Current generation preserved in all notebooks. Cursors
  retained — no unintentional resets. Exhausted state preserved.
- **Crossref**: Current generation initialized with `*` cursor for new
  bilingual lanes; legacy pages preserved where applicable.

## Audit Results (2026-07-14)

| Check | Status |
|---|---|
| notebook_schema_safe | true |
| discovery_query_ready | true |
| backfill_state_safe | true |
| page_journal_safe | true |
| receipt_provenance_safe | true |
| migration_safe | true |
| Schema errors | 0 |
| Generation errors | 0 |
| Journal errors | 0 |
| Receipt errors | 0 |
| Pristine unbound lanes (never activated) | 50 |

The 50 pristine unbound lanes are query/provider pairs whose cursor is
still `*` with no pages succeeded, no journals, and no errors — they
have never performed a network request. These are counted in the
summary but do not appear as individual warnings.

## Recovery Inspect Results

- Recovery mode: `inspect-only` (no write operations)
- Recovery operations needed: 0
- Cursor divergence: 0
- Historical generation warnings: 0
- Lock files seen: 2 (normal operational state)

## Catalog State

- Categories: 5 (all Chinese)
- English directories: 0
- Chinese aliases: present (within same language)
- Directory structure: healthy
- Folder integrity: safe
- Broken/escaping links: 0
- Unknown directories: 0
- Pending: 0
- Missing decisions: 0
- Stale decisions: 0
- Unapplied results: 0
- Classification complete: true
- Writer category safe: true

## Discovery Dry-run

- Mode: `--dry-run` (no network I/O, no cursor advancement, no paper allocation)
- All 5 notebooks present in plan
- Provider lanes correctly configured for OpenAlex and Crossref
- Refresh/backfill dual lanes enabled
- Page budgets: 2 refresh pages, 5 backfill pages per lane (defaults)

## Final Verification

- Full pytest suite: 1708 passed, 8 skipped, 3 deselected, 0 failed
- Agent acceptance: passed (all 6 stages: pre-flight, syntax, full pytest,
  hygiene, pack, post-flight)
- Snapshot: runtime-zero, 1.0 MB, 490 files, 0 runtime entries
- AGENTS.md and CLAUDE.md: identical

## Classification Method

The four formal papers were classified by an agent performing manual
semantic analysis of each paper's Catalog v3.2 content. Classification
results were imported through the validated manual backend, passing
result schema validation, result validator checks, and formal apply
transactions before being recorded as applied decisions.

## Notes

- Real migration transaction artifacts (mapping JSON, plan JSON, backup
  files) are stored outside the source tree in operator-managed runtime
  directories and are excluded from the runtime-zero snapshot.
- This summary contains no real paper names, author lists, full SHAs,
  complete cursors, or transaction identifiers.
- Network discovery has not been re-started after migration. A real
  `--from-enabled-notebooks` run (without `--dry-run`) is required to
  advance cursors and populate candidates.

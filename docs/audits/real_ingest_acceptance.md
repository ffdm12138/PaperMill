# Real Ingest Acceptance Report (Redacted)

Generated at: 2026-06-30 Asia/Shanghai. Redacted and moved from `reports/` so
runtime reports and paper lists do not enter source snapshots.

## Summary

- Baseline formal library validation passed before positive ingest rehearsals.
- Network metadata positive ingest passed end to end through staging, PDF fetch,
  MinerU conversion, catalog curation, commit, catalog rebuild, and validation.
- Manual PDF positive ingest passed after running through the conda `mineru`
  environment with UTF-8 output enabled.
- Duplicate DOI rehearsal was blocked before commit, as expected.
- Final validation commands passed: rebuild all catalog, validate library, audit
  metadata quality, doctor ingest pipeline, directory hygiene, pytest, and
  snapshot packaging.

## Redaction Notes

- Real DOI values, paper ids, hashes, local queue contents, and formal paper
  paths were removed from this long-term audit copy.
- The original runtime report belonged under `reports/` and is intentionally
  excluded from snapshots.
- Future reproducible audit records that should live in source control should be
  written directly under `docs/audits/` after removing local runtime data.

## Operational Findings Preserved

- Real ingest commands must use `conda run -n mineru python` or `conda activate mineru`
  first. Bare `python` now resolves to the conda environment (Windows Store aliases removed).
- Windows console runs should set `PYTHONIOENCODING=utf-8` before emitting JSON
  containing non-ASCII text.
- Manual PDF ingest order remains: stage PDF, convert with MinerU, resolve
  metadata from converted Markdown, curate catalog, formalize, commit, rebuild
  all catalog, validate.
- Network metadata ingest order remains: stage DOI-bearing metadata, fetch PDF,
  convert, curate, formalize, commit, rebuild all catalog, validate.

## Final Verification Summary

- retired merged-index publication check: passed.
- `validate_v2_library.py`: passed with no hard errors.
- `audit_metadata_quality.py`: passed with no hard errors.
- `doctor_ingest_pipeline.py`: passed.
- `check_directory_hygiene.py`: passed.
- `pytest -q`: passed in the acceptance run.
- `pack_repo.py`: rebuilt the source snapshot while excluding generated catalog
  indexes and real paper/raw/paper_raw/import_work assets.

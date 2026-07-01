# Real Ingest Acceptance Report

Generated at: 2026-06-30 10:18 Asia/Shanghai; updated after manual PDF retry at 2026-06-30 10:45 Asia/Shanghai

## Summary

- Initial baseline before network positive: 18 papers, passed.
- Network metadata positive: 18 -> 19, passed.
- Manual PDF positive: 19 -> 20, passed.
- Negative duplicate DOI rehearsal: pass, preflight reported `doi_duplicate` before commit.
- Final validate/audit/doctor/pytest: passed.
- Freeze recommendation: freeze complete ingest v2.1 with one operational rule: all real ingest commands must run through the `.conda` MinerU environment, not bare `python`.

## Initial Baseline Before Network Positive

Note: the shell `python` command currently resolves to `<LOCAL_USER_HOME>\AppData\Local\Microsoft\WindowsApps\python.exe` and exits as the Windows Store placeholder. I ran the requested commands with the bundled Python executable:

`<LOCAL_USER_HOME>\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`

- `scripts/doctor_ingest_pipeline.py`: exit 0, `valid=true`, `blocking_count=0`; ingest subset `69 passed`.
- `scripts/rebuild_all_catalog.py --apply`: exit 0, `papers=18 written=True`.
- `scripts/validate_v2_library.py`: exit 0, `valid=True errors=0 warnings=3`.
- `scripts/audit_metadata_quality.py`: exit 0, `total=18`, `errors=[]`.
- `scripts/check_directory_hygiene.py`: exit 0, `valid=true`, `warning_count=0`.
- `pytest -q`: exit 0, `374 passed, 3 warnings`.
- `scripts/pack_repo.py`: exit 0, `mineru_snapshot.zip`, `226 files`, `0.3 MB`.

## Initial Catalog And Metadata Separation

- `data/catalog/all.catalog.json` contains 18 entries and remains content-oriented.
- No embedded bibliographic fields such as DOI, authors, year, journal, venue, volume, issue, pages, display, or raw metadata were found in catalog content.
- Each formal single-paper catalog has `paper_id`, `paper_number`, and `asset_refs`.
- `asset_refs.metadata` is present as an asset path reference; it is not an embedded metadata object and is accepted by `validate_v2_library.py`.
- Formal metadata retains DOI, authors, year, journal, volume, issue/pages or article number, and `metadata_match` for hard validation. One existing paper, `2015_Wekker_山区对流边界层高度综述`, still has soft publication issue/pages warnings, but audit hard errors remain empty.

## Manual PDF Initial Attempt Before Conda MinerU Env

Input: `data/raw/tc_9_255_2015.pdf`.

Isolation: copied only this PDF to `data/import_work/real_ingest_acceptance/manual_raw/` and ran stage with `--raw-dir` so the 186 existing `data/raw/*.pdf` files were not bulk-staged.

Results:

- Stage: exit 0, created `data/paper_raw/000001/000001.pdf`; sha256 `6bde99b1d6ed71a10affa3e60b0fcd9eeed92e331465ee7f72f8945340fbe9dc`.
- Metadata match: exit 1, status `unmatched`; warning showed PyMuPDF/fitz is not installed, so PDF text DOI extraction was skipped.
- Convert: exit 1, failed with `[WinError 2] 系统找不到指定的文件。`; MinerU CLI/runtime is not available.
- Resolve candidates: exit 0, `status=no_candidates`.
- Curate dry-run: exit 1, correctly rejected by gate: `paper_raw curation requires metadata_match.status matched or manual_confirmed`.
- Commit: not attempted, because metadata was not matched and conversion did not produce Markdown/images.

Disposition: moved to `data/paper_raw/quarantine/real_ingest_acceptance_manual_000001/`.

## Manual PDF Positive Retry With Conda MinerU Env

Input: `data/paper_raw/quarantine/real_ingest_acceptance_manual_000001/000001.pdf`, copied to isolated `data/import_work/real_ingest_acceptance/manual_retry_raw/manual_retry.pdf`.

All commands used `conda run -n mineru ...` with `PYTHONIOENCODING=utf-8`; no bulk staging from `data/raw/` was performed.

Results:

- Stage: exit 0, created `data/paper_raw/000001/000001.pdf`; sha256 `6bde99b1d6ed71a10affa3e60b0fcd9eeed92e331465ee7f72f8945340fbe9dc`.
- Metadata match: exit 0, `status=matched`, source `crossref`.
- Convert: exit 0, real MinerU conversion succeeded in about 69 s, producing `000001.md`, `images/`, and `output/000001`.
- Resolver: exit 0, no unmatched items.
- Curate dry-run: exit 0, generated `curation_prompt.md`.
- Generated `000001.catalog.json` from the MinerU Markdown as content-only catalog; schema validation passed and forbidden catalog keys were empty before apply.
- Curate apply: exit 0, curated to `2015_Divine_Regional_melt_pond_fraction_and_albedo_of_thin_Arctic_first_year_drift_ice_in_late_summer`.
- Commit: exit 0, imported as `paper_number=0000000000000020`; `all_catalog_count=20`.

New formal assets:

- `data/papers/2015_Divine_Regional_melt_pond_fraction_and_albedo_of_thin_Arctic_first_year_drift_ice_in_late_summer/2015_Divine_Regional_melt_pond_fraction_and_albedo_of_thin_Arctic_first_year_drift_ice_in_late_summer.metadata.json`
- `data/papers/2015_Divine_Regional_melt_pond_fraction_and_albedo_of_thin_Arctic_first_year_drift_ice_in_late_summer/2015_Divine_Regional_melt_pond_fraction_and_albedo_of_thin_Arctic_first_year_drift_ice_in_late_summer.catalog.json`
- Matching Markdown, PDF, images directory, and `0000000000000020.paper.number`.

Post-commit checks for the new paper:

- New single-paper catalog has `paper_id`, `paper_number`, and `asset_refs`; forbidden catalog keys are empty under project validation.
- New metadata retains DOI `10.5194/tc-9-255-2015`, 8 authors, year `2015`, journal `The Cryosphere`, volume `9`, issue `1`, pages `255-268`, and `metadata_match.status=matched`.
- `audit_metadata_quality.py` reports only soft warnings for this paper (`missing abstract`, `missing keywords`, `missing source.raw_record`); hard errors remain empty.

## Network Metadata Positive Rehearsal

Input DOI: `10.5194/tc-18-4933-2024`.

Source used for candidate metadata: The Cryosphere article page, `https://tc.copernicus.org/articles/18/4933/2024/`.

Results:

- Stage metadata: exit 0, created `data/paper_raw/000001/`.
- Fetch PDF: functionally succeeded and attached `000001.pdf` with sha256 `3222e83e1bfe84bd7dfadc15b91defd0cef7afcc05429db085fb2b25a76b583f`; command exit was 1 because Windows GBK stdout could not encode a narrow-space character in the printed JSON. Re-running later commands with `PYTHONIOENCODING=utf-8` avoided this output issue.
- Fetch resolver observations: Unpaywall returned 422 and OpenAlex returned 429, but publisher OA resolver succeeded and downloaded from the Copernicus PDF URL.
- Metadata match: exit 0, `status=matched`.
- Preflight strict: exit 0, `status=ready_for_convert`, `blocking=false`, `errors=[]`.
- Convert with `--only-preflight-ready`: exit 1, `preflight_status=ready_for_convert`, then failed at MinerU CLI with `[WinError 2] 系统找不到指定的文件。`.
- Curate dry-run: exit 0, generated `data/paper_raw/000001/curation_prompt.md`.
- Commit: intentionally not attempted per rehearsal scope.

Follow-up with `.conda` MinerU environment:

- Environment: `conda run -n mineru python --version` -> Python 3.10.20.
- `conda run -n mineru python -c "import fitz"`: PyMuPDF OK.
- `conda run -n mineru python -c "import mineru"`: MinerU import OK.
- `conda run -n mineru mineru --version`: MinerU 3.4.0.
- `conda run -n mineru python scripts/check_mineru_processes.py`: GPU visible, MinerU lock unlocked, runner `cli`.
- `conda run -n mineru python scripts/preflight_paper_raw_import.py --all --strict`: `ready_for_convert`, `blocking_count=0`.
- `conda run -n mineru python scripts/convert_paper_raw_batch.py --source-id 000001 --only-preflight-ready --apply`: exit 0, real MinerU conversion succeeded in about 96 s, producing `000001.md`, `images/`, and `output/000001`.
- Generated content-only `000001.catalog.json`; schema validation passed and forbidden catalog keys were empty.
- `conda run -n mineru python scripts/curate_paper_raw.py --source-id 000001 --apply`: exit 0, curated to `2024_Gadde_Contribution_of_blowing_snow_sublimation_to_the_surface_mass_balance_of_Antarctica`.
- `conda run -n mineru python scripts/commit_paper_raw_to_papers.py --all-ready --apply`: exit 0, imported as `paper_number=0000000000000019`; `all_catalog_count=19`.

Disposition: committed to `data/papers/2024_Gadde_Contribution_of_blowing_snow_sublimation_to_the_surface_mass_balance_of_Antarctica/`.

## Negative Duplicate DOI Rehearsal

Input DOI: `10.5194/acp-25-12535-2025`, already present in formal paper `2025_Huang_雪粒破碎增强升华`.

Results:

- Stage metadata: exit 0, created `data/paper_raw/000002/`.
- Metadata match: exit 1, `source PDF or metadata missing`; expected because this negative candidate was metadata-only.
- Preflight strict: exit 1, `blocking_count=1`, errors included `doi_duplicate`, `metadata_unmatched`, and `pdf_missing`.
- Commit: not attempted.

Disposition: moved to `data/paper_raw/quarantine/real_ingest_acceptance_dup_000002/`.

## Final Verification

- `conda run -n mineru python scripts/rebuild_all_catalog.py --apply`: exit 0, `papers=20 written=True`.
- `conda run -n mineru python scripts/validate_v2_library.py`: exit 0, `valid=True errors=0 warnings=3`.
- `conda run -n mineru python scripts/audit_metadata_quality.py`: exit 0, `total=20`, `errors=[]`.
- `conda run -n mineru python scripts/doctor_ingest_pipeline.py`: exit 0, `valid=true`, `blocking_count=0`.
- `conda run -n mineru pytest -q`: exit 0, `374 passed, 8 warnings`.
- `conda run -n mineru python scripts/pack_repo.py`: exit 0, `mineru_snapshot.zip` rebuilt. Generated catalog indexes and real paper/raw/paper_raw/import_work assets were excluded from the snapshot by the current pack rules.
- Final all.catalog check: `count=20`, forbidden catalog hits empty.
- Final new manual-paper catalog check: `paper_id`, `paper_number`, and `asset_refs` present; forbidden catalog keys empty.
- Final new manual-paper metadata check: DOI, authors, year, journal, volume, issue/pages, and `metadata_match` present.

## Freeze Recommendation

Recommend freezing complete ingest v2.1 with the following operational constraints:

- Always run real ingest commands with `conda run -n mineru ...` or the absolute interpreter under `%USERPROFILE%\.conda\envs\mineru\`.
- Set `PYTHONIOENCODING=utf-8` for Windows console runs to avoid JSON output failures under GBK.
- Continue forbidding bare `python` for this repository's real ingest workflow.

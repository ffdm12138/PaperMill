# Literature Library Manager Skill

Use this skill for pure v2 `paper_raw` literature library work.

Formal import commands（manual PDF path — convert first, then resolve metadata from converted md）:

```bat
:: Windows cmd.exe only (Git Bash: use export VAR=value instead)
set MINERU_REQUIRE_GPU=true
set CUDA_VISIBLE_DEVICES=0
set MINERU_RUNNER=cli_api_proxy
set MINERU_API_URL=http://127.0.0.1:8000
python scripts/start_mineru_services.py --wait --restart-if-stale
python scripts/stage_raw_pdfs_to_paper_raw.py --move --apply
python scripts/check_mineru_processes.py
python scripts/smoke_mineru_conversion.py --paper-number 0000000000000001 --apply --report reports/smoke_mineru_conversion.json
python scripts/convert_paper_raw_gpu.py --all --apply --report reports/convert_paper_raw.json
python scripts/resolve_paper_raw_metadata.py --all-unmatched --apply --allow-network
python scripts/curate_paper_raw.py --all-ready --dry-run
python scripts/curate_paper_raw.py --all-ready --apply
python scripts/formalize_paper_raw.py --all-ready --apply --report reports/formalize_paper_raw.json
python scripts/commit_paper_raw_to_papers.py --all-ready --apply
python scripts/reconcile_catalog_folders.py --apply
python scripts/validate_v2_library.py
python scripts/stop_mineru_services.py
```

Formal rollback/reingest SOP（only when explicitly rebuilding the formal library）:

```bash
# Dry-run first — always.
conda run -n mineru python scripts/rollback_formal_papers_to_paper_raw.py --all-papers --report reports/rollback_dryrun.json
# Review report: summary.blocking_errors == 0 and summary.failed == 0 before apply.
# Apply only when dry-run is clean.
conda run -n mineru python scripts/rollback_formal_papers_to_paper_raw.py --all-papers --apply --report reports/rollback_apply.json
conda run -n mineru python scripts/validate_rolled_back_paper_raw.py
conda run -n mineru python scripts/curate_paper_raw.py --all-ready --dry-run
# After paper_raw_catalog_curator writes <paper_number>.catalog.json:
conda run -n mineru python scripts/curate_paper_raw.py --all-ready --apply
conda run -n mineru python scripts/formalize_paper_raw.py --all-ready --apply --report reports/formalize_after_rollback.json
conda run -n mineru python scripts/commit_paper_raw_to_papers.py --all-ready --apply
conda run -n mineru python scripts/validate_v2_library.py
conda run -n mineru python scripts/pack_repo.py
```

- Default mode is **dry-run**; ``--apply`` is required to mutate data.
- ``--paper-number``, ``--paper-name``, and ``--all-papers`` are mutually exclusive.
- ``--paper-name`` resolves via active rollback journals first (for crash recovery), then active ledger entries.  Interrupted rollbacks are recovered by repeating the same command.
- ``--report PATH`` writes a structured JSON report; review ``summary.blocking_errors`` before applying.
- Never manually delete rollback journals, quarantine directories, or lock files.
- Never directly modify the paper_number ledger during rollback.
- ``--keep-catalog`` does not exist; Catalog browsing uses category folders, not merged index files.

## Launching MinerU services

Recommended (conda on PATH):

```bash
conda run -n mineru python scripts/start_mineru_services.py --wait --restart-if-stale
conda run -n mineru python scripts/check_mineru_processes.py
conda run -n mineru python scripts/smoke_mineru_conversion.py --paper-number 0000000000000001 --apply --report reports/smoke_mineru_conversion.json
conda run -n mineru python scripts/convert_paper_raw_gpu.py --all --apply --report reports/convert_paper_raw.json
conda run -n mineru python scripts/stop_mineru_services.py
```

如果 conda 不在 PATH，用 env python 绝对路径：

```bash
C:\Users\Admin\.conda\envs\mineru\python.exe scripts\start_mineru_services.py --wait --restart-if-stale
C:\Users\Admin\.conda\envs\mineru\python.exe scripts\check_mineru_processes.py
C:\Users\Admin\.conda\envs\mineru\python.exe scripts\smoke_mineru_conversion.py --paper-number 0000000000000001 --apply --report reports\smoke_mineru_conversion.json
C:\Users\Admin\.conda\envs\mineru\python.exe scripts\convert_paper_raw_gpu.py --all --apply --report reports/convert_paper_raw.json
C:\Users\Admin\.conda\envs\mineru\python.exe scripts\stop_mineru_services.py
```

start_mineru_services.py must resolve Scripts/mineru-api.exe from the current Python env (find_mineru_api_exe). Do not manually background mineru-api.exe as a long-term SOP.

Network metadata path（metadata 已带 DOI，先行）: stage_network_metadata_to_paper_raw.py
→ fetch_pdf_for_paper_raw.py → convert → curate → formalize → commit → rebuild → validate.
Metadata-only PDF fetch only fills existing 16-digit `paper_raw` workspaces:
DOI comes from `<paper_number>.metadata.json`, not `doi.csv`; fetch does not
allocate paper_numbers; successful PDFs attach through duplicate guard as
`<paper_number>.pdf`. Default fetch is OA-only. Header-based fetch is explicit,
uses a fixed in-code User-Agent, and never persists header values.
手动 PDF 路径 metadata resolver 依赖转换后的 md，必须在 MinerU 转换之后运行（先转换，再解析）。
v2.3 状态机：staging 阶段 reserve 16 位 paper_number；`curate_paper_raw.py` 只校验并写 `catalog_ready`（不改名、不分配 paper_number）；
初始 catalog 生成阶段 `screening.read_decision` 必须固定为 `"pending"`，不得写成 `must_read` /
`maybe_read` / `skip`；这些值只用于 post-triage / writing-stage catalog 或人工筛选；
Phase 1–3 `formalize_paper_raw.py` 对 frozen workspace 必须 fail closed；后续 Phase 5 才启用 plan-only formalize，再由 Phase 6 commit staging 改名。
`commit_paper_raw_to_papers.py` 只接收数字 workspace 的 current formalization plan，通过外置 journal 前向恢复安装。
`formalize_paper_raw.py` / `commit_paper_raw_to_papers.py` 支持完整路径隔离
（`--paper-raw-dir`/`--papers-dir`/`--ledger-path`/`--all-catalog-path`）；测试/agent 必须传 tmp
`--ledger-path` 与 tmp `--all-catalog-path`，禁止污染真实 `data/catalog`。metadata 候选读转换后 Markdown 前 100 行。
`formalize_paper_raw.py` 默认只接受 `converted_current`；legacy converted assets 必须在
缺 manifest 的已转换资产必须先生成当前 manifest 或重新转换。普通 ingest 测试夹具从 16 位 `paper_number`
开始；formalize 不改名/repoint。`Catalog.load()`
是 tolerant read-only snapshot，不写 ledger/marker/catalog 索引。

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

Writing workspace creation:

```bash
python scripts/create_write_job.py --job-id review_001 --paper-numbers 0000000000000001
```

Manual PDF staging SOP:

- `data/raw/` is the manual PDF queue / raw 是待处理队列.
- Normal manual ingest uses `stage_raw_pdfs_to_paper_raw.py --move --apply`.
- Successful staging consumes PDFs from `data/raw/` and places them under `data/paper_raw/<paper_number>/<paper_number>.pdf`.
- Copy mode is only for debugging, backup, tests, or explicit one-off inspection.
- MinerU conversion requires GPU / MinerU 正式转换必须使用 GPU. Staging does not need GPU; formal
  ingest uses `convert_paper_raw_gpu.py`, which defaults `MINERU_REQUIRE_GPU=true`,
  `CUDA_VISIBLE_DEVICES=0`, and checks `torch.cuda.is_available()`. The lower-level
  `convert_paper_raw_batch.py` is compatibility/debug plumbing.
- Batch conversion should use persistent `mineru-api` via `MINERU_RUNNER=cli_api_proxy` and
  `MINERU_API_URL=http://127.0.0.1:8000`. `/health` is liveness only, not GPU conversion readiness.
  Start/reuse managed mineru-api with `python scripts/start_mineru_services.py --wait --restart-if-stale`,
  verify `check_mineru_processes.py` verdict is `READY_FOR_CONVERSION`, run one
  `smoke_mineru_conversion.py` report, then run formal `--all --apply`. Stop with
  `python scripts/stop_mineru_services.py`. Start mineru-api with
  `CUDA_VISIBLE_DEVICES=0` in its own shell; setting it only in the client cannot
  change an already-running service.
  The formal wrapper reads `reports/smoke_mineru_conversion.json` by default; pass
  `--smoke-report <path>` only when overriding that path.
- `start_fast_api_mode.bat` is a compatibility wrapper around the Python starter and uses
  `--restart-if-stale`.
- Large PDF MinerU conversion has no process-level timeout. Health/preflight/HTTP
  and lock wait timeouts are separate checks.
- Metadata title/author/affiliation/abstract/keyword/DOI candidates come from
  converted Markdown first 100 lines as front-matter evidence before PDF title fallback.
- Multi-source formal conversion must not use `MINERU_RUNNER=cli`; single-source CLI is for
  debug/test only.
- `paper_raw` conversion is idempotent. Existing `<paper_number>.md` + `images/` is skipped by
  default, successful conversion writes `<paper_number>.conversion.json`, and stale/partial
  conversion states require explicit `--force-reconvert`.
- Non-localhost API exposure requires `MINERU_API_KEY` unless an explicit unsafe override is set.

Facts:

- `data/raw/` is the manual PDF queue.
- `data/paper_raw/` is the pre-ingest workspace.
- `data/papers/` is formal storage.
- `data/catalog/<category>/` is the folder-backed writing browse view.
- `data/catalog/paper_number_ledger.json` owns stable numbering.
- `write/jobs/<job_id>/article/<paper_number>/` is the writing article workspace.
- `metadata.json` is the bibliographic source of truth for BibTeX.
- Each paper Catalog is content-only.
- Network OpenAlex/CrossRef metadata with a valid DOI is staged as
  Metadata remains resolved until independent PDF evidence writes the match/freeze receipts.
- Formalize/commit produce the final catalog asset references as part of the
  active transaction.  Stale legacy catalogs must be regenerated or repaired
  outside the active workflow; no retired repair script is supported here.
- catalog natural-language values default to Chinese; JSON keys/schema enums stay English, and technical terms may remain English or mixed Chinese-English.
- Initial generated catalog uses `screening.read_decision = "pending"`; final read decisions are post-triage / writing-stage annotations.
- metadata remains original/canonical bibliographic facts and is not rewritten for catalog Chinese localization.

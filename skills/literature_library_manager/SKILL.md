# Literature Library Manager Skill

Use this skill for pure v2 `paper_raw` literature library work.

Formal import commands（manual PDF path — convert first, then resolve metadata from converted md）:

```bash
set MINERU_REQUIRE_GPU=true
set CUDA_VISIBLE_DEVICES=0
set MINERU_RUNNER=cli_api_proxy
set MINERU_API_URL=http://127.0.0.1:8000
python scripts/start_mineru_services.py --wait
python scripts/stage_raw_pdfs_to_paper_raw.py --move --apply
python scripts/convert_paper_raw_gpu.py --all --apply --report reports/convert_paper_raw.json
python scripts/resolve_paper_raw_metadata.py --all-unmatched --apply --allow-network
python scripts/curate_paper_raw.py --all-ready --dry-run
python scripts/curate_paper_raw.py --all-ready --apply
python scripts/formalize_paper_raw.py --all-ready --apply --report reports/formalize_paper_raw.json
python scripts/commit_paper_raw_to_papers.py --all-ready --apply
python scripts/rebuild_all_catalog.py --apply
python scripts/validate_v2_library.py
python scripts/stop_mineru_services.py
```

Network metadata path（metadata 已带 DOI，先行）: stage_network_metadata_to_paper_raw.py
→ fetch_pdf_for_paper_raw.py → convert → curate → formalize → commit → rebuild → validate.
手动 PDF 路径 metadata resolver 依赖转换后的 md，必须在 MinerU 转换之后运行（先转换，再解析）。
v2.2 状态机：`curate_paper_raw.py` 只校验并写 `catalog_ready`（不改名、不分配 paper_number）；
`formalize_paper_raw.py` 是 commit 前必经步骤（改名 + reserve 16 位 paper_number + `ready_for_commit`）；
`commit_paper_raw_to_papers.py` 只接收 `ready_for_commit`，事务性安装，失败回滚不污染 `data/papers`。
`formalize_paper_raw.py` / `commit_paper_raw_to_papers.py` 支持完整路径隔离
（`--paper-raw-dir`/`--papers-dir`/`--ledger-path`/`--all-catalog-path`）；测试/agent 必须传 tmp
`--ledger-path` 与 tmp `--all-catalog-path`，禁止污染真实 `data/catalog`。metadata 候选读转换后 Markdown 前 100 行。

Writing workspace creation:

```bash
python scripts/create_write_job.py --job-id review_001 --paper-numbers 0000000000000001
```

Manual PDF staging SOP:

- `data/raw/` is the manual PDF queue / raw 是待处理队列.
- Normal manual ingest uses `stage_raw_pdfs_to_paper_raw.py --move --apply`.
- Successful staging consumes PDFs from `data/raw/` and places them under `data/paper_raw/<source_id>/<source_id>.pdf`.
- Copy mode is only for debugging, backup, tests, or explicit one-off inspection.
- MinerU conversion requires GPU / MinerU 正式转换必须使用 GPU. Staging does not need GPU; formal
  ingest uses `convert_paper_raw_gpu.py`, which defaults `MINERU_REQUIRE_GPU=true`,
  `CUDA_VISIBLE_DEVICES=0`, and checks `torch.cuda.is_available()`. The lower-level
  `convert_paper_raw_batch.py` is compatibility/debug plumbing.
- Batch conversion should use persistent `mineru-api` via `MINERU_RUNNER=cli_api_proxy` and
  `MINERU_API_URL=http://127.0.0.1:8000`. Start/reuse mineru-api with
  `python scripts/start_mineru_services.py --wait`, then stop with
  `python scripts/stop_mineru_services.py`. Start mineru-api with
  `CUDA_VISIBLE_DEVICES=0` in its own shell; setting it only in the client cannot
  change an already-running service.
- `start_fast_api_mode.bat` is a compatibility wrapper around the Python starter.
- Large PDF MinerU conversion has no process-level timeout. Health/preflight/HTTP
  and lock wait timeouts are separate checks.
- Metadata title/author/affiliation/abstract/keyword/DOI candidates come from
  converted Markdown first 100 lines as front-matter evidence before PDF title fallback.
- Multi-source formal conversion must not use `MINERU_RUNNER=cli`; single-source CLI is for
  debug/test only.
- `paper_raw` conversion is idempotent. Existing `<source_id>.md` + `images/` is skipped by
  default, successful conversion writes `<source_id>.conversion.json`, and stale/partial
  conversion states require explicit `--force-reconvert`.
- Non-localhost API exposure requires `MINERU_API_KEY` unless an explicit unsafe override is set.

Facts:

- `data/raw/` is the manual PDF queue.
- `data/paper_raw/` is the pre-ingest workspace.
- `data/papers/` is formal storage.
- `data/catalog/all.catalog.json` is the local generated content-only writing index.
- `data/catalog/paper_number_ledger.json` owns stable numbering.
- `write/jobs/<job_id>/article/<paper_number>/` is the writing article workspace.
- `metadata.json` is the bibliographic source of truth for BibTeX.
- `catalog.json` and `all.catalog.json` are content-only.
- catalog natural-language values default to Chinese; JSON keys/schema enums stay English, and technical terms may remain English or mixed Chinese-English.
- metadata remains original/canonical bibliographic facts and is not rewritten for catalog Chinese localization.

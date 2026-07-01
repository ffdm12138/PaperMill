# Project Status

本文档是 MinerU v2 文献资产库的当前状态总览：冻结版本、主流程、边界、待办与禁止事项。
新 agent 或未来维护者应先读此文件，再进入 `docs/PROJECT_CONTRACT.md` 与
`docs/DEPENDENCIES_AND_EXTERNAL_TOOLS.md`。

## 1. 当前冻结版本

### ingest v2.3 current incremental state

- 新 ingest 的 `data/paper_raw/<id>/` 统一使用 16 位 `paper_number`。PDF-first 与 metadata-first staging 第一步 reserve 编号、创建 `paper_raw/<paper_number>/`、写 ledger reserved item 与 `<paper_number>.paper.number` marker。
- `--paper-number` / `--paper-numbers` 是正常 CLI 唯一 paper_raw selector；旧 source-id selector 已退出正常流程，6 位目录只允许 `scripts/legacy/` migration 工具处理。
- 本轮不打新 tag；`ingest-v2.2` 仍是上一冻结 tag。

### ingest v2.2 frozen

- tag：`ingest-v2.2`（v2.1 已被状态机重构取代）。
- v2.2 变化：`curate_paper_raw.py` 不再改名/不再分配 paper_number/不再写 `ready_for_commit`，只写 `catalog_ready`；新增 `scripts/formalize_paper_raw.py` 在 `data/paper_raw` 内完成正式化（canonical paper_id 改名 + reserve 16 位 paper_number + 回填 catalog 链接 + `<paper_id>.formalization.json` + `<16位>.paper.number` marker + `ready_for_commit`）；`commit_paper_raw_to_papers.py` 退化为事务性安装（final validate → staging copytree → 自检 → `os.replace` → activate ledger → rebuild all.catalog → postcheck → 删源），后置失败回滚删除 `data/papers/<paper_id>`，不污染正式库。catalog/metadata schema 不变。
- 验收：`validate_v2_library.py` / `audit_metadata_quality.py` / `doctor_ingest_pipeline.py` /
  `pytest -q` 全绿。
- catalog schema 保持 v2.0；新 metadata schema 为 v1.1，使用 `paper_number` / `paper_raw_id`，不再生成 `source_id`。
- CLI 路径隔离：`formalize_paper_raw.py` 与 `commit_paper_raw_to_papers.py` 都暴露 `--paper-raw-dir`/`--papers-dir`/`--ledger-path`/`--all-catalog-path`；测试/agent 必须传 tmp `--ledger-path` 与 tmp `--all-catalog-path`，禁止污染真实 `data/catalog`（默认值指向真实账本，被 gitignore，`git status` 看不出）。
- `ready_for_commit` 阶段 ledger reserved entry 必须 repoint 到 formalized 后的 `<paper_id>` 工作区（不是旧 `0000000000000001`），由 `PaperNumberLedger.repoint_reserved` 在 formalize rename 后完成。
- metadata 候选读转换后 Markdown 前 100 行（first 100 lines，非 10）。
- formalize 只接受 `converted_current` conversion manifest；缺 manifest 的已转换资产为 `conversion_manifest_missing`，必须先生成当前 manifest 或重新转换。`preserve_paper_number` 走 reserved-specific 保留编号语义，不走 legacy `repoint()`。
- 普通 ingest/commit 测试夹具从 16 位 `paper_number` 开始，happy path 不手写 `.formalization.json` 或 `ready_for_commit`；marker 必须由 allocator/staging/ledger helper 写入。
- `Catalog.load()` 缺少 all.catalog 时构建 tolerant read-only snapshot，不写 ledger、marker、per-paper catalog、all.catalog 或 paper_index。

### writing v0.1 frozen

- tag：`writing-v0.1`
- 验收：mechanical writing loop、deterministic quality checks、two real-topic samples。
- 不修改 writer 工作流。

## 2. 当前主流程

### 入库主流程（两条路径）

Network metadata path（metadata 先行，已有 DOI）:
```text
network metadata (with DOI)
-> data/paper_raw/<0000000000000001>/
-> fetch_pdf_for_paper_raw
-> MinerU convert（hybrid-engine + medium + auto）
-> catalog curation（content-only，写 catalog_ready）
-> formalize_paper_raw（改名 + reserve paper_number + ready_for_commit）
-> commit data/papers/<paper_id>/
-> rebuild data/catalog/all.catalog.json
-> validate / audit / doctor
```

Manual PDF path（先转换，再从转换后的 md 解析 metadata）:
```text
data/raw/*.pdf
-> stage_raw_pdfs_to_paper_raw --move --apply
-> data/paper_raw/<0000000000000001>/
-> MinerU convert（hybrid-engine + medium + auto）
-> resolve_paper_raw_metadata（读转换后的 md，抽取候选并联网验证/查询）
-> catalog curation（content-only，写 catalog_ready）
-> formalize_paper_raw（改名 + reserve paper_number + ready_for_commit）
-> commit data/papers/<paper_id>/
-> rebuild data/catalog/all.catalog.json
-> validate / audit / doctor
```

手动 PDF 导入时，metadata resolver 必须基于 MinerU 转换完成后的 md，因此顺序是**先转换，再解析 metadata**。
`data/raw/` is a queue / raw 是待处理队列；正常 stage 必须 `--move --apply`，成功后 raw 中对应
PDF 应消失。copy 模式只用于调试、备份、测试或明确的一次性检查，不是默认导入规范。
MinerU conversion requires GPU / MinerU 正式转换必须使用 GPU：formal ingest 使用
`scripts/convert_paper_raw_gpu.py`，默认 `MINERU_REQUIRE_GPU=true`、`CUDA_VISIBLE_DEVICES=0`，
并在转换前检查 `nvidia-smi` 与 `torch.cuda.is_available()`。`stage_raw_pdfs_to_paper_raw.py`
不需要 GPU；底层 `convert_paper_raw_batch.py` 仅作为兼容/调试入口保留，也会 warning 并执行同样的
formal GPU 默认。CPU/no-GPU 只允许调试：显式 `MINERU_ALLOW_CPU=true` 或 `MINERU_REQUIRE_GPU=false`。
批量转换优先使用持久 `mineru-api`：推荐入口是
`python scripts/start_mineru_services.py --wait`，然后设置 `MINERU_RUNNER=cli_api_proxy` 与
`MINERU_API_URL=http://127.0.0.1:8000`。`mineru-api` 必须在它自己的 shell 中以
`CUDA_VISIBLE_DEVICES=0` 启动；client 侧设置不能改变已运行的 `mineru-api`。
转换完成后可用 `python scripts/stop_mineru_services.py` 关闭服务。大 PDF MinerU 转换没有
进程级 timeout；health/preflight/HTTP 与 `MinerULock` 等待 timeout 不是 PDF 固定秒数限制。
metadata 标题/作者/单位/摘要/关键词/DOI 候选优先来自转换后 Markdown 的物理前 100 行
front-matter evidence，PDF title extraction 只作 fallback。
手动 PDF 初始 unmatched，不要用 `--only-preflight-ready` 挡住转换。网络 metadata 已有 DOI，可安全使用
`--only-preflight-ready`。两条路径 commit 前都要求 `metadata_match.status` 为 `matched` 或
`manual_confirmed`、DOI 非空、catalog 合法。

### 写作主流程

```text
selected catalog / paper numbers
-> write/jobs/<job_id>/article/<paper_number>/
-> TeX / BibTeX（references.bib 仅从 metadata 生成）
-> compile / check（check_write_tex_project.py）
-> quality check（check_write_quality_text.py）
```

## 3. 当前边界

- `metadata` 是书目信息事实源（DOI / 作者 / 年份 / 期刊 / 卷期页）。
- `catalog`（schema v2.0）是 content-only，不含书目字段。
- catalog 自然语言 value 默认尽量中文；JSON key/schema enum 保持英文，技术名词可中英混写。
  metadata 保持原始/规范书目事实，不因 catalog 中文化而改写。
- `write/jobs/` 是运行时，不提交（只跟踪 `.gitkeep`）。
- `write/jobs/<job_id>/article/` is the only writing article workspace.
- TeX 不得直接读 `data/papers`、`data/raw` 或 `data/paper_raw`。
- 真实入库 / 转换 / 写作必须使用 `conda run -n mineru`。
- snapshot 不含真实 data 与 `write/jobs` 运行产物。

## 4. 当前待办

- writer v0.2 P0（已实现）：`doctor_write_pipeline.py`、`create_write_job.py`。
- Writing system status（已明确）：
  - Default article-writing skill: `skills/catalog_tex_writer`.
  - Support skills (not article-writing): `paper_raw_metadata_resolver`、`paper_raw_catalog_curator`、`literature_library_manager`.
  - Advanced workflow: `scripts/write_review.py` / `src/writer/*`（含 `src/server.py` `/write/jobs/*` HTTP API），是 advanced / experimental multi-stage writer workflow，不是 legacy、不是默认推荐入口。
  - 默认稳定主入口是 catalog-to-TeX mini loop（`create_write_job.py` → `write_catalog_tex_article.py` → `check_write_tex_project.py` → `check_write_quality_text.py`）。
  - Active writing workspace: `write/jobs/<job_id>/article/<paper_number>/`；BibTeX/cite-key 只从 copied article metadata 生成。
  - Deprecated workspace: the legacy llm work directory is forbidden and must not be used by active writing flows.
- 第三方解耦（本轮完成）：`src/fetch/proxy.py` 已抽出共享代理逻辑；
  Sci-Hub 标注为 unsafe optional / 默认 disabled / 不属于 OA_ONLY 主流程。
- 后续可选（planned only）：
  - job-local literature matrix
  - mechanism / table / figure outline
  - stronger deterministic writing quality checks

## 5. 不做事项

- 不引入 RAG / embedding / vector DB / ChromaDB。
- 不内置 LLM client。
- 不把真实 data / `write/jobs` 运行产物入 snapshot。
- 不让外部 metadata 直接写正式库（必须经 `paper_raw` + validate/audit）。
- 不放宽 Sci-Hub 启用条件。


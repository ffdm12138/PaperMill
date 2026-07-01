# 文献工坊 (PaperMill)

本项目是本地文献资产库、AI 可读目录和博士论文级综述写作工作区。它只保留纯 v2 `paper_raw` 工作流，不做向量库、RAG、embedding，也不内置 LLM client。所有 prompt 和写作步骤只生成可复制文本或结构化模板。

## Project status and documentation map

- **ingest v2.2 已冻结**（tag `ingest-v2.2`；v2.1 已被状态机重构取代：curate 不再改名，新增 `formalize_paper_raw.py`，commit 退化为事务性安装）；**writing v0.1 已冻结**（tag `writing-v0.1`）。本轮不打新 tag。
- 项目**不使用 RAG / embedding / vector database / ChromaDB**，也不内置 LLM client。
- 真实入库 / 转换 / 写作必须使用 `conda run -n mineru ...`（PATH 上的 python 是 Windows Store 别名，会静默退出）。
- snapshot 不包含真实 `data/` 文献资产与 `write/jobs/` 运行产物（只跟踪 `.gitkeep`）。

文档入口：

- [AGENTS.md](AGENTS.md) — 所有 coding agent 的项目操作规约（状态、边界、主流程、提交前检查）
- [CLAUDE.md](CLAUDE.md) — Claude/Codex 类 agent 操作提醒与关键边界速查
- [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md) — 当前冻结版本、主流程、边界、待办与禁止事项总览
- [docs/PROJECT_CONTRACT.md](docs/PROJECT_CONTRACT.md) — 不可改变的核心契约与边界
- [docs/DEPENDENCIES_AND_EXTERNAL_TOOLS.md](docs/DEPENDENCIES_AND_EXTERNAL_TOOLS.md) — 依赖、网络服务、PDF resolver、Sci-Hub unsafe optional
- [docs/WRITING_QUALITY_ACCEPTANCE.md](docs/WRITING_QUALITY_ACCEPTANCE.md) — 写作质量验收规则
- [docs/WRITER_PRODUCTIZATION_PLAN.md](docs/WRITER_PRODUCTIZATION_PLAN.md) — writer v0.2 产品化计划
- [reports/real_ingest_acceptance.md](reports/real_ingest_acceptance.md) — 真实入库验收记录

## Quick validation

全部使用 mineru conda 环境（PATH 上的 `python` 是 Windows Store 别名，会静默退出）：

```bash
conda run -n mineru pytest -q
conda run -n mineru python scripts/pack_repo.py
conda run -n mineru python scripts/validate_v2_library.py
conda run -n mineru python scripts/audit_metadata_quality.py
conda run -n mineru python scripts/doctor_ingest_pipeline.py
```

## Explicit non-goals

- no RAG / no embedding / no vector DB / no ChromaDB
- no LLM client in code（所有 prompt/写作步骤只生成文本或模板）
- 不提交真实 data 与 `write/jobs` 运行产物（只跟踪 `.gitkeep`）


## 唯一数据流

两条入库路径，区别在于 metadata 与 MinerU 转换的先后：

```text
Network metadata path（metadata 先行，已有 DOI）:
  stage_network_metadata_to_paper_raw  -> fetch_pdf_for_paper_raw
  -> convert_paper_raw_gpu -> curate_paper_raw -> formalize_paper_raw
  -> commit_paper_raw_to_papers -> rebuild_all_catalog

Manual PDF path（先转换，再从转换后的 md 解析 metadata）:
  stage_raw_pdfs_to_paper_raw --move --apply
  -> convert_paper_raw_gpu          # MinerU 转换在 metadata resolve 之前
  -> resolve_paper_raw_metadata     # 读转换后的 md，抽取候选并联网验证/查询
  -> curate_paper_raw -> formalize_paper_raw
  -> commit_paper_raw_to_papers -> rebuild_all_catalog
```

For manual PDF imports, metadata resolver depends on converted Markdown and must run
after MinerU conversion. 手动 PDF 导入时，metadata resolver 必须基于 MinerU 转换完成后的
md，因此顺序是先转换，再解析/匹配 metadata。v2.2 状态机：`curate_paper_raw` 只校验
metadata/catalog 并写 `catalog_ready`（不改名、不分配 paper_number）；`formalize_paper_raw`
是 commit 前必经步骤，在 paper_raw 内完成 canonical paper_id 改名、reserve 16 位
paper_number、回填 catalog、置 `ready_for_commit`；`commit_paper_raw_to_papers` 只接收
`ready_for_commit`，事务性安装，失败回滚不污染 `data/papers`。`data/papers` 不允许半成品。
`formalize_paper_raw.py` / `commit_paper_raw_to_papers.py` 支持完整路径隔离
（`--paper-raw-dir`/`--papers-dir`/`--ledger-path`/`--all-catalog-path`）；测试/agent 必须传
tmp ledger 与 tmp all.catalog，禁止污染真实 `data/catalog`。metadata 候选读转换后 Markdown 前 100 行。
写作流程按 `paper_number` 复制到 `write/jobs/<job_id>/article/<paper_number>/`。

正式资产只允许位于：

```text
data/papers/<paper_id>/<paper_id>.metadata.json
data/papers/<paper_id>/<paper_id>.catalog.json
data/papers/<paper_id>/<paper_id>.md
data/papers/<paper_id>/<paper_id>.pdf
data/papers/<paper_id>/images/
data/papers/<paper_id>/<16位编号>.paper.number
```

`data/catalog/all.catalog.json`、`data/catalog/paper_index.json` 和 `data/catalog/paper_number_ledger.json` 是本地生成的 API/写作运行时索引，不提交真实库状态；源码快照只提交对应 `.template.json` 空模板。旧数据不自动迁移。

## 事实源与主键

- **metadata**（`<paper_id>.metadata.json`）：BibTeX/书目信息事实源（DOI、作者、年份、期刊、卷期页、链接、metadata_match）。
- **catalog**（`<paper_id>.catalog.json`，schema v2.0，**content-only**）：大模型快速筛选精读文献的内容索引。只含正文内容理解（content_identity、classification、screening、research_card、evidence_profile、content_notes、provenance），**不含** DOI/作者/年份/期刊/卷期页等书目字段（这些只在 metadata）。catalog 与 metadata 仅通过 `paper_number`/`paper_id` 关联。
  catalog 自然语言内容默认尽量使用中文，便于中文检索、分类、选文和写作 workflow；JSON key/schema enum 保持英文，技术名词和专有名词可中英混写，metadata 仍保留原始/规范书目信息。
- **paper_number**（16 位）：API 与写作流程主键。大模型先看 `all.catalog.json`（content-only）选号，再按 `paper_number` 读 metadata 取书目信息。writing 主流程使用 `write/jobs/<job_id>/article/<paper_number>/`，当前主入口为 `create_write_job.py` / `prepare_write_article_workdir.py`。`all.catalog` 是内容索引不是书目库；references/BibTeX 只从 metadata 生成。写作有两层入口：**推荐稳定主入口是 catalog-to-TeX mini loop**（`create_write_job.py` → `write_catalog_tex_article.py` → `check_write_tex_project.py` → `check_write_quality_text.py`）；`write_review.py` 与 `src/server.py` 的 `/write/jobs/*` HTTP API 是 lower-level / experimental 多阶段入口（不是 legacy，但不是默认主入口）。两者共用 `write/jobs/<job_id>/article/<paper_number>/`，都不读已退役的 llm work 目录。

## Metadata 完整性门槛

- 网络/搜索 metadata 导入必须有 DOI，并写入 `metadata.identifiers.doi`；没有 DOI 的候选不得 stage 到 `paper_raw`。
- 手动 PDF 可以先进入 `data/paper_raw/<000001>/` 并保持 `metadata_match.status = unmatched`，但匹配或人工确认没有 DOI 时不得 curation/commit。
- 手动 PDF 正常导入时，`data/raw/` is a queue / raw 是待处理队列；成功 stage 会把 PDF 移到
  `data/paper_raw/<source_id>/<source_id>.pdf`，因此 raw 中对应 PDF 应消失。
- curation、formal commit 和正式库 validate 都要求 DOI 非空；不完整的 `paper_raw` 留在原工作区，写 `.import_status.json` 说明原因。
- LLM/curator 只能补空 metadata 字段，不能编造 DOI，也不能覆盖已有非空 DOI。
- BibTeX 和 APA 参考文献只从 metadata 读取标题、作者、venue、卷期页、DOI 和 URL，不从 catalog 或 MinerU 正文拼接。

## 入库 / 写作 / API 命令链

完整命令链（含手动 PDF 导入、网络 metadata 导入、curation、commit、API、写作工作流、MinerU 运行参数）
见 [AGENTS.md](AGENTS.md) 与 `docs/PROJECT_STATUS.md`；写作详见
[docs/WRITER_PRODUCTIZATION_PLAN.md](docs/WRITER_PRODUCTIZATION_PLAN.md) 与
[skills/catalog_tex_writer/](skills/catalog_tex_writer/)。所有真实命令必须 `conda run -n mineru ...`。

要点速记：

- MinerU conversion requires GPU / MinerU 正式转换必须使用 GPU。`stage_raw_pdfs_to_paper_raw.py`
  不需要 GPU；formal ingest 使用 `scripts/convert_paper_raw_gpu.py`，默认 `MINERU_REQUIRE_GPU=true`、
  `CUDA_VISIBLE_DEVICES=0`，并检查 `nvidia-smi` 与 `torch.cuda.is_available()`。CPU/no-GPU 只允许调试：显式设置 `MINERU_ALLOW_CPU=true`
  或 `MINERU_REQUIRE_GPU=false`。
- 手动 PDF 放 `data/raw/`；正常导入 SOP 使用 `stage_raw_pdfs_to_paper_raw.py --move --apply`
  消费 raw 队列。copy 模式只用于调试、备份、测试或明确的一次性检查，不是默认导入规范。
  手动 PDF 路径顺序：
  `convert_paper_raw_gpu.py` 先转换；
  `resolve_paper_raw_metadata.py` 再从转换后的 md 解析/联网验证 metadata
  （不要在没有 md 时跑 resolver；也不要用 `--only-preflight-ready` 挡住初始
  unmatched 的手动 PDF 转换）。
- 网络/搜索 metadata 必须有合法 DOI 才能进入 `paper_raw`。网络路径 metadata 已带 DOI，先 `fetch_pdf_for_paper_raw.py` 取 PDF，再转换，无需 resolve 步骤。
- `curate_paper_raw.py` 不调用大模型：`--dry-run` 写 curation prompt，`--apply` 应用 content-only catalog；
  metadata 空字段由 `resolve_paper_raw_metadata.py` / enrichment 补齐，不在 curate 阶段处理。详见
  [skills/paper_raw_catalog_curator/](skills/paper_raw_catalog_curator/)。
- MinerU 默认 `hybrid-engine + medium + auto`，批量转换默认单进程防 GPU OOM。批量转换优先使用
  持久 `mineru-api` 服务：`MINERU_RUNNER=cli_api_proxy` 与 `MINERU_API_URL=http://127.0.0.1:8000`；
  推荐先运行 `python scripts/start_mineru_services.py --wait` 启动或复用服务，转换后用
  `python scripts/stop_mineru_services.py` 关闭。`MINERU_RUNNER=cli` 是 fallback，批量时可能每篇
  PDF 冷启动 MinerU。Windows 可用 `start_fast_api_mode.bat`，它会委托新的 Python 启动脚本。
  `mineru-api` 必须在它自己的 shell 中以 `CUDA_VISIBLE_DEVICES=0` 启动；只在 client 设置
  `CUDA_VISIBLE_DEVICES` 不能改变已经运行的服务。底层 `convert_paper_raw_batch.py` 保留用于调试/兼容，
  正式 SOP 使用 `convert_paper_raw_gpu.py`。
  `start_fast_api_mode.bat` 会先检查健康的现有 `mineru-api` 并复用；如果端口 8000 被占用但
  `/health` 不通，会拒绝启动新服务以避免重复加载模型。多篇 formal batch 不允许
  `MINERU_RUNNER=cli` 冷启动，单篇 CLI 仅用于测试/调试。
  大 PDF MinerU 转换没有进程级 timeout，可能运行很久；health/preflight/网络请求和 MinerU lock
  等待 timeout 仍然保留，但它们不是 PDF 转换超时。确认卡死时先运行
  `python scripts/check_mineru_processes.py`，再按需运行 `python scripts/stop_mineru_services.py`。
  `paper_raw` 转换幂等：已有 `<source_id>.md` + `images/` 默认 skipped；成功转换写
  `<source_id>.conversion.json` 和 `.import_status.json: converted`；只有显式
  `--force-reconvert` 才清理旧 md/images/output 并重跑 MinerU。
  非 localhost API 暴露必须设置 `MINERU_API_KEY`。

Formal conversion command:

```bash
conda run -n mineru python scripts/start_mineru_services.py --wait
conda run -n mineru python scripts/convert_paper_raw_gpu.py --source-id 000001 --apply
conda run -n mineru python scripts/convert_paper_raw_gpu.py --all --apply --report reports/convert_paper_raw.json
conda run -n mineru python scripts/stop_mineru_services.py
```

Metadata title/author candidates for manual PDFs come from the converted Markdown first:
read `data/paper_raw/<source_id>/<source_id>.md` physical first 100 lines as
front-matter evidence for title, authors, affiliations, abstract, keywords, and
DOI candidates before any PDF text title fallback, and keep DOI gates strict.

GPU conversion setup (Windows cmd):

```bat
set MINERU_REQUIRE_GPU=true
set CUDA_VISIBLE_DEVICES=0
set MINERU_RUNNER=cli_api_proxy
set MINERU_API_URL=http://127.0.0.1:8000
```

PowerShell:

```powershell
$env:MINERU_REQUIRE_GPU="true"
$env:CUDA_VISIBLE_DEVICES="0"
$env:MINERU_RUNNER="cli_api_proxy"
$env:MINERU_API_URL="http://127.0.0.1:8000"
```

Linux / bash:

```bash
export MINERU_REQUIRE_GPU=true
export CUDA_VISIBLE_DEVICES=0
export MINERU_RUNNER=cli_api_proxy
export MINERU_API_URL=http://127.0.0.1:8000
```

## Default writing entry

1. Select papers by `paper_number` from `data/catalog/all.catalog.json`.
2. Create a writing job with `scripts/create_write_job.py`.
3. Generate the TeX project with `scripts/write_catalog_tex_article.py`.
4. Validate with `scripts/check_write_tex_project.py` and `scripts/check_write_quality_text.py`.

`skills/catalog_tex_writer` is the default article-writing skill for this path.

`skills/paper_raw_metadata_resolver`, `skills/paper_raw_catalog_curator`, and `skills/literature_library_manager` are support / ingest / library-management skills, not competing article-writing skills.

`scripts/write_review.py` (with `src/writer/*` and the `src/server.py` `/write/jobs/*` HTTP API) remains available as an advanced / experimental multi-stage writer workflow, but it is not the default recommended entry. Both paths use `write/jobs/<job_id>/article/<paper_number>/` and neither reads the legacy/forbidden llm work directory.

## 数据与版权边界

`data/raw/`、`data/paper_raw/`、`data/papers/` 中的文献资产按版权数据处理，不进入源码分发。
真实 `data/papers` 不进入 snapshot，但本地真实库必须通过 `validate_v2_library.py` 和
`audit_metadata_quality.py` 的硬错误检查。`write/jobs/` 运行产物不提交（只跟踪 `.gitkeep`）。

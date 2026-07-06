# 文献工坊 (PaperMill)

本项目是本地文献资产库、AI 可读目录和博士论文级综述写作工作区。它只保留纯 v2 `paper_raw` 工作流，不做向量库、RAG、embedding，也不内置 LLM client。所有 prompt 和写作步骤只生成可复制文本或结构化模板。

## Ingest duplicate guard

New ingest entrypoints run duplicate checks before creating or mutating `data/paper_raw`:
manual PDF staging and fetch/attach block duplicate PDF content by sha256/md5; network
metadata staging and metadata resolution block duplicate DOI across both `data/paper_raw`
and `data/papers`. Duplicate hits do not reserve ledger numbers, create workspaces, move
source PDFs, overwrite attached PDFs, or write duplicate PDF hashes into metadata.
`preflight_paper_raw_import.py`, commit readiness, and `audit_ingest_duplicates.py --strict`
remain final defenses.

The dedup index covers **every** paper_raw workspace, not only 16-digit folders:
`build_ingest_duplicate_index()` admits any `data/paper_raw/` subdir (except
`quarantine/`, hidden, `output`/`images`/`__pycache__`) that contains ≥1 asset
marker (`*.metadata.json`, `.import_status.json`, `stage_manifest.json`,
`*.paper.number`, `*.pdf`, `*.md`), via `is_paper_raw_workspace()`. Legacy /
untitled / formalized workspaces (e.g. `1979_sykest_untitled/`, which carry a
`*.paper.number` marker despite the non-16-digit folder name) are dedup sources
like any other. Duplicate workspaces are audited and cleaned by
`scripts/audit_paper_raw_duplicate_workspaces.py` (moves losers into
`data/paper_raw/quarantine/duplicate_workspaces/`; never deletes, never recycles
paper_numbers, never lowers `max_number`).

## Paper number admin maintenance

Normal ingest paper numbers are allocated only by `PaperNumberLedger`; they are
monotonic and never recycled. Do not write scripts that scan folder names and
take `max(existing)+1`. Allocation is ledger-first and serialized with
`data/paper_raw/.paper_raw_write.lock`; existing 16-digit directories and
`.paper.number` markers are part of the monotonic floor, so empty orphan and
metadata-only numbers are not reused. The only supported maintenance entrypoints are:

```bash
python scripts/audit_paper_number_ledger.py --strict --detect-orphans --report reports/paper_number_audit.json
python scripts/reset_paper_number_ledger.py --compact-paper-raw --sort year --dry-run --report reports/paper_number_compact_dryrun.json
```

`reset_paper_number_ledger.py --reset-empty` and `--compact-paper-raw` are
admin-only special operations. They refuse to run when `data/papers/` contains
formal paper directories, default to dry-run, and require
`--i-understand-this-rewrites-paper-numbers --reason ...` for `--apply`.
Quarantined workspaces are excluded, and `.paper.number` marker parsing must
strip the full `.paper.number` suffix, never `Path.stem`.
`audit_paper_number_ledger.py --fix-empty-orphans --apply --reason ...` may
delete only strictly empty orphan directories; metadata-only workspaces are
normal and must not be deleted.

## Project status and documentation map

- **ingest v2.3 strict-only 为当前增量状态**（不打新 tag）：新 `paper_raw` 工作区统一为 16 位 `paper_number`，staging 第一步 reserve 编号；正常 CLI 只接受 `--paper-number` / `--paper-numbers`，旧 6 位编号仅限 `scripts/legacy/` migration。**ingest v2.2 已冻结**（tag `ingest-v2.2`）；**writing v0.1 已冻结**（tag `writing-v0.1`）。
- 项目**不使用 RAG / embedding / vector database / ChromaDB**，也不内置 LLM client。
- 真实入库 / 转换 / 写作必须使用 `conda run -n mineru ...` 或 `conda activate mineru` 后运行 Python 脚本。
- snapshot（source profile）不包含真实 `data/` 文献资产与 `write/jobs/` 运行产物；
  audit profile 下的 `mineru_snapshot.zip` 包含轻量文本/结构文件（.json / .md / .yaml / .bib 等）。
  详见 `AGENTS.md` §7。

文档入口：

- [AGENTS.md](AGENTS.md) — 所有 coding agent 的项目操作规约（状态、边界、主流程、提交前检查）
- [CLAUDE.md](CLAUDE.md) — Claude/Codex 类 agent 操作提醒与关键边界速查
- [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md) — 当前冻结版本、主流程、边界、待办与禁止事项总览
- [docs/PROJECT_CONTRACT.md](docs/PROJECT_CONTRACT.md) — 不可改变的核心契约与边界
- [docs/DEPENDENCIES_AND_EXTERNAL_TOOLS.md](docs/DEPENDENCIES_AND_EXTERNAL_TOOLS.md) — 依赖、网络服务、PDF resolver、Sci-Hub removed
- [docs/WRITING_QUALITY_ACCEPTANCE.md](docs/WRITING_QUALITY_ACCEPTANCE.md) — 写作质量验收规则
- [docs/WRITER_PRODUCTIZATION_PLAN.md](docs/WRITER_PRODUCTIZATION_PLAN.md) — writer v0.2 产品化计划
- [docs/SCRIPT_USAGE.md](docs/SCRIPT_USAGE.md) — 所有 scripts/*.py 的用途、风险分类和推荐命令索引
- [docs/audits/real_ingest_acceptance.md](docs/audits/real_ingest_acceptance.md) — 脱敏真实入库验收记录

## Quick validation

全部使用 mineru conda 环境（推荐 `conda activate mineru` 或 `conda run -n mineru python ...`）：

```bash
conda run -n mineru pytest -q
conda run -n mineru python scripts/pack_repo.py  # 默认 audit profile，含轻量文本文件
conda run -n mineru python scripts/validate_v2_library.py
conda run -n mineru python scripts/audit_metadata_quality.py
conda run -n mineru python scripts/doctor_ingest_pipeline.py
```

## Formal rollback/reingest SOP

仅在明确需要重建正式库时使用；默认删除旧 catalog 并强制重新生成，`--keep-catalog` 只用于 debug。

```bash
conda run -n mineru python scripts/rollback_formal_papers_to_paper_raw.py --all --dry-run --report reports/rollback_dryrun.json
# 确认 report.summary.blocking_errors == 0 且 failed == 0 后：
conda run -n mineru python scripts/rollback_formal_papers_to_paper_raw.py --all --apply --report reports/rollback_apply.json
conda run -n mineru python scripts/validate_rolled_back_paper_raw.py
conda run -n mineru python scripts/curate_paper_raw.py --all-matched --dry-run
# LLM/子代理按 paper_raw_catalog_curator 生成 <paper_number>.catalog.json 后：
conda run -n mineru python scripts/curate_paper_raw.py --all-ready --apply
conda run -n mineru python scripts/formalize_paper_raw.py --all-ready --apply --report reports/formalize_after_rollback.json
conda run -n mineru python scripts/commit_paper_raw_to_papers.py --all-ready --apply
conda run -n mineru python scripts/validate_v2_library.py
conda run -n mineru python scripts/pack_repo.py  # 默认 audit profile，含轻量文本文件
```

## Explicit non-goals

- no RAG / no embedding / no vector DB / no ChromaDB
- no LLM client in code（所有 prompt/写作步骤只生成文本或模板）
- 不提交真实 data 与 `write/jobs` 运行产物（只跟踪 `.gitkeep`）


## 核心数据区分工

```
data/paper_raw/    ← 工作区 / 待处理队列（可修改、可重跑、可丢弃）
data/papers/       ← 正式库 / 只读资产（commit 后不可半成品、不可原地修改）
```

**重要原则：** `data/paper_raw` 是工作区，用于 staging、转换、解析 metadata、curation、
formalize 等所有处理步骤；`data/papers` 是正式入库，只接受已通过全部校验的 `ready_for_commit`
资产，由 `commit_paper_raw_to_papers.py` 事务性安装。**一切处理都在 paper_raw 内完成，
papers 只有最终结果。**

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
md，因此顺序是先转换，再解析/匹配 metadata。v2.3 状态机：`curate_paper_raw` 只校验
metadata/catalog 并写 `catalog_ready`（不改名、不分配 paper_number）；`formalize_paper_raw`
是 commit 前必经步骤，在 paper_raw 内完成 canonical paper_id 改名、复用/校验 staging 已
reserved 的 16 位 paper_number、回填 catalog、置 `ready_for_commit`；`commit_paper_raw_to_papers` 只接收
`ready_for_commit`，事务性安装，失败回滚不污染 `data/papers`。`data/papers` 不允许半成品。

Metadata-only paper_raw PDF fetch is an in-place completion step:
`fetch_pdf_for_paper_raw.py` scans existing 16-digit `data/paper_raw/<paper_number>/`
workspaces, reads DOI only from `<paper_number>.metadata.json`, never reads `doi.csv`,
never allocates a new paper_number, and attaches successful downloads through duplicate
guard as `<paper_number>.pdf`. Default fetch remains OA-only; header-based fetch is
explicit (`--resolver header-based`), uses a fixed in-code User-Agent, and never persists
header values.
`formalize_paper_raw.py` / `commit_paper_raw_to_papers.py` 支持完整路径隔离
（`--paper-raw-dir`/`--papers-dir`/`--ledger-path`/`--all-catalog-path`）；测试/agent 必须传
tmp ledger 与 tmp all.catalog，禁止污染真实 `data/catalog`。metadata 候选读转换后 Markdown 前 100 行。
`formalize_paper_raw.py` 只接受 `converted_current`（存在当前 conversion manifest）；
缺 manifest 的已转换资产必须先生成当前 manifest 或重新转换。普通 ingest
测试夹具从 16 位 `paper_number` 工作区开始，`preserve_paper_number` 使用 reserved-specific 语义。
`Catalog.load()` 的缺失 all.catalog fallback 是 tolerant read-only snapshot，不写 ledger/marker。
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
- **catalog**（`<paper_id>.catalog.json`，schema v3.1，**content-only**）：大模型快速筛选精读文献的内容索引。只含正文内容理解（library_locator、content_identity、classification、screening、research_card、writing_value、evidence_profile、figure_inventory、terminology、quality_control、provenance），**不含** DOI/作者/年份/期刊/卷期页等书目字段（这些只在 metadata）。catalog 与 metadata 仅通过 `paper_number`/`paper_id` 关联。
  catalog 自然语言内容默认尽量使用中文，便于中文检索、分类、选文和写作 workflow；JSON key/schema enum 保持英文，技术名词和专有名词可中英混写，metadata 仍保留原始/规范书目信息。metadata schema v2.0 不再承载中文标题、摘要、关键词、notes。
  catalog 的中文内容（`content_identity.content_title_zh`、`research_card.mechanisms`、`research_card.limitations`、`writing_value.short_summary` 等）必须由 LLM 子代理从 Markdown 正文生成，不接受直接拼接或手工编写。每次入库前必须运行子代理补全。
  初始 catalog 生成 / paper_raw curator / 入库前 catalog 生成阶段，`screening.read_decision` 必须固定为 `"pending"`；禁止在该阶段写成 `must_read` / `maybe_read` / `skip`。这些最终精读决策值仅用于 post-triage / writing-stage catalog 或人工筛选后的工作区。
- **paper_number**（16 位）：API 与写作流程主键。大模型先看 `all.catalog.json`（content-only）选号，再按 `paper_number` 读 metadata 取书目信息。writing 主流程使用 `write/jobs/<job_id>/article/<paper_number>/`，当前主入口为 `create_write_job.py` / `prepare_write_article_workdir.py`。`all.catalog` 是内容索引不是书目库；references/BibTeX 只从 metadata 生成。写作有两层入口：**推荐稳定主入口是 catalog-to-TeX mini loop**（`create_write_job.py` → `write_catalog_tex_article.py` → `check_write_tex_project.py` → `check_write_quality_text.py`）；`write_review.py` 与 `src/server.py` 的 `/write/jobs/*` HTTP API 是 lower-level / experimental 多阶段入口（不是 legacy，但不是默认主入口）。两者共用 `write/jobs/<job_id>/article/<paper_number>/`，都不读已退役的 llm work 目录。

## Metadata 完整性门槛

- 网络/搜索 metadata 导入必须有 DOI，并写入 `metadata.identifiers.doi`；没有 DOI 的候选不得 stage 到 `paper_raw`。
- 手动 PDF 可以先进入 `data/paper_raw/<0000000000000001>/` 并保持 `metadata_match.status = unmatched`，但匹配或人工确认没有 DOI 时不得 curation/commit。
- 手动 PDF 正常导入时，`data/raw/` is a queue / raw 是待处理队列；成功 stage 会把 PDF 移到
  `data/paper_raw/<paper_number>/<paper_number>.pdf`，因此 raw 中对应 PDF 应消失。
- curation、formal commit 和正式库 validate 都要求 DOI 非空；不完整的 `paper_raw` 留在原工作区，写 `.import_status.json` 说明原因。
- LLM/curator 只能补空 metadata 字段，不能编造 DOI，也不能覆盖已有非空 DOI。
- BibTeX 和 APA 参考文献只从 metadata 读取标题、作者、venue、卷期页、DOI 和 URL，不从 catalog 或 MinerU 正文拼接。

Catalog asset path invariant:
- 16-digit staging `paper_raw/<paper_number>/` catalogs may reference `<paper_number>.md`.
- Formalized `paper_raw/<paper_id>/` and `data/papers/<paper_id>/` catalogs must reference
  `<paper_id>.md` in both `library_locator.asset_refs.markdown` and `provenance.markdown_path`.
- Use `scripts/repair_catalog_asset_refs.py --dry-run` first, then `--apply` only after
  reviewing the report.

## Ingest layered semantics

Ingest layered semantics (conversion does not require metadata; formalize/commit does):

Conversion layer:
- PDF conversion to Markdown/images does not require complete metadata.
- Missing DOI or unmatched metadata must not block MinerU conversion.
- Conversion output Markdown is a valid metadata-resolution source.

Formal library layer:
- Formalize/commit requires strict metadata.
- DOI must be valid; metadata_match.status must be matched or manual_confirmed.
- BibTeX is generated from metadata, never from catalog.

Summary: convert first is allowed; commit requires metadata.

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
  OpenAlex/CrossRef search metadata staged with a valid DOI is authoritative for this path:
  it writes `metadata_match.status = "matched"` and `.import_status.json status = "metadata_matched"`.
- `curate_paper_raw.py` 不调用大模型：`--dry-run` 写 curation prompt，`--apply` 应用 content-only catalog；
  metadata 空字段由 `resolve_paper_raw_metadata.py` / enrichment 补齐，不在 curate 阶段处理。详见
  [skills/paper_raw_catalog_curator/](skills/paper_raw_catalog_curator/)。
  生成阶段提示词要求 `screening.read_decision` 固定为 `"pending"`，只保留相关性、新颖性、方法质量评分与中文理由；不要在入库前 catalog 生成阶段判断最终精读结论。
- MinerU 默认 `hybrid-engine + medium + auto`，批量转换默认单进程防 GPU OOM。批量转换优先使用
  持久 `mineru-api` 服务：`MINERU_RUNNER=cli_api_proxy` 与 `MINERU_API_URL=http://127.0.0.1:8000`；
  推荐先运行 `python scripts/start_mineru_services.py --wait --restart-if-stale` 启动或复用 managed 服务，转换后用
  `python scripts/stop_mineru_services.py` 关闭。`MINERU_RUNNER=cli` 是 fallback，批量时可能每篇
  PDF 冷启动 MinerU。Windows 可用 `start_fast_api_mode.bat`，它会委托新的 Python 启动脚本。
  `mineru-api` 必须在它自己的 shell 中以 `CUDA_VISIBLE_DEVICES=0` 启动；只在 client 设置
  `CUDA_VISIBLE_DEVICES` 不能改变已经运行的服务。底层 `convert_paper_raw_batch.py` 保留用于调试/兼容，
  正式 SOP 使用 `convert_paper_raw_gpu.py`。
  `/health` 只代表 liveness，不代表 GPU conversion readiness。正式批量转换要求：
  managed service identity、`check_mineru_processes.py` verdict 为 `READY_FOR_CONVERSION`、
  且最近 24 小时内有成功的 `smoke_mineru_conversion.py` 单篇报告。
  Formal `convert_paper_raw_gpu.py --all --apply` uses the default smoke report
  `reports/smoke_mineru_conversion.json`; `--smoke-report <path>` is only needed for overrides.
  `start_fast_api_mode.bat` 仅复用 managed healthy `mineru-api`；健康但 unmanaged/stale 时用
  `--restart-if-stale` 重启。如果端口 8000 被占用但 `/health` 不通，会拒绝启动新服务以避免重复加载模型。
  多篇 formal batch 不允许
  `MINERU_RUNNER=cli` 冷启动，单篇 CLI 仅用于测试/调试。
  大 PDF MinerU 转换没有进程级 timeout，可能运行很久；health/preflight/网络请求和 MinerU lock
  等待 timeout 仍然保留，但它们不是 PDF 转换超时。确认卡死时先运行
  `python scripts/check_mineru_processes.py`，再按需运行 `python scripts/stop_mineru_services.py`。
  `check_mineru_processes.py` reports conversion lock owner, paper_number, stage, and
  `LOCK_ACTIVE` / `LOCK_OWNER_DEAD` / `LOCK_STUCK_SUSPECTED` verdict.
  `paper_raw` 转换幂等：已有 `<paper_number>.md` + `images/` 默认 skipped；成功转换写
  `<paper_number>.conversion.json` 和 `.import_status.json: converted`；只有显式
  `--force-reconvert` 才清理旧 md/images/output 并重跑 MinerU。
  转换会先尝试复用 `output/mineru_cache/` 中经 PDF md5/sha256/file size 和
  `backend/method/lang/effort` 校验的 raw output；cache hit 不触发 GPU preflight、
  mineru-api health check 或 `MinerULock`。`--ignore-output-cache` 禁用查找，
  `--cache-only` 只恢复 cache、不运行 MinerU。`output/` 不进 git/snapshot。
  非 localhost API 暴露必须设置 `MINERU_API_KEY`。

Formal conversion command:

```bash
conda run -n mineru python scripts/start_mineru_services.py --wait --restart-if-stale
conda run -n mineru python scripts/convert_paper_raw_gpu.py --paper-number 0000000000000001 --apply
conda run -n mineru python scripts/check_mineru_processes.py
conda run -n mineru python scripts/smoke_mineru_conversion.py --paper-number 0000000000000001 --apply --report reports/smoke_mineru_conversion.json
conda run -n mineru python scripts/convert_paper_raw_gpu.py --all --apply --report reports/convert_paper_raw.json
conda run -n mineru python scripts/stop_mineru_services.py
```

如果 `conda` 不在 PATH（例如 agent 工具 shell 或 cmd/PowerShell），用 env python 绝对路径：

```bash
C:\Users\Admin\.conda\envs\mineru\python.exe scripts\start_mineru_services.py --wait --restart-if-stale
C:\Users\Admin\.conda\envs\mineru\python.exe scripts\convert_paper_raw_gpu.py --paper-number 0000000000000001 --apply
C:\Users\Admin\.conda\envs\mineru\python.exe scripts\check_mineru_processes.py
C:\Users\Admin\.conda\envs\mineru\python.exe scripts\smoke_mineru_conversion.py --paper-number 0000000000000001 --apply --report reports\smoke_mineru_conversion.json
C:\Users\Admin\.conda\envs\mineru\python.exe scripts\stop_mineru_services.py
```

start_mineru_services.py must resolve Scripts/mineru-api.exe from the current Python env (find_mineru_api_exe). Do not manually background mineru-api.exe as a long-term SOP.

Metadata title/author candidates for manual PDFs come from the converted Markdown first:
read `data/paper_raw/<paper_number>/<paper_number>.md` physical first 100 lines as
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

`must_read` / `maybe_read` / `skip` are post-triage / writing-stage read decisions. New global catalog entries start as `pending`; use `--read-decision` only after a writing job or human screening has annotated that decision.

`skills/catalog_tex_writer` is the default article-writing skill for this path.

`skills/paper_raw_metadata_resolver`, `skills/paper_raw_catalog_curator`, and `skills/literature_library_manager` are support / ingest / library-management skills, not competing article-writing skills.

`scripts/write_review.py` (with `src/writer/*` and the `src/server.py` `/write/jobs/*` HTTP API) remains available as an advanced / experimental multi-stage writer workflow, but it is not the default recommended entry. Both paths use `write/jobs/<job_id>/article/<paper_number>/` and neither reads the legacy/forbidden llm work directory.

## 数据与版权边界

`data/raw/`、`data/paper_raw/`、`data/papers/` 中的文献资产按版权数据处理，不进入源码分发。
默认 audit-profile snapshot (`mineru_snapshot.zip`) 可包含 `.json` / `.md` 等
元数据样本（不含完整 PDF），纯源码分发使用 `--profile source`。
本地真实库必须通过 `validate_v2_library.py` 和 `audit_metadata_quality.py` 的硬错误检查。
`write/jobs/` 运行产物不提交（只跟踪 `.gitkeep`）。

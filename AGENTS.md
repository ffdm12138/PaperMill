# AGENTS.md

面向所有 coding agent 的项目操作规约。修改本仓库前必须阅读：

- `docs/PROJECT_STATUS.md`（当前冻结状态与主流程总览）
- `docs/PROJECT_CONTRACT.md`（不可破坏契约）
- `docs/SCRIPT_USAGE.md`（所有 scripts/*.py 用途与风险索引）
- `docs/TESTING.md`（测试分层、agent 验收与 fixture 规则）
- `README.md`（文档入口地图）

新 agent 建议阅读顺序：`AGENTS.md` → `docs/PROJECT_STATUS.md` → `docs/PROJECT_CONTRACT.md` → `docs/TESTING.md` → `README.md`。
按改动类型深入：ingest 改动看 `docs/PROJECT_CONTRACT.md`、`docs/DEPENDENCIES_AND_EXTERNAL_TOOLS.md`、
`docs/audits/real_ingest_acceptance.md`；writing 改动看 `docs/WRITING_QUALITY_ACCEPTANCE.md`、
`docs/WRITER_PRODUCTIZATION_PLAN.md`、`skills/catalog_tex_writer`；fetch/依赖改动看
`docs/DEPENDENCIES_AND_EXTERNAL_TOOLS.md` 与 `docs/PROJECT_CONTRACT.md`。

## 1. 项目状态

- ingest v2.3 strict-only current incremental state（不打新 tag）：新 ingest 的 `data/paper_raw/<id>/` 使用 16 位 `paper_number`，staging 第一步即 reserve 编号并写 `<paper_number>.paper.number` marker；正常 CLI 只接受 `--paper-number` / `--paper-numbers`，旧 6 位编号仅限 `scripts/legacy/` migration。
- `paper_number` 正常分配必须由 `PaperNumberLedger` 统一负责，单调递增、不回收空洞；任何脚本不得通过扫描目录最大值自行分配。allocator 采用 monotonic ledger-first：分配前取 ledger max、ledger items、现有 16 位 `paper_raw` 目录和所有 `.paper.number` marker 的最大值再 +1；empty orphan / marker-only / metadata-only 编号都不得复用。所有 `paper_raw` 写事务统一使用 `data/paper_raw/.paper_raw_write.lock`，锁顺序只能是 `paper_raw_write.lock -> paper_number_ledger.lock -> workspace/file writes`，ledger lock 只短暂用于 ledger 读写，不能在复制 PDF 或批量写 workspace 文件时长期持有。特殊清零/压缩重编号只允许使用 admin-only `scripts/audit_paper_number_ledger.py` 与 `scripts/reset_paper_number_ledger.py`，且必须在 `data/papers/` 无正式目录时执行。
- ingest v2.2 frozen，tag `ingest-v2.2`（v2.1 已被状态机重构取代：curate 不再改名/不再 commit，新增 `formalize_paper_raw.py` 在 paper_raw 内完成正式化，`commit` 退化为事务性安装）。
- writing v0.1 frozen，tag `writing-v0.1`。
- catalog schema 为 v3.1（content-only，含 `library_locator` / `content_identity.content_title_zh` / `writing_value` / `quality_control`）；metadata schema 为 v2.0（`paper_number` / `paper_raw_id`，不生成 `source_id`）；不修改 writer v0.1 行为。
- 当前增量改动必须文档化，不打新 tag。

## 2. 不可违反的边界

- `metadata`（`<paper_id>.metadata.json`）是书目信息事实源（DOI、作者、年份、期刊、卷期页、BibTeX）。
- `catalog`（schema v3.1）是 content-only：`all.catalog` 不得含 DOI/作者/年份/venue/metadata/display 等书目字段，
  两者仅通过 `paper_number`/`paper_id` 关联。
- catalog 自然语言 value 默认尽量使用中文；JSON key/schema enum 保持英文，技术名词可中英混写。
  metadata 不中文化、不改写 DOI/作者/期刊/年份/BibTeX；metadata v2.0 不再承载中文标题、摘要、关键词、notes。
  catalog 的中文内容（`content_identity.content_title_zh`、`research_card.mechanisms`、`research_card.limitations`、`writing_value.short_summary`）必须由 LLM 子代理从 Markdown 正文生成，不得直接拼接或手工填写。
- 初始 catalog 生成 / paper_raw curator / 入库前 catalog 生成阶段，`screening.read_decision` 必须固定为 `"pending"`。
  禁止在该阶段写成 `must_read` / `maybe_read` / `skip`；这些值只允许出现在后续 post-triage /
  writing-stage catalog、人工筛选或精读 triage 阶段。
- `references.bib` 只从复制的 metadata 生成，绝不从 catalog 或正文拼接。
- `write/jobs` 是写作运行时，不提交（只跟踪 `.gitkeep`）；TeX 不得直接读 `data/papers`、`data/raw`、
  `data/paper_raw`，只能读 job-local 复制副本。
- 不做 RAG / embedding / vector DB / ChromaDB；不内置 LLM client，所有 prompt/写作步骤只生成文本或模板。
- Sci-Hub resolver 已移除（removed）：项目不提供、不注册 Sci-Hub resolver；PDF 获取按「原始链接 → OA → 出版商专用解析器 → header_based DOI landing fallback（默认 `https://doi.org/{doi}`，无需额外配置）→ 失败报告」优先级执行，不再有任何 unsafe fallback。
- 网络 metadata 进入 `paper_raw` 前必须有合法 DOI；没有 DOI 的候选不得 stage。
- 正式入库必须通过 `validate_v2_library.py` 与 `audit_metadata_quality.py` 的硬错误检查。
- `.paper.number` marker 解析必须裁剪完整 `.paper.number` 后缀，禁止用 `Path.stem` 解析；否则会产生 `0000000000000001.paper` 这类污染。

## 3. 运行环境

- Git Bash 已配置 `conda init bash`，终端直接 `conda activate mineru` 即可进入
  mineru 环境（Python 3.10.20）。`conda run -n mineru python ...` 仍是稳定 SOP，
  特别适合脚本和 agent 工具调用。
  本机还安装了 vnpy / biga / bili_delete 等 conda 环境，`conda activate <name>` 即可切换。
- mineru 环境中预装了 `wget` 1.21.4，可用于网络资源下载。
- 注意：本 agent 的 bash 工具使用非交互 shell，不自动加载 `.bashrc`；
  此时应使用 `conda run -n mineru python ...` 或绝对路径
  `C:\Users\Admin\.conda\envs\mineru\python.exe` 调用。
  conda mineru 环境实际路径：`C:\Users\Admin\.conda\envs\mineru`（Python 3.10.20）。
- Windows 控制台先设置编码（避免 GBK 下中文/JSON 输出失败）：cmd.exe 用 `set PYTHONIOENCODING=utf-8`、PowerShell 用 `$env:PYTHONIOENCODING="utf-8"`、Bash/Git Bash 用 `export PYTHONIOENCODING=utf-8`。不要混用 shell 语法。
- Do not mix shell syntaxes: cmd.exe 用 `set VAR=value`、PowerShell 用 `$env:VAR="value"`、Bash/Git Bash/Linux/macOS 用 `export VAR=value`。Never write `set CUDA_VISIBLE_DEVICES=0 && ...` in Git Bash.
- Most formal MinerU entrypoints set the required GPU env internally（`MINERU_REQUIRE_GPU=true`、`CUDA_VISIBLE_DEVICES=0`、`MINERU_RUNNER=cli_api_proxy`、`MINERU_API_URL=http://127.0.0.1:8000`）。Agents should prefer calling the Python entrypoints directly instead of hand-writing shell-specific env assignments.
- MinerU 正式转换必须使用 GPU：默认 `MINERU_REQUIRE_GPU=true`、`CUDA_VISIBLE_DEVICES=0`。
  `stage_raw_pdfs_to_paper_raw.py` 不需要 GPU；formal ingest 使用 `convert_paper_raw_gpu.py`，
  底层 `convert_paper_raw_batch.py` 也会强制 formal GPU 默认与 `torch.cuda.is_available()` preflight。
  CPU/no-GPU 只允许调试：显式设置 `MINERU_ALLOW_CPU=true` 或 `MINERU_REQUIRE_GPU=false`。

## 4. 数据边界

- **`data/paper_raw` 是工作区（workspace）**：所有处理（staging、转换、metadata 解析、curation、formalize）都在此完成，文件夹可修改、可重跑、可丢弃。
- **`data/papers` 是正式库（committed library）**：只接受 `ready_for_commit` 资产，事务性安装，不可半成品，不可原地修改。一切处理在 paper_raw 内完成后再入库。
- `data/raw`、`data/paper_raw`、`data/papers`、`data/import_work`、`write/jobs` 为运行时 / 真实数据区。
- git tracked 只应包含 `.gitkeep` 与 catalog `.template.json` 空模板；`pack_repo.py` 默认 audit profile 会额外扫描全工作区的轻量文本/结构文件（`.json`/`.md`/`.yaml`/`.toml`/`.csv`/`.bib`/`.tex` 等），
  包括被 `.gitignore` 忽略的运行时样本（如 `data/papers/`、`data/paper_raw/` 中的 catalog、metadata、markdown、source_records），
  但**不包含 PDF、图片、日志、缓存、二进制文件**。
  **zip 中出现轻量运行时文本文件不代表 git 污染**。
  纯源码分发应使用 `python scripts/pack_repo.py --profile source`。
- 任何 TeX 编译产物不进入 git tracked 或 snapshot。

## 5. 主流程

入库主流程分两条路径。手动 PDF 路径必须先 MinerU 转换、再从转换后的 md 解析 metadata：

```bash
# Manual PDF path: convert first, resolve metadata from converted md second.
# Windows cmd.exe: set MINERU_RUNNER=cli_api_proxy / set MINERU_API_URL=http://127.0.0.1:8000
# PowerShell:     $env:MINERU_RUNNER="cli_api_proxy" / $env:MINERU_API_URL="http://127.0.0.1:8000"
# Bash/Git Bash:  export MINERU_RUNNER=cli_api_proxy / export MINERU_API_URL=http://127.0.0.1:8000
conda run -n mineru python scripts/start_mineru_services.py --wait --restart-if-stale
conda run -n mineru python scripts/stage_raw_pdfs_to_paper_raw.py --move --dry-run --report reports/stage_raw_dryrun.json
conda run -n mineru python scripts/stage_raw_pdfs_to_paper_raw.py --move --apply --report reports/stage_raw_move.json
conda run -n mineru python scripts/check_mineru_processes.py
conda run -n mineru python scripts/smoke_mineru_conversion.py --paper-number 0000000000000208 --apply --report reports/smoke_mineru_conversion.json
conda run -n mineru python scripts/convert_paper_raw_gpu.py --all --apply --report reports/convert_paper_raw.json
conda run -n mineru python scripts/resolve_paper_raw_metadata.py --all-unmatched --apply --allow-network --write-candidates --report reports/resolve_candidates.json
conda run -n mineru python scripts/curate_paper_raw.py --all-ready --apply
conda run -n mineru python scripts/formalize_paper_raw.py --all-ready --apply --report reports/formalize_paper_raw.json
conda run -n mineru python scripts/commit_paper_raw_to_papers.py --all-ready --apply
conda run -n mineru python scripts/rebuild_all_catalog.py --apply
conda run -n mineru python scripts/validate_v2_library.py
conda run -n mineru python scripts/stop_mineru_services.py
```

正式库全量回退再入库 SOP（仅在明确需要重建正式库时使用）：

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
conda run -n mineru python scripts/pack_repo.py
```

`rollback_formal_papers_to_paper_raw.py --keep-catalog` 仅用于 debug；正式 SOP 使用默认 delete catalog，强制重新生成 content-only catalog。

如果 `conda` 不在 PATH（例如 agent 工具 shell 或 cmd/PowerShell），用 env python 绝对路径
（以服务启停与转换为例）：

```bash
C:\Users\Admin\.conda\envs\mineru\python.exe scripts/start_mineru_services.py --wait --restart-if-stale
C:\Users\Admin\.conda\envs\mineru\python.exe scripts\check_mineru_processes.py
C:\Users\Admin\.conda\envs\mineru\python.exe scripts\smoke_mineru_conversion.py --paper-number 0000000000000208 --apply --report reports\smoke_mineru_conversion.json
C:\Users\Admin\.conda\envs\mineru\python.exe scripts/convert_paper_raw_gpu.py --all --apply --report reports/convert_paper_raw.json
C:\Users\Admin\.conda\envs\mineru\python.exe scripts\stop_mineru_services.py
```

start_mineru_services.py must resolve Scripts/mineru-api.exe from the current Python env (find_mineru_api_exe). Do not manually background mineru-api.exe as a long-term SOP.

手动 PDF 导入时，metadata resolver 必须基于 MinerU 转换完成后的 md，因此顺序是先转换，再解析/匹配
metadata。不要在没有 md 时跑 resolver；初始 unmatched 的手动 PDF 不要用 `--only-preflight-ready`
挡住转换；`--only-preflight-ready` 是 legacy/compatibility flag，正常 SOP 使用 `--only-convertible`。
手动 PDF 正常导入时，`data/raw/` 是待处理队列；成功 stage 后对应 PDF 应从 raw 消失。
正常 SOP 必须使用 `stage_raw_pdfs_to_paper_raw.py --move --apply`。copy 模式只用于调试、备份、
测试或明确的一次性检查，不是默认导入规范。
批量转换应先启动持久 `mineru-api`（推荐 `python scripts/start_mineru_services.py --wait --restart-if-stale`；
Windows 兼容入口 `start_fast_api_mode.bat` 会委托该脚本），
再设置 `MINERU_RUNNER=cli_api_proxy` 与 `MINERU_API_URL=http://127.0.0.1:8000`，避免每篇 PDF 冷启动。
`mineru-api` 必须在它自己的 shell 中以 `CUDA_VISIBLE_DEVICES=0` 启动；只在 client 进程设置
`CUDA_VISIBLE_DEVICES` 不能改变已经运行的 `mineru-api`。formal conversion 会同时检查
`nvidia-smi` 与当前 Python 环境的 `torch.cuda.is_available()`。
`/health` 只代表 liveness，不代表 GPU conversion readiness。正式批量转换前必须满足：
managed service identity、`check_mineru_processes.py` verdict 为 `READY_FOR_CONVERSION`、
且最近 24 小时内有成功的 `smoke_mineru_conversion.py` 单篇报告。
Formal `convert_paper_raw_gpu.py --all --apply` reads the default smoke report
`reports/smoke_mineru_conversion.json`; pass `--smoke-report <path>` only when overriding it.
`start_fast_api_mode.bat` 是 single-instance helper：仅复用 managed healthy `mineru-api`，健康但
unmanaged/stale 时必须 `--restart-if-stale` 重启；端口 8000 被占用但 `/health` 不通时拒绝启动新服务。
多篇 formal batch 不允许 `MINERU_RUNNER=cli`
冷启动；单篇 CLI 仅用于测试/调试。`paper_raw` 转换是幂等的，已有 `<paper_number>.md` + `images/`
默认 skipped；只有显式 `--force-reconvert` 才删除旧 md/images/output 并重跑 MinerU。
转换前默认尝试复用 `output/mineru_cache/`；命中必须校验 PDF md5、sha256、file size 与
`backend/method/lang/effort`，cache hit 不触发 GPU preflight、mineru-api health check 或 `MinerULock`。
`--ignore-output-cache` 禁用查找，`--cache-only` 只恢复 cache、不运行 MinerU；`output/` 不进 git/snapshot。
大 PDF MinerU 转换没有进程级 timeout，不再因固定秒数被杀；health/preflight/HTTP 请求和
`MinerULock` 等待 timeout 仍可保留，它们不是 PDF 固定秒数限制。确认卡死时先运行
`python scripts/check_mineru_processes.py`，再按需运行 `python scripts/stop_mineru_services.py`
或 `python scripts/stop_mineru_services.py --all-mineru-api`。
metadata resolver 获取标题/作者/单位/摘要/关键词/DOI 候选时必须优先读取转换后 `<paper_number>.md`
的物理前 100 行作为 front-matter evidence；Markdown first 100 lines candidates take precedence
before PDF title fallback。
非 localhost API 必须设置 `MINERU_API_KEY`，除非显式使用 unsafe override。

网络 metadata 路径（metadata 已带 DOI，先行）：先 `stage_network_metadata_to_paper_raw.py --apply`
与 `fetch_pdf_for_paper_raw.py --all --apply`，再接 `convert_paper_raw_gpu.py` → `curate_paper_raw.py`
→ `formalize_paper_raw.py` → `commit_paper_raw_to_papers.py`（网络 metadata 已有合法 DOI，无需 resolve 步骤）。
Network search metadata staged from OpenAlex/CrossRef with a valid DOI must write
`metadata_match.status = "matched"` and `.import_status.json status = "metadata_matched"`.
OpenAlex/CrossRef discovery 只是候选来源；`scripts/discover_papers.py --hide-existing`
只隐藏 discovery JSONL 中已存在 DOI 的候选，summary 仍记录统计，底层
`stage_network_metadata_to_paper_raw.py` / `PaperRawAllocator` duplicate gate 必须继续硬拦。
OpenAlex 凭据只能来自环境变量 `OPENALEX_EMAIL` / `OPENALEX_API_KEY`，源码、文档、
日志和 snapshot 中不得出现真实 key/email；文档示例只能使用 placeholder。
metadata-only paper_raw PDF fetch 只补齐已有 16 位 `data/paper_raw/<paper_number>/` 工作区：
DOI 只来自 `<paper_number>.metadata.json`，不读 `doi.csv`、不接受额外 DOI list、不分配新
paper_number；成功 PDF 必须经 duplicate guard / `PaperRawAllocator.attach_pdf()` 落为
`<paper_number>.pdf`。默认仍是 OA-only；`--resolver header-based` 需显式启用，User-Agent
固定写在 Python 代码中，Cookie/Authorization 等 header 只允许本次运行使用，明文不得入
metadata、report 或日志。

v2.3 状态机职责边界：`curate_paper_raw.py` 只校验 metadata/catalog 并写 `status=catalog_ready`，**不再改名、不再分配 paper_number、不再写 ready_for_commit**；
初始 catalog 生成阶段的 `screening.read_decision` 必须保持 `"pending"`，只生成 relevance/novelty/method-quality 评分和中文 reason，不做最终精读决策；
`formalize_paper_raw.py` 是 commit 前必经步骤，在 `data/paper_raw/<paper_number>/` 内使用 staging 已 reserved 的 16 位 paper_number 完成 canonical paper_id 改名、回填 catalog 链接、写 `<paper_id>.formalization.json` + `<16位>.paper.number` marker，置 `status=ready_for_commit`；rename 后 ledger reserved entry 必须 repoint 到 formalized 后的 `<paper_id>` 工作区（不是旧 `0000000000000001`）；
Formalize must canonicalize `catalog.library_locator.asset_refs.*` and `catalog.provenance.markdown_path`
to final `<paper_id>` filenames. Stale `<paper_number>.md` provenance is invalid in
formalized workspaces and `data/papers`; use `scripts/repair_catalog_asset_refs.py` for repair.
`commit_paper_raw_to_papers.py` 只接收 `ready_for_commit` 的已正式化文件夹，做 final validate → 事务性安装（staging copytree → 自检 → `os.replace` → activate ledger → rebuild all.catalog → postcheck → 删源），任何后置失败回滚删除 `data/papers/<paper_id>`、不污染正式库。`data/papers` 不允许半成品。commit 在 staging 清理后、写正式 manifest 前必须删除所有 `*.asset_manifest.json` 残留，确保 `data/papers/<paper_id>/` 恰有一个 `<paper_id>.asset_manifest.json`。`rebuild_all_catalog.py --apply` 对正式库是 strict：`data/papers/<paper_id>/` 只要出现任意正式资产（metadata/catalog/md/pdf/asset_manifest/images/`*.paper.number`）就必须完整；缺项必须失败、返回非 0、**不写/不覆盖** `all.catalog.json`/`paper_index.json`，不得静默跳过写空索引。`validate_v2_library.py` 必须对所有 `*.asset_manifest.json` 做 glob 扫描并拒绝额外 manifest。`doctor_ingest_pipeline.py` 的 pytest step 使用 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` 隔离环境与 300s timeout，超时返回 blocking failure（`returncode=124`）。

`formalize_paper_raw.py` 与 `commit_paper_raw_to_papers.py` 都支持完整路径隔离参数
（`--paper-raw-dir` / `--papers-dir` / `--ledger-path` / `--all-catalog-path`）。
agent / Codex 测试时**必须**传 tmp `--ledger-path` 与 tmp `--all-catalog-path`，避免污染真实 `data/catalog`；
默认值指向真实 `data/catalog/paper_number_ledger.json` / `all.catalog.json`（被 gitignore，`git status` 看不出，但会静默污染真实编号账本）。
metadata 标题/作者/单位/摘要/关键词/DOI 候选优先读取转换后 Markdown 的物理前 100 行 front-matter evidence（**first 100 lines，不是 10 行**）。
formalize 只接受带当前 `<paper_number>.conversion.json` 的 `converted_current` 资产；缺 manifest 的已转换资产必须先生成当前 manifest 或重新转换。普通 ingest 测试夹具必须从 16 位
`paper_number` 工作区开始，并通过 formalize 生成 `.formalization.json`、`.paper.number` 与
`ready_for_commit`，不得手写 happy-path 正式化产物。`preserve_paper_number` 使用 reserved-specific
语义保留编号，不走 legacy `repoint()`。`Catalog.load()` 缺少 all.catalog 时只构建 tolerant
read-only snapshot，不写 ledger、marker 或 catalog 索引。

写作主流程：selected catalog / paper numbers → `write/jobs/<job_id>/article/` → TeX/BibTeX →
compile/check（`check_write_tex_project.py`）→ quality check（`check_write_quality_text.py`）。
详见 `docs/WRITER_PRODUCTIZATION_PLAN.md` 与 `skills/catalog_tex_writer`。

详细规则与边界见 `docs/PROJECT_STATUS.md`、`docs/PROJECT_CONTRACT.md`。

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

## 6. 正式资产结构

```text
data/papers/<paper_id>/<paper_id>.metadata.json
data/papers/<paper_id>/<paper_id>.catalog.json
data/papers/<paper_id>/<paper_id>.md
data/papers/<paper_id>/<paper_id>.pdf
data/papers/<paper_id>/images/
data/papers/<paper_id>/<16位编号>.paper.number
```

> **Note on snapshot profiles**: `mineru_snapshot.zip` 默认使用 **audit profile**，
> 会额外包含 `.gitignore` 忽略的轻量文本/结构文件（.json / .md / .yaml / .toml / .bib / .tex 等），
> 但**不含 PDF、图片、二进制文件**。
> 纯源码分发使用 `python scripts/pack_repo.py --profile source`。
> 详见 §7。,

项目验收分两层：

**A. Git hygiene** — 检查 git tracked 文件是否包含运行时资产（严格）

```bash
git status --short
git ls-files write/jobs                       # 应仅 .gitkeep
git ls-files data/papers data/paper_raw data/raw data/import_work  # 应仅 .gitkeep
```

**B. Audit snapshot** — `mineru_snapshot.zip` 是轻量审计快照，额外包含
全工作区的轻量文本/结构文件（`.json` / `.md` / `.yaml` / `.toml` / `.csv`
/ `.bib` / `.tex` / `.py` / `.sh` / `.bat` 等），包括被 `.gitignore` 忽略的
运行时样本，但仍拒绝 PDF、图片、日志、缓存、密钥、二进制文件等。
详见 `scripts/pack_repo.py` 中的 `LIGHTWEIGHT_ALLOWED_SUFFIXES`、
`HEAVY_OR_BINARY_DENIED_SUFFIXES`、`DENIED_NAMES`、`DENIED_PATH_PARTS` 和大小限制。
`mineru_snapshot.zip` 是 **lightweight audit/handoff snapshot**，不是完整数据备份。
`data/paper_raw/` 和 `data/papers/` 各最多保留 5 个样例 workspace（按目录名升序确定性选择）。
真实磁盘数据不受影响。如需完整论文数据，请使用专门的备份/导出流程。

验收命令（默认 mode=audit 自动含 git hygiene + snapshot 验证）：

```bash
# 每次 agent 改动后默认运行（快速验收，覆盖核心边界）：
conda run -n mineru python scripts/agent_acceptance.py

# 大范围重构后运行全量：
conda run -n mineru python scripts/agent_acceptance.py --full

# 纯源码验收（不包含 runtime sample）：
conda run -n mineru python scripts/pack_repo.py --profile source
conda run -n mineru python scripts/agent_acceptance.py --profile source
```

- 默认模式运行 `compileall` → fast acceptance tests → `git hygiene` → `pack_repo.py` → snapshot 验证。
- `--full` 模式运行全量 `pytest -q`。
- `--no-pack` 仅允许调试使用，普通 agent 改动不得只跑 `--no-pack` 作为最终验收。
- 不允许只运行 `pack_repo.py` 就宣称完成。
- 不允许直接运行裸 `pytest` 作为最终验收；裸 `pytest` 仅用于开发调试。
- agent 最终回复必须包含 `agent_acceptance.py` 的最后输出，尤其必须包含 `[OK] agent acceptance passed` 和 `[OK] Packed: mineru_snapshot.zip`。
- 按主题拆 commit，不要把多个主题混进一个 commit。

## 8. ingest 前置重复检测

ingest duplicate guard 是前置硬门禁，不是后置清理：

- 手动 PDF staging 前必须检查待导入 PDF 是否与 `data/paper_raw` 或 `data/papers` 中已有 PDF 内容重复（sha256 + md5）；命中时不得创建新 `paper_raw`、不得占用 ledger、`--move` 下不得移动源 PDF。
- 网络 metadata staging 前必须检查 DOI 是否已存在于 `data/paper_raw` 或 `data/papers`；同一 input batch 内重复 DOI 也必须阻断，除非显式 `--skip-duplicates` 跳过重复项。
- metadata resolver 的候选 DOI 必须同时检查 paper_raw 队列和 formal papers；manual confirm / `--force` 不得绕过 DOI/PDF 内容重复硬门禁。已 citation-ready（matched/manual_confirmed + DOI）的 metadata 默认完全 no-op：不联网、不重写 metadata、不写 candidates/patch/report/import_status；只有显式 `--force` 才重新解析。
- fetch/attach PDF 前必须检查 PDF 内容重复；命中时不得覆盖当前 paper_raw PDF，不得把重复 PDF hash 写入 metadata。
- `preflight_paper_raw_import.py`、formalize/commit readiness 和 `audit_ingest_duplicates.py --strict` 是最后防线，不是第一道防线。
- `*.metadata.candidates.json` / `*.metadata.patch.json` / resolver report 都是 sidecar 诊断文件，不是正式 metadata；DuplicateIndex 只读正式 `*.metadata.json` 与 PDF hash，不得把 sidecar DOI 当成硬重复来源。
- ingest duplicate guard 必须覆盖**所有** paper_raw 工作区，不仅是 16 位编号目录。`data/paper_raw/` 下存在两类工作区：(1) 严格 16 位 `paper_number` staging 工作区（如 `0000000000000206/`），(2) 历史 / untitled / formalized 工作区（如 `1979_sykest_untitled/`，内部带 `.paper.number` marker 与 `metadata.paper_number`）。`build_ingest_duplicate_index()` 通过 `is_paper_raw_workspace()`（依据是否存在 metadata/import_status/stage_manifest/paper.number/pdf/md 等资产）识别工作区，绝不把“不是 16 位编号目录”当成“不是 paper_raw 工作区”。`scripts/audit_paper_raw_duplicate_workspaces.py` 负责审计并清理（移入 `quarantine/duplicate_workspaces/`，不删除、不回收 paper_number、不降 `max_number`）。
- `scripts/audit_paper_number_ledger.py --detect-orphans` 可区分正常 `metadata_only_workspace` 与异常 `empty_orphan_dir`；只有显式 `--fix-empty-orphans --apply --reason ...` 才能清理严格空目录，metadata-only workspace 绝不能清理。

# AGENTS.md

面向所有 coding agent 的项目操作规约。修改本仓库前必须阅读：

- `docs/PROJECT_STATUS.md`（当前冻结状态与主流程总览）
- `docs/PROJECT_CONTRACT.md`（不可破坏契约）
- `README.md`（文档入口地图）

新 agent 建议阅读顺序：`AGENTS.md` → `docs/PROJECT_STATUS.md` → `docs/PROJECT_CONTRACT.md` → `README.md`。
按改动类型深入：ingest 改动看 `docs/PROJECT_CONTRACT.md`、`docs/DEPENDENCIES_AND_EXTERNAL_TOOLS.md`、
`reports/real_ingest_acceptance.md`；writing 改动看 `docs/WRITING_QUALITY_ACCEPTANCE.md`、
`docs/WRITER_PRODUCTIZATION_PLAN.md`、`skills/catalog_tex_writer`；fetch/依赖改动看
`docs/DEPENDENCIES_AND_EXTERNAL_TOOLS.md` 与 `docs/PROJECT_CONTRACT.md`。

## 1. 项目状态

- ingest v2.3 strict-only current incremental state（不打新 tag）：新 ingest 的 `data/paper_raw/<id>/` 使用 16 位 `paper_number`，staging 第一步即 reserve 编号并写 `<paper_number>.paper.number` marker；正常 CLI 只接受 `--paper-number` / `--paper-numbers`，旧 6 位编号仅限 `scripts/legacy/` migration。
- ingest v2.2 frozen，tag `ingest-v2.2`（v2.1 已被状态机重构取代：curate 不再改名/不再 commit，新增 `formalize_paper_raw.py` 在 paper_raw 内完成正式化，`commit` 退化为事务性安装）。
- writing v0.1 frozen，tag `writing-v0.1`。
- catalog schema 保持 v2.0；新 metadata schema 为 v1.1（`paper_number` / `paper_raw_id`，不生成 `source_id`）；不修改 writer v0.1 行为。
- 当前增量改动必须文档化，不打新 tag。

## 2. 不可违反的边界

- `metadata`（`<paper_id>.metadata.json`）是书目信息事实源（DOI、作者、年份、期刊、卷期页、BibTeX）。
- `catalog`（schema v2.0）是 content-only：`all.catalog` 不得含 DOI/作者/年份/venue/metadata/display 等书目字段，
  两者仅通过 `paper_number`/`paper_id` 关联。
- catalog 自然语言 value 默认尽量使用中文；JSON key/schema enum 保持英文，技术名词可中英混写。
  metadata 不中文化、不改写 DOI/作者/期刊/年份/BibTeX。
- `references.bib` 只从复制的 metadata 生成，绝不从 catalog 或正文拼接。
- `write/jobs` 是写作运行时，不提交（只跟踪 `.gitkeep`）；TeX 不得直接读 `data/papers`、`data/raw`、
  `data/paper_raw`，只能读 job-local 复制副本。
- 不做 RAG / embedding / vector DB / ChromaDB；不内置 LLM client，所有 prompt/写作步骤只生成文本或模板。
- Sci-Hub resolver 是 unsafe optional：默认 disabled，不属于 OA_ONLY 主流程；仅 `AccessMode.CUSTOM` 且
  `allow_scihub=True` 时才启用，且不得放宽该条件。
- 网络 metadata 进入 `paper_raw` 前必须有合法 DOI；没有 DOI 的候选不得 stage。
- 正式入库必须通过 `validate_v2_library.py` 与 `audit_metadata_quality.py` 的硬错误检查。

## 3. 运行环境

- 真实入库 / 转换 / 写作验收命令必须使用 `conda run -n mineru ...`
  （PATH 上的 `python` 是 Windows Store 别名，会静默退出 code 49）。
- Windows 控制台先 `set PYTHONIOENCODING=utf-8`，避免 GBK 下中文/JSON 输出失败。
- MinerU 正式转换必须使用 GPU：默认 `MINERU_REQUIRE_GPU=true`、`CUDA_VISIBLE_DEVICES=0`。
  `stage_raw_pdfs_to_paper_raw.py` 不需要 GPU；formal ingest 使用 `convert_paper_raw_gpu.py`，
  底层 `convert_paper_raw_batch.py` 也会强制 formal GPU 默认与 `torch.cuda.is_available()` preflight。
  CPU/no-GPU 只允许调试：显式设置 `MINERU_ALLOW_CPU=true` 或 `MINERU_REQUIRE_GPU=false`。

## 4. 数据边界

- `data/papers`、`data/raw`、`data/paper_raw`、`data/import_work`、`write/jobs` 为运行时 / 真实数据区。
- snapshot 只含 `.gitkeep` 与 catalog `.template.json` 空模板；`pack_repo.py` 强制排除真实数据与生成的
  catalog 索引（`all.catalog.json`、`paper_index.json`、`paper_number_ledger.json`）。
- 任何 PDF / Markdown / images / TeX 编译产物不进入 snapshot。

## 5. 主流程

入库主流程分两条路径。手动 PDF 路径必须先 MinerU 转换、再从转换后的 md 解析 metadata：

```bash
# Manual PDF path: convert first, resolve metadata from converted md second.
set MINERU_RUNNER=cli_api_proxy
set MINERU_API_URL=http://127.0.0.1:8000
conda run -n mineru python scripts/start_mineru_services.py --wait
conda run -n mineru python scripts/stage_raw_pdfs_to_paper_raw.py --move --dry-run --report reports/stage_raw_dryrun.json
conda run -n mineru python scripts/stage_raw_pdfs_to_paper_raw.py --move --apply --report reports/stage_raw_move.json
conda run -n mineru python scripts/convert_paper_raw_gpu.py --all --apply --report reports/convert_paper_raw.json
conda run -n mineru python scripts/resolve_paper_raw_metadata.py --all-unmatched --apply --allow-network --write-candidates --report reports/resolve_candidates.json
conda run -n mineru python scripts/curate_paper_raw.py --all-ready --apply
conda run -n mineru python scripts/formalize_paper_raw.py --all-ready --apply --report reports/formalize_paper_raw.json
conda run -n mineru python scripts/commit_paper_raw_to_papers.py --all-ready --apply
conda run -n mineru python scripts/rebuild_all_catalog.py --apply
conda run -n mineru python scripts/validate_v2_library.py
conda run -n mineru python scripts/stop_mineru_services.py
```

手动 PDF 导入时，metadata resolver 必须基于 MinerU 转换完成后的 md，因此顺序是先转换，再解析/匹配
metadata。不要在没有 md 时跑 resolver；初始 unmatched 的手动 PDF 不要用 `--only-preflight-ready`
挡住转换（该 flag 适合已有 matched metadata 的网络路径）。
手动 PDF 正常导入时，`data/raw/` 是待处理队列；成功 stage 后对应 PDF 应从 raw 消失。
正常 SOP 必须使用 `stage_raw_pdfs_to_paper_raw.py --move --apply`。copy 模式只用于调试、备份、
测试或明确的一次性检查，不是默认导入规范。
批量转换应先启动持久 `mineru-api`（推荐 `python scripts/start_mineru_services.py --wait`；
Windows 兼容入口 `start_fast_api_mode.bat` 会委托该脚本），
再设置 `MINERU_RUNNER=cli_api_proxy` 与 `MINERU_API_URL=http://127.0.0.1:8000`，避免每篇 PDF 冷启动。
`mineru-api` 必须在它自己的 shell 中以 `CUDA_VISIBLE_DEVICES=0` 启动；只在 client 进程设置
`CUDA_VISIBLE_DEVICES` 不能改变已经运行的 `mineru-api`。formal conversion 会同时检查
`nvidia-smi` 与当前 Python 环境的 `torch.cuda.is_available()`。
`start_fast_api_mode.bat` 是 single-instance helper：已有健康 `mineru-api` 时复用，端口 8000
被占用但 `/health` 不通时拒绝启动新服务。多篇 formal batch 不允许 `MINERU_RUNNER=cli`
冷启动；单篇 CLI 仅用于测试/调试。`paper_raw` 转换是幂等的，已有 `<paper_number>.md` + `images/`
默认 skipped；只有显式 `--force-reconvert` 才删除旧 md/images/output 并重跑 MinerU。
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

v2.3 状态机职责边界：`curate_paper_raw.py` 只校验 metadata/catalog 并写 `status=catalog_ready`，**不再改名、不再分配 paper_number、不再写 ready_for_commit**；
`formalize_paper_raw.py` 是 commit 前必经步骤，在 `data/paper_raw/<paper_number>/` 内使用 staging 已 reserved 的 16 位 paper_number 完成 canonical paper_id 改名、回填 catalog 链接、写 `<paper_id>.formalization.json` + `<16位>.paper.number` marker，置 `status=ready_for_commit`；rename 后 ledger reserved entry 必须 repoint 到 formalized 后的 `<paper_id>` 工作区（不是旧 `0000000000000001`）；
`commit_paper_raw_to_papers.py` 只接收 `ready_for_commit` 的已正式化文件夹，做 final validate → 事务性安装（staging copytree → 自检 → `os.replace` → activate ledger → rebuild all.catalog → postcheck → 删源），任何后置失败回滚删除 `data/papers/<paper_id>`、不污染正式库。`data/papers` 不允许半成品。

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

## 6. 正式资产结构

```text
data/papers/<paper_id>/<paper_id>.metadata.json
data/papers/<paper_id>/<paper_id>.catalog.json
data/papers/<paper_id>/<paper_id>.md
data/papers/<paper_id>/<paper_id>.pdf
data/papers/<paper_id>/images/
data/papers/<paper_id>/<16位编号>.paper.number
```

## 7. 提交前检查

```bash
conda run -n mineru pytest -q
conda run -n mineru python scripts/pack_repo.py
git status --short
git ls-files write/jobs                       # 应仅 .gitkeep
git ls-files data/papers data/paper_raw data/raw data/import_work  # 应仅 .gitkeep
```

- 确认无真实 data / `write/jobs` runtime 被 staged。
- 按主题拆 commit，不要把多个主题混进一个 commit。
- 每次代码改动后运行测试并生成 `mineru_snapshot.zip`。


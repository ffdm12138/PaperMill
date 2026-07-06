# 项目核心契约

本仓库是本地文献资产库、AI 可读目录和综述写作工作区。正式入库只允许走 v2 `paper_raw` 工作流。

> **`data/paper_raw` = 工作区 / 待处理队列**（所有处理步骤都在此完成，可修改、可重跑、可丢弃）。
> **`data/papers` = 正式库**（只读，commit 后的最终资产，不允许半成品，不允许原地修改）。
> 一切处理在 paper_raw 内完成后再入库，papers 只有最终结果。

## 不可改变的规则

- 不做向量库（vector DB）、RAG、embedding 或 ChromaDB。
- 不内置 LLM client；所有 prompt 和写作步骤只生成文本或模板。
- 所有新文献先进入 `data/paper_raw/<0000000000000001>/` 这类 16 位 `paper_number` 工作区。
- MinerU 正常路径只处理 `data/paper_raw/<paper_number>/<paper_number>.pdf`。
- MinerU conversion requires GPU / MinerU 正式转换必须使用 GPU；formal ingest 使用
  `scripts/convert_paper_raw_gpu.py`，默认 `MINERU_REQUIRE_GPU=true`、`CUDA_VISIBLE_DEVICES=0`，
  并要求 `nvidia-smi` 与当前 Python 环境的 `torch.cuda.is_available()` 均通过。
  CPU/no-GPU 只允许显式调试：`MINERU_ALLOW_CPU=true` 或 `MINERU_REQUIRE_GPU=false`。
- 正式资产只保存在 `data/papers/<paper_id>/`，同目录保存 PDF、Markdown、metadata、catalog、images 和 paper number。
- API 与写作只读取本地生成的 `data/catalog/all.catalog.json`、`data/catalog/paper_index.json`、`data/catalog/paper_number_ledger.json` 和 `data/papers/<paper_id>/`；源码快照只提交对应 `.template.json` 空模板，不提交真实库索引。
- metadata 管书目信息和 BibTeX 事实；catalog（schema v3.1）只管正文内容理解（library_locator、content_identity、分类、研究卡片、写作价值、证据画像、图表清单、质量控制、精读筛选 `screening`），**不含** DOI/作者/年份/期刊/卷期页等书目字段。两者仅通过 `paper_number`/`paper_id` 关联。
- catalog 自然语言 value 默认尽量使用中文，服务中文检索、分类、选文和写作 workflow；JSON key/schema enum 保持英文，专业名词可中英混写。metadata 保留原始/规范书目事实，不因 catalog 中文化而改写；metadata v2.0 不再承载中文标题 / 摘要 / 关键词 / notes。
  catalog 的中文内容（`content_identity.content_title_zh`、`research_card.mechanisms`、`research_card.limitations`、`writing_value.short_summary`）必须由 LLM 子代理从 Markdown 正文生成，不接受直接拼接或手工填写。每次入库前必须运行子代理补全。
- 初始 catalog 生成 / paper_raw curator / 入库前 catalog 生成阶段，`screening.read_decision` 必须固定为 `"pending"`；禁止在该阶段写成 `must_read` / `maybe_read` / `skip`。这些最终精读决策值只允许出现在 post-triage / writing-stage catalog、人工筛选或精读 triage 阶段。
- **metadata is bibliographic truth; catalog is content understanding; paper_number links them. all.catalog is a content index, not a bibliography database.** references/BibTeX 必须从 metadata 生成，绝不从 catalog 生成。
- catalog 由项目级 skill `paper_raw_catalog_curator` 在 commit 前从 MinerU Markdown 生成（content-only，不生成 metadata patch）；`curate_paper_raw.py` 只校验并写 `catalog_ready`，不改名、不分配 paper_number。metadata 空字段由 metadata resolver/enrichment 补齐，不覆盖非空字段。
- Catalog path refs are stage-specific: 16-digit staging catalogs may reference
  `<paper_number>.md`, but formalized paper_raw and `data/papers` catalogs must reference
  `<paper_id>.md` in both `library_locator.asset_refs.markdown` and `provenance.markdown_path`.
- `data/catalog/all.catalog.json` 只聚合 catalog 内容（content-only，无 metadata）；`data/catalog/paper_index.json` 做 paper_number→路径映射（也不含书目字段）。这些文件由 `scripts/rebuild_all_catalog.py --apply` 在本地生成，需要书目信息时按 paper_number 读正式 paper 文件夹中的 metadata。
- 网络/搜索 metadata 导入必须有 DOI，并写入 `metadata.identifiers.doi`；没有 DOI 的搜索结果不得进入 `paper_raw`。
  OpenAlex/CrossRef search metadata with a valid DOI is staged as
  `metadata_match.status = "matched"` and `.import_status.json status = "metadata_matched"`.
- Metadata-only PDF fetch 只能补齐已有 `data/paper_raw/<16位 paper_number>/` 工作区：DOI 只读
  `<paper_number>.metadata.json` 的 `identifiers.doi`，不读 `doi.csv`，不接受额外 DOI list，
  不分配新 paper_number，不按 title/DOI/URL basename 命名 PDF；成功 PDF 必须经
  `PaperRawAllocator.attach_pdf()` / duplicate guard 落为 `<paper_number>.pdf`。
- Header-based PDF fetch 只能显式启用（`--resolver header-based`），默认 OA_ONLY 不包含它；
  其 User-Agent 固定在代码中，用户每次运行只传 Cookie/Authorization 等额外 header，header
  明文不得进入 metadata、report 或日志。
- 手动 PDF 可以先生成无 DOI 的空壳 metadata，但只有补齐 DOI 且 `metadata_match.status` 为 `matched` 或 `manual_confirmed` 后才能 curation/commit。
- 正式库 `data/papers/<paper_id>/` 中每篇论文必须有 DOI；metadata 不完整的 `paper_raw` 保留在 `paper_raw`，不得入库。
- LLM/curator 只能补 metadata 空字段，不能编造 DOI，不能覆盖非空 DOI。
- 全局 `references.bib` 已移除；写作 per-job `references.bib` 由 `bibtex_from_metadata` 从 metadata 逐篇生成。
- JSON 写入必须原子化：filelock、临时文件、`os.replace`。
- 外部输入的 id、文件名和路径必须校验并通过 safe child 解析。
- ingest duplicate guard 是前置硬门禁：PDF staging / fetch / attach 前必须用 sha256 + md5 检查 `data/paper_raw` 和 `data/papers`；network metadata staging / metadata resolver apply 前必须检查 DOI 是否已存在于 `data/paper_raw` 或 `data/papers`。命中 DOI/PDF 内容重复时，不得创建新 paper_raw、不得占用 ledger、不得移动源 PDF、不得覆盖 PDF、不得写入 catalog。
- preflight / formalize / commit 前必须本地查重：重复 DOI、PDF sha/md5、标题/作者/年份或正文指纹不得新建正式 paper；这些 gate 是最后防线，不是第一道防线。
- ingest duplicate guard 必须覆盖**所有** paper_raw 工作区，不仅 16 位编号目录。`data/paper_raw/` 下有两类工作区：严格 16 位 `paper_number` staging 工作区，和历史/untitled/formalized 工作区（folder 名非 16 位，但内部带 `*.paper.number` marker 与 `metadata.paper_number`/`paper_raw_id`）。判断 paper_raw 工作区依据 `is_paper_raw_workspace()`（是否存在 `*.metadata.json`/`.import_status.json`/`stage_manifest.json`/`*.paper.number`/`*.pdf`/`*.md` 等资产），不得仅凭目录名是否为 16 位数字。重复工作区由 `scripts/audit_paper_raw_duplicate_workspaces.py` 审计并清理：移入 `data/paper_raw/quarantine/duplicate_workspaces/`，**绝不删除、绝不回收 paper_number、绝不降低 `max_number`**，ledger 对应 entry 置 `state=quarantined_duplicate` 并 repoint `folder_path` 到 quarantine。
- `paper_number` 为 16 位长期编号，只递增不回收；staging 阶段 reserve（state=reserved），`formalize_paper_raw.py` 只复用/校验该 reserved number，并在改名后 repoint ledger folder_path；commit 成功时 activate（state=active），commit 失败回滚为 reserved。
- 正常 ingest 的 `paper_number` 只能由 `PaperNumberLedger` 分配，不得用 `max(existing folders)+1`、不得回收空洞、不得重新解释历史编号。特殊清零或 `paper_raw` 压缩重编号仅允许 admin-only `scripts/reset_paper_number_ledger.py`，默认 dry-run，`--apply` 必须显式确认并填写 `--reason`，且 `data/papers/` 非空时必须拒绝。
- `.paper.number` 文件名解析必须裁剪完整 `.paper.number` 后缀，禁止用 `Path.stem`；所有 marker 与 ledger 写入必须原子化，并通过 lock 串行化。
- 测试不得访问真实网络；网络 provider 必须 mock。
- 正式入库必须通过 `validate_v2_library.py` 与 `audit_metadata_quality.py` 的硬错误检查；未通过的 `paper_raw` 不得入库。
- `write/jobs/` 是写作运行时，不提交（只跟踪 `.gitkeep`）；TeX 不得直接引用 `data/papers`、`data/raw` 或 `data/paper_raw`，只能读 job-local 复制副本。
- Sci-Hub resolver 已移除（removed）：项目不提供、不注册 Sci-Hub resolver；PDF 获取按「原始链接 → OA → header_based DOI landing fallback（默认 `https://doi.org/{doi}`，无需额外配置）→ 失败报告」优先级执行，不再有任何 unsafe fallback。
- metadata-only PDF fetch priority: 1. original links already present in metadata; 2. legal OA resolvers; 3. publisher-specific / preprint / PMC resolvers (sciengine_direct, biorxiv, pmc_oa); 4. header_based DOI landing fallback, default https://doi.org/{doi}; 5. rich failure report.
- 每次代码改动后必须运行测试并生成 `mineru_snapshot.zip`。
- 新 ingest 的 `data/paper_raw/<id>/` 必须使用 16 位 `paper_number`；staging 即 reserve ledger 并写 `<paper_number>.paper.number` marker。正常 CLI 只接受 `--paper-number` / `--paper-numbers`；6 位旧编号只允许 `scripts/legacy/` migration 工具处理。
- CLI 路径隔离：`formalize_paper_raw.py` 与 `commit_paper_raw_to_papers.py` 必须支持 `--paper-raw-dir`/`--papers-dir`/`--ledger-path`/`--all-catalog-path`；测试/agent 运行时必须传 tmp `--ledger-path` 与 tmp `--all-catalog-path`，禁止污染真实 `data/catalog/paper_number_ledger.json` / `all.catalog.json`。
- `ready_for_commit` 阶段 ledger reserved entry 必须指向 formalized 后的 `<paper_id>` 工作区（formalize rename 后由 `repoint_reserved` 同步），不得停留在旧 `0000000000000001`。
- `formalize_paper_raw.py` 只接受 `converted_current` conversion manifest；缺 manifest 的已转换资产必须先生成当前 manifest 或重新转换。`preserve_paper_number` 必须以 `state=reserved` 保留具体编号，不得走 legacy `repoint()` 创建/复用 active 语义。
- 普通 ingest/commit 测试夹具必须从 16 位 `paper_number` paper_raw 工作区开始；除 repair、audit、corruption 负例外，不得手写 `.formalization.json` 或 `ready_for_commit`，marker 必须来自 allocator/staging/ledger helper。
- **正式库目录完整性硬契约**：`data/papers/<paper_id>/` 只要出现任意正式资产（`<pid>.metadata.json`/`<pid>.catalog.json`/`<pid>.md`/`<pid>.pdf`/`<pid>.asset_manifest.json`/`images/`/`*.paper.number`）就必须**完整**，缺任一 required asset 即违法。`rebuild_all_catalog.py --apply` 遇不完整正式库目录必须失败（返回非 0、打印 `not written`、**不写也不覆盖** `all.catalog.json` / `paper_index.json`），不得静默跳过写空索引。
- **asset_manifest 单一性硬契约**：正式库每篇论文**恰有一个** `<paper_id>.asset_manifest.json`；`paper_raw` 阶段的 `<paper_number>.asset_manifest.json` 不得进入 `data/papers`。`commit_paper_raw_to_papers.py` 在 staging 清理后、写正式 manifest 前必须删除所有 `*.asset_manifest.json` 残留；`validate_v2_library.py` 必须对所有 `*.asset_manifest.json` 做 glob 扫描并拒绝额外 manifest（"unexpected asset manifest"）。
- `Catalog.load()` 在 all.catalog 缺失时只能构建 tolerant read-only snapshot，不得写 ledger、marker、per-paper catalog、all.catalog 或 paper_index。
- metadata 标题/作者/单位/摘要/关键词/DOI 候选优先读取转换后 Markdown 物理前 100 行（first 100 lines）。

## 唯一正式流程（两条路径）

Network metadata path（metadata 先行，已有 DOI）:
```text
network metadata (with DOI)
-> data/paper_raw/<0000000000000001>/
-> PDF fetch
-> MinerU convert
-> curation（写 catalog_ready）
-> formalize（改名 + reuse/repoint reserved paper_number + ready_for_commit）
-> commit 到 data/papers/<paper_id>/
-> rebuild all.catalog
-> writing v0.1 按 paper_number 复制到 write/jobs/<job_id>/article/<paper_number>/
```

Manual PDF path（先转换，再从转换后的 md 解析 metadata）:
```text
data/raw/*.pdf
-> stage_raw_pdfs_to_paper_raw --move --apply
-> data/paper_raw/<0000000000000001>/
-> MinerU convert         # 转换在 metadata resolve 之前
-> resolve metadata       # 读转换后的 md，抽取候选，联网验证/查询
-> curation（写 catalog_ready）
-> formalize（改名 + reuse/repoint reserved paper_number + ready_for_commit）
-> commit 到 data/papers/<paper_id>/
-> rebuild all.catalog
-> writing v0.1 按 paper_number 复制到 write/jobs/<job_id>/article/<paper_number>/
```

手动 PDF 导入时，metadata resolver 依赖转换后的 Markdown，必须在 MinerU 转换之后运行
（先转换，再解析）。两条路径 curation/commit 前都要求 `metadata_match.status` 为 `matched`
或 `manual_confirmed` 且 `identifiers.doi` 非空。
手动 PDF 正常导入时，`data/raw/` is a queue / raw 是待处理队列；成功 stage 必须消费 raw 中的
PDF，并移动到 `data/paper_raw/<paper_number>/<paper_number>.pdf`。copy 模式只允许用于调试、备份、
测试或明确的一次性检查，不是默认手动导入 SOP。
`stage_raw_pdfs_to_paper_raw.py` 不需要 GPU；formal MinerU conversion 使用
`convert_paper_raw_gpu.py`，底层 `convert_paper_raw_batch.py` 仅作兼容/调试入口。批量转换优先
先运行 `python scripts/start_mineru_services.py --wait --restart-if-stale` 启动/复用持久 `mineru-api`，再使用
`MINERU_RUNNER=cli_api_proxy` + `MINERU_API_URL=http://127.0.0.1:8000`；持久 `mineru-api`
必须在自己的 shell 中以 `CUDA_VISIBLE_DEVICES=0` 启动。
`/health` 只代表 liveness，不代表 GPU conversion readiness；正式批量转换前必须确认 managed
service identity、`check_mineru_processes.py` verdict 为 `READY_FOR_CONVERSION`，并有成功的
`smoke_mineru_conversion.py` 单篇报告。`start_fast_api_mode.bat` 仅复用 managed healthy `mineru-api`；
Formal batch conversion reads the default smoke report at
`reports/smoke_mineru_conversion.json`; `--smoke-report` is only an override.
端口占用但 `/health` 不通时不得再启动新服务。多篇 formal batch 不得使用 `MINERU_RUNNER=cli` 冷启动；单篇 CLI 仅用于测试/调试。
MinerU PDF 转换进程不设置固定 timeout；health/preflight/HTTP 与 `MinerULock` 等待 timeout
是独立保护，不等同于 PDF 固定秒数限制。metadata 标题/作者/单位/摘要/关键词/DOI 候选必须优先读
转换后 Markdown 的物理前 100 行作为 front-matter evidence；PDF title fallback 只有在 Markdown
缺失或前 100 行无可靠候选时才使用。
`paper_raw` conversion must be idempotent：已有 `<paper_number>.md` + `images/` 默认跳过；
成功转换写 `<paper_number>.conversion.json`，该 manifest 是 transient artifact，formal commit
必须清理，不能进入 `data/papers/`。
转换前可复用本地 `output/mineru_cache/` raw-output cache，但命中必须同时校验 PDF md5、sha256、
file size 与 `backend/method/lang/effort`；未带 manifest 且无可校验 PDF hash 的旧 output 不得自动复用。
cache hit 只恢复 md/images/conversion manifest/asset manifest，不触发 GPU preflight、mineru-api health
check 或 `MinerULock`。`--force-reconvert` 绕过 cache，`--ignore-output-cache` 禁用 cache 查找，
`--cache-only` 只恢复 cache、不运行 MinerU。`output/` 不进入 git 或 snapshot。

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

Writing uses only job-local article copies under `write/jobs/<job_id>/article/<paper_number>/`.

## 正式目录

```text
data/papers/<paper_id>/<paper_id>.metadata.json
data/papers/<paper_id>/<paper_id>.catalog.json
data/papers/<paper_id>/<paper_id>.md
data/papers/<paper_id>/<paper_id>.pdf
data/papers/<paper_id>/images/
data/papers/<paper_id>/<16位编号>.paper.number
```

## 验收命令

```bash
# 推荐：agent 统一验收（git hygiene → 测试 → 打包 → 快照验证）
conda run -n mineru python scripts/agent_acceptance.py

# 全量测试
conda run -n mineru python scripts/agent_acceptance.py --full

# 纯源码打包（不含运行时样本）
conda run -n mineru python scripts/pack_repo.py --profile source
```

真实 `data/papers/` 文献资产和本地生成的 catalog 索引不进入 git tracked 或 source-profile zip。
但默认 audit-profile zip (`mineru_snapshot.zip`) 是审计快照（audit snapshot），会额外包含
被 `.gitignore` 忽略的部分运行时样本（catalog/metadata/md/source_records 等），
**zip 中出现 allowlisted runtime sample 不代表 git 污染**。

> **`validate_v2_library.py` 的范围**：该脚本设计用于真实的本地正式库
> （`data/papers/`，包含完整 PDF/images/marker/manifest 等）。
> audit snapshot 可能只含部分样本，解压后直接跑完整的 `validate_v2_library.py`
> 可能会产生合理的 false positive（缺失 PDF/图片等）。
> 如果需要验证样本结构，使用 `--no-check-paths` 跳过本地资产存在性检查。

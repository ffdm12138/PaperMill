# 项目核心契约

本仓库是本地文献资产库、AI 可读目录和综述写作工作区。正式入库只允许走 v2 `paper_raw` 工作流。

## 不可改变的规则

- 不做向量库（vector DB）、RAG、embedding 或 ChromaDB。
- 不内置 LLM client；所有 prompt 和写作步骤只生成文本或模板。
- 所有新文献先进入 `data/paper_raw/<000001>/`。
- MinerU 只能处理 `data/paper_raw/<000001>/<000001>.pdf`。
- MinerU conversion requires GPU / MinerU 正式转换必须使用 GPU；formal ingest 使用
  `scripts/convert_paper_raw_gpu.py`，默认 `MINERU_REQUIRE_GPU=true`、`CUDA_VISIBLE_DEVICES=0`，
  并要求 `nvidia-smi` 与当前 Python 环境的 `torch.cuda.is_available()` 均通过。
  CPU/no-GPU 只允许显式调试：`MINERU_ALLOW_CPU=true` 或 `MINERU_REQUIRE_GPU=false`。
- 正式资产只保存在 `data/papers/<paper_id>/`，同目录保存 PDF、Markdown、metadata、catalog、images 和 paper number。
- API 与写作只读取本地生成的 `data/catalog/all.catalog.json`、`data/catalog/paper_index.json`、`data/catalog/paper_number_ledger.json` 和 `data/papers/<paper_id>/`；源码快照只提交对应 `.template.json` 空模板，不提交真实库索引。
- metadata 管书目信息和 BibTeX 事实；catalog（schema v2.0）只管正文内容理解（分类、研究卡片、证据画像、精读筛选 `screening`），**不含** DOI/作者/年份/期刊/卷期页等书目字段。两者仅通过 `paper_number`/`paper_id` 关联。
- catalog 自然语言 value 默认尽量使用中文，服务中文检索、分类、选文和写作 workflow；JSON key/schema enum 保持英文，专业名词可中英混写。metadata 保留原始/规范书目事实，不因 catalog 中文化而改写。
- **metadata is bibliographic truth; catalog is content understanding; paper_number links them. all.catalog is a content index, not a bibliography database.** references/BibTeX 必须从 metadata 生成，绝不从 catalog 生成。
- catalog 由项目级 skill `paper_raw_catalog_curator` 在 commit 前从 MinerU Markdown 生成（content-only，不生成 metadata patch）；`curate_paper_raw.py` 只校验并写 `catalog_ready`，不改名、不分配 paper_number。metadata 空字段由 metadata resolver/enrichment 补齐，不覆盖非空字段。
- `data/catalog/all.catalog.json` 只聚合 catalog 内容（content-only，无 metadata）；`data/catalog/paper_index.json` 做 paper_number→路径映射（也不含书目字段）。这些文件由 `scripts/rebuild_all_catalog.py --apply` 在本地生成，需要书目信息时按 paper_number 读正式 paper 文件夹中的 metadata。
- 网络/搜索 metadata 导入必须有 DOI，并写入 `metadata.identifiers.doi`；没有 DOI 的搜索结果不得进入 `paper_raw`。
- 手动 PDF 可以先生成无 DOI 的空壳 metadata，但只有补齐 DOI 且 `metadata_match.status` 为 `matched` 或 `manual_confirmed` 后才能 curation/commit。
- 正式库 `data/papers/<paper_id>/` 中每篇论文必须有 DOI；metadata 不完整的 `paper_raw` 保留在 `paper_raw`，不得入库。
- LLM/curator 只能补 metadata 空字段，不能编造 DOI，不能覆盖非空 DOI。
- 全局 `references.bib` 已移除；写作 per-job `references.bib` 由 `bibtex_from_metadata` 从 metadata 逐篇生成。
- JSON 写入必须原子化：filelock、临时文件、`os.replace`。
- 外部输入的 id、文件名和路径必须校验并通过 safe child 解析。
- commit 前必须本地查重：重复 DOI、PDF sha、标题/作者/年份或正文指纹不得新建正式 paper。
- `paper_number` 为 16 位长期编号，只递增不回收；在 `formalize_paper_raw.py` 阶段 reserve（state=reserved），commit 成功时 activate（state=active），commit 失败回滚为 reserved。
- 测试不得访问真实网络；网络 provider 必须 mock。
- 正式入库必须通过 `validate_v2_library.py` 与 `audit_metadata_quality.py` 的硬错误检查；未通过的 `paper_raw` 不得入库。
- `write/jobs/` 是写作运行时，不提交（只跟踪 `.gitkeep`）；TeX 不得直接引用 `data/papers`、`data/raw` 或 `data/paper_raw`，只能读 job-local 复制副本。
- Sci-Hub resolver 是 unsafe optional：默认 disabled，不属于 `OA_ONLY` 主流程；仅 `AccessMode.CUSTOM` 且 `allow_scihub=True` 时才启用，且不得放宽该条件。
- 每次代码改动后必须运行测试并生成 `mineru_snapshot.zip`。
- CLI 路径隔离：`formalize_paper_raw.py` 与 `commit_paper_raw_to_papers.py` 必须支持 `--paper-raw-dir`/`--papers-dir`/`--ledger-path`/`--all-catalog-path`；测试/agent 运行时必须传 tmp `--ledger-path` 与 tmp `--all-catalog-path`，禁止污染真实 `data/catalog/paper_number_ledger.json` / `all.catalog.json`。
- `ready_for_commit` 阶段 ledger reserved entry 必须指向 formalized 后的 `<paper_id>` 工作区（formalize rename 后由 `repoint_reserved` 同步），不得停留在旧 `000001`。
- metadata 标题/作者/单位/摘要/关键词/DOI 候选优先读取转换后 Markdown 物理前 100 行（first 100 lines）。

## 唯一正式流程（两条路径）

Network metadata path（metadata 先行，已有 DOI）:
```text
network metadata (with DOI)
-> data/paper_raw/<000001>/
-> PDF fetch
-> MinerU convert
-> curation（写 catalog_ready）
-> formalize（改名 + reserve paper_number + ready_for_commit）
-> commit 到 data/papers/<paper_id>/
-> rebuild all.catalog
-> writing v0.1 按 paper_number 复制到 write/jobs/<job_id>/article/<paper_number>/
```

Manual PDF path（先转换，再从转换后的 md 解析 metadata）:
```text
data/raw/*.pdf
-> stage_raw_pdfs_to_paper_raw --move --apply
-> data/paper_raw/<000001>/
-> MinerU convert         # 转换在 metadata resolve 之前
-> resolve metadata       # 读转换后的 md，抽取候选，联网验证/查询
-> curation（写 catalog_ready）
-> formalize（改名 + reserve paper_number + ready_for_commit）
-> commit 到 data/papers/<paper_id>/
-> rebuild all.catalog
-> writing v0.1 按 paper_number 复制到 write/jobs/<job_id>/article/<paper_number>/
```

手动 PDF 导入时，metadata resolver 依赖转换后的 Markdown，必须在 MinerU 转换之后运行
（先转换，再解析）。两条路径 curation/commit 前都要求 `metadata_match.status` 为 `matched`
或 `manual_confirmed` 且 `identifiers.doi` 非空。
手动 PDF 正常导入时，`data/raw/` is a queue / raw 是待处理队列；成功 stage 必须消费 raw 中的
PDF，并移动到 `data/paper_raw/<source_id>/<source_id>.pdf`。copy 模式只允许用于调试、备份、
测试或明确的一次性检查，不是默认手动导入 SOP。
`stage_raw_pdfs_to_paper_raw.py` 不需要 GPU；formal MinerU conversion 使用
`convert_paper_raw_gpu.py`，底层 `convert_paper_raw_batch.py` 仅作兼容/调试入口。批量转换优先
先运行 `python scripts/start_mineru_services.py --wait` 启动/复用持久 `mineru-api`，再使用
`MINERU_RUNNER=cli_api_proxy` + `MINERU_API_URL=http://127.0.0.1:8000`；持久 `mineru-api`
必须在自己的 shell 中以 `CUDA_VISIBLE_DEVICES=0` 启动。
`start_fast_api_mode.bat` 必须先检查已有健康 `mineru-api`，端口占用但 `/health` 不通时不得
再启动新服务。多篇 formal batch 不得使用 `MINERU_RUNNER=cli` 冷启动；单篇 CLI 仅用于测试/调试。
MinerU PDF 转换进程不设置固定 timeout；health/preflight/HTTP 与 `MinerULock` 等待 timeout
是独立保护，不等同于 PDF 固定秒数限制。metadata 标题/作者/单位/摘要/关键词/DOI 候选必须优先读
转换后 Markdown 的物理前 100 行作为 front-matter evidence；PDF title fallback 只有在 Markdown
缺失或前 100 行无可靠候选时才使用。
`paper_raw` conversion must be idempotent：已有 `<source_id>.md` + `images/` 默认跳过；
成功转换写 `<source_id>.conversion.json`，该 manifest 是 transient artifact，formal commit
必须清理，不能进入 `data/papers/`。

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
python scripts/doctor_ingest_pipeline.py
python scripts/rebuild_all_catalog.py --apply
python scripts/validate_v2_library.py
python scripts/audit_metadata_quality.py
python scripts/check_directory_hygiene.py
pytest -q
python scripts/pack_repo.py
```

真实 `data/papers/` 文献资产和本地生成的 catalog 索引不进入源码快照；但本地真实库必须通过
`validate_v2_library.py` 和 `audit_metadata_quality.py` 的硬错误检查。

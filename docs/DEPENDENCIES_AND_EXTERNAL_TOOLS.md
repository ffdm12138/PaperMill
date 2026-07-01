# Dependencies and External Tools

本文档列出 MinerU v2 文献资产库的运行时依赖、本地可选接口、网络 metadata 服务、
PDF access resolver 体系，以及明确不引入的依赖。目的是让新 agent 或未来维护者一眼看清
“项目对外部世界有什么依赖、什么被禁止”。

## 1. Core runtime dependencies

| 依赖 | 用途 |
| --- | --- |
| `mineru[all]` | PDF / DOCX / 图片 → Markdown + 图片抽取，hybrid-engine 后端 |
| `PyMuPDF` | PDF 文本 / DOI 辅助抽取（轻量、无需 GPU） |
| `requests` | metadata 查询与 PDF fetch |
| `pydantic` | 数据模型校验 |
| `loguru` | 日志 |
| `filelock` | 原子化 JSON 写入与并发锁 |
| `pytest` | 测试 |

这些是正式入库 / 转换 / 写作链路的硬依赖，不包含向量库或 RAG 相关包。

## 2. Optional local interfaces

可选的本地服务接口，非正式入库主流程所必需：

- `FastAPI` / `uvicorn`：本地只读 API（`python -m src.server`），默认绑定 `127.0.0.1`。
- `Gradio`：可选的交互界面。

## 3. Network metadata services

下列网络服务只用于产生 **candidate metadata** 或 **PDF URL**，绝不直接写
`data/papers/`。candidate 必须先有合法 DOI，再走 `paper_raw` → match → curation →
commit 才能进入正式库。

- Crossref（DOI → 书目元数据）
- OpenAlex（书目元数据 / OA 位置）
- Semantic Scholar（书目元数据 / 引用）
- Unpaywall（合法 OA PDF URL）
- arXiv（预印本元数据与 PDF）
- bioRxiv（预印本元数据与 PDF）
- PMC OA（PubMed Central 开放获取）

> 边界：网络 metadata 进入 `paper_raw` 前必须有合法 DOI；没有 DOI 的候选不得
> stage。正式入库必须通过 `validate_v2_library.py` / `audit_metadata_quality.py`。

## 4. PDF access resolvers

PDF 获取由 `src/fetch/access_policy.py` + `src/fetch/resolver_registry.py` 统一调度，
按 `AccessMode` 选择启用哪些 resolver：

- **OA / direct resolvers**：`unpaywall`、`openalex`、`semantic_scholar`、`arxiv`、
  `publisher_oa`、`springer_direct`、`biorxiv`、`pmc_oa`。真正开放获取 / 合法公开来源，
  无需 token、不绕付费墙。属于 `OA_ONLY` 默认链路。
- **publisher / TDM resolvers**：`wiley_tdm`、`elsevier_tdm`、`publisher_tdm`。需要
  免费注册的 API token，属于机构 / 授权语义，仅在 `INSTITUTIONAL` 或 `CUSTOM` 下启用。
- **institutional / browser-assisted / custom resolvers**：`institutional_browser`、
  `browser_assisted`、`local_manual`、`custom`、`ref_downloader`。需要用户操作或机构订阅。
- **Sci-Hub**：`unsafe optional`，默认 disabled，**不属于 OA_ONLY 主流程**。仅
  `AccessMode.CUSTOM` 且 `allow_scihub=True` 时才启用。本仓库不放宽该启用条件。

代理配置统一走 `src/fetch/proxy.py::get_fetch_proxies()`，读取 `FETCH_PROXY` 环境变量，
返回 `requests` 可用的 proxies dict 或 `None`（直连）。合法 TDM / publisher resolver
不再语义上依赖 Sci-Hub 模块。

## 5. Explicitly removed / not used

项目明确不引入、不 vendor：

- ChromaDB
- sentence-transformers
- 任何向量数据库（vector database）
- embedding / RAG 管线
- 内置 LLM client（所有 prompt / 写作步骤只生成文本或结构化模板）

## 6. Data boundary

以下路径是运行时产物 / 版权语料，**不进入源码快照**（`pack_repo.py` 强制排除）：

- `data/raw/`、`data/paper_raw/`、`data/papers/`、`data/import_work/`
- `data/catalog/all.catalog.json`、`data/catalog/paper_index.json`、
  `data/catalog/paper_number_ledger.json`（源码快照只提交对应 `.template.json` 空模板）
- `write/jobs/`（写作运行时，只跟踪 `.gitkeep`）
- 任何 PDF / Markdown / images / TeX 编译产物

数据语义边界：

- `all.catalog` 是 **content-only** 内容索引，不是书目库。
- `metadata`（`<paper_id>.metadata.json`）是 BibTeX / DOI / authors / year / journal 的
  事实源；catalog 与 metadata 仅通过 `paper_number` / `paper_id` 关联。

## 7. Environment rule

真实入库 / 转换 / 写作命令必须使用 mineru conda 环境，不要用 PATH 上的
Windows Store python 别名（会静默退出）：

```bash
conda run -n mineru python scripts/<x>.py
# 或绝对路径
%USERPROFILE%\.conda\envs\mineru\python.exe scripts/<x>.py
```

Windows 下建议先设置编码，避免中文输出乱码：

```bash
set PYTHONIOENCODING=utf-8
```

## 8. MinerU batch runner

MinerU conversion requires GPU / MinerU 正式转换必须使用 GPU。formal ingest 使用
`scripts/convert_paper_raw_gpu.py`，默认 `MINERU_REQUIRE_GPU=true`、`CUDA_VISIBLE_DEVICES=0`，
并在转换前检查 `nvidia-smi` 与当前 Python 环境的 `torch.cuda.is_available()`。
`stage_raw_pdfs_to_paper_raw.py` 不需要 GPU；底层 `convert_paper_raw_batch.py` 仅作兼容/调试入口。
CPU/no-GPU 只允许调试：显式设置 `MINERU_ALLOW_CPU=true` 或 `MINERU_REQUIRE_GPU=false`。

Formal conversion command:

```bash
conda run -n mineru python scripts/convert_paper_raw_gpu.py --all --apply --report reports/convert_paper_raw.json
```

Windows cmd:

```bash
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

批量 MinerU 转换优先使用持久 `mineru-api` 服务，避免每篇 PDF 都冷启动模型。Windows 可用
`start_fast_api_mode.bat` 启动，或按本地 MinerU 安装启动 `mineru-api` 后设置以上变量。
`mineru-api` 必须在它自己的 shell 中以 `CUDA_VISIBLE_DEVICES=0` 启动；只在 client 进程设置
`CUDA_VISIBLE_DEVICES` 不能改变已经运行的服务。
`start_fast_api_mode.bat` 是 single-instance helper：已有健康 `mineru-api` 时复用；端口
8000 被占用但 `/health` 不通时拒绝再启动，避免重复加载模型或 GPU OOM。
`MINERU_RUNNER=cli` 只保留给单篇测试/调试；多篇 formal batch 会 hard fail，除非显式传
`--allow-cold-cli-batch` 做 debug/benchmark。

`paper_raw` conversion is idempotent：已有 `<source_id>.md` + `images/` 的目录默认跳过；
成功转换会写 `<source_id>.conversion.json` 和 `.import_status.json: converted`。`output/`
不是可信 converted 判据；出现 `stale_conversion` 或 `partial_conversion` 时不要反复运行
转换命令，检查目录后必要时显式加 `--force-reconvert`。

本地 FastAPI 默认只绑定 `127.0.0.1`。如设置 `MINERU_API_HOST=0.0.0.0` 或其它非 localhost
地址，必须设置 `MINERU_API_KEY`，或显式承担风险设置
`MINERU_ALLOW_UNAUTHENTICATED_PUBLIC_API=true`。
### Current MinerU service entry

Use `python scripts/start_mineru_services.py --wait` to start or reuse the
persistent local `mineru-api`, then run `python scripts/convert_paper_raw_gpu.py
--source-id 000001 --apply` or `python scripts/convert_paper_raw_gpu.py --all
--apply`. Stop the service with `python scripts/stop_mineru_services.py`.
`start_fast_api_mode.bat` is a compatibility wrapper around the Python starter.

MinerU PDF conversion has no process-level timeout; large PDFs may run for a long
time. Health checks, preflight checks, HTTP request timeouts, and `MinerULock`
wait timeouts remain allowed because they are not PDF conversion timeouts.

For manual metadata matching, title/author/affiliation/abstract/keyword/DOI
candidates come from the converted Markdown first 100 lines as front-matter
evidence before PDF title fallback. DOI gates stay strict.

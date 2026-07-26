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

## 3. Network metadata services

下列网络服务只用于产生 **candidate metadata** 或 **PDF URL**，绝不直接写
`data/papers/`。candidate 必须先有合法 DOI，再走 `paper_raw` → match → curation →
commit 才能进入正式库。

- Crossref（关键词 DOI / 书目元数据搜索 + DOI → 书目元数据）
- OpenAlex（书目元数据 / OA 位置）

> 网络关键词搜索 metadata 收敛为 **Crossref + OpenAlex** 两个 provider；Semantic Scholar
> 不再作为关键词 metadata 搜索 provider（仅保留为 DOI→PDF fetch resolver，见第 4 节）。

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
- **header-based DOI resolver**：`header_based`。不属于 `OA_ONLY` 默认链路。
  它在两种情况下启用：
  1. 显式调用 `fetch_pdf_for_paper_raw.py --resolver header-based`；
  2. `--resolver auto` 时作为最后的 DOI landing fallback（默认 `https://doi.org/{doi}`），
     无需 `--base-url` 或 `--url-template` 即可运行。
  User-Agent 固定在 Python 代码中；用户每次运行可传 Cookie/Authorization 等
  额外 header，但 header 明文不得写入 metadata、report 或日志。
- **Sci-Hub**：**已移除（removed）**。项目不提供、不注册 Sci-Hub resolver。PDF 获取按
  「原始链接 → OA → 出版商专用解析器（sciengine_direct 等）→ header_based DOI landing fallback
  （默认 `https://doi.org/{doi}`）→ 失败报告」
  优先级执行，不再有任何 unsafe fallback。
  `allow_scihub` 字段、`fetch_scihub.py` 模块、`SciHubResolver` 类均已删除。

Metadata/discovery API 代理配置走 `src/fetch/proxy.py::get_fetch_proxies()`，
读取 `FETCH_PROXY` 环境变量，返回 `requests` 可用的 proxies dict 或 `None`（直连）。
PDF/HTML content transport 不使用 `FETCH_PROXY`：它走 `src/fetch/pdf_transport.py`
direct first，再对可重试直连失败执行一次显式 `MINERU_PDF_PROXY_URL` fallback。

metadata-only PDF fetch priority:
1. original links already present in metadata (`metadata.links.pdf_url` / `url` / `publisher_url` / `repository_url`)
2. legal OA resolvers (unpaywall, openalex, semantic_scholar, arxiv, publisher_oa, springer_direct)
3. publisher-specific resolvers, e.g. `sciengine_direct` for `10.1360/` DOIs
4. preprint / PMC resolvers (biorxiv, pmc_oa)
5. header_based DOI landing fallback, default `https://doi.org/{doi}`
6. report failure

## Compliance And License Notes

Original repository code is covered by the repository license. Third-party
dependencies, external tools, APIs/services, models, PDFs, converted Markdown,
images, metadata, and writing outputs keep their own licenses or terms.
Do not describe the entire stack as MIT licensed.

Key notices:

- MinerU: MinerU Open Source License, based on Apache-2.0 with additional terms.
- PyMuPDF/MuPDF: AGPL-or-commercial.
- FastAPI: MIT.
- ref-downloader bridge: external integration with the upstream MIT project.

Read `THIRD_PARTY_NOTICES.md`. Use the read-only audits:

```bash
conda run -n mineru python scripts/audit_third_party_licenses.py --strict
conda run -n mineru python scripts/audit_source_provenance.py --strict
```

PDF content fetches use `src/fetch/pdf_transport.py`: direct first, then one
explicit `MINERU_PDF_PROXY_URL` fallback only for retryable direct failures.
Metadata/discovery API requests continue to use `FETCH_PROXY`.

## 5. Explicitly removed / not used

项目明确不引入、不 vendor：

- ChromaDB
- sentence-transformers
- 任何向量数据库（vector database）
- embedding / RAG 管线
- 内置 LLM client（所有 prompt / 写作步骤只生成文本或结构化模板）

## 6. Data boundary

In ingest v2.3, normal `data/paper_raw/<id>/` workspaces use the 16-digit
`paper_number` reserved by staging. Six-digit `source_id` directories are
legacy/migration only and must be migrated or repaired before normal conversion/formalize.

以下路径是运行时产物 / 版权语料，默认 audit 与 source profile 均不得进入源码快照：

- `data/raw/`、`data/paper_raw/`、`data/papers/`、`data/import_work/`
- `data/catalog/` 分类链接与 `.state/` 分类状态、
  `data/catalog/paper_number_ledger.json`（源码快照只提交对应 `.template.json` 空模板）
- `write/jobs/`（写作运行时，只跟踪 `.gitkeep`）
- PDF / Markdown / images / TeX 编译产物

> **注意**：默认 audit profile 可包含 dirty/untracked 的轻量源码和
> `tests/fixtures/synthetic_library/` 合成 fixture，但绝不扫描或抽样真实 runtime。
> `snapshot_manifest.json` 必须报告 `runtime_files_included=0`。

数据语义边界：

- 每篇正式论文的 Catalog 是 **content-only** 内容档案，不是书目库。
- `metadata`（`<paper_name>.metadata.json`）是 BibTeX / DOI / authors / year / journal 的
  事实源；catalog 与 metadata 仅通过 `paper_number` / `paper_name` 关联。

## 7. Environment rule

真实入库 / 转换 / 写作命令必须使用 mineru conda 环境。
Git Bash 已配置 `conda init bash`，终端直接 `conda activate mineru` 即可使用。
以下两种方式均可（推荐 `conda run` 用于脚本/agent 调用）：

```bash
conda run -n mineru python scripts/<x>.py
# 或绝对路径
%USERPROFILE%\.conda\envs\mineru\python.exe scripts/<x>.py
```

注意：本 agent 的 bash 工具使用非交互 shell，不加载 `.bashrc`，此时需用 `conda run` 或绝对路径。

Windows 下建议先设置编码，避免中文输出乱码：

```bat
:: Windows cmd.exe only
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
conda run -n mineru python scripts/start_mineru_services.py --wait --restart-if-stale --port 8000
conda run -n mineru python scripts/check_mineru_processes.py
conda run -n mineru python scripts/smoke_mineru_conversion.py --paper-number 0000000000000001 --apply --report reports/smoke_mineru_conversion.json
conda run -n mineru python scripts/convert_paper_raw_gpu.py --all --apply --report reports/convert_paper_raw.json
```

Windows cmd:

```bat
:: Windows cmd.exe only
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
`/health` 只代表 liveness，不代表 GPU conversion readiness。正式批量转换要求 managed service
identity、`check_mineru_processes.py` verdict 为 `READY_FOR_CONVERSION`，且最近 24 小时内有成功的
`smoke_mineru_conversion.py` 单篇报告。
`start_fast_api_mode.bat` 是 single-instance helper：仅复用 managed healthy `mineru-api`；健康但
unmanaged/stale 时用 `--restart-if-stale` 重启。端口 8000 被占用但 `/health` 不通时拒绝再启动，
避免重复加载模型或 GPU OOM。
`MINERU_RUNNER=cli` 只保留给单篇测试/调试；多篇 formal batch 会 hard fail，除非显式传
`--allow-cold-cli-batch` 做 debug/benchmark。

`paper_raw` conversion is idempotent：已有 `<paper_number>.md` + `images/` 的目录默认跳过；
成功转换会写 `<paper_number>.conversion.json` 和 `.import_status.json: converted`。`output/`
不是可信 converted 判据；出现 `stale_conversion` 或 `partial_conversion` 时不要反复运行
转换命令，检查目录后必要时显式加 `--force-reconvert`。

本地 FastAPI 默认只绑定 `127.0.0.1`。如设置 `MINERU_API_HOST=0.0.0.0` 或其它非 localhost
地址，必须设置 `MINERU_API_KEY`，或显式承担风险设置
`MINERU_ALLOW_UNAUTHENTICATED_PUBLIC_API=true`。
### Current MinerU service entry

Use `python scripts/start_mineru_services.py --wait --restart-if-stale` to start
or reuse the persistent local `mineru-api`. Before formal batch conversion, run
`python scripts/check_mineru_processes.py` and one
`python scripts/smoke_mineru_conversion.py --paper-number 0000000000000001 --apply`.
Then run `python scripts/convert_paper_raw_gpu.py --all --apply`. Stop the
service with `python scripts/stop_mineru_services.py`.
`smoke_mineru_conversion.py` without `--apply` is readiness-only and cannot
unlock batch conversion.
`start_fast_api_mode.bat` is a compatibility wrapper around the Python starter.

如果 conda 不在 PATH，用 env python 绝对路径：

```bash
C:\Users\Admin\.conda\envs\mineru\python.exe scripts\start_mineru_services.py --wait --restart-if-stale
C:\Users\Admin\.conda\envs\mineru\python.exe scripts\check_mineru_processes.py
C:\Users\Admin\.conda\envs\mineru\python.exe scripts\smoke_mineru_conversion.py --paper-number 0000000000000001 --apply --report reports\smoke_mineru_conversion.json
C:\Users\Admin\.conda\envs\mineru\python.exe scripts\convert_paper_raw_gpu.py --all --apply
C:\Users\Admin\.conda\envs\mineru\python.exe scripts\stop_mineru_services.py
```

start_mineru_services.py must resolve Scripts/mineru-api.exe from the current Python env (find_mineru_api_exe). Do not manually background mineru-api.exe as a long-term SOP.

MinerU PDF conversion has no process-level timeout; large PDFs may run for a long
time. Health checks, preflight checks, HTTP request timeouts, and `MinerULock`
wait timeouts remain allowed because they are not PDF conversion timeouts.

For manual metadata matching, title/author/affiliation/abstract/keyword/DOI
candidates come from the converted Markdown first 100 lines as front-matter
evidence before PDF title fallback. DOI gates stay strict.

## 9. OpenAlex credentials

OpenAlex API calls use two optional environment variables for polite pool / API key access:

| Variable | Purpose | Required |
| --- | --- | --- |
| `OPENALEX_EMAIL` | `mailto=` param for polite pool (rate-limit boost) | No (anonymous fallback) |
| `OPENALEX_API_KEY` | `Authorization: Bearer` for higher rate limits | No (anonymous fallback) |

**Contract:**
- Only read from process environment variables. No `.env` file, no config file, no file-based fallback.
- Load once per request via `src.services.openalex_credentials.load_openalex_credentials()`.
- Missing or empty variables → anonymous access (not an error).
- Consumers (`src.discovery.search_openalex`, `src.fetch.fetch_openalex`) import from the centralized module, never read `os.environ` directly.
- Credential values (email, API key) must never appear in logs, error messages, reports, or snapshot output. Use `safe_summary()` or `safe_request_error_summary()` for diagnostics.
- The pack_repo secret scanner (`scripts/pack_repo.py`) tracks hardcoded credential assignments (`OPENALEX_EMAIL=…`, `OPENALEX_API_KEY=…`) and blocks pack if found outside `tests/`.

**Usage (environment setup):**

```bash
# PowerShell
$env:OPENALEX_EMAIL="your@email.com"
$env:OPENALEX_API_KEY="your_key_if_needed"

# Git Bash / Linux
export OPENALEX_EMAIL="your@email.com"
export OPENALEX_API_KEY="your_key_if_needed"
```

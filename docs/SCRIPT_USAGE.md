# Script Usage Index

所有 `scripts/*.py` 的用途、风险分类和推荐命令索引。

## Normal ingest SOP（正常入库主流程）

| 脚本 | 状态 | 用途 | 修改数据 | 推荐命令 |
|------|------|------|----------|----------|
| `stage_raw_pdfs_to_paper_raw.py` | active | 手动 PDF → paper_raw staging | 是（移动 PDF、分配编号） | `--move --apply --report ...` |
| `stage_network_metadata_to_paper_raw.py` | active | 网络 metadata → paper_raw staging | 是（写 metadata、分配编号） | `--apply` |
| `convert_paper_raw_gpu.py` | active | GPU MinerU 批量转换 paper_raw | 是（写 md/images） | `--all --apply --report ...` |
| `resolve_paper_raw_metadata.py` | active | 解析/匹配 paper_raw metadata | 是（写 metadata） | `--all-unmatched --apply --allow-network --write-candidates` |
| `curate_paper_raw.py` | active | 校验 metadata/catalog，写 `catalog_ready` | 是（写 status） | `--all-ready --apply` |
| `formalize_paper_raw.py` | active | 正式化 paper_raw → ready_for_commit | 是（改名、回填 catalog） | `--all-ready --apply --report ...` |
| `commit_paper_raw_to_papers.py` | active | 事务性安装 paper_raw → data/papers | 是（移动文件） | `--all-ready --apply` |
| `rebuild_all_catalog.py` | active | 重建 all.catalog.json | 是 | `--apply` |
| `validate_v2_library.py` | active | 验证正式库完整性 | 否 | 直接运行 |
| `pack_repo.py` | active | 打包审计快照（audit snapshot） | 是（写 zip） | `python scripts/pack_repo.py`（默认 audit）或 `--profile source`（纯源码） |
| `agent_acceptance.py` | active | **agent 统一收尾命令**（fast 默认 / `--full` 全量） | 是（写 zip） | `python scripts/agent_acceptance.py` / `--full` / `--profile source` |

## Metadata / PDF fetch（元数据与 PDF 获取）

| 脚本 | 状态 | 用途 | 修改数据 | 推荐命令 |
|------|------|------|----------|----------|
| `fetch_pdf_for_paper_raw.py` | active | DOI → PDF 获取并 attach | 是（写 PDF） | `--paper-number <id> --resolver auto --apply` |
| `match_paper_raw_metadata.py` | active | 匹配 paper_raw metadata | 是 | `--apply` |
| `attach_pdf_to_paper_raw.py` | active | 手动 attach PDF 到 paper_raw | 是 | `--apply` |

## MinerU conversion（MinerU 转换）

| 脚本 | 状态 | 用途 | 修改数据 | 推荐命令 |
|------|------|------|----------|----------|
| `start_mineru_services.py` | active | 启动 mineru-api 服务 | 否 | `--wait --restart-if-stale` |
| `stop_mineru_services.py` | active | 停止 mineru-api 服务 | 否 | 直接运行 |
| `check_mineru_processes.py` | diagnostic | 检查 mineru 进程健康状态 | 否 | 直接运行 |
| `smoke_mineru_conversion.py` | diagnostic | 单篇 live smoke test 验证 GPU 转换就绪 | 默认仅就绪诊断（无副作用）；`--apply` 才执行真实转换（写转换产物） | `--paper-number <id> --apply --report ...` |
| `convert_paper_raw_batch.py` | active | 底层批量转换（被 convert_paper_raw_gpu 调用） | 是 | 通常不直接调用 |
| `benchmark_mineru.py` | diagnostic | MinerU 性能基准测试 | 是（写临时文件） | 按需运行 |
| `restore_paper_raw_from_mineru_output_cache.py` | repair | 从 output cache 恢复 paper_raw | 是 | 按需运行 |
| `run_paper_raw_gpu_conversion_then_resolve.py` | compatibility wrapper | GPU 转换 + resolve 组合脚本 | 是 | 不推荐，用 SOP 分步命令 |

## Writing workflow（写作流程）

| 脚本 | 状态 | 用途 | 修改数据 | 推荐命令 |
|------|------|------|----------|----------|
| `create_write_job.py` | active | 创建写作 job | 是 | 按需运行 |
| `prepare_write_article_workdir.py` | active | 准备写作文章工作目录 | 是 | 按需运行 |
| `write_catalog_tex_article.py` | active | 从 catalog 生成 TeX 文章 | 是 | 按需运行 |
| `write_review.py` | active | 生成审稿意见 | 是 | 按需运行 |
| `check_write_tex_project.py` | diagnostic | 检查 TeX 项目完整性 | 否 | 直接运行 |
| `check_write_quality_text.py` | diagnostic | 检查写作质量 | 否 | 直接运行 |
| `export_job_bib.py` | active | 导出 job BibTeX | 是 | 按需运行 |
| `validate_write_job.py` | diagnostic | 验证写作 job | 否 | 直接运行 |
| `doctor_write_pipeline.py` | diagnostic | 诊断写作流水线 | 否 | 直接运行 |

## Validation / doctor / audit（验证与审计）

| 脚本 | 状态 | 用途 | 修改数据 | 推荐命令 |
|------|------|------|----------|----------|
| `audit_ingest_duplicates.py` | audit | 审计重复 PDF/DOI | 否 | `--strict` |
| `audit_metadata_quality.py` | audit | 审计 metadata 质量 | 否 | 直接运行 |
| `audit_paper_number_ledger.py` | admin-only | 审计编号账本、检测 empty orphan / metadata-only workspace；`--fix-empty-orphans --apply --reason ...` 仅清理严格空目录 | 是（仅显式 fix/reset/compact） | 默认只审计；正常不清理 |
| `audit_paper_raw_duplicate_workspaces.py` | audit | 审计并清理重复 paper_raw 工作区 | 是（移入 quarantine） | `--apply-cleanup` |
| `audit_raw_vs_paper_raw.py` | audit | 审计 raw vs paper_raw 一致性 | 否 | 直接运行 |
| `preflight_paper_raw_import.py` | audit | 入库前预检 | 否 | 直接运行 |
| `validate_rolled_back_paper_raw.py` | audit | 验证回退后的 paper_raw | 否 | 直接运行 |
| `validate_metadata_only_assets.py` | compatibility wrapper | 验证 metadata-only 资产 | 否 | 兼容入口 |
| `doctor_ingest_pipeline.py` | diagnostic | 诊断 ingest 流水线 | 否 | 直接运行 |
| `check_directory_hygiene.py` | diagnostic | 检查目录卫生 | 否 | 直接运行 |
| `discover_papers.py` | diagnostic | 发现/扫描 papers | 否 | 直接运行 |

> **强契约（validation / rebuild / doctor）**
> - `rebuild_all_catalog.py --apply` 对正式库是 **strict** 的：`data/papers/<paper_id>/`
>   只要出现任意正式资产（metadata/catalog/md/pdf/asset_manifest/images/`*.paper.number`）
>   就必须**完整**；缺 PDF、images、asset_manifest 或 `paper.number` marker 都会导致
>   `--apply` 失败、返回非 0、且 **不写/不覆盖** `all.catalog.json` / `paper_index.json`。
>   脚本不得静默跳过不完整论文，也不得写空索引覆盖已有索引。
> - `validate_v2_library.py`：正式库每篇论文 **只能有一个** `<paper_id>.asset_manifest.json`；
>   `paper_raw` 阶段的 `<paper_number>.asset_manifest.json` 不得出现在 `data/papers`。
>   validator 会 glob 全部 `*.asset_manifest.json` 并拒绝额外 manifest（"unexpected asset manifest"）。
> - `doctor_ingest_pipeline.py`：其 pytest step 使用隔离环境
>   `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` 与 **300s timeout**，避免本地第三方 pytest 插件
>   导致诊断流程挂起；超时返回 blocking failure（`returncode=124`，`error="timed out after 300s"`）。

## Repair / admin-only（修复与管理）

| 脚本 | 状态 | 用途 | 修改数据 | 推荐命令 |
|------|------|------|----------|----------|
| `repair_catalog_asset_refs.py` | repair | 修复 catalog asset refs | 是 | `--dry-run` 先，再 `--apply` |
| `repair_corrupted_markers.py` | repair | 修复损坏的 marker 文件 | 是 | 按需运行 |
| `repair_ledger_folder_names.py` | repair | 修复 ledger 文件夹名 | 是 | 按需运行 |
| `repair_metadata_only_assets.py` | repair | 修复 metadata-only 资产 | 是 | 按需运行 |
| `repair_paper_raw_derived_files.py` | repair | 修复 paper_raw 派生文件 | 是 | 按需运行 |
| `repair_stale_formal_asset_manifests.py` | repair | 删除正式库残留 `<paper_number>.asset_manifest.json` | 是（删 stale） | `--dry-run` 先，再 `--apply` |
| `reset_paper_number_ledger.py` | admin-only | 重置编号账本（危险） | 是（清零） | 仅 admin，正常绝不运行 |
| `reconcile_paper_raw_non_destructive.py` | repair | 非破坏性协调 paper_raw | 是 | 按需运行 |
| `quarantine_unreferenced_workspaces.py` | admin-only | 隔离未引用工作区 | 是（移入 quarantine） | 按需运行 |

## Legacy / rejected compatibility wrappers（遗留/拒绝兼容入口）

| 脚本 | 状态 | 用途 | 正常 SOP 替代 |
|------|------|------|---------------|
| `rollback_papers_to_raw.py` | rejected legacy entrypoint | 旧版回退入口 | `rollback_formal_papers_to_paper_raw.py` |
| `prep_rolled_back_for_formalize.py` | rejected legacy entrypoint | 旧版回退后准备 | 正常 SOP: `curate → formalize → commit` |
| `validate_metadata_only_assets.py` | compatibility wrapper | metadata-only 资产验证 | `validate_v2_library.py` |
| `run_paper_raw_gpu_conversion_then_resolve.py` | compatibility wrapper | 转换+resolve 组合 | SOP 分步命令 |

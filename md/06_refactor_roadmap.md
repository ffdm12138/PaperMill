# 重构路线图

## 使用约定

- 状态：`[x]` 已完成（附验收证据）；`[ ]` 未完成；`[~]` 进行中。
- 每个条目必须写"验收标准"（哪个测试/门证明它），完成时更新状态并保留证据说明。
- 新的结构性工作先在此登记，再动手。

## 2026-07 全量整改（本轮）

### 阶段 A：架构

- [x] A1 微修复：TODO_MARKERS 单源到 `src/writer/safe_write.py`；
  `workspace/receipt` 改从 `utils.timestamps` 取 `now_iso`；
  `local_evidence`/`fsync_dir` 公名化；MinerU 进程管理 3 处静默宽 except 窄化；
  删除过期 skip 测试。验收：writer/resolver/mineru 定向测试 + fast gate。
- [x] A2 utils 叶子归位：naming/path_utils/file_fingerprint/logging_setup →
  `src/utils/`；新增 `src/utils/process.py::is_pid_alive`。
  验收：全库旧路径 grep 清零 + fast gate。
- [x] A3 `src/mineru/` 组包：converter/cleaner/runtime/lock/service_manager/smoke；
  137 处 dotted 引用与 monkeypatch 字符串全量替换。验收：mineru 家族 148 测试 + fast gate。
- [x] A4 `src/staging/` 组包：network_metadata_{staging,canonical} 迁出 services。
  验收：staging 定向测试 + fast gate。
- [x] A5 解散 `src/services/`：paper_number_admin→ingest、paper_library→
  catalog_folders、repository_hygiene→utils、bib→writer；联动 CLAUDE.md/
  AGENTS.md/PROJECT_CONTRACT/TESTING/SECURITY 路径。验收：cmp 字节一致 + fast gate。
- [x] A6 分层护栏终版：新 LAYER_ORDER（root 仅剩 server/prompt_builder 且
  禁被 import）、ALLOWED 严格向下不变量、函数体 lazy import 纳管、
  SANCTIONED/SANCTIONED_LATE 各带理由。验收：`tests/hygiene/test_layering.py`
  两用例 + fast gate。
- [x] A7 canonical-hash A 族单源：`src/utils/canonical_json.py`；8 处接线；
  C 族站点加"禁统一"注释。验收：discovery/catalog_folders 499 回归 + fast gate。
- [x] A8 时间戳收口：6 处字节形状相同的内联 UTC/本地实现改走
  `utils.timestamps`。验收：定向测试 + fast gate。
- [x] A9 巨型函数拆解：service_manager.start_services（5 helper）、
  commit.resume_commit（逐 phase 六 helper，_fault 注入点逐字保留）、
  relevance_profiles.build_relevance_profile_plan（按 Step 六 helper）、
  pending_queue 双 drain（epoch 六段 + 非 staging 路径）、coordinator 批执行
  （7 阶段 helper + 闭包升格）。验收：各自定向套件（discovery 505 测试全绿）
  + `verify_discovery_final_architecture.py` + fast gate。
- [x] A10 脚本引擎下沉：reset-state 审计引擎 → `src/discovery/audits/reset_state.py`
  （脚本 1244→79 行）；conversion 谓词 → `src/ingest/conversion_gates.py`；
  fetch 候选分类 → `src/fetch/access_policy.py`；rollback 目标发现 →
  `src/ingest/rollback.py::discover_all_papers_rollback_targets`。CLI 面字节
  兼容。验收：subprocess/契约测试 + fast gate。
- [x] A11 化妆：`src/catalog/freeze.py` 恢复常规排版（纯空白改动）；
  三个无空格 sys.path 头归一两行拼法（其余脚本已是多数派或有意 guarded 形态）。
  验收：契约测试钉值 + fast gate。

### 阶段 B：文档治理

- [x] md/ 八篇中文规范 + 六个 hygiene 测试把 md/ 纳入扫描 +
  pack 规则正向断言。验收：docs 相关 hygiene 套件 + 快照含 md/。
- [x] CLAUDE.md：md/ 指针 + PowerShell 7 说明；AGENTS.md 字节同步。
  验收：test_docs_alignment。
- [x] docs 漂移修复：SCRIPT_USAGE 幽灵行、DEPENDENCIES:274、SECURITY:52/70、
  ARCHITECTURE 分层描述、README 链接、PROJECT_STATUS 日期条目。

### 阶段 C：写作 Skills

- [x] `catalog_review_writer` + `catalog_research_proposal_writer` 三件套 +
  schema + examples + 伴生 hygiene 测试。
- [x] `scripts/export_write_job_bib.py`、`scripts/check_write_planning_docs.py`、
  `create_write_job.py --workflow`；集成测试；SCRIPT_USAGE 行；
  write/README 工作流说明。

### 终验

- [x] fast gate + `--full` + pack + 解包抽查（md/、skills/ 新目录在快照内、
  `runtime_files_included=0`、旧模块路径无残留）。

## 后续提案（本轮不做，动手前先补条目细化）

- [x] writer 族 naive 时间戳 tz-aware 化：`src/writer/job_manager.py`、
  `catalog_matcher.py`、`ingest/mineru_output_cache.py` 与 6 个写作/doctor 脚本
  统一走 `utils.timestamps.now_iso()`。核查过无解析方消费这些字段、无测试钉死
  旧形状。两处保留 naive 并写明理由：`src/mineru/lock.py`（锁龄用 naive
  `datetime.now()` 相减，改 aware 会抛 TypeError）、
  `scripts/test_runtime_workspace.py`（独立测试基建，不得依赖 `src`）。
  验收：hygiene + writer/ingest 定向套件 + fast gate（2026-07-27 复审波）。
- [ ] `discovery/contracts/notebook.py` 关键词契约下沉，消解
  catalog_folders→discovery 豁免族（需评估 relevance/backfill 联动，代价大）。
- [ ] relevance 五档 profile 的来源问题（PROJECT_STATUS 记录的"deliberately
  unresolved"），需要 frozen-taxonomy 方案。
- [ ] `library ~ workspace` 2-cycle 的彻底消解（lifecycle 归属再讨论）。
- [~] 无用顶层 import 清理：粗扫描（AST，剔除 `annotations` 误报后）在
  src/scripts 报告约 300 处候选，但其中混有跨模块 re-export（删除会断下游），
  需要 pyflakes/ruff 级别的工具逐项确认；顺带评估是否把 lint 纳入验收门。
  2026-07-27 决定不在整改尾声批删，避免破坏 re-export 面。
  2026-07-27 复审波已单独清掉一个确认安全的子集：`pending_queue.py` 拆分后
  遗留的 14 个死导入（逐名 grep 确认无 `from ... import` 消费者、无 monkeypatch
  目标），以及 `rollback.py` 重复的 `LEDGER_RANK`、`metadata_resolve` 的
  `_read_json` 别名双份、audit 脚本无人调用的 `_now()`。余量仍待工具化确认。

- [ ] `src/fetch/access_policy.py` 职责拆分：该模块现同时承载 resolver 策略与
  paper_raw 候选分类（`classify_pdf_fetch_candidate` 会读文件系统）。复审波只
  更正了 docstring 与去掉重复的 `_read_json`，拆到 `src/fetch/candidates.py`
  留待独立提案——刚完成大搬迁，无功能收益的再搬动不划算。

- [ ] 收紧 13 条已声明但无实际导入的 `ALLOWED` 边（`catalog_folders→catalog`、
  `metadata_resolve→{fetch,library,workspace}`、`writer→{catalog,library}`
  及 root 的 7 条）。属"授权宽于实需"，非违规。

## PDF 获取链路修复（2026-07-27）

起因：`data/paper_raw` 2332 个工作区全部有合法 DOI，但只有 150 个拿到 PDF。
统计 514 份 `fetch_result.json` + 2891 条 `transport_attempts` 并做只读探针后，
确认是六个叠加问题，最大的两个都不是"抓取逻辑写错了"。

- [x] **联系邮箱单一权威层**：新增 `src/utils/contact_email.py`（放 utils 是因为
  `utils/rate_limit.py` 的 mailto override 也要用，放 fetch 会破坏分层）。
  占位邮箱判为"未配置"——实测 `anonymous@example.com` 让 Unpaywall 恒返回
  HTTP 422（364/364 全灭），换真实邮箱即 200。顺带修好 `provider_headers`
  重复拼接 `(mailto:…)` 的潜伏 bug（override 生效时才显形）。
  验收：`tests/unit/test_contact_email.py`、`test_metadata_rate_limit.py`。
- [x] **OA 候选排序 + 多候选下载**：新增 `src/fetch/oa_locations.py`，
  `FetchResult.pdf_candidates` 承载全部位置，`fetch_pipeline` 逐个尝试。
  根治"N 收敛成 1"缺陷——三个 OA resolver 此前都只取 provider 的 best，
  而 best 恒为出版商副本，恰好是被拦的那一个。实测封锁存量抽样 45 篇中
  53% 有仓储副本，且仓储主机从当前出口可直接下到真 `%PDF`。
  验收：`tests/unit/test_oa_locations.py`、`test_fetch_pipeline.py` 多候选用例。
- [x] **主机可达性策略**：新增 `src/fetch/host_policy.py`，集合来自实测
  （大量尝试、零成功），用于排序降权、跳过必然徒劳的代理重试、报告
  `blocked_publisher` 判定。声明为有时效的启发式，非硬禁止。
  验收：`tests/unit/test_host_policy.py`。
- [x] **代理重试收敛**：原计划做出口 IP 自检，实测否决——探针端点本身在直连
  路径被拦，且数据显示代理把 21 次瞬时失败转成了成功，一刀切关闭会丢真收益。
  改为只在"被拦主机 + 403"时跳过代理重试（实测 551→548 全部复现 403，零成功），
  连接级失败照常回退。验收：`tests/unit/test_pdf_transport.py`。
- [x] **resolver 本地短路**：`PdfResolver.applies_to()` 默认 True，
  arxiv/biorxiv/pmc_oa/springer/wiley/elsevier 按 DOI 前缀与已有标识本地判定。
  跳过者不进 `resolver_chain` 也不产生 attempts，只记入 `resolvers_skipped`，
  让日志显示真失败而非恒定空转（此前每篇浪费 3 次网络往返）。
- [x] **批量跑存量能力**：`FetchSelection` + `select_fetch_candidates` 落在
  `access_policy.py`（与候选分类同层），脚本只做 wiring。新增
  `--skip-attempted` / `--retry-after-days` / `--doi-prefix` / `--limit` /
  `--report-blocked`，报告逐条 flush。实测 `--skip-attempted` 选出 1818 个
  从未尝试过的工作区，`--doi-prefix 10.5194 --skip-attempted` 选出 567 个。
- [x] **元数据源头**：`search_openalex.parse_openalex_work` 的 `url=` 从
  `work["id"]`（OpenAlex 实体 URI）改为 `primary_location.landing_page_url`，
  回退 `doi.org`。此前 2256/2332 的 `links.url` 是 `https://openalex.org/W…`，
  害得 `original_link` 每篇去爬 SPA 空壳。只影响新入库；存量由
  `original_link` 侧的记录型主机过滤兜底，不改已冻结的 metadata。
- [x] **死代码**：`original_link_resolver` 中 `with` 块之后的不可达分支、
  `fetch_pipeline` 的 `except TypeError` 兼容垫片（它托着的其实是过时的测试
  替身，已改测试而非留垫片）、`landing_page` 恒为 0 的 `depth` 及其死分支、
  与 `url_safety` 重复的 `_looks_like_pdf`。另修 `landing_page` 的 `visited`
  未跨递归层共享（同一 URL 会被不同分支重复抓），并给候选广度加上限。
- [ ] 约 239 篇"OA 但只有出版商副本"与约 160 篇闭源，需换出口或机构访问；
  `--report-blocked` 已能导出清单，走人工/校园网通道，代码侧无解。

## PDF 身份验证器重构（2026-07-31，最终合同版）

起因：1067 篇 PDF 实测暴露旧提取/匹配三层错误——字节扫描 miss FlateDecode
压缩流（正确 PDF 判 mismatch）；"字节扫描有结果就不走文本层"补丁抓到未压缩
引用流里的参考文献 DOI（247 篇 identifier_conflict 中 ~110 篇是引用误报、
~98 篇是预印本↔正式版变体、39 篇尾随标点）；DOI 平面集合 + 宽正则吞残片。
执行历史还证明 rematch --apply 与 freeze 耦合、中途崩溃留部分落盘、已冻结
被跳过导致新旧提取器结果混杂。

验收标准（18 条不变量，见 `C:\Users\Admin\.claude\plans\foamy-cooking-manatee.md`）：
弱/中证据永不触发 conflict；强证据精确 DOI 必 matched；related_version =
家族候选 + 书目单一口径（标题≥0.85 强一致 + 作者重叠 + 年份 ±2 或缺失记
insufficient）；identifier_conflict 为高精度低召回结论（冲突阈值 0.55 与
匹配阈值 0.85 分离，仅唯一可信结构化主 DOI + 完整负面书目证据）；trusted
（strong∪medium）≥2 → ambiguous 先于版本关系；stable identifier 先分流且
ISBN 不能单独证明章节；manual 确认 = automatic/final 双层（覆盖集
{ambiguous, unverifiable, related_version}，identifier_conflict 不可覆盖）；
fetch 与 freeze 解耦（fetch 写 receipt 不冻结，日常 freeze 走
`freeze_paper_raw_metadata.py --all-eligible`）；迁移 = plan-hash →
receipts-only → freeze-eligible 三段式，journaled multi-file transaction、
细粒度子状态、阶段 fail-closed、abort manifest（含 originally-missing 处理）、
workspace_inventory_hash 防目录漂移、同 plan 重跑幂等（不增 revision）、
维护模式代码强制 fail-closed（入口 + 锁内复检）；迁移工具宽容读取旧
mismatch，完成后删除该读取路径（核销点）；全部相关测试 + fast gate +
--full + packer 通过后才允许真实 apply。

- [x] I1 `src/utils/identifiers.py` 候选清洗层（normalize_doi 语义不变；
  clean_extracted_doi_candidate / join_line_broken_doi_lines /
  extract_doi_candidates / is_valid_doi）。验收：`tests/unit/test_doi_normalization.py`。
- [x] I2 `src/metadata/pdf_identity.py` v2 提取器（DoiEvidence +
  extracted_identifiers + parser_failures 全路径后判 extraction_failed；
  XMP 明确键 strong、文本层 labeled 判定、字节扫描仅诊断；确定性约束）。
  验收：`tests/unit/test_pdf_identity_v2.py` + `test_pdf_identity_text_layer.py` 重写。
- [x] I3 `src/metadata/identity_match.py` 决策器（四态书目分离阈值、
  trusted 计数、唯一结构化主字段优势、stable 分支、家族表 + 前缀迁移表）。
  验收：`tests/unit/test_identity_match.py`。
- [x] I4 `src/metadata/pdf_match.py` receipt v2.0（automatic/final 双层 +
  manual_confirmation；验证器重放已存证据不重提取）+ `src/ingest/status.py`
  ALLOWED 扩展 + `import_status.py` 映射重映射与 duplicate 修复 +
  `locking.py` 迁移守卫。验收：契约测试更新 + `test_frozen_v32_transaction_pipeline.py`。
- [x] I5 调用点解耦（fetch/convert/resolve 不冻结、锁内复检守卫）+
  `freeze_paper_raw_metadata.py --all-eligible`（先 freeze 后 status，
  崩溃只补状态）+ `confirm_paper_raw_pdf_identity.py`（--confirmed-by /
  --expected-receipt-sha256 写入协议）+ formalize/commit 入口守卫。
  验收：集成测试 + 守卫 fail-closed 测试。
- [x] I6 `rematch_paper_raw_pdf_identity.py` 事务式迁移工具（plan 含基线 +
  inventory hash + plan_content_hash；journal 子状态机 + 阶段 fail-closed +
  abort manifest + 幂等 freeze + 宽容读取器）。验收：
  `tests/integration/test_pdf_identity_migration_transaction.py`。
- [x] I7 真实迁移两轮（2026-07-31）。第一轮：574 冻结重建、574/574 闭包通过。
  第二轮（最终策略复审）：只读审计发现 585 matched 中 405 篇仅靠年份/作者
  单一佐证（title_sim 低至 0.10-0.28），按最终合同收紧强证据规则
  （首页 labeled DOI → strong 仅限：标题强一致，或 可靠作者重叠 + 年份兼容；
  仅年份/仅常见姓氏不升 strong；冲突阈值 0.60）。事务重迁移：matched 585→180、
  ambiguous 224→629、unverifiable 258 不变、conflict 0 不变；179 冻结重建且
  179/179 闭包通过、零悬挂、零 v1、零 mismatch。405 篇降级论文保留
  labeled DOI + 年份证据进 receipt，为人工复核清单。
- [~] I8 文档契约同步（PROJECT_CONTRACT/PROJECT_STATUS/SCRIPT_USAGE/
  ARCHITECTURE/md_01_architecture 已同步；CLAUDE↔AGENTS 核对无需改动）。
  宽容读取器核销：迁移完成后 `_read_status_tolerant`/`_tolerant_update_status`
  仍在迁移工具内（一次性工具保留，未进入 runtime）；runtime 写入已 v2-only。
  验收：fast gate + --full + packer 解包抽查。

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

- [ ] writer 族 naive 时间戳 tz-aware 化：**行为变更**（持久化字符串形状改变），
  需单独提案，涉及 `src/writer/job_manager.py` 等与 6 个写作脚本；
  完成后把 `docs/PROJECT_STATUS.md` 的时间戳表述改回无条件形式。
- [ ] `discovery/contracts/notebook.py` 关键词契约下沉，消解
  catalog_folders→discovery 豁免族（需评估 relevance/backfill 联动，代价大）。
- [ ] relevance 五档 profile 的来源问题（PROJECT_STATUS 记录的"deliberately
  unresolved"），需要 frozen-taxonomy 方案。
- [ ] `library ~ workspace` 2-cycle 的彻底消解（lifecycle 归属再讨论）。

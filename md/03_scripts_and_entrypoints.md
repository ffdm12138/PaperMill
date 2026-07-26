# 脚本与入口规范

## scripts/ 职责边界

- `scripts/*.py` 只做三件事：argparse、组装（wiring）、结果打印/退出码。
  业务逻辑、领域谓词、审计引擎一律放 `src/`（呼应 PROJECT_CONTRACT 的
  no-facade 原则）。
- 判断标准：一个脚本若含有能被单测的领域函数（状态判定、路径推导、
  数据校验、报告聚合），那部分就该下沉到 src/ 对应层。
- 有意 standalone 的基础设施脚本是例外，且**不得**新增 src import：
  `agent_acceptance.py`、`pack_repo.py`（仅保留 repository_hygiene 一条）、
  `test_runtime_workspace.py`、`cleanup_test_caches.py`、
  `verify_discovery_final_architecture.py`（AST 审计器，与被审计实现有意隔离）。
- 审计类脚本引擎下沉后的家：`src/discovery/audits/`（目录名含 audit，
  被 discovery 架构守卫的 allowed_patterns 豁免）。

## 入口初始化（_bootstrap）

- 操作性脚本一律先 `sys.path.insert(0, str(Path(__file__).parent.parent))`
  （统一两行拼法），再 `from scripts import _bootstrap`：它负责 settings 校验、
  运行目录创建、日志配置。
- 不使用 editable install 方案：验收会在解包后的快照内运行脚本，
  脚本必须自举。
- `config/settings.py` 的 import 必须保持无副作用。

## dry-run 与 --apply 惯例

- 一切有写入的操作脚本缺省只读（plan/report），显式 `--apply` 才落盘。
- 真实网络、真实 MinerU、迁移 `--apply` 永不进入测试与验收路径。
- 破坏性admin 操作（ledger reset/compact 等）另有确认参数，缺省拒绝。

## 报告输出规范

- 运行报告写 `reports/` 下 JSON（gitignore 已忽略），文件名带批次戳；
  写入用原子写；内容剥离 URL 查询串、不含凭证、不含绝对用户路径
  （用 `normalize_repo_path`）。
- 脚本的人类输出行保持稳定：验收与 subprocess 测试可能钉住字面输出
  （如 `[OK] agent acceptance passed`）。改输出格式前先搜测试。

## 脚本清单义务

- 每个新增 `scripts/*.py` 必须在 `docs/SCRIPT_USAGE.md` 加一行用途说明——
  `tests/hygiene/test_docs_boundary_terms.py` 会校验清单覆盖所有根脚本。
- 退役脚本的流程：删文件 + 在契约测试里加 tombstone（断言文件不存在）+
  同步删 SCRIPT_USAGE 行。反例教训：`reconcile_discovery_v4_migration.py`
  删了脚本但清单行残留了一段时间。

## CLI 面兼容

- 被 subprocess 驱动的测试钉住的脚本（rollback、reset-state 审计等），
  参数名、缺省值、报告字段、退出码都属于兼容面；重构（如引擎下沉）时
  必须保持字节兼容。

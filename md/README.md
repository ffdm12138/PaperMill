# MinerU 改动治理文档（md/）

本目录是面向**后续改动**的治理规范，中文为主（代码标识符、路径、命令保持英文原样）。
它回答的问题是"改动应该怎么做"，而不是"系统现在长什么样"。

## 定位与边界

| 文档域 | 角色 | 语言 | 冲突时优先级 |
| --- | --- | --- | --- |
| `CLAUDE.md` = `AGENTS.md`（根目录，字节一致） | Agent 契约：硬边界、验收门、安全红线 | 英文 | **最高** |
| `md/`（本目录） | 改动治理规范：分层、编码、脚本、测试、skill、流程 | 中文 | 次之 |
| `docs/` | 现状参考：架构、契约细则、脚本用法、测试说明 | 英文 | 参考 |

裁决顺序：`CLAUDE.md` 与本目录冲突时以 `CLAUDE.md` 为准，并把冲突当作必须修复的
文档缺陷记入 [06_refactor_roadmap.md](06_refactor_roadmap.md)。

## 阅读顺序

| 文档 | 一句话 |
| --- | --- |
| [01_architecture.md](01_architecture.md) | 分层规则、依赖方向、如何新增模块与豁免缝 |
| [02_coding_standards.md](02_coding_standards.md) | 单源工具义务、时间戳、锁序、错误处理、网络访问 |
| [03_scripts_and_entrypoints.md](03_scripts_and_entrypoints.md) | scripts/ 只做 wiring、_bootstrap、--apply 惯例、清单义务 |
| [04_testing_and_acceptance.md](04_testing_and_acceptance.md) | 测试分层、隔离工作区、验收门、Windows 陷阱 |
| [05_skills.md](05_skills.md) | Skill 三件套、伴生测试、三段式 LLM 文件交接、引用红线 |
| [06_refactor_roadmap.md](06_refactor_roadmap.md) | 重构路线图与勾选状态、每项的验收标准 |
| [07_change_workflow.md](07_change_workflow.md) | 一次改动的标准流程与文档同步矩阵 |

## 本目录自身的写作红线

md/ 下的文档被 hygiene 测试扫描（与 docs/、skills/ 同等待遇），写作时必须遵守：

- 文件一律 UTF-8 无 BOM、LF 行尾；文件名一律 ASCII（内容可以是中文）。
- 写作产物路径一律写 `write/jobs/<job_id>/...` 完整形态；禁止出现 legacy token
  （旧 LLM 工作目录路径、旧全局参考文献库写法、metadata 挂在 catalog 下的点号
  形态等——完整禁词表以 `tests/hygiene/test_no_legacy_writing_workflow.py` 与
  `tests/hygiene/test_no_legacy_mainflow_refs.py` 的常量为准，此处故意不引用
  字面量以免自触扫描）。
- 不得出现 OpenAlex 凭证环境变量名的字面量；凭证约定只指向
  `docs/DEPENDENCIES_AND_EXTERNAL_TOOLS.md` §9。
- ```bash 围栏代码块内禁止 cmd 式 `set VAR=` 赋值；Windows shell 示例用
  ```powershell 或 ```bat 围栏。
- 提到 Catalog 初筛决策时，只说初始 `screening.read_decision` 恒为 `"pending"`；
  写作阶段的终值不得写成初筛条件（细节见 05）。

## 维护义务

任何结构性改动（模块移动、分层调整、新脚本、新 skill、新契约）落地后，
按 [07_change_workflow.md](07_change_workflow.md) 的同步矩阵更新本目录对应文档，
并在 [06_refactor_roadmap.md](06_refactor_roadmap.md) 勾选或登记新条目。

# 改动流程与文档同步

## 一次改动的标准流程

1. **读契约**：`CLAUDE.md` →（结构性改动）`md/README.md` 起步的相关篇 →
   相关 `docs/`。
2. **设计**：确定层位、复用点、测试策略；大改动先在
   [06_refactor_roadmap.md](06_refactor_roadmap.md) 登记条目与验收标准。
3. **改码**：遵守 [01](01_architecture.md)/[02](02_coding_standards.md)/
   [03](03_scripts_and_entrypoints.md)。
4. **targeted tests**：先跑受影响面的测试（隔离 `PYTHONPYCACHEPREFIX`，
   见 [04](04_testing_and_acceptance.md)）。
5. **fast gate**：`python scripts/agent_acceptance.py`，要求字面
   `[OK] agent acceptance passed` + `[OK] Packed: mineru_snapshot.zip`。
6. **发布级改动**再加 `--full`，并解包抽查快照。
7. **同步文档**：按下方矩阵。
8. **提交**：仅在用户明示后进行。

## git 红线

未经用户明确要求，不执行 `git reset/checkout/clean/add/commit/push`；
保留与本次任务无关的脏工作区改动。只读 git 命令（status/log/diff/grep）随意。

## 文档同步矩阵（改了 X → 必须同步 Y）

| 改动 | 必须同步 |
| --- | --- |
| 新增/删除 `scripts/*.py` | `docs/SCRIPT_USAGE.md` 行（测试强制）；退役另加 tombstone 测试 |
| 分层/包结构 | `tests/hygiene/test_layering.py` 表 + `docs/ARCHITECTURE.md` + [01](01_architecture.md) |
| Agent 契约措辞 | `CLAUDE.md` 修改后**字节复制**为 `AGENTS.md` |
| 新增/修改 skill | skill 三件套 + 伴生 hygiene 测试 + [05](05_skills.md) 清单表 + `write/README.md`（写作类） |
| 依赖/外部工具 | `docs/DEPENDENCIES_AND_EXTERNAL_TOOLS.md` |
| 阶段性状态 | `docs/PROJECT_STATUS.md` 加日期条目 |
| 路线图条目完成 | [06_refactor_roadmap.md](06_refactor_roadmap.md) 勾选 + 写验收证据 |
| 移动模块 | 全库 grep 旧路径清零（含 monkeypatch 字符串、`__import__("...")`、文档路径字面量） |

## CLAUDE.md 修改协议

`CLAUDE.md` 与 `AGENTS.md` 必须字节一致（`tests/hygiene/test_docs_alignment.py`）。
标准操作：只编辑 `CLAUDE.md`，然后：

```bash
cp CLAUDE.md AGENTS.md
cmp CLAUDE.md AGENTS.md
```

`cmp` 无输出即一致。skill 目录内同名成对文件同理。

## 验收报告义务

最终交付报告必须包含：两行 `[OK]` 字面输出、`--full` 结果（如适用）、
快照解包抽查结论（含 `runtime_files_included=0`）、以及本次改动触发的
文档同步矩阵完成情况。

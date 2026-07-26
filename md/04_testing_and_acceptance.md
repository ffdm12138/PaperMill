# 测试与验收规范

## 测试分层与 markers

- 目录即分层：`tests/{unit,contract,integration,e2e,hygiene,performance,
  security,slow}` + `factories/helpers/fixtures`。marker 在 `pytest.ini`
  声明并 `--strict-markers`；真实进程测试必须带 `process` marker 并清理
  完整进程树。
- 进程时序测试用事件/队列等同步原语，禁止固定 sleep。

## Hygiene 思想

契约写成测试：分层 DAG、文档一致性、禁词表、快照规则、skill 结构都由
`tests/hygiene/` 扫描强制。新契约的正确顺序是**先落 hygiene 测试再写文档**；
文档（docs/、md/、skills/）本身也在扫描范围内。

## 隔离工作区

- 所有测试组经 `TestRuntimeWorkspace`（`scripts/test_runtime_workspace.py`）
  取得隔离工作区；工作区在系统临时目录下、带机器可读 marker。
- 测试一律 `tmp_path` + fake provider + fake MinerU + 隔离 ledger/index 根；
  绝不触碰真实 `data/`、真实网络、真实 GPU。
- 手工跑 pytest 时必须给 `PYTHONPYCACHEPREFIX` 指向系统临时目录下的
  **非 mineru 前缀**目录（如 `%LOCALAPPDATA%\Temp\ccagent_pycache`），
  否则 `__pycache__` 落进仓库会让验收 pre-flight fail-closed；
  名字带 mineru 前缀又会被 stale-workspace 扫描误报。

## 验收门

```bash
python scripts/agent_acceptance.py
```

- fast gate：例行改动的必过门（并行分组，约 1 分钟）。
- `--full`：发布、broad refactor、最终交付前必过；`--process` 真进程行为；
  `--stress` 放大竞态。
- 成功判据是两行**字面**输出：`[OK] agent acceptance passed` 与
  `[OK] Packed: mineru_snapshot.zip`。测试通过 ≠ 验收通过——运行残留
  （`__pycache__`、basetemp、legacy 缓存）会让门失败，且验收只检测不代删。

## 打包验证

- 权威打包器 `scripts/pack_repo.py`；快照必须 runtime-zero
  （manifest `runtime_files_included=0`）。
- 交付前解包抽查：源码/tests/docs/md/skills 齐全、无 runtime 泄漏、
  被移动模块的旧路径不存在。

## Windows 陷阱（引用 CLAUDE.md，不复述全文）

- 绝不把 Windows 路径塞进 `PYTEST_ADDOPTS`/`addopts`（POSIX shlex 会剥反斜杠）。
- 子进程一律参数列表 + `shell=False` + 独立 env dict；路径只经 env 传递。
- 缓存禁落盘符根、仓库根、真实 `data/`。
- 需要 PowerShell 时用 `pwsh`（PowerShell 7），不与 Windows PowerShell 5.1 搏斗。

## 清理命令（缺省 dry-run）

```powershell
python scripts/cleanup_test_caches.py
python scripts/cleanup_test_caches.py --legacy-flattened-root
python scripts/cleanup_test_caches.py --legacy-flattened-root --apply
```

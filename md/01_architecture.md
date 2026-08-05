# 架构与分层规范

## 分层事实源

机器事实源是 `tests/hygiene/test_layering.py`：它以数据形式（`LAYER_ORDER`、
`ALLOWED`、`SANCTIONED`、`SANCTIONED_LATE`）编码目标架构，并用 AST 扫描
src/ 全部模块的 import（含函数体内的 lazy import）。本文只讲规则与流程，
不复刻清单——清单以测试文件为准，防止文档腐化。

当前层序（低 → 高）：

```text
utils < mineru < metadata < catalog ~ workspace < library < ingest
      < catalog_folders < fetch < discovery < metadata_resolve
      < staging < writer < root
```

- `utils`：纯叶子工具（identifiers/timestamps/jsonio/atomic_io/fs/rate_limit/
  naming/path_utils/file_fingerprint/logging_setup/process/canonical_json/
  repository_hygiene），不 import 任何 src 包。
- `mineru`：MinerU 转换运行时（converter/cleaner/runtime/lock/service_manager/smoke）。
- `root`：仅 `src/server.py` 与 `src/prompt_builder.py` 两个组合顶点。
  root 可以 import 一切；**任何包不得 import root**（测试强制）。

## 依赖方向规则

1. 只向下 import：`ALLOWED` 表中每条边必须在 `LAYER_ORDER` 中严格向下，
   测试 `test_allowed_table_is_a_strict_downward_dag` 强制该不变量——
   环只能通过 `SANCTIONED`/`SANCTIONED_LATE` 显式表达。
2. 模块顶层 import 与函数体内 lazy import 都被扫描。lazy import 不是绕过
   分层的手段，只是控制加载时机的手段；跨层 lazy 边必须进 `SANCTIONED_LATE`。
3. 同包内部 import 不受限制，但同包内也不得用下划线私名做跨模块接口
   （见 [02_coding_standards.md](02_coding_standards.md)）。

## 新增模块与包的流程

1. 先判层：纯工具 → `src/utils/`；领域事实 → 对应领域包（metadata/catalog/
   workspace/library）；编排/事务 → ingest/discovery/staging；界面组合 → root。
2. 新增一个包时必须同步修改 `test_layering.py` 的 `LAYER_ORDER` 与 `ALLOWED`
   （表键与层序必须一一对应，测试强制），并在提交说明里写明层位理由。
3. 未登记的包会让守卫 fail-closed（`unknown src package`），这是特性不是缺陷。

## Sanctioned seam（豁免缝）流程

- 只有"单模块 → 单模块"的具体缝可以豁免，禁止放宽整包边。
- 每条豁免必须附带理由注释；**删掉理由等于删掉豁免**。
- 申请一条新缝前先回答：能否把被依赖的符号下沉到更低层？能否改为在组合层
  （root/脚本）注入？两者都不行才写豁免。
- 现有豁免族（理由见测试文件内注释）：metadata↔discovery.models 的候选值对象、
  fetch→ProviderClient 的统一 HTTP 强制、staging_gateway→staging 的组合边界、
  catalog_folders→discovery.contracts.notebook 的关键词契约、
  library~workspace 的声明式 2-cycle、ingest→catalog_folders 的提交后对账。

## 单一权威层原则

每个契约只在一层权威（呼应 CLAUDE.md "Keep each contract authoritative in
one layer"）：runtime-zero 规则在 `src/utils/repository_hygiene.py`；
`.import_status.json` 唯一写者在 `src/ingest/import_status.py`；DOI/身份
磁盘扫描唯一入口是 `WorkspaceRegistry`；纸号真相只在 `PaperNumberLedger`；
PDF 身份决策唯一权威在 `src/metadata/identity_match.py`（证据分层、
四态书目、版本家族、automatic/final 人工确认模型），`pdf_match.py` 只做
receipt 壳与重放验证（重放已存证据、绝不重提取）；PDF 身份迁移期间，
`data/paper_raw/.pdf_identity_migration.json` 维护标志关闭所有其他
paper_raw 写入者（入口 + 写锁内复检），仅迁移工具凭 run_id + plan hash
操作。新代码发现自己在"顺手"重复某层契约时，应当改为调用权威层。

## 与路线图的联动

分层表的任何演进（新包、缝的增删、层序调整）都要在
[06_refactor_roadmap.md](06_refactor_roadmap.md) 登记条目并写清验收标准
（哪个测试证明它）。

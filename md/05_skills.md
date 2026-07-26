# Skill 规范

Skill 位于仓库根 `skills/<skill_name>/`，是"Claude 按文件契约完成一步 LLM
工作"的封装。仓库不实现内置 LLM client——skill 本身就是 LLM 步骤。

## 三件套与目录结构

```text
skills/<skill_name>/
  SKILL.md        # 权威操作文件：frontmatter + 角色 + 边界 + 流程 + 输出清单
  README.md       # 人类叙述版摘要
  CLAUDE.md       # 负空间契约：先声明"不是什么"，再列读/写白名单
  *_schema.json   # 结构化产物的 JSON Schema（draft-07，additionalProperties:false）
  examples/       # 与 schema 一致的示例实例（hygiene 测试会 jsonschema.validate）
```

- `SKILL.md` 首字节必须是 `---\n`；frontmatter 的 `name:` 必须与目录名完全一致。
- skill 目录若同时存在 `CLAUDE.md` 与 `AGENTS.md`，两者必须字节一致
  （`tests/hygiene/test_docs_alignment.py`）；只放 `CLAUDE.md` 最省事。
- 语言惯例：frontmatter/标题/章节名英文，守则正文中文；需要被测试 grep 的
  关键短语中英并写。

## 伴生 hygiene 测试义务

每个 skill 配一个 `tests/hygiene/test_<skill>_skill.py`（轻量模板参照
`test_catalog_tex_writer_skill.py`）：文件存在性、frontmatter、**字面**边界
短语 grep、schema 可加载且 example 通过校验。想强制的守则必须以字面串写进
`SKILL.md`，测试才能钉住它。

## 三段式 LLM 文件交接模式

1. **prepare（Python）**：确定性地生成任务信封/工作区，绑定输入 sha256，
   缺省 dry-run、`--apply` 落盘。
2. **执行（Claude/skill）**：只读任务引用的文件，把产物写到指定路径；
   不碰任务外的任何数据。
3. **validate/apply（Python）**：schema + 语义校验，事务化应用，写回执；
   重跑幂等。

## 通用红线

- 引用/BibTeX/CSL/APA/author-year key 只来自 Metadata；Catalog 只用于理解
  内容，结构上就不携带书目字段（`FORBIDDEN_BIBLIOGRAPHIC_KEYS` 递归禁止）。
- 写作类 skill 只读 job 内文件（`write/jobs/<job_id>/selected_catalog.json`
  与 `write/jobs/<job_id>/article/<paper_number>/`），绝不直读 `data/papers`、
  `data/paper_raw`、`data/raw`。
- 初始 `screening.read_decision` 恒为 `"pending"`，不得作为选纸条件；
  终值语义属于写作阶段之后的人工分诊。
- 不虚构：引用、数字、结论都必须能回溯到 job 内证据（paper_number + bib_key）。
- 产物路径一律 `write/jobs/<job_id>/...`；`write/jobs/*` 运行产物永不提交。

## Skill 清单

| Skill | 角色 |
| --- | --- |
| `paper_raw_metadata_resolver` | 从转换证据解析 citation-ready Metadata（候选 + 纯书目 patch） |
| `paper_raw_catalog_curator` | 为冻结工作区产出 Catalog v3.2 内容档案与 paper_name |
| `catalog_folder_classifier` | 按 Catalog 把正式论文分类进中文关键词目录 |
| `catalog_tex_writer` | 预备好的 write job 上写小型 TeX 文章（mini article） |
| `catalog_review_writer` | 按 catalog 范围写主题综述 + 研究空白 + 潜在方向（md 中间件 → TeX 成品） |
| `catalog_research_proposal_writer` | 先射箭后画靶：综述 → 方法 → 结果与数据分析规划（研究骨架） |
| `literature_library_manager` | 端到端管理文献库（转换/解析/curation/formalize/commit/rollback） |

新增 skill 时同步更新本表与 [07_change_workflow.md](07_change_workflow.md)
的同步矩阵要求项。

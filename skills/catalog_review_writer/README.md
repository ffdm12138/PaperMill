# Catalog Review Writer

基于用户划定的 catalog 范围写研究主题综述的 skill。它接在既有写作管线上：
`create_write_job.py --workflow review --categories <关键词...>` 先把入选论文
（catalog 内容投影 + 论文目录完整拷贝）准备进 `write/jobs/<job_id>/`，本 skill
在 job 内完成三段工作——

1. **理解**：逐篇读 copied Catalog（必要时对照 copied Markdown 全文），
   产出八列文献矩阵 `reports/literature_matrix.md`。
2. **规划（Markdown 交接文档）**：综述大纲（问题链/机制链组织）、研究空白、
   潜在研究方向，以及机器可核验的 `planning/review_plan.json`
   （每个论断都带 paper_number + bib_key 证据链）。
3. **成品（TeX）**：introduction / 主题章 / research_gaps / future_directions /
   conclusion，引用一律来自 job 内 metadata 生成的 `references.bib`。

它是 metadata-cited、catalog-informed 的：Catalog 提供内容理解，Metadata 提供
全部引用事实，两者绝不混用。产物永远留在 `write/jobs/<job_id>/` 内
（gitignore 忽略），通过 `check_write_planning_docs.py`、
`check_write_tex_project.py`、`check_write_quality_text.py` 三道确定性门验收。

与相邻 skill 的分工：`catalog_tex_writer` 写小型 mini article；
`catalog_research_proposal_writer` 在综述之外继续写方法与研究计划（先射箭后
画靶）；本 skill 专注综述 + 空白 + 方向。

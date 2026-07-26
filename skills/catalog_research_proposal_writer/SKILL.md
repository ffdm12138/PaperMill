---
name: catalog_research_proposal_writer
description: Scaffold a research proposal from a prepared MinerU write job and the user's own project description - literature review, research gaps, methods design, and a planned results/data-analysis roadmap (introduction + methods ready, rest is a skeleton).
---

# Catalog Research Proposal Writer

先射箭后画靶：先综述与问题，再写方法，再规划结果与数据分析。基于用户划定的
catalog 范围与用户自己的研究项目描述，把论文的 Introduction 与 Methods 搭好，
并给后续结果部分一个可执行的研究骨架。步骤间交接用 Markdown，最终交付用 TeX。

## Role

你是 catalog research proposal writer。你的任务不是入库、不是写 mini article、
也不只是综述，而是：围绕用户的研究项目，先完成主题综述与研究空白（射箭），
再据此设计研究方法，并规划结果与数据分析工作（画靶）——产出一份诚实的研究
计划。本 skill 产出的是研究计划（a research plan, not results）。

## 事实源与边界（必须遵守）

权威边界见 `docs/PROJECT_CONTRACT.md`；本 skill 不与之冲突。

- 只读 job 内文件：`write/jobs/<job_id>/job.json`、
  `write/jobs/<job_id>/input/research_input.md`、
  `write/jobs/<job_id>/selected_catalog.json`、
  `write/jobs/<job_id>/article/<paper_number>/`。
- Do not read `data/papers` directly. Do not read `data/paper_raw`,
  `data/raw`, or non-job-local data paths directly.
- 若 `input/research_input.md` 缺失或仍含「（待填）」占位符，停止并要求用户
  先补全，不得代填研究项目事实。
- 引用事实只来自 job-local `article/<paper_number>/<paper_name>.metadata.json`，
  经 `bibtex_from_metadata()` 或 `src.writer.bib.bibtex_for_entry()` 生成；
  `references.bib` 一律由 `scripts/export_write_job_bib.py` 导出。
  Catalog 只用于理解内容，绝不作为引用来源。
- Do not guess DOI. 不得编造引用、数字或结论。**结果部分只允许"计划进行的
  分析"，不得虚构数据、实验结果或统计数值（no fake data, no invented
  results）**；`results_plan` 中每一项 `status` 恒为 "planned"。
- 每个提出的方法必须在 `grounded_in` 中引用至少一篇实际使用该方法的入选论文
  （paper_number + bib_key）；对用户项目的适配改动单独写明。
- Quantitative claims 必须保留数值、单位、方向/幅度与不确定性，附 `bib_key`，
  并可回溯到 `article/<paper_number>/` 的 copied Markdown。
- 初始 `screening.read_decision` 恒为 "pending"，不得将其作为筛选条件。
- No RAG, no embeddings, no vector database, no LLM client in code.
- 不得创建 `planning/selected_papers.json` 或 `planning/workset_manifest.json`
  （JobManager 旧路径专名）。`write/jobs/*` runtime products are never committed.
- Intermediates are Markdown under `write/jobs/<job_id>/planning/` and
  `write/jobs/<job_id>/reports/`; the final deliverable is TeX only under
  `write/jobs/<job_id>/tex/`.

## Inputs

```text
write/jobs/<job_id>/
  job.json                    # workflow == "catalog_research_proposal"
  input/research_input.md     # 用户填写的研究项目描述（问题/对象与数据/条件/预期）
  selected_catalog.json       # 入选论文的内容投影（无书目字段）
  article/<paper_number>/     # 每篇论文的完整拷贝（catalog/metadata/md/images）
```

## Workflow

1. STEP 0 前置检查 + bib 导出：确认 `input/research_input.md` 已填写（无
   「（待填）」残留）；运行
   `python scripts/export_write_job_bib.py --job-id <job_id>` 生成
   `tex/references.bib`。
2. STEP 1 文献矩阵：`reports/literature_matrix.md`，八列同 review skill
   （paper_number、bib_key、study object、method/data、key conclusion、
   role in the review、limitation/uncertainty、key quantitative claims）。
3. STEP 2 综述大纲：`planning/review_outline.md`。Organize the review around
   a problem chain or mechanism chain, not a paper-by-paper list；大纲必须
   收敛到用户项目所处的位置。
4. STEP 3 研究空白：`planning/research_gaps.md`，每个 gap 带证据链，并标注
   哪些 gap 是用户项目要打的靶。
5. STEP 4 方法设计：`planning/methods_design.md`。每个方法写：目的、来自
   语料的出处（谁用过、怎么用的）、对用户项目的适配、数据需求。
6. STEP 5 结果与数据分析规划：`planning/results_plan.md`。列出计划进行的
   分析（用哪个方法、预期产出形态、如何验证/交叉检验），全部明示为计划。
7. STEP 6 机器可核验计划：`planning/proposal_plan.json`，符合本目录
   `proposal_plan_schema.json`（methods_design 逐项 `grounded_in` 非空；
   `results_plan` 逐项 `status` 为 "planned"）。
8. STEP 7 TeX 成品：`tex/main.tex` + `tex/sections/`：`introduction.tex`
   （框定用户项目）、`literature_review.tex`、`research_gaps.tex`、
   `tex/sections/methods.tex`、`tex/sections/research_plan.tex`（结果与数据
   分析规划，含风险与局限/不确定性段落）、`tex/sections/conclusion.tex`
   （结论与预期贡献）。Do not use template sentences such as `X指出：X`.
9. STEP 8 校验：依次运行
   `python scripts/check_write_planning_docs.py --job-id <job_id>`、
   `python scripts/check_write_tex_project.py --job-id <job_id>`、
   `python scripts/check_write_quality_text.py --job-id <job_id>`。

## Outputs

1. `reports/literature_matrix.md`
2. `planning/review_outline.md`、`planning/research_gaps.md`、
   `planning/methods_design.md`、`planning/results_plan.md`
3. `planning/proposal_plan.json`（schema: `proposal_plan_schema.json`）
4. `tex/main.tex`、`tex/sections/*.tex`、`tex/references.bib`（STEP 0 生成）
5. `reports/proposal_writing_report.json`（自报：产出清单 + 每步状态）

## Quality Acceptance

- Introduction 与 Methods 达到可直接迭代的成稿骨架；结果部分是清晰的研究
  计划而非结果陈述。
- 每个方法有语料出处（grounded_in）；每个分析项挂在某个方法上且明示 planned。
- 所有 bib key 被引用；quantitative claims 可回溯；有结论章与风险/局限
  （不确定性）段落。
- 通过 `check_write_planning_docs.py`、`check_write_tex_project.py`、
  `check_write_quality_text.py` 三道确定性门。

## Output Checklist

- [ ] 未读取任何 job 外数据路径；未代填用户研究项目事实
- [ ] 未编造任何引用、数字、结论；结果部分全部为计划（planned）
- [ ] Markdown 交接文档齐全且非模板；方法均有语料出处
- [ ] `proposal_plan.json` 通过 schema 校验
- [ ] TeX 成品通过三道校验脚本
- [ ] 产出全部位于 `write/jobs/<job_id>/` 之内

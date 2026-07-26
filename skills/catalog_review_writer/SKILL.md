---
name: catalog_review_writer
description: Write a Chinese-first topic literature review with research gaps and proposed directions from a prepared MinerU write job, using Markdown planning intermediates and a job-local TeX deliverable.
---

# Catalog Review Writer

围绕用户划定的 catalog 范围（分类目录选出的论文集合），写一篇研究主题综述：
先产出 Markdown 交接文档（文献矩阵、综述大纲、研究空白、潜在方向），再产出
最终 TeX 成品。步骤间交接用 Markdown，最终交付用 TeX。

## Role

你是 catalog review writer。你的任务不是入库、不是解析 metadata、不是写
catalog、不是分类，而是：在一个已准备好的 write job 里，基于入选论文的
Catalog 内容证据，组织一篇有问题链/机制链的主题综述，明确研究空白，并提出
有证据支撑的潜在研究方向。

## 事实源与边界（必须遵守）

权威边界见 `docs/PROJECT_CONTRACT.md`；本 skill 不与之冲突。

- 只读 job 内文件：`write/jobs/<job_id>/job.json`、
  `write/jobs/<job_id>/selected_catalog.json`、
  `write/jobs/<job_id>/article/<paper_number>/`（含复制的
  `<paper_name>.catalog.json`、`<paper_name>.metadata.json`、
  `<paper_name>.md` 全文与 `images/`）。
- Do not read `data/papers` directly. Do not read `data/paper_raw`,
  `data/raw`, or non-job-local data paths directly.
- 引用事实只来自 job-local `article/<paper_number>/<paper_name>.metadata.json`，
  经 `bibtex_from_metadata()` 或 `src.writer.bib.bibtex_for_entry()` 生成；
  `references.bib` 一律由 `scripts/export_write_job_bib.py` 导出。
  Catalog 只用于理解内容，绝不作为引用来源（citations only from Metadata）。
- Do not guess DOI. 不得编造引用、数字或结论（never fabricate citations,
  numbers, or conclusions）。Quantitative claims 必须保留数值、单位、
  方向/幅度与不确定性，附 `bib_key`，并可回溯到 `article/<paper_number>/`
  的 copied Markdown。
- 初始 `screening.read_decision` 恒为 "pending"，不得将其作为筛选条件。
- No RAG, no embeddings, no vector database, no LLM client in code.
- 选纸是 Python 步骤（`create_write_job.py --workflow review --categories ...`），
  不是本 skill 的职责；job 不存在或 selected_catalog.json 缺失时停止并提示先建 job。
- 不得创建 `planning/selected_papers.json` 或 `planning/workset_manifest.json`
  （JobManager 旧路径专名）。`write/jobs/*` runtime products are never committed.
- Intermediates are Markdown under `write/jobs/<job_id>/planning/` and
  `write/jobs/<job_id>/reports/`; the final deliverable is TeX only under
  `write/jobs/<job_id>/tex/`.

## Inputs

```text
write/jobs/<job_id>/
  job.json                    # workflow == "catalog_review"
  selected_catalog.json       # 入选论文的内容投影（无书目字段）
  article/<paper_number>/     # 每篇论文的完整拷贝（catalog/metadata/md/images）
```

## Workflow

1. STEP 0 bib 导出：运行 `python scripts/export_write_job_bib.py --job-id <job_id>`
   生成 `tex/references.bib`；任何 metadata 不 citation-ready 的论文会 fail-closed。
2. STEP 1 文献矩阵：逐篇读 catalog（必要时对照 copied Markdown），写
   `reports/literature_matrix.md`——每篇一行，八列：paper_number、bib_key、
   研究对象（study object）、方法与数据（method/data）、关键结论
   （key conclusion）、综述角色（role in the review）、局限与不确定性
   （limitation/uncertainty）、关键定量结论（key quantitative claims）。
3. STEP 2 综述大纲：`planning/review_outline.md`。Organize the review around a
   problem chain or mechanism chain, not a paper-by-paper list（围绕问题链或
   机制链组织，不逐篇罗列）。
4. STEP 3 研究空白：`planning/research_gaps.md`。每个 gap 写清它为何重要，
   并给出证据链。
5. STEP 4 潜在方向：`planning/proposed_directions.md`。每个方向说明针对哪些
   gap、站在哪些工作之上、可行性如何。
6. STEP 5 机器可核验计划：`planning/review_plan.json`，符合本目录
   `review_plan_schema.json`。每个 research gap 与 proposed direction 必须
   给出证据链（paper_number + bib_key），无证据不写。
7. STEP 6 TeX 成品：`tex/main.tex` + `tex/sections/`：`introduction.tex`、
   若干 `theme_<slug>.tex` 主题章、`research_gaps.tex`（含局限与不确定性
   讨论）、`future_directions.tex`、`tex/sections/conclusion.tex`。
   Do not use template sentences such as `X指出：X`. 不留 TEMPLATE_ONLY/
   待补全类占位符。
8. STEP 7 校验：依次运行
   `python scripts/check_write_planning_docs.py --job-id <job_id>`、
   `python scripts/check_write_tex_project.py --job-id <job_id>`、
   `python scripts/check_write_quality_text.py --job-id <job_id>`，全部通过
   才算完成。

## Outputs

1. `reports/literature_matrix.md`
2. `planning/review_outline.md`、`planning/research_gaps.md`、
   `planning/proposed_directions.md`
3. `planning/review_plan.json`（schema: `review_plan_schema.json`）
4. `tex/main.tex`、`tex/sections/*.tex`、`tex/references.bib`（STEP 0 生成）
5. `reports/review_writing_report.json`（自报：产出清单 + 每步状态）

## Quality Acceptance

- 每篇入选论文在文献矩阵中恰有一行，且五要素（study object、method/data、
  key conclusion、role in the review、limitation/uncertainty）非空。
- 综述正文每个实质性论断带 `\cite{}`；所有 bib key 被引用；每个
  quantitative claim 可回溯（paper_number + bib_key）。
- 有引言与结论章；有独立的研究空白讨论，含不确定性/局限段落。
- 通过 `check_write_planning_docs.py`、`check_write_tex_project.py`、
  `check_write_quality_text.py` 三道确定性门。

## Output Checklist

- [ ] 未读取任何 job 外数据路径（`data/papers`、`data/paper_raw`、`data/raw`）
- [ ] 未编造任何引用、数字、结论；引用全部来自 job 内 metadata
- [ ] Markdown 交接文档齐全且非模板
- [ ] `review_plan.json` 通过 schema 校验，证据链完整
- [ ] TeX 成品通过三道校验脚本
- [ ] 产出全部位于 `write/jobs/<job_id>/` 之内

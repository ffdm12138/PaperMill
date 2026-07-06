---
name: paper_raw_metadata_resolver
description: Ingest-side metadata resolver for PDFs after MinerU conversion. Read the converted Markdown first, extract DOI/title/authors/year/venue candidates, verify them online (or search online when missing), and emit candidates + a patch in the same schema as network-fetched metadata. Never fabricate DOI/author/year/venue; never set metadata_match.status.
---

# Paper Raw Metadata Resolver Skill

Read converted Markdown first 100 lines for title/author candidates before PDF title fallback.
Use the same front-matter evidence for affiliation, abstract, keyword, and DOI candidate review.

Use this skill when a `data/paper_raw/<paper_number>/` folder has a PDF and/or
MinerU Markdown but its metadata is **unmatched** or missing fields. The skill
proposes metadata candidates and a patch; it does NOT apply them to formal
metadata and does NOT decide `metadata_match.status`.

## Role

你是 paper_raw metadata resolver。你的任务不是写 catalog、不是写综述，而是为
metadata 缺失/unmatched 的 paper_raw 文献生成**带证据的 metadata 候选**与**只补空字段的
patch**，供脚本 (`scripts/resolve_paper_raw_metadata.py --apply`) 在通过校验后应用。

## 事实源与边界（必须遵守）

权威边界见 `docs/PROJECT_CONTRACT.md`；本 skill 不与之冲突。

- 输入**只来自** `data/paper_raw/<paper_number>/`。必须先读 `<paper_number>.md`
  （转换后 MinerU Markdown，**候选主证据**），再读 `<paper_number>.metadata.json`
  （目标空壳，判断哪些字段为空、哪些需补），可参考 `<paper_number>.metadata.candidates.json`。
- PDF 文件名、OCR/MinerU 文本片段、既有 candidates 只是**辅助 hint**，不能作为唯一
  metadata 来源；大模型不能凭空补 DOI/作者/期刊/年份。
- **不得读取** `data/papers`、`data/catalog/`、`all.catalog.json`、`write/`。
- **不得生成** catalog、`paper_number`；**不得 commit**；**不得移动或修改** `data/papers`。
- **不得编造** DOI、作者、年份、期刊、卷期页、article number。不确定的字段留空。
- DOI 必须来自：PDF 原文 / MinerU Markdown 原文 / 文件名 / Crossref / OpenAlex /
  Unpaywall 等可信网络源。**LLM 推测的 DOI 一律视为无效。**
- 从 Markdown 提取 DOI 时，只扫描正文头部区域（参考文献/Bibliography/参考文献 之前）；
  参考文献区的 DOI 不能作为本文 DOI。
- patch 只能补空字段（最终由 `merge_missing_metadata` 合并，不覆盖非空字段）。
- **不得**把 `metadata_match.status` 改成 `matched` 或 `manual_confirmed`。resolver 只能
  产出 candidates + patch + evidence + confidence + source + mismatch reason；是否
  matched/manual_confirmed 由现有验证 / 人工确认 / formalize→commit 路径
  （`scripts/resolve_paper_raw_metadata.py --apply`、`formalize_paper_raw.py`、`commit_paper_raw_to_papers.py`）
  根据 DOI 有效性、重复检测、冲突检测、完整性规则决定。
- 输出 patch 结构同 `empty_metadata` 子集，与网络抓取 metadata **同一 schema**，不分叉；
  BibTeX 由 `bibtex_from_metadata` 从该结构生成，不因来源不同分叉。
- **只负责 metadata candidates 和 metadata patch**；不负责 catalog，不生成 classification /
  research_card / screening / evidence_profile，不读写 all.catalog，不从正文内容总结论文贡献，
  只处理书目信息（DOI/作者/年份/期刊/卷期页）。

## 证据层级

- **Primary evidence (required)**: converted MinerU Markdown from `data/paper_raw/<paper_number>/<paper_number>.md`。
- **Optional hints**: PDF filename、既有的空壳 metadata.json、DOI-like strings、title-like strings、OCR snippets。PDF filename 绝对不能作为唯一 metadata 来源。
- **手动 PDF 路径顺序**：先 MinerU 转换，再跑 metadata resolver。For manual PDF imports, do not run metadata resolver before MinerU conversion has produced Markdown（先转换，再解析）。
- **status 权限区分**：LLM-facing skill 只产出 candidate metadata patch / evidence / source / confidence / mismatch reason，**绝不自己把 `metadata_match.status` 置为 `matched`**。`matched` / `manual_confirmed` 只能由 deterministic apply step (`scripts/resolve_paper_raw_metadata.py --apply`) 在 schema/DOI/title/author/year/venue 校验通过或显式 `--manual-confirm` 后盖章。大模型 skill 不盖章；脚本 apply 层在确定性校验通过或人工确认后才可以盖章。

## 联网验证规则（Cases）

resolver 先读转换后的 Markdown 抽取候选，再按下述规则联网验证/查询：

- **Case A — md 中抽到 DOI**：必须联网验证 DOI（Crossref/OpenAlex/Unpaywall）。核对
  title、authors、year、venue。网络结果优先于 md 中可能 OCR 错误的字段。
  若 DOI 查不到或结果明显不匹配，标记 `decision = manual_review`（不能强行 `auto_matched`），
  并记录 evidence/source/confidence/mismatch reason。
- **Case B — md 中只抽到 title**：必须联网查询 title。核对 title 相似度、作者、年份、venue，
  不能只因为 title 部分相似就直接 matched。网络 title-search 候选最多 `manual_review`，
  记录 evidence/source/confidence/mismatch reason。
- **Case C — md 中只有很弱的线索**：用 title-like string、PDF 文件名、正文前几段、
  DOI-like pattern 联网查。找不到可靠结果则 `decision = manual_review`/`no_candidates`，
  不得编造完整 metadata。
- **Case D — md 中完全抽不到候选**：必须联网查。若无法可靠匹配，保持
  `metadata_match.status = unmatched`，输出 `decision = no_candidates`，等待人工确认，
  不得入库，不得编造 metadata。

## 联网不可用时 fail-closed

当前环境若无联网能力，resolver 必须 fail-closed：

- 保持 `metadata_match.status = unmatched`（绝不伪造 matched metadata）；
- 保留已从 Markdown 抽取的候选（写入 `<paper_number>.metadata.candidates.json`，附 evidence）；
- `decision` 设为 `manual_review`/`no_candidates`，`.import_status.json` 记为
  `metadata_manual_review_required` / `metadata_resolve_failed`；
- 请求人工复核，等待联网或人工补全后再走验证/commit 路径。

## 输入

```
data/paper_raw/<paper_number>/
  <paper_number>.md                # 转换后 MinerU Markdown，候选主证据，必读
  <paper_number>.metadata.json     # 目标空壳 metadata，必读（判断哪些字段为空）
  <paper_number>.pdf
  images/
  <paper_number>.metadata.candidates.json   # 可选，网络候选参考
```

## 输出

在同一文件夹下输出两个 JSON：

1. `<paper_number>.metadata.candidates.json` —— 符合 `metadata_candidate_schema.json`。
   保存所有候选，每个候选带 `evidence[]`（证据来自 md 哪一行 / 哪个 DOI / 哪个网络源）
   和 `warnings`；含 `recommendation{best_candidate_id, decision, reason}`。
   `decision ∈ {auto_matched, manual_review, rejected, no_candidates}`。
2. `<paper_number>.metadata.patch.json` —— 符合 `metadata_patch_schema.json`，
   只含建议补齐的空字段（结构同 `empty_metadata` 子集），由
   `merge_missing_metadata` 合并。

## 候选评分（仅排序/分档，不是事实来源）

```
score = 0.40*title_sim + 0.20*author_sim + 0.15*year_match
      + 0.15*doi_source_conf + 0.10*venue_presence
```

- score 只用于候选排序与判断进入 auto/manual 档；**不能单独作为 metadata 事实来源**。
- 事实只能来自：DOI 权威源、PDF/Markdown 原文、人工确认。
- 网络 title-search 候选（DOI 仅来自 Crossref/OpenAlex 搜索）**永不 auto_matched**，
  最多 `manual_review`，只能由 `--manual-confirm --apply` 变成 `manual_confirmed`。

## 接入

- 生成候选/patch 后，由脚本应用：
  ```bash
  python scripts/resolve_paper_raw_metadata.py --paper-number <paper_number> --write-candidates   # 只写候选/报告
  python scripts/resolve_paper_raw_metadata.py --paper-number <paper_number> --apply              # 高置信自动 matched
  python scripts/resolve_paper_raw_metadata.py --paper-number <paper_number> --apply --manual-confirm --candidate-id cand_002
  ```
- 脚本会校验：DOI 是否有效/可解析、是否与 data/paper_raw 或 data/papers 中的 DOI/PDF md5/sha256 重复、是否冲突、
  metadata 是否完整、是否覆盖非空字段。任一失败则保持 unmatched。

## Output Checklist

- `<paper_number>.metadata.candidates.json`（含 evidence + recommendation）
- `<paper_number>.metadata.patch.json`（只补空字段）
- 未修改 `<paper_number>.metadata.json`
- 未设置 `metadata_match.status`
- 未编造任何 DOI/作者/年份/期刊/卷期页


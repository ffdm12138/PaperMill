# CLAUDE.md — paper_raw_metadata_resolver skill

This is an ingest-side metadata resolver skill for PDFs after MinerU conversion.
It is not an article-writing skill.
It is not a catalog curator.
It resolves bibliographic metadata for imported PDFs by reading the converted
Markdown first, then verifying or completing the result online.
Read the converted Markdown physical first 100 lines for title/author/
affiliation/abstract/keyword/DOI candidates before PDF title fallback.

与 `SKILL.md` 同源。你是 paper_raw metadata resolver：在 PDF 导入、MinerU 转换完成后，
为 `data/paper_raw/<source_id>/` 生成带证据的 metadata 候选与只补空字段的 patch。

这是 PDF 导入后、MinerU 转换完成后使用的 metadata 补全/匹配 skill。它必须先读取
转换完成的 md 文档，从中抽取标题/DOI/作者/年份/期刊等候选线索；抽到候选必须联网
验证，抽不到足够线索必须联网查询；最终输出格式必须与网络抓取 metadata 的正式格式
一致。它不是 writing skill，不是 catalog curator，不生成 catalog。

- **手动 PDF 导入必须先转换再解析：**metadata resolver 必须在 MinerU 转换产出 md 之后运行。
  转换前没有 md，resolver 无法抽取标题/DOI/作者等候选。For manual PDF imports, do not run
  metadata resolver before MinerU conversion has produced Markdown.
- 必须先读 `data/paper_raw/<source_id>/<source_id>.md`（转换后 MinerU Markdown，候选主证据）；
  再读 `<source_id>.metadata.json`（目标空壳，判断哪些字段为空、哪些需补）。
- PDF 文件名 / OCR 文本片段 / 既有 candidates 只是辅助 hint，不能作为唯一 metadata 来源。
- 只在 `data/paper_raw/<source_id>/` 内工作；不得读 `data/papers`/catalog/`write/`。
- 不得编造 DOI/作者/年份/期刊/卷期页；LLM 推测的 DOI 无效。
- 联网验证：md 抽到 DOI 必须联网核对（Crossref/OpenAlex/Semantic Scholar/Unpaywall），
  网络结果优先于 md 中 OCR 错误字段；md 抽不到足够线索则联网查询。
- 联网不可用时 fail-closed：保持 `metadata_match.status = unmatched`，保留已抽取候选，
  请求人工复核，绝不伪造 matched metadata。
- 不得设置 `metadata_match.status`（尤其不能自己盖章 `matched`）；只能输出 candidates +
  patch + warnings + evidence + confidence + source + mismatch reason。是否
  matched/manual_confirmed 由现有验证 / 人工确认 / commit 路径决定。
- patch 只补空字段，结构同 `empty_metadata` 子集，由 `merge_missing_metadata` 合并，
  与网络抓取 metadata 同一 schema，不分叉。

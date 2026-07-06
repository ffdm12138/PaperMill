# Paper Raw Catalog Curator

This is an ingest-side catalog curation support skill.
It reads the converted MinerU Markdown and generates a content-only catalog.
It is not a metadata resolver.
It is not an article-writing skill.

与 `SKILL.md` 同源。你是 paper_raw catalog curator：基于 MinerU Markdown/PDF/images
生成 v3.1 content-only catalog。它不是 metadata resolver，不生成 metadata patch，
不写 DOI/作者/年份/期刊等书目字段，不生成 `paper_number`，不移动或改 `data/papers`。
初始 catalog 生成阶段 `screening.read_decision` 必须固定为 `"pending"`；不得输出
`must_read` / `maybe_read` / `skip`，这些值只用于 post-triage / writing-stage catalog 或人工筛选。
catalog curator 必须在转换完成产出 md 之后运行（读 md 生成 catalog）。正式 commit 要求 metadata 与 catalog 都通过校验。详见 `SKILL.md`。

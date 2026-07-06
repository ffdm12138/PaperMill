# Paper Raw Catalog Curator

Project skill for curating a single `data/paper_raw/<paper_number>/` folder.

输入是 metadata JSON 与 MinerU Markdown；任务是生成用于快速筛选精读文献的
catalog（v3.1，content-only）。

- metadata 是书目信息事实源；catalog 是筛选事实源。
- 初始 catalog 生成阶段 `screening.read_decision` 必须固定为 `"pending"`；`must_read` /
  `maybe_read` / `skip` 仅用于 post-triage / writing-stage catalog 或人工筛选后的精读决策。
- catalog 自然语言内容默认使用中文；JSON key/schema enum 保持英文，技术名词可中英混写。
- metadata 保留原始/规范书目信息，不因 catalog 中文化而改写。
- 不得生成 metadata patch；不得生成 `paper_number`；不得入库或改 `data/papers`。
- 详细规则见 `SKILL.md`；schema 见 `catalog_schema.json`；示例见 `examples/`。


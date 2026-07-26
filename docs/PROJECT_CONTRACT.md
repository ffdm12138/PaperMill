# Project contract

Discovery DOI and provider identity decisions cover both `paper_raw` and
generation-valid active `papers`. Formal/raw collisions fail closed. A batch has
one staging context, one Registry cold build and one journal full scan; repair
backlog is probed only with an explicit batch budget, and matched records are
always revalidated. Batch staging never combines or removes the two durable
ledger checkpoints required for each newly allocated paper.

Formal publication generation is the durable
`data/papers/.formal_publication_state.json` revision. Each entry binds
`paper_number`, canonical path/name, and the asset-manifest, Metadata, and
Catalog SHA-256 values. Formal assets are immutable after commit; every
staging batch rechecks this closure before DOI/identity lookup and fails closed
on missing, stale, or drifted publication state.
Localized workspace damage yields a typed partial Registry only for explicit
audit/repair. Discovery still rejects every `complete=False` Registry.

1. Metadata v2.0 contains no `paper_name`, LLM content, or match state. It must
   independently generate CSL-JSON, BibTeX, and styled references.
2. Journal articles require valid DOI; conference/chapter/thesis/report records
   require their type-specific stable identifier or URL.
3. PDF matching writes an independent receipt and never edits Metadata. DOI
   conflict cannot fall back to title matching or manual override.
4. Freeze validates schema, citation artifacts, PDF/match hashes, provider
   provenance, raw records, year, and first author. Normal services cannot edit
   any frozen closure asset.
5. Catalog v3.2 is the only active Catalog. It stores complete content
   understanding, trusted abstract provenance, logical evidence references, and
   `paper_name`, but no authoritative bibliographic record.
6. `paper_name` equals the frozen Metadata year/author prefix plus the LLM Chinese
   content title. Conflicts or path-budget failures require Catalog repair; no
   suffix, hash, number, truncation, or Python translation is allowed.
7. Every active raw workspace and main asset uses the 16-digit `paper_number`.
8. Conversion requires attached PDF; Catalog requires Metadata frozen and
   conversion complete; formalize requires Catalog frozen; commit requires a
   current formalization plan.
9. Formalize writes only a plan/status. Commit copies immutable bytes into
   hidden staging, renames there, validates, atomically installs, activates the
   ledger, publishes indexes, and deletes raw only after durable evidence.
10. Commit and rollback journals live outside data they delete. Their public
    coordinators automatically resume the sole active journal; conflicting or
    ambiguous transactions fail closed, and durable phase evidence is checked
    against the filesystem, ledger, and published index before each mutation.
11. Ledger activation plus a validated formal directory is the formal commit
    point. Catalog folders are repairable browsing state and writers fail closed
    when `DIRTY` exists or `all` differs from the active formal registry.
12. Writer citation output and citation keys read only Metadata. Catalog cannot
    substitute for missing Metadata.
13. Runtime data, secrets, local tool state (``.workbuddy/``, ``.reasonix/``),
    runtime reports (``data/cleanup_report.json``), and paper workspaces
    (``data/paper_raw/``, ``data/papers/``) never enter source snapshots.
    Audit fixtures must be synthetic and live under
    ``tests/fixtures/synthetic_library``. Source snapshot packaging follows a
    strict runtime-zero policy defined in
    ``src/utils/repository_hygiene.py``.
14. Source-record provider names must be normalized through
    ``normalize_provider_slug()`` and validated by resolved containment before
    any filesystem write. Metadata ``raw_record_path`` must be a POSIX-relative
    path under ``source_records/``.
15. ``PaperLibrary`` is defined only at ``src.catalog_folders.paper_library``. No
    compatibility wrapper or alternate import path should exist.
16. Formalization application entry is ``scripts/formalize_paper_raw.py``
    calling ``src.ingest.formalization`` directly — no facade layer between
    CLI and domain logic.
17. Only Metadata v2.0 and Catalog v3.2 are accepted. No schema conversion,
    old-layout reader, or compatibility fallback may be added to the active
    repository; unsupported inputs are regenerated outside this pipeline.

## 关键词 discovery 双通道契约

Refresh starts a new first-page scan; Backfill resumes the durable notebook
cursor. Provider pages are journaled before cursor CAS. Pending candidates use
leases and DOI/title-resolution locks, and the allocator's final duplicate gate
remains authoritative.

The production data set contains five enabled Chinese keyword notebooks; legacy schema-v3 notebooks must be migrated to schema v4 before discovery can use them.
Inventory and a fixed reviewed source mapping must precede any authorized
apply; the applied transaction must preserve every query, provider generation,
request signature, cursor, generation history, and page journal. Mapping and
plan evidence is operator/runtime state, never active source or snapshot data.
The concurrent discovery `--dry-run` is read-only and must expose those lane
identities together with refresh pages, backfill pages, worker count, and page
budget without provider I/O or cursor/page-journal mutation.

## 未绑定 Backfill 的严格 Pristine 契约

An unbound backfill generation (``request_signature`` is empty) is valid only
when every progress, failure, retry, terminal, and history field is in its
pristine default state.  Any non-pristine unbound state is **corruption** and
must fail closed in every consumer:

- **Schema validator** — ``_validate_backfill_state()`` rejects the notebook
  with ``NotebookCorruptError``.
- **KeywordNotebookStore** — ``ensure_backfill_generation()`` rejects the bind
  and leaves the file untouched (SHA verified).
- **Audit** — reports a ``generation`` error (durable progress, terminal, or
  history) or warning (transient failure/retry only); never counts a
  non-pristine lane as ``pristine_unbound_lanes``.
- **Migration** — ``_is_pristine_backfill()`` returns ``False``, blocking
  merge.
- **Recovery** — inspect-only; reports errors for non-pristine states, never
  suggests a blind signature bind.

The authoritative definition lives in **``src/discovery/backfill_state.py``**
as ``is_strictly_pristine_unbound_backfill()`` and
``describe_nonpristine_unbound_backfill()``.  All consumers import and reuse
these helpers; no module may inline its own partial pristine check.

The system must never repair an unbound non-pristine state by silently binding
a new request signature, resetting the cursor, clearing history, or deleting
page journals.

## Catalog 分类生命周期契约

18. `paper_number` 是 16 位永久机器身份，用于 ledger、assignment、task、result、
    receipt、transaction。
19. `paper_name` 是正式论文目录名 (`data/papers/<paper_name>`)，也是 Catalog
    分类目录中的链接名。内部架构不存在 `paper_id`。
20. DOI keyword notebook 的 `keyword_zh` 是 Catalog 分类目录和 `keyword_id`
    的唯一来源。`search_queries` 同时包含中文和英文搜索 query，全部参与
    OpenAlex/Crossref DOI 搜索；英文 query 永远不创建 Catalog 类别或目录。
    添加或删除英文搜索词不改变 `keyword_id` 或已有分类决定。
21. `assignment` 是分类事实源；Catalog 文件夹链接是 assignment 的可恢复投影。
22. Notebook `enabled=true` 进入 active Registry；`enabled=false` 不进入
    active Registry、不创建目录、不生成 task。缺少 `enabled` 字段 fail closed。
23. 删除 active notebook 时，对应 Category 必须退休、目录受控清理、不再生成
    task、Writer 不再读取。不得出现幽灵分类。
24. Notebook 的 `classification.guidance_zh`、`classification.aliases_zh`、
    `classification.exclusions_zh`、`enabled` 以 notebook 为准；旧 Registry
    不得覆盖 notebook 新值。
25. `definition_hash` 包含 `category_id`、`keyword_zh`、`guidance_zh`、
    `aliases_zh`、`exclusions_zh`、classifier contract version。不包含
    search_queries、provider cursor、updated_at、source path、statistics。
    添加或修改搜索 query 不改变 `definition_hash`，不触发重新分类。
    修改中文分类定义（guidance_zh/aliases_zh/exclusions_zh）才会使对
    应分类决定失效。
26. Classification apply 必须经过事务 journal (`apply_journal/<paper_number>/
    <task_id>.json`)，状态：planned → assignment_written → links_reconciled →
    validated → receipt_written → committed。任一阶段崩溃可恢复。
27. Catalog 迁移必须经过事务 journal (`catalog_keyword_index/<tx_id>.json`)，
    状态：planned → inputs_validated → backup_complete → registry_written →
    directories_reconciled → assignments_migrated → tasks_planned → validated →
    committed。所有校验先于删除，任一阶段失败可恢复或回滚。
28. Doctor 对所有 drift 和未完成事务 fail closed。`errors` 非空时，
    `folder_integrity_safe`、`classification_complete`、`writer_category_safe`
    必须全部为 false。
29. Writer 在非 `all` 分类读取前必须调用 `assert_writer_safe()`，要求
    `writer_category_safe=true` 且 `errors=[]`，否则拒绝执行。
30. 零分类论文（所有 category 均为 `matched=false`）可以完成：存在于 `all/`、
    不在任何分类目录、不在 `_pending/`。
31. 纯 `paper_name` rename 不改变 `paper_number`，assignment 仍归属原
    paper_number。分类 semantic decision 不因纯 rename 自动失效。
32. 禁止 fake classifier 写真实分类；禁止根据候选目录猜测并生成 notebook；
    禁止重置真实 provider cursor。
33. Active discovery notebook 只接受 schema v4；`keyword_zh` 是唯一中文分类
    identity，`search_queries` 同时保存中文和英文 provider query。
34. `enabled=true` 必须同时拥有 active zh/en query；disabled draft 可以暂时
    not-ready。任何 enabled 定义修改若不能保持 ready，必须整笔失败且不写入。
35. Discovery audit 只读并校验 notebook、provider generation/signature、page
    journal、provenance 与 registry 的 identity closure；不能修复或移动运行时文件。
36. Discovery recovery for legacy v3 notebooks 当前只支持 inspect；输出必须绑定
    `keyword_id/query_id/provider/generation/request_signature` 与 page-chain 证明，
    cursor 分叉、signature/generation 冲突或无证明状态必须 fail closed。
37. Network discovery staging 只有
    `pending_queue → stage_network_metadata_records → DiscoveryStageTransaction
    → WorkspaceRegistry` 一条生产路径。Registry 是 DOI/identity 唯一扫描入口，
    refresh 以 copy-on-write 原子发布；未知 workflow profile、损坏 ledger 或刷新
    失败均在分配前 fail closed。Allocator 不承载 discovery 业务，pending queue
    不作 workspace reuse/duplicate/allocation 决策，旧索引磁盘刷新无 fallback。
38. `reserved` 缺失证据仍是 unsettled；`metadata_staged` 缺失 metadata、source
    record、receipt、stage manifest、import status 或 marker 必须在分配前返回
    `repair_required`。
39. Discovery identity 的唯一实体键是 `identity_key + paper_number`，同一编号可
    保存多个 provider identity，freeze 不得丢失。Registry refresh 原子发布；
    Transaction 每 candidate 复用一次锁内 ledger load，新 staging 保留 reserved
    与 metadata_staged 两个 durable save，成功后直接 publish、无 post-refresh。
40. Enabled discovery notebook 必须绑定非 sentinel、taxonomy-resolved relevance
    profile；missing/legacy/profile-unbound 在 provider I/O 前 fail closed。Provider
    lane generation 只属于 cursor/page，不得充当 relevance generation。
41. Profile apply 以 `keyword_id`、request-signature profile hash、candidate
    relevance profile hash 及 query/provider/lane identity 识别旧观察，关闭旧
    `profile_unbound/passed/verification_deferred` 后才提交新 profile/generation。
    Drain index 与 durable claim 都必须复核 active profile hash。
42. Relevance-profile transaction root 同时只能存在一个 `state=applying` journal；
    新 plan 必须拒绝并要求恢复旧事务。Discovery 与 profile apply 共享排他锁。
43. Candidate lifecycle 分类只能由 page-journal classifier 提供。仅
    `pending/resolution_pending/ready` 可由 profile transaction 关闭；
    `processing/failed_retryable` 必须先按 Discovery recovery 对账；completed
    terminal lifecycle、relevance、DOI evidence 与 receipts 永不改写。未知
    lifecycle 使整个 plan 不可 apply。
44. Profile apply 的 Phase A 除固定 lock 行为外零写入，并验证 exact
    before/expected-after bytes。时间、transaction ID、typed reason 与 mutation ID
    均在 plan 固定；apply/resume 不得重新生成。
45. Durable DOI projection 只来自 `staged/emitted/existing_duplicate/
    duplicate_observation`，与当前 profile 解耦。Batch runtime 持有不可变的完整
    active-profile mapping，forced rebuild 必须显式复用同一 mapping。
46. A/B/C 先冻结一次共享宽召回 corpus，再离线 replay；manifest 同时绑定
    sampling/replay profiles 与文件 hash/size/count。Replay 不度量不同 provider
    request filter/sort 的召回率，人工 Precision 不得推断。

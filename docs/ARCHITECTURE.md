# Architecture

MinerU v2 使用单一入库工作区和单一正式目录。

## Data Flow（网络 metadata 路径概览）

```text
network metadata (with DOI)
-> paper_raw source folder
-> PDF fetch (by DOI)
-> MinerU conversion
-> curation（catalog_ready）
-> formalize（改名 + reserve paper_number + ready_for_commit）
-> formal paper folder
-> all catalog rebuild
```

Network metadata path: metadata arrives with a DOI already assigned; PDF fetch follows
from that DOI; conversion follows PDF staging. 手动 PDF 路径见下方 "Manual PDF ingest
order"（先转换，再从 md 解析 metadata）。

## Manual PDF ingest order

```text
1. Put PDF into data/raw/ (manual PDF queue).
2. Run stage_raw_pdfs_to_paper_raw.py --move --apply to consume the queue and allocate data/paper_raw/<raw_id>/.
3. Run `convert_paper_raw_gpu.py --all --apply` for formal MinerU conversion (produces <raw_id>.md).
4. Run paper_raw_metadata_resolver:
   - read converted <raw_id>.md,
   - extract DOI/title/authors/year/venue candidates,
   - verify online (or search online when md lacks enough candidates),
   - produce a schema-compatible metadata patch / resolved candidates.
5. Run paper_raw_catalog_curator:
   - read converted <raw_id>.md,
   - produce a content-only catalog (v2.0).
   `curate_paper_raw.py --apply` validates metadata/catalog and writes `.import_status.json status=catalog_ready`; it does NOT rename the folder or allocate a paper_number.
6. Run `formalize_paper_raw.py --all-ready --apply`: in `data/paper_raw` rename folder/files to `<paper_id>`, reserve a 16-digit `paper_number`, backfill catalog links, write `<paper_id>.formalization.json` + `<16位>.paper.number`, and set `status=ready_for_commit`.
7. Run `commit_paper_raw_to_papers.py --all-ready --apply`: transactional install (final validate → staging copytree → self-check → os.replace → activate ledger → rebuild all.catalog → postcheck → delete source). Only `ready_for_commit` folders are accepted; any post-install failure rolls back and removes `data/papers/<paper_id>`.
8. Rebuild data/catalog/all.catalog.json.
```

Both `paper_raw_metadata_resolver` and `paper_raw_catalog_curator` depend on the
converted MinerU Markdown. The metadata resolver produces bibliographic metadata
(same schema as network-fetched metadata); the catalog curator produces a
content-only catalog (no DOI/authors/year/journal). They may run in parallel
after conversion, but a formal commit to `data/papers/` requires **both** valid
metadata and a valid content catalog — incomplete `paper_raw` folders stay
outside `data/papers/`.

`formalize_paper_raw.py` and `commit_paper_raw_to_papers.py` both expose full
path-isolation flags (`--paper-raw-dir` / `--papers-dir` / `--ledger-path` /
`--all-catalog-path`). Tests and agents MUST pass a tmp `--ledger-path` and tmp
`--all-catalog-path` to avoid polluting the real `data/catalog/`. After
formalize renames `000001` → `<paper_id>`, the reserved ledger entry is
repointed to the renamed `<paper_id>` workspace (via `repoint_reserved`), so at
`ready_for_commit` the ledger matches the real folder. Metadata candidates are
read from the converted Markdown first 100 physical lines.

Formal conversion enters through `scripts/convert_paper_raw_gpu.py`; the lower-level
`convert_paper_raw_batch.py` remains for compatibility/debugging and warns on direct
formal use. The wrapper defaults `MINERU_REQUIRE_GPU=true`, `CUDA_VISIBLE_DEVICES=0`,
and requires both `nvidia-smi` and current-env `torch.cuda.is_available()` to pass.
If using persistent `mineru-api`, start that server with `CUDA_VISIBLE_DEVICES=0` in
its own shell; client-side environment changes cannot alter an already-running API process.
Recommended service control is `python scripts/start_mineru_services.py --wait`
before conversion and `python scripts/stop_mineru_services.py` after conversion.
MinerU conversion has no process-level timeout; health/preflight/lock timeouts
are separate checks. Metadata title/author/affiliation/abstract/keyword/DOI
candidates come from converted Markdown first 100 lines as front-matter evidence
before PDF title fallback.

`data/raw/` is a queue / raw 是待处理队列 for manual PDF imports. Successful
normal staging moves PDFs out of `data/raw/`; copy mode is reserved for
debugging, backup, tests, or explicit one-off inspection.

### `--only-preflight-ready` guidance

- **Network metadata path:** `--only-preflight-ready` is safe — metadata already has DOI
  and the preflight check passes (`ready_for_convert`).
- **Manual PDF bootstrap path:** do NOT use `--only-preflight-ready` on the initial
  conversion. A manual PDF starts with `metadata_match.status = unmatched`, so preflight
  will NOT return `ready_for_convert` and the flag would skip it. Convert first without
  the flag, then resolve metadata from the converted Markdown.

## Components

- `src/services/v2_library.py`: paper_raw allocation, PDF staging, MinerU conversion guard, curation, formal commit, paper_number ledger, all.catalog rebuild, formal asset validation, and metadata-derived BibTeX helpers.
- `src/catalog.py`: read-only all catalog access and compact catalog text.
- `src/library.py`: formal Markdown and image lookup through all catalog.
- `src/server.py`: v2 read/API work surface; it never commits directly to `data/papers/`.
- `scripts/*paper_raw*.py`: the only formal import CLIs.
- `src/bib.py`: per-paper BibTeX via `bibtex_from_metadata` from `metadata.json` (no global `references.bib`); `src/writer/bib_manager.py` exports a per-job `tex/references.bib`.
- `scripts/create_write_job.py` / `scripts/prepare_write_article_workdir.py`: create a catalog-first writing job and copy selected formal paper folders to `write/jobs/<job_id>/article/<paper_number>/`.

## Writing boundary

- `skills/catalog_tex_writer` is the only default article-writing skill.
- `skills/paper_raw_metadata_resolver` and `skills/paper_raw_catalog_curator` are ingest-side support skills, not article-writing skills.
- `skills/literature_library_manager` is a library / ingest management skill; it may describe how to create a writing job, but it is not the article-writing skill.
- `scripts/write_review.py` / `src/writer/*` is an advanced / experimental multi-stage writer workflow, not the default entry and not legacy.
- All active writing workflows must use `write/jobs/<job_id>/article/<paper_number>/` as the only copied article workspace.
- Active writing workflows must not read from or write to the legacy llm work directory; it is forbidden.

## Facts

- `data/paper_raw/` is the pre-ingest workspace.
- `data/papers/` is the only formal asset storage.
- `data/catalog/all.catalog.json` is the local generated content-only catalog index (per-paper catalog schema v2.0; no bibliographic metadata); the repository commits `all.catalog.template.json` instead of real local state.
- catalog natural-language values default to Chinese for Chinese search, classification, paper selection, and writing workflows; JSON keys/schema enums stay English, and technical terms may remain English. Metadata remains the original/canonical bibliographic source.
- `data/catalog/paper_index.json` is the local generated paper_number → asset path index (metadata/catalog/markdown/pdf/images), no bibliographic fields; the repository commits `paper_index.template.json`.
- `data/catalog/paper_number_ledger.json` owns local long-term numbering; the repository commits `paper_number_ledger.template.json`, not the real ledger.
- `write/jobs/<job_id>/article/<paper_number>/` is the only model-facing copied article workspace.
- Network/search metadata records must carry DOI before they can be staged into `paper_raw`.
- Manual PDF records may start without DOI, but curation and formal commit require `metadata.identifiers.doi`.
- Formal library metadata completeness is enforced in `src/services/v2_library.py` and `scripts/validate_v2_library.py`; incomplete `paper_raw` folders stay outside `data/papers/`.
- Bibliography helpers read only metadata fields, never catalog summaries or converted Markdown text.

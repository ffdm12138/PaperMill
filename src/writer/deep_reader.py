"""全文精读：从 write/jobs/<job_id>/article/ 读取全文生成精读笔记模板 + 证据表 + 候选图。

默认要求先运行 ``prepare-workset``（产出 ``workset_manifest.json``），
从 ``write/jobs/<job_id>/article/<paper_number>/`` 读取全文与图片，
不直接读取 ``data/papers/``。

状态语义：
  deep_read() 只设置 deep_read_prompt_generated=True，不设置 deep_read_notes_filled。
  mark_deep_reading_filled() 校验笔记非空模板后设置 deep_read_notes_filled=True。

前置：selected_papers.json 必须 selection_status=confirmed。
"""
import json
import re
import shutil
from pathlib import Path

from src.writer.job_manager import JobManager
from src.catalog_folders.reader import CatalogFolderReader, create_safe_catalog_reader
from src.naming import safe_child, validate_paper_name
from src.writer.catalog_matcher import load_selected, selected_paper_names
from src.writer.bib_manager import job_local_bib_keys, resolve_work_dir
from src.writer.safe_write import TODO_MARKERS, write_text_safely
from config.settings import PAPERS_DIR, PAPER_MD_MAX_CHARS
from src import bib as bibmod

_WORKSET_MANIFEST = "planning/workset_manifest.json"


NOTE_TEMPLATE = """# {pid}

## Citation
\\cite{{{bib_key}}}

## Why this paper is selected
（待填）

## Research problem
（待填）

## Method
（待填）

## Key equations / parameterizations
（待填）

## Key figures
（待填）

## Main findings
（待填）

## Limitations
（待填）

## How it supports my story
（待填）

## Directly usable sentences or ideas
（待填）

## Evidence extracted from full text
（待填，标注正式 Markdown 中可定位的位置）

## Figure candidates
（待填：列出 images/ 中值得引用的图及理由）
"""


def _figure_candidates_block(pid: str, library) -> str:
    imgs = library.list_images(pid)
    if not imgs:
        return f"- {pid}: 无图片\n"
    lines = [f"- {pid}: {len(imgs)} 张图，候选："]
    for name in imgs[:10]:
        lines.append(f"  - `{name}` ——（待填图意与是否引用）")
    return "\n".join(lines) + "\n"


def _load_workset(job_id: str, jm: JobManager, jdir: Path) -> tuple[dict | None, dict[str, str]]:
    """Load workset_manifest.json and build paper_name → work_dir mapping.

    Returns (manifest, pid_to_work_dir).  manifest is None if not prepared.
    ``work_dir`` values are resolved against the job root (relative or absolute),
    so the manifest stays portable.
    """
    wp = jdir / _WORKSET_MANIFEST
    if not wp.exists():
        return None, {}
    manifest = json.loads(wp.read_text(encoding="utf-8"))
    mapping: dict[str, str] = {}
    for entry in manifest.get("copied", []):
        pid = entry.get("paper_name", "")
        wd = entry.get("work_dir", "")
        if pid and wd:
            mapping[pid] = str(resolve_work_dir(jdir, wd))
    return manifest, mapping


class _WorksetLibrary:
    """Minimal PaperLibrary-like facade over copied article directories."""
    def __init__(self, pid_to_dir: dict[str, str], max_chars: int = PAPER_MD_MAX_CHARS):
        by_id: dict[str, Path] = {}
        for pid, rel in pid_to_dir.items():
            by_id[pid] = Path(rel)
        self._by_id = by_id
        self._max_chars = max_chars

    def exists(self, pid: str) -> bool:
        pdir = self._by_id.get(pid)
        return pdir is not None and (pdir / f"{pid}.md").exists()

    def list_images(self, pid: str) -> list[str]:
        pdir = self._by_id.get(pid)
        if pdir is None:
            return []
        img_dir = pdir / "images"
        if not img_dir.is_dir():
            return []
        return [str(p.relative_to(pdir)) for p in img_dir.iterdir() if p.is_file()]

    def read_multiple(self, pids: list[str], _dummy=None) -> dict[str, str]:
        out: dict[str, str] = {}
        for pid in pids:
            pdir = self._by_id.get(pid)
            if pdir is None:
                continue
            md = pdir / f"{pid}.md"
            if md.exists():
                text = md.read_text(encoding="utf-8")
                out[pid] = text[:self._max_chars] if self._max_chars else text
        return out


_FORBIDDEN_SOURCE_TOKENS = ("/data/raw", "/data/paper_raw", "/data/llm_work")


def _is_forbidden_source(path: Path) -> bool:
    """article 副本来源必须是正式 papers 目录，禁止 raw/paper_raw/llm_work。

    这是显式的安全防护（防止用户手工传入旧 data/llm_work 目录），
    不是旧 workflow 入口；token 直接以字面量出现以便防回流扫描识别。
    """
    rel = path.resolve().as_posix().lower()
    return any(rel.endswith(t) or f"{t}/" in rel for t in _FORBIDDEN_SOURCE_TOKENS)


def prepare_workset(job_id: str, jm: JobManager | None = None,
                    overwrite: bool = False,
                    papers_dir: Path = PAPERS_DIR,
                    catalog: CatalogFolderReader | None = None,
                    apply: bool = True) -> dict:
    """Plan or copy selected papers into write/jobs/<job_id>/article/<paper_number>/."""
    jm = jm or JobManager()
    catalog = catalog or create_safe_catalog_reader()
    sel = load_selected(job_id, jm)
    if sel.get("selection_status") != "confirmed":
        raise RuntimeError("selected_papers.json is not confirmed，拒绝复制 workset")

    jdir = jm.job_dir(job_id)
    article_dir = safe_child(jdir, "article")

    copied: list[dict] = []
    skipped: list[dict] = []
    for item in sel.get("selected_papers", []):
        pid = item.get("paper_name", "")
        entry = catalog.get(pid)
        if entry is None:
            skipped.append({"paper_name": pid, "reason": "not_in_catalog"})
            continue
        number = str(entry.get("paper_number", "") or "")
        if not number:
            skipped.append({"paper_name": pid, "reason": "no_paper_number"})
            continue
        try:
            source = Path(str(entry["formal_directory"]))
        except (KeyError, ValueError):
            skipped.append({"paper_name":pid,"paper_number":number,"reason":"formal_registry_resolution_failed"})
            continue
        if _is_forbidden_source(source) or not source.exists():
            skipped.append({"paper_name": pid, "paper_number": number,
                            "reason": "formal_folder_missing_or_forbidden"})
            continue
        target = safe_child(article_dir, number)
        if target.exists():
            if not overwrite:
                skipped.append({"paper_name": pid, "paper_number": number, "reason": "exists"})
                continue
            if apply:
                shutil.rmtree(target)
        if apply:
            article_dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target)
            status = "copied"
        else:
            status = "planned"
        # work_dir is job-relative (article/<paper_number>) so the manifest stays
        # portable; readers resolve it against the job root (see resolve_work_dir).
        try:
            rel_work_dir = target.relative_to(jdir).as_posix()
        except ValueError:
            rel_work_dir = str(target)
        copied.append({"paper_name": pid, "paper_number": number,
                        "work_dir": rel_work_dir, "status": status})

    manifest = {
        "job_id": job_id,
        "dry_run": not apply,
        "copied": copied,
        "skipped": skipped,
        "work_root": "write/jobs/<job_id>/article/",
    }
    if apply:
        manifest_path = jdir / "planning" / "workset_manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    return manifest


def deep_read(job_id: str, paper_names: list[str] | None = None,
              force: bool = False,
              jm: JobManager | None = None,
              catalog: CatalogFolderReader | None = None) -> dict:
    """对 selected_papers 生成精读笔记模板 + 证据表 + 候选图 + 精读 prompt。

    默认从 write/jobs/<job_id>/article/<paper_number>/ 读取全文与图片
    （要求先运行 prepare-workset --apply）。

    前置：catalog_selection_confirmed=True。
    paper_names 为 None 时取 selected_papers.json 中已确认的列表。
    """
    jm = jm or JobManager()
    # ``catalog`` is retained in the signature for callers/tests but no longer
    # used here: cite keys now come from job-local article metadata.
    jdir = jm.job_dir(job_id)
    meta = jm.load_meta(job_id) or {}
    if meta.get("steps", {}).get("deep_read_notes_filled") and not force:
        raise RuntimeError("deep-read notes are already marked filled; pass force=True to overwrite with backups.")

    # 前置校验：必须已确认 selected
    sel = load_selected(job_id, jm)
    if sel.get("selection_status") != "confirmed":
        raise RuntimeError(
            "Cannot deep-read before selected_papers.json is confirmed. "
            "Run confirm-papers first.")
    if paper_names is None:
        paper_names = [p["paper_name"] for p in sel.get("selected_papers", [])]
    if not paper_names:
        raise RuntimeError("selected_papers 为空，无可精读文献。")
    for pid in paper_names:
        try:
            validate_paper_name(pid)
        except ValueError as e:
            raise RuntimeError(f"Invalid paper_name: {pid!r} — {e}")

    # Resolve reading source from the prepared job-local article workset.
    manifest, pid_to_dir = _load_workset(job_id, jm, jdir)
    if manifest is None:
        raise RuntimeError(
            "workset_manifest.json not found. "
            "Run `scripts/create_write_job.py --paper-nums ... --apply` first.")
    missing = [pid for pid in paper_names if pid not in pid_to_dir]
    if missing:
        raise RuntimeError(
            f"workset missing papers: {missing}. "
            "Re-run prepare-workset to copy them into write/jobs/<job_id>/article/.")
    source_label = f"write/jobs/{job_id}/article"
    lib = _WorksetLibrary(pid_to_dir)
    for pid in paper_names:
        if not lib.exists(pid):
            raise RuntimeError(f"找不到 Markdown ({source_label}): {pid}")

    notes_dir = jdir / "reading" / "paper_notes"
    notes_dir.mkdir(parents=True, exist_ok=True)

    # Cite keys come from the job-local copied article metadata, not the global
    # catalog — they must match export_job_bib / copy_figures exactly.
    bib_key_of = job_local_bib_keys(pid_to_dir)
    full_texts = lib.read_multiple(paper_names)

    created_notes = []
    write_results = []
    fig_lines = []
    for pid in paper_names:
        bib = bib_key_of.get(pid, "")
        note_path = notes_dir / f"{pid}.md"
        wr = write_text_safely(
            note_path,
            NOTE_TEMPLATE.format(pid=pid, bib_key=bib),
            force=force,
        )
        if not wr["written"]:
            raise RuntimeError(f"refuse to overwrite user-filled note: {note_path}")
        write_results.append(wr)
        created_notes.append(str(note_path))
        fig_lines.append(_figure_candidates_block(pid, lib))

    ev = jdir / "reading" / "evidence_table.md"
    ev_wr = write_text_safely(ev,
        "# 证据表\n\n"
        "| Claim | Supporting paper | Bib key | Evidence location | Use in thesis |\n"
        "|---|---|---|---|---|\n"
        "| （待填） | | | | |\n",
        force=force,
    )
    if not ev_wr["written"]:
        raise RuntimeError(f"refuse to overwrite user-filled evidence table: {ev}")
    write_results.append(ev_wr)

    fc = jdir / "reading" / "figure_candidates.md"
    fc_wr = write_text_safely(
        fc,
        "# 候选引用图片清单\n\n> 由 deep_reader 自动列出图片，图意待填。\n\n"
        + "".join(fig_lines),
        force=force,
    )
    if not fc_wr["written"]:
        raise RuntimeError(f"refuse to overwrite user-filled figure candidates: {fc}")
    write_results.append(fc_wr)

    fulltext_block = ""
    for pid in paper_names:
        md = full_texts.get(pid, "(读取失败)")
        bib = bib_key_of.get(pid, "")
        fulltext_block += f"\n\n## [{pid}]  \\cite{{{bib}}}\n\n{md}\n"

    prompt = f"""请对下面若干篇文献全文逐一精读，为每篇产出结构化笔记，并整合一份证据表。

每篇笔记字段：
Why this paper is selected / Research problem / Method / Key equations / Key figures /
Main findings / Limitations / How it supports my story / Directly usable sentences /
Evidence extracted from full text（标注可定位位置）/ Figure candidates（从图片清单中选）。

证据表按 `| Claim | Supporting paper | Bib key | Evidence location | Use in thesis |` 输出。
所有结论必须能在全文中找到证据，禁止编造。
# 文献全文
{fulltext_block}
"""
    prompt_path = jdir / "logs" / "prompts" / "02_deep_reading_prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")

    jm.set_step(job_id, "deep_read_prompt_generated", True)
    return {
        "prompt": prompt,
        "prompt_path": str(prompt_path),
        "notes": created_notes,
        "evidence_table": str(ev),
        "figure_candidates": str(fc),
        "writes": write_results,
        "notes_filled": False,
        "source": source_label,
    }


def _section_filled(text: str) -> bool:
    """判断一段笔记文本是否已被实质填充（非纯模板）"""
    # 去掉标题行后，正文里去掉待填标记，看是否还有实质内容
    body = re.sub(r"^#.*$", "", text, flags=re.MULTILINE)
    body = re.sub(r"^\|.*\|$", "", body, flags=re.MULTILINE)  # 去表头/占位行
    for marker in TODO_MARKERS:
        body = body.replace(marker, "")
    # 剩余非空白字符数
    remaining = len(re.sub(r"\s+", "", body))
    return remaining >= 20


def validate_deep_reading_notes(job_id: str, jm: JobManager | None = None) -> list[str]:
    """校验精读笔记是否已被实质填充。返回错误列表（空=通过）"""
    jm = jm or JobManager()
    jdir = jm.job_dir(job_id)
    errors = []
    pids = selected_paper_names(job_id, jm)
    if not pids:
        return ["selected_papers 未确认或为空，无法校验精读笔记"]

    notes_dir = jdir / "reading" / "paper_notes"
    for pid in pids:
        np = notes_dir / f"{pid}.md"
        if not np.exists():
            errors.append(f"缺少精读笔记: reading/paper_notes/{pid}.md")
            continue
        if not _section_filled(np.read_text(encoding="utf-8")):
            errors.append(f"精读笔记仍为模板/待填: {pid}.md")

    # 证据表至少一条有效 claim
    ev = jdir / "reading" / "evidence_table.md"
    if ev.exists():
        ev_text = ev.read_text(encoding="utf-8")
        # 有效行：表格数据行，含 paper_name 或 bib_key 标识，非占位
        valid_rows = [ln for ln in ev_text.splitlines()
                      if ln.startswith("| ") and "待填" not in ln
                      and ln.count("|") >= 5 and not ln.startswith("| Claim")]
        # 去掉表头分隔行
        valid_rows = [r for r in valid_rows if not re.match(r"^\|\s*[-:|]+\s*\|", r)]
        if not valid_rows:
            errors.append("evidence_table.md 无有效 claim（至少一条含 paper_name/bib_key 的数据行）")
        else:
            # 检查证据表覆盖率：选中的 paper_name 至少 70% 出现在 evidence 行中
            referenced_pids = set()
            for row in valid_rows:
                for pid in pids:
                    if pid in row:
                        referenced_pids.add(pid)
            if len(referenced_pids) < max(1, len(pids) * 0.7):
                missing = set(pids) - referenced_pids
                errors.append(f"evidence_table 覆盖不足（{len(referenced_pids)}/{len(pids)} 篇），"
                              f"缺少: {sorted(missing)}")
    else:
        errors.append("缺少 reading/evidence_table.md")
    return errors


def mark_deep_reading_filled(job_id: str, jm: JobManager | None = None) -> dict:
    """校验精读笔记通过后设置 deep_read_notes_filled=True"""
    jm = jm or JobManager()
    errors = validate_deep_reading_notes(job_id, jm)
    if errors:
        return {"filled": False, "errors": errors}
    jm.set_step(job_id, "deep_read_notes_filled", True)
    return {"filled": True, "errors": []}

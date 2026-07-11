"""科研故事线构建：基于精读笔记 + 证据表组织博士论文级故事线

状态语义：
  build_story() 要求 deep_read_notes_filled=True，只设置 story_prompt_generated，
  不设置 story_plan_filled。
  mark_story_filled() 校验 story_plan 非模板后设置 story_plan_filled=True。
"""
import json
import re
from pathlib import Path

from src.writer.job_manager import JobManager
from src.writer.catalog_matcher import load_selected, selected_paper_ids
from src.writer.safe_write import write_text_safely
from src.writer.bib_manager import load_workset_manifest, job_local_bib_keys


def _job_local_catalog_cards(pid_to_work_dir: dict[str, Path]) -> dict[str, dict]:
    """Build writer cards from copied Catalog v3.2 documents.

    Keeps build_story job-local: content summaries come from the copied article
    Catalog, never a merged global document.
    """
    out: dict[str, dict] = {}
    for pid, wd in pid_to_work_dir.items():
        cat_path = Path(wd) / f"{pid}.catalog.json"
        if cat_path.exists():
            try:
                cat = json.loads(cat_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cat = {}
            identity = cat.get("content_identity") or {}
            abstract = cat.get("abstract") or {}
            methods = cat.get("methods") or {}
            writing = cat.get("writing_value") or {}
            out[pid] = {
                "paper_number": cat.get("paper_number"),
                "content_title_zh": identity.get("content_title_zh"),
                "research_domains": identity.get("research_domains") or [],
                "one_sentence_zh": abstract.get("one_sentence_zh") or "",
                "methods_summary_zh": methods.get("overview_zh") or "",
                "key_findings": [x.get("finding_zh", "") for x in cat.get("key_findings") or []],
                "writing_use_cases": writing.get("use_cases") or [],
                "read_decision": (cat.get("screening") or {}).get("read_decision", "pending"),
                "asset_references": cat.get("figures_and_tables") or [],
            }
        else:
            out[pid] = {}
    return out


TODO_MARKERS = ["TODO", "待填", "（待填）", "TEMPLATE_ONLY", "由大模型补全", "待补全"]

# story_plan 必须包含的章节
STORY_REQUIRED_SECTIONS = [
    "scientific_background", "contradiction_or_gap", "mechanism_chain",
    "literature_logic", "research_entry_point", "introduction_logic", "method_logic",
]

# paper_notes 中优先抽取的 section（## 标题）
NOTE_SECTIONS = [
    "Research problem", "Method", "Key equations / parameterizations",
    "Main findings", "Limitations", "How it supports my story",
    "Evidence extracted from full text", "Figure candidates",
]


def extract_note_sections(note_text: str, sections: list[str] | None = None) -> dict:
    """结构化抽取 paper_notes 各 section 内容。

    返回 {section_title: text}；缺的 section 为空字符串。
    若全部 section 都缺，调用方应 fallback 到截断。
    """
    sections = sections or NOTE_SECTIONS
    out = {s: "" for s in sections}
    # 按 ## 标题切分
    parts = re.split(r"^##\s+(.+)$", note_text, flags=re.MULTILINE)
    # parts 形如 [前导, 标题1, 正文1, 标题2, 正文2, ...]
    for i in range(1, len(parts) - 1, 2):
        title = parts[i].strip()
        body = parts[i + 1].strip()
        for s in sections:
            if s.lower() in title.lower():
                out[s] = body
                break
    return out


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def build_story(job_id: str, force: bool = False, jm: JobManager | None = None,
                catalog: object | None = None) -> dict:
    """生成故事线 prompt + story_plan/chapter_outline 模板。

    前置：deep_read_notes_filled=True。
    """
    jm = jm or JobManager()
    # ``catalog`` is retained in the signature for callers/tests but no longer
    # used here: cite keys and Catalog v3.2 summaries come from the
    # job-local copied article workspace.
    jdir = jm.job_dir(job_id)
    meta = jm.load_meta(job_id) or {}
    if meta.get("steps", {}).get("story_plan_filled") and not force:
        raise RuntimeError("story plan is already marked filled; pass force=True to overwrite with backups.")

    # 前置：精读笔记必须已填
    jm.require_step(job_id, "deep_read_notes_filled", "build-story")

    plan_dir = jdir / "planning"
    read_dir = jdir / "reading"

    norm = _read(jdir / "input" / "normalized_task.md")
    sel = load_selected(job_id, jm)
    sel_json = json_dumps(sel)
    evidence = _read(read_dir / "evidence_table.md")
    fig_cand = _read(read_dir / "figure_candidates.md")

    # 结构化抽取每篇笔记；cite key 与 Catalog 内容均来自 job-local article 副本。
    pid_to_work_dir = load_workset_manifest(job_id, jm)
    bib_map = job_local_bib_keys(pid_to_work_dir)
    catalog_cards = _job_local_catalog_cards(pid_to_work_dir)

    notes_dir = read_dir / "paper_notes"
    notes_summary = ""

    if notes_dir.exists():
        for n in sorted(notes_dir.glob("*.md")):
            pid = n.stem
            text = _read(n)
            secs = extract_note_sections(text)
            notes_summary += f"\n\n### [{pid}]  \\cite{{{bib_map.get(pid,'')}}}\n"
            card = catalog_cards.get(pid, {})
            notes_summary += f"one_sentence: {card.get('one_sentence_zh', '')}\n"
            notes_summary += f"methods: {card.get('methods_summary_zh', '')}\n"
            notes_summary += f"key_findings: {'; '.join(card.get('key_findings') or [])}\n"
            notes_summary += f"writing_use_cases: {'; '.join(card.get('writing_use_cases') or [])}\n"
            for sk, sv in secs.items():
                if sv:
                    notes_summary += f"- **{sk}**: {sv[:600]}\n"

    # story_plan 模板（含必填 section 占位）
    story_plan = plan_dir / "story_plan.md"
    story_wr = write_text_safely(story_plan,
        "# 科研故事线（story_plan）\n\n"
        "> STATUS: TEMPLATE_ONLY —— 请由大模型基于 logs/prompts/03_storyline_prompt.md 填写。\n\n"
        + "\n".join(f"## {s}\n（待填）\n" for s in STORY_REQUIRED_SECTIONS) + "\n",
        force=force,
    )
    if not story_wr["written"]:
        raise RuntimeError(f"refuse to overwrite user-filled story plan: {story_plan}")

    outline = plan_dir / "chapter_outline.md"
    outline_wr = write_text_safely(outline,
        "# 章节大纲（chapter_outline）\n\n"
        "> STATUS: TEMPLATE_ONLY —— 待大模型基于故事线补全。\n\n"
        "## Introduction\n- （待填）\n\n## Method\n- （待填）\n",
        force=force,
    )
    if not outline_wr["written"]:
        raise RuntimeError(f"refuse to overwrite user-filled chapter outline: {outline}")

    prompt = f"""你是博士论文导师。请基于下面的研究任务、精读笔记与证据表，组织博士论文级科研故事线。

要求：
1. 输出必须包含以下 section（用 ## 标题）：{STORY_REQUIRED_SECTIONS}
2. 每步标注支撑文献 bib_key（\\cite{{bib_key}}）；
3. 给出 introduction 与 method 的章节大纲；
4. 禁止编造，强事实必须来自 paper_notes/evidence_table。

# 研究任务
{norm}

# 已确认精读文献（selected_papers.json）
{sel_json}

# 证据表
{evidence}

# 精读笔记结构化摘要
{notes_summary or '(暂无，请先生成精读笔记)'}

# 候选图清单
{fig_cand}
"""
    prompt_path = jdir / "logs" / "prompts" / "03_storyline_prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")

    # 只标记 prompt 已生成
    jm.set_step(job_id, "story_prompt_generated", True)
    return {
        "prompt": prompt,
        "prompt_path": str(prompt_path),
        "story_plan": str(story_plan),
        "chapter_outline": str(outline),
        "writes": [story_wr, outline_wr],
        "plan_filled": False,
    }


def _text_is_filled(text: str, min_chars: int = 20) -> bool:
    """判断文本是否非纯模板"""
    body = text
    for marker in TODO_MARKERS + ["TEMPLATE_ONLY"]:
        body = body.replace(marker, "")
    return len(re.sub(r"\s+", "", body)) >= min_chars


def validate_story_filled(job_id: str, jm: JobManager | None = None) -> list[str]:
    """校验 story_plan 是否已被实质填充。返回错误列表（空=通过）"""
    jm = jm or JobManager()
    jdir = jm.job_dir(job_id)
    errors = []
    sp = jdir / "planning" / "story_plan.md"
    co = jdir / "planning" / "chapter_outline.md"
    if not sp.exists():
        errors.append("缺少 planning/story_plan.md")
    else:
        text = sp.read_text(encoding="utf-8")
        # 必须含所有 section
        for s in STORY_REQUIRED_SECTIONS:
            if s not in text:
                errors.append(f"story_plan.md 缺少 section: {s}")
        # 不能含 TEMPLATE_ONLY 标记
        if "TEMPLATE_ONLY" in text:
            errors.append("story_plan.md 仍是 TEMPLATE_ONLY 模板")
        # 每个 section 必须非待填
        for s in STORY_REQUIRED_SECTIONS:
            m = re.search(rf"##\s*{re.escape(s)}\s*\n(.+?)(?=^##|\Z)", text,
                          re.MULTILINE | re.DOTALL)
            if m and not _text_is_filled(m.group(1)):
                errors.append(f"story_plan.md section '{s}' 仍为待填")
    if not co.exists():
        errors.append("缺少 planning/chapter_outline.md")
    return errors


def mark_story_filled(job_id: str, jm: JobManager | None = None) -> dict:
    jm = jm or JobManager()
    errors = validate_story_filled(job_id, jm)
    if errors:
        return {"filled": False, "errors": errors}
    jm.set_step(job_id, "story_plan_filled", True)
    return {"filled": True, "errors": []}


# 延迟导入避免循环
def json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, indent=2)

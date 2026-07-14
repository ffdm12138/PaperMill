"""LaTeX 项目生成：main.tex + introduction.tex + method.tex + references.bib

状态语义：
  build_tex() 默认要求 story_plan_filled=True，生成模板后只设置
  tex_template_generated + bib_exported，不设置 tex_content_filled。
  mark_tex_content_filled() 校验正文非模板后设置 tex_content_filled=True。

覆盖保护：write_text_safely() 默认不覆盖已有文件，force=True 时先备份再覆盖。
"""
import re
from pathlib import Path

from src.writer.job_manager import JobManager
from src.writer.bib_manager import export_job_bib, validate_job_citations, load_workset_manifest, job_local_bib_keys
from src.writer.catalog_matcher import load_selected, selected_paper_names
from src.writer.story_builder import extract_note_sections
from src.writer.safe_write import write_text_safely

TODO_MARKERS = ["TODO", "待填", "（待填）", "TEMPLATE_ONLY", "由大模型补全", "待补全"]
MIN_TEX_CHARS = 120  # 正文去空白后最小字符数（与 validate_write_job 一致）

MAIN_TEX = r"""\documentclass[12pt,a4paper]{ctexart}

\usepackage{graphicx}
\usepackage{amsmath}
\usepackage{amssymb}
\usepackage{natbib}
\usepackage{geometry}
\usepackage{hyperref}

\geometry{margin=2.5cm}

\title{__TITLE__}
\author{}
\date{}

\begin{document}

\maketitle

\input{sections/introduction}
\input{sections/method}

\bibliographystyle{plainnat}
\bibliography{references}

\end{document}
"""

INTRO_TEX = r"""% STATUS: TEMPLATE_ONLY
% 请由大模型或人工根据 logs/prompts/04_tex_writing_prompt.md 填写正文。

% Introduction —— 博士论文标准
% 结构：宏观重要性 → 气候/工程/灾害/模型意义 → 已有认识 →
%      实验/观测/理论/模型各自解决什么 → 关键机制链条 → 当前方法不足 →
%      本研究切入点 → 研究目标与贡献
% 所有引用用 \cite{bib_key}，禁止裸作者年份，禁止编造。

\section{Introduction}

% 1. 研究对象的宏观重要性
（待填）

% 2. 在气候、工程、灾害、模型或观测中的意义
（待填）

% 3. 已有研究如何认识该问题
（待填）

% 4. 已有实验/观测/理论/模型分别解决了什么
（待填）

% 5. 关键机制链条
（待填）

% 6. 当前方法的不足
（待填）

% 7. 本研究的切入点
（待填）

% 8. 本章/本文的研究目标和贡献
（待填）
"""

METHOD_TEX = r"""% STATUS: TEMPLATE_ONLY
% 请由大模型或人工根据 logs/prompts/04_tex_writing_prompt.md 填写正文。

% Method —— 综述型方法
% 结构：文献筛选原则 → 分类框架 → 机制分析框架 →
%       模型比较框架 → 证据整合方法 → 图表公式整理原则
% 所有引用用 \cite{bib_key}，禁止裸作者年份，禁止编造。

\section{Method}

% 1. 文献筛选原则
（待填）

% 2. 分类框架
（待填）

% 3. 机制分析框架
（待填）

% 4. 模型比较框架
（待填）

% 5. 证据整合方法
（待填）

% 6. 图表和公式整理原则
（待填）
"""


def latex_escape(s: str) -> str:
    """转义 LaTeX 特殊字符，防止编译失败"""
    return (s.replace("\\", r"\textbackslash{}")
             .replace("&", r"\&")
             .replace("%", r"\%")
             .replace("$", r"\$")
             .replace("#", r"\#")
             .replace("_", r"\_")
             .replace("{", r"\{")
             .replace("}", r"\}"))


def build_tex(job_id: str, title: str | None = None,
              force: bool = False, template_only: bool = False,
              jm: JobManager | None = None) -> dict:
    """生成 LaTeX 项目 + references.bib + 写作 prompt。

    默认要求 story_plan_filled=True；template_only=True 可在未完成前序时生成空模板。
    覆盖保护：force=False 时已有文件跳过；force=True 先备份。

    完全 job-local：references.bib 与 notes summary 的 cite key 均来自
    ``article/<paper_number>/*.metadata.json``，不访问全局 Catalog / PaperLibrary。
    """
    jm = jm or JobManager()
    jdir = jm.job_dir(job_id)
    tex_dir = jdir / "tex"
    (tex_dir / "sections").mkdir(parents=True, exist_ok=True)

    if not template_only:
        # 正式生成要求前序内容完成
        jm.require_step(job_id, "catalog_selection_confirmed", "build-tex")
        jm.require_step(job_id, "deep_read_notes_filled", "build-tex")
        jm.require_step(job_id, "story_plan_filled", "build-tex")

    meta = jm.load_meta(job_id) or {}
    title = latex_escape(title or meta.get("topic", job_id))

    writes = {
        "main.tex": write_text_safely(tex_dir / "main.tex",
                                      MAIN_TEX.replace("__TITLE__", title), force=force),
        "introduction.tex": write_text_safely(tex_dir / "sections" / "introduction.tex",
                                              INTRO_TEX, force=force),
        "method.tex": write_text_safely(tex_dir / "sections" / "method.tex",
                                        METHOD_TEX, force=force),
    }

    # references.bib is generated from job-local article metadata for the
    # selected papers (requires prepare-workset --apply). In template-only mode
    # we tolerate a missing workset so the empty-template escape hatch still
    # works before the pipeline has been run.
    try:
        bib_keys = _selected_bib_keys(job_id, jdir, jm=jm)
        bib_info = export_job_bib(job_id, bib_keys, jm=jm)
    except RuntimeError:
        if not template_only:
            raise
        bib_keys = []
        bib_path = tex_dir / "references.bib"
        bib_path.write_text(
            "% references.bib: workset not prepared yet (template-only mode).\n"
            "% Run prepare-workset --apply, then re-run tex to populate it.\n",
            encoding="utf-8")
        bib_info = {"references_bib": str(bib_path), "count": 0}

    # 写作 prompt（含完整证据链）
    norm = _read(jdir / "input" / "normalized_task.md")
    story = _read(jdir / "planning" / "story_plan.md")
    outline = _read(jdir / "planning" / "chapter_outline.md")
    evidence = _read(jdir / "reading" / "evidence_table.md")
    sel_json = _read_json(jdir / "planning" / "selected_papers.json")
    fig_cand = _read(jdir / "reading" / "figure_candidates.md")
    notes_summary = _build_notes_summary(job_id, jdir, jm=jm)

    prompt = f"""请基于下面的研究任务、故事线与精读证据，撰写博士论文标准的 introduction.tex 与 method.tex。

要求：
1. introduction 按 8 步结构（宏观重要性→意义→已有认识→各方解决什么→机制链条→不足→切入点→目标贡献），有矛盾递进；
2. method 按综述型结构（筛选原则→分类→机制分析→模型比较→证据整合→图表公式整理）；
3. 所有引用用 \\cite{{bib_key}}，可用 key：{bib_keys}；
4. 禁止裸作者年份、禁止编造文献/图号/公式/DOI；
5. 强事实必须来自 paper_notes/evidence_table；
6. 填完后把文件顶部 `% STATUS: TEMPLATE_ONLY` 改为 `% STATUS: CONTENT_FILLED`；
7. 输出两段完整 LaTeX 正文（不含 \\section）。

# 研究任务
{norm}

# 故事线（story_plan.md）
{story}

# 章节大纲（chapter_outline.md）
{outline}

# 证据表（evidence_table.md）
{evidence}

# 精读笔记摘要
{notes_summary}

# 已选文献（selected_papers.json）
{sel_json}

# 候选图清单
{fig_cand}
"""
    prompt_path = jdir / "logs" / "prompts" / "04_tex_writing_prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")

    jm.set_step(job_id, "tex_template_generated", True)
    if not template_only:
        jm.set_step(job_id, "bib_exported", True, extra={"used_bib_keys": bib_keys})
    return {
        "writes": writes,
        "main_tex": str(tex_dir / "main.tex"),
        "introduction_tex": str(tex_dir / "sections" / "introduction.tex"),
        "method_tex": str(tex_dir / "sections" / "method.tex"),
        "references_bib": bib_info["references_bib"],
        "bib_count": bib_info["count"],
        "prompt": prompt,
        "prompt_path": str(prompt_path),
        "content_filled": False,
    }


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _read_json(p: Path) -> str:
    return _read(p) if p.exists() else "{}"


def _build_notes_summary(job_id: str, jdir: Path, jm: JobManager | None = None) -> str:
    notes_dir = jdir / "reading" / "paper_notes"
    if not notes_dir.exists():
        return ""
    try:
        bib_map = job_local_bib_keys(load_workset_manifest(job_id, jm))
    except RuntimeError:
        bib_map = {}
    out = ""
    for n in sorted(notes_dir.glob("*.md")):
        pid = n.stem
        secs = extract_note_sections(_read(n))
        out += f"\n### [{pid}] \\cite{{{bib_map.get(pid,'')}}}\n"
        for sk, sv in secs.items():
            if sv:
                out += f"- **{sk}**: {sv[:600]}\n"
    return out


def _selected_bib_keys(job_id: str, jdir: Path,
                       jm: JobManager | None = None) -> list[str]:
    """从 selected_papers.json（已确认）取 bib_key 列表。

    Keys are derived from the job-local copied article metadata via
    ``job_local_bib_keys`` (no global catalog), so the prompt's available-key
    list matches the keys ``export_job_bib`` writes into ``references.bib``.
    """
    sel = load_selected(job_id, jm)  # 传 jm，不从默认目录错读
    pid_to_work_dir = load_workset_manifest(job_id, jm)
    bib_map = job_local_bib_keys(pid_to_work_dir)
    keys = []
    for it in sel.get("selected_papers", []):
        bk = bib_map.get(it.get("paper_name", ""), "")
        if bk:
            keys.append(bk)
    return keys


def _tex_is_content(text: str) -> bool:
    """判断 tex 是否已填正文（非 TEMPLATE_ONLY 模板）"""
    if "TEMPLATE_ONLY" in text:
        return False
    # 去掉注释与待填标记后的实质内容长度
    body = re.sub(r"%.*", "", text)
    for marker in TODO_MARKERS:
        body = body.replace(marker, "")
    return len(re.sub(r"\s+", "", body)) >= 200


def validate_tex_content(job_id: str, jm: JobManager | None = None) -> list[str]:
    """校验 introduction/method 正文是否已填。返回错误列表（空=通过）"""
    jm = jm or JobManager()
    jdir = jm.job_dir(job_id)
    errors = []
    cite = validate_job_citations(job_id, jm=jm)

    for name in ["introduction", "method"]:
        p = jdir / "tex" / "sections" / f"{name}.tex"
        if not p.exists():
            errors.append(f"缺少 tex/sections/{name}.tex")
            continue
        text = p.read_text(encoding="utf-8")
        if "TEMPLATE_ONLY" in text:
            errors.append(f"{name}.tex 仍含 TEMPLATE_ONLY 模板标记")
            continue
        if any(m in text for m in ["待填", "待补全", "由大模型补全"]):
            errors.append(f"{name}.tex 仍含待填标记")
            continue
        if len(re.sub(r"\s+", "", re.sub(r"%.*", "", text))) < MIN_TEX_CHARS:
            errors.append(f"{name}.tex 正文字符数不足（阈值 {MIN_TEX_CHARS}）")

    # 至少有 \cite{}
    if not cite["cited_keys"]:
        errors.append("introduction/method 中无任何 \\cite{}")
    # 所有 \cite 在 references.bib 中
    for k in cite["missing_in_bib"]:
        errors.append(f"\\cite{{{k}}} 在 references.bib 中找不到")
    return errors


def mark_tex_content_filled(job_id: str, jm: JobManager | None = None) -> dict:
    jm = jm or JobManager()
    errors = validate_tex_content(job_id, jm)
    if errors:
        return {"filled": False, "errors": errors}
    jm.set_step(job_id, "tex_content_filled", True)
    return {"filled": True, "errors": []}

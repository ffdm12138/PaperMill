"""Validate writing job artifacts without script-level imports."""
from __future__ import annotations

import re
from pathlib import Path

from src.writer.bib_manager import validate_job_citations, validate_job_bib, portability_check
from src.writer.catalog_matcher import load_selected
from src.writer.job_manager import JobManager
from src.writer.safe_write import TODO_MARKERS


MIN_TEX_CHARS = 120


def _has_todo(text: str) -> bool:
    return any(m in text for m in TODO_MARKERS)


def validate_job(job_id: str, jm: JobManager | None = None) -> dict:
    """Validate a writing job and update run_meta validation state."""
    jm = jm or JobManager()
    jdir = jm.job_dir(job_id)
    errors: list[str] = []
    warnings: list[str] = []
    if not jdir.exists():
        return {"valid": False, "errors": [f"任务不存在: {job_id}"], "warnings": [],
                "portable": False, "cited_keys": [], "bib_keys": []}

    must = {
        "logs/run_meta.json": jdir / "logs" / "run_meta.json",
        "input/research_input.md": jdir / "input" / "research_input.md",
        "input/normalized_task.md": jdir / "input" / "normalized_task.md",
        "planning/selected_papers.json": jdir / "planning" / "selected_papers.json",
        "reading/evidence_table.md": jdir / "reading" / "evidence_table.md",
        "planning/story_plan.md": jdir / "planning" / "story_plan.md",
        "planning/chapter_outline.md": jdir / "planning" / "chapter_outline.md",
        "tex/main.tex": jdir / "tex" / "main.tex",
        "tex/sections/introduction.tex": jdir / "tex" / "sections" / "introduction.tex",
        "tex/sections/method.tex": jdir / "tex" / "sections" / "method.tex",
        "tex/references.bib": jdir / "tex" / "references.bib",
    }
    for name, path in must.items():
        if not path.exists():
            errors.append(f"缺少 {name}")

    meta = jm.load_meta(job_id) or {"steps": {}}
    steps = meta.get("steps", {})

    if must["planning/selected_papers.json"].exists():
        sel = load_selected(job_id, jm)
        if sel.get("selection_status") != "confirmed":
            errors.append("selected_papers.json 未确认（selection_status != confirmed）")
        elif not sel.get("selected_papers"):
            errors.append("selected_papers.json confirmed 但 selected_papers 为空")
    for step in ["catalog_selection_confirmed", "deep_read_notes_filled",
                 "story_plan_filled", "tex_template_generated", "tex_content_filled"]:
        if not steps.get(step):
            errors.append(f"run_meta.steps.{step} 不为 True")

    for name in ["introduction", "method"]:
        path = jdir / "tex" / "sections" / f"{name}.tex"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if "TEMPLATE_ONLY" in text:
            errors.append(f"{name}.tex 仍含 TEMPLATE_ONLY 模板标记")
        elif _has_todo(text):
            errors.append(f"{name}.tex 仍含 TODO/待填 标记")
        elif len(re.sub(r"\s+", "", re.sub(r"%.*", "", text))) < MIN_TEX_CHARS:
            errors.append(f"{name}.tex 正文字符数不足 {MIN_TEX_CHARS}")

    cite = validate_job_citations(job_id, jm=jm)
    for key in cite["missing_in_bib"]:
        errors.append(f"\\cite{{{key}}} 在 references.bib 中找不到")
    for key in cite["unused_in_bib"]:
        warnings.append(f"references.bib 中 {key} 未被引用")
    errors += [f"job bib: {e}" for e in validate_job_bib(job_id, jm=jm)]

    for tex in jdir.rglob("*.tex"):
        text = tex.read_text(encoding="utf-8")
        for match in re.finditer(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]*)\}", text):
            ref = match.group(1).strip()
            resolved = (tex.parent / ref).resolve()
            try:
                rel = resolved.relative_to(jdir.resolve())
                if "figures" not in str(rel).replace("\\", "/"):
                    errors.append(f"{tex.name}: \\includegraphics{{{ref}}} 未指向 figures/")
            except ValueError:
                errors.append(f"{tex.name}: \\includegraphics{{{ref}}} 指向 job 目录外")
                continue
            if not resolved.exists():
                errors.append(f"{tex.name}: \\includegraphics{{{ref}}} 文件不存在")
            else:
                parts = Path(ref).parts
                if len(parts) >= 2:
                    pid_dir = parts[-2]
                    readme = jdir / "figures" / pid_dir / "README.md"
                    if not readme.exists():
                        errors.append(f"figures/{pid_dir}/ 缺少 README.md（图源记录）")
                    elif "source_article_image" not in readme.read_text(encoding="utf-8"):
                        errors.append(f"figures/{pid_dir}/README.md 缺少 source_article_image")

    evidence = must["reading/evidence_table.md"]
    if evidence.exists():
        ev_text = evidence.read_text(encoding="utf-8")
        valid_rows = [ln for ln in ev_text.splitlines()
                      if ln.startswith("| ") and "待填" not in ln
                      and ln.count("|") >= 5 and not ln.startswith("| Claim")]
        valid_rows = [r for r in valid_rows if not re.match(r"^\|\s*[-:|]+\s*\|", r)]
        if not valid_rows:
            errors.append("evidence_table.md 无有效 claim")

    story_plan = must["planning/story_plan.md"]
    if story_plan.exists():
        text = story_plan.read_text(encoding="utf-8")
        if "TEMPLATE_ONLY" in text:
            errors.append("story_plan.md 仍是 TEMPLATE_ONLY 模板")
        for sec in ["scientific_background", "contradiction_or_gap", "mechanism_chain",
                    "literature_logic", "research_entry_point",
                    "introduction_logic", "method_logic"]:
            if sec not in text:
                errors.append(f"story_plan.md 缺少 section: {sec}")

    port = portability_check(job_id, jm=jm)
    errors += port["errors"]

    valid = len(errors) == 0
    if valid:
        jm.set_step(job_id, "validated", True)
        jm.set_status(job_id, "validated")
    else:
        jm.set_step(job_id, "validated", False)
    return {"valid": valid, "errors": errors, "warnings": warnings,
            "portable": port["portable"], "portability_note": port["note"],
            "cited_keys": cite["cited_keys"], "bib_keys": cite["bib_keys"]}

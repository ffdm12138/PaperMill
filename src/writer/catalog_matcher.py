"""Catalog matching from deterministic Catalog v3.2 projections."""
from __future__ import annotations

import json

from src.catalog_folders.reader import CatalogFolderReader, create_safe_catalog_reader
from src.utils.naming import validate_paper_name
from src.utils.timestamps import now_iso
from src.utils.atomic_io import atomic_write_json
from src.writer.job_manager import JobManager


AWAITING_REVIEW = "awaiting_llm_or_manual_review"


def load_selected(job_id: str, jm: JobManager | None = None) -> dict:
    jm = jm or JobManager()
    path = jm.job_dir(job_id) / "planning" / "selected_papers.json"
    if not path.exists():
        return {
            "selected_papers": [],
            "selection_status": AWAITING_REVIEW,
            "confirmed_at": None,
            "confirmed_by": None,
            "notes": "",
        }
    return json.loads(path.read_text(encoding="utf-8"))


def selected_paper_names(job_id: str, jm: JobManager | None = None) -> list[str]:
    data = load_selected(job_id, jm)
    if data.get("selection_status") != "confirmed":
        return []
    return [p.get("paper_name") for p in data.get("selected_papers", []) if p.get("paper_name")]


def build_compact_catalog_text(papers: list[dict]) -> str:
    return "\n\n".join(json.dumps(paper, ensure_ascii=False) for paper in papers)


def match_catalog(
    job_id: str,
    jm: JobManager | None = None,
    catalog: CatalogFolderReader | None = None,
    force: bool = False,
    categories: list[str] | None = None,
    category_mode: str = "union",
) -> dict:
    """Generate a catalog matching prompt and empty selected list."""
    jm = jm or JobManager()
    catalog = catalog or create_safe_catalog_reader()
    jdir = jm.job_dir(job_id)
    norm = (jdir / "input" / "normalized_task.md").read_text(encoding="utf-8")

    papers = catalog.list_papers(categories, mode=category_mode)
    compact = "\n\n--- batch ---\n\n".join(build_compact_catalog_text(batch) for batch in catalog.compact_batches(categories, mode=category_mode))

    candidates = []
    for item in papers:
        candidates.append({
            "paper_number": item.get("paper_number"),
            "paper_name": item.get("paper_name"),
            "title": item.get("content_title_zh") or "",
            "catalog_priority": item.get("read_decision"),
            "candidate_reason": "",
            "expected_use": "",
            "need_fulltext": None,
        })

    cand_path = jdir / "planning" / "catalog_candidates.json"
    atomic_write_json(cand_path, {
        "topic_summary": "",
        "core_research_questions": [],
        "candidate_papers": candidates,
        "excluded_papers": [],
        "status": "prompt_generated",
        "match_categories": categories or ["all"], "category_mode": category_mode,
    }, indent=2)

    sel_path = jdir / "planning" / "selected_papers.json"
    if sel_path.exists() and not force:
        existing = load_selected(job_id, jm)
        if existing.get("selection_status") == "confirmed":
            raise RuntimeError("selected_papers.json is confirmed; pass force=True to rebuild.")
    atomic_write_json(sel_path, {
        "selected_papers": [],
        "selection_status": AWAITING_REVIEW,
        "confirmed_at": None,
        "confirmed_by": None,
        "notes": "",
    }, indent=2)

    plan = jdir / "planning" / "reading_plan.md"
    plan.write_text(
        "# 阅读计划\n\n"
        "## 研究任务摘要\n" + norm[:400] + "\n\n"
        "## 候选文献\n\n见 catalog_candidates.json。\n\n"
        "## 最终精读列表\n\n见 selected_papers.json。\n",
        encoding="utf-8",
    )

    prompt = f"""你是一位科研导师。请根据研究任务与 Catalog v3.2 快速投影，判断哪些文献需要全文精读。

要求：
1. 输出 JSON；
2. candidate_papers 给出入选文献、理由、预期用途和 need_fulltext；
3. excluded_papers 给出排除文献和理由；
4. 只基于目录信息判断，不编造目录里没有的文献。

# 研究任务
{norm}

# 文献目录
{compact}
"""
    prompt_path = jdir / "logs" / "prompts" / "01_catalog_matching_prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")

    jm.set_step(job_id, "catalog_match_prompt_generated", True)
    return {
        "prompt": prompt,
        "prompt_path": str(prompt_path),
        "candidates_path": str(cand_path),
        "selected_path": str(sel_path),
        "reading_plan_path": str(plan),
        "selection_confirmed": False,
    }


def confirm_selected_papers(
    job_id: str,
    selected: list[dict],
    confirmed_by: str = "manual",
    jm: JobManager | None = None,
    catalog: CatalogFolderReader | None = None,
) -> dict:
    jm = jm or JobManager()
    catalog = catalog or create_safe_catalog_reader()
    if not selected:
        raise ValueError("selected cannot be empty")

    by_id = {p["paper_name"]: p for p in catalog.list_papers()}
    enriched = []
    paper_names = []
    for item in selected:
        pid = item.get("paper_name")
        if not pid:
            raise ValueError("selected item missing paper_name")
        validate_paper_name(pid)
        entry = by_id.get(pid, {})
        enriched.append({
            "paper_number": entry.get("paper_number") or item.get("paper_number", ""),
            "paper_name": pid,
            "reason": item.get("reason", ""),
            "expected_use": item.get("expected_use", ""),
            "priority": item.get("priority", 3),
        })
        paper_names.append(pid)

    sel_path = jm.job_dir(job_id) / "planning" / "selected_papers.json"
    atomic_write_json(sel_path, {
        "selected_papers": enriched,
        "selection_status": "confirmed",
        "confirmed_at": now_iso(),
        "confirmed_by": confirmed_by,
        "notes": "",
    }, indent=2)
    jm.set_step(job_id, "catalog_selection_confirmed", True, extra={"selected_papers": paper_names})
    jm.append_note(job_id, f"确认精读文献 {len(paper_names)} 篇 by {confirmed_by}")
    return {
        "selected_path": str(sel_path),
        "selected_papers": enriched,
        "paper_names": paper_names,
        "selection_confirmed": True,
    }

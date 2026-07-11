"""综述写作 CLI（严格多阶段工作流）

子命令:
  create          创建任务
  match           生成目录匹配 prompt + candidates（不确认 selected）
  confirm-papers  确认 selected_papers（设置 catalog_selection_confirmed）
  prepare-workset 将 selected_papers 全文复制到 write/jobs/<job_id>/article/<paper_number>/
  deep-read       生成精读 prompt + 笔记模板（要求 selected 已确认）
  mark-deep-read  校验精读笔记已填 → 设置 deep_read_notes_filled
  story           生成故事线 prompt + 模板（要求 deep_read_notes_filled）
  mark-story      校验 story_plan 已填 → 设置 story_plan_filled
  tex             生成 TeX 模板 + references.bib（要求 story_plan_filled，默认不覆盖）
  mark-tex        校验 TeX 正文已填 → 设置 tex_content_filled
  copy-figures    复制指定图到 figures/（需 --figures 或 --from-candidates-marked）
  validate        全量校验 → 通过设置 validated

所有 LLM 步骤只生成 prompt/模板，由人手动跑后回填；mark-* 校验后才标记完成。
"""
import sys
import argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
from src.writer.job_manager import JobManager
from src.writer.topic_parser import normalize_task
from src.writer.catalog_matcher import match_catalog, confirm_selected_papers
from src.writer.deep_reader import deep_read, mark_deep_reading_filled, prepare_workset
from src.writer.story_builder import build_story, mark_story_filled
from src.writer.tex_project import build_tex, mark_tex_content_filled
from src.writer.figure_manager import copy_figures
from config.settings import CATALOG_FOLDER_ROOT, PAPERS_DIR
from src.catalog_folders.reader import CatalogFolderReader


def cmd_create(args):
    jm = JobManager()
    info = jm.create(topic=args.topic, input_file=args.input,
                     target=args.target, language=args.language)
    norm = normalize_task(info["job_id"], jm)
    logger.info(f"任务创建: {info['job_id']}")
    logger.info(f"  目录: {info['job_dir']}")
    logger.info(f"  normalized_task: {norm['normalized_path']}")
    logger.info(f"  下一步: match --job {info['job_id']}")
    print(info["job_id"])


def cmd_match(args):
    info = match_catalog(args.job, categories=args.categories, category_mode=args.category_mode)
    logger.info(f"目录匹配完成: {args.job}")
    if args.categories:
        logger.info(f"  categories: {args.categories}")
    logger.info(f"  prompt: {info['prompt_path']}")
    logger.info(f"  candidates: {info['candidates_path']}（含候选，need_fulltext=null）")
    logger.info(f"  selected: {info['selected_path']}（空，待 confirm-papers）")
    logger.info("  下一步：跑匹配 prompt → confirm-papers --job ... --papers ...")


def cmd_prepare_workset(args):
    """将 selected_papers 全文复制到 write/jobs/<job_id>/article/<paper_number>/。"""
    try:
        manifest = prepare_workset(args.job, overwrite=args.overwrite, apply=args.apply)
    except RuntimeError as exc:
        logger.error(str(exc))
        return
    action = "复制" if args.apply else "计划"
    logger.info(f"workset {action}完成: {len(manifest['copied'])} 项, {len(manifest['skipped'])} 跳过")
    for item in manifest["copied"]:
        logger.info(f"{action}: {item['paper_id']} ({item['paper_number']}) -> {item['work_dir']}")
    for item in manifest["skipped"]:
        logger.warning(f"跳过: {item}")
    logger.info(f"下一步: deep-read --job {args.job}（LLM 应只读 write/jobs/{args.job}/article/）")


def cmd_confirm_papers(args):
    # Paper IDs come from --papers, paper_numbers from --paper-numbers.
    # Resolve paper_numbers via Catalog into paper_ids.
    paper_ids = list(args.papers or [])
    catalog = CatalogFolderReader(root=CATALOG_FOLDER_ROOT, papers_dir=PAPERS_DIR)
    if args.paper_numbers:
        all_papers = catalog.list_papers()
        by_number = {p.get("paper_number"): p for p in all_papers}
        for num in args.paper_numbers:
            entry = by_number.get(num)
            if entry is None:
                logger.error(f"paper_number not found: {num}")
                return
            paper_ids.append(entry.get("paper_id"))
    if not paper_ids:
        logger.error("至少需要 --papers 或 --paper-numbers 中的一个")
        return
    selected = [{"paper_id": pid, "reason": "", "expected_use": "", "priority": 3}
                for pid in paper_ids]
    info = confirm_selected_papers(args.job, selected, confirmed_by="manual")
    logger.info(f"已确认精读文献 {len(info['paper_ids'])} 篇: {info['paper_ids']}")
    logger.info(f"  selected: {info['selected_path']}")
    logger.info(f"  下一步: prepare-workset --job {args.job} --apply")
    logger.info(f"  然后: deep-read --job {args.job}")


def cmd_deep_read(args):
    info = deep_read(args.job, paper_ids=args.papers, force=args.force)
    logger.info(f"精读 prompt+模板已生成: {args.job}，{len(info['notes'])} 篇笔记")
    logger.info(f"  来源: {info.get('source', '?')}")
    logger.info(f"  prompt: {info['prompt_path']}")
    logger.info(f"  notes_filled={info['notes_filled']}（需手动填笔记后 mark-deep-read）")


def cmd_mark_deep_read(args):
    info = mark_deep_reading_filled(args.job)
    if info["filled"]:
        logger.info(f"✅ 精读笔记校验通过，deep_read_notes_filled=True")
        logger.info(f"  下一步: story --job {args.job}")
    else:
        logger.error("❌ 精读笔记校验未通过：")
        for e in info["errors"]:
            logger.error(f"  - {e}")
        sys.exit(1)


def cmd_story(args):
    info = build_story(args.job, force=args.force)
    logger.info(f"故事线 prompt+模板已生成: {args.job}")
    logger.info(f"  prompt: {info['prompt_path']}")
    logger.info(f"  plan_filled={info['plan_filled']}（需手动填 story_plan 后 mark-story）")


def cmd_mark_story(args):
    info = mark_story_filled(args.job)
    if info["filled"]:
        logger.info(f"✅ 故事线校验通过，story_plan_filled=True")
        logger.info(f"  下一步: tex --job {args.job}")
    else:
        logger.error("❌ 故事线校验未通过：")
        for e in info["errors"]:
            logger.error(f"  - {e}")
        sys.exit(1)


def cmd_tex(args):
    info = build_tex(args.job, title=args.title, force=args.force,
                     template_only=args.template_only)
    logger.info(f"TeX 模板+references.bib 已生成: {args.job}")
    for k, v in info["writes"].items():
        logger.info(f"  {k}: {v['action']}" + (f" (backup: {v['backup']})" if v['backup'] else ""))
    logger.info(f"  references.bib: {info['bib_count']} 条")
    logger.info(f"  content_filled={info['content_filled']}（需手动填正文后 mark-tex）")


def cmd_mark_tex(args):
    info = mark_tex_content_filled(args.job)
    if info["filled"]:
        logger.info(f"✅ TeX 正文校验通过，tex_content_filled=True")
        logger.info(f"  下一步: copy-figures / validate --job {args.job}")
    else:
        logger.error("❌ TeX 正文校验未通过：")
        for e in info["errors"]:
            logger.error(f"  - {e}")
        sys.exit(1)


def cmd_copy_figures(args):
    figures = []
    if args.figures:
        # 格式 paper_id:image 或 paper_id:image:caption
        for spec in args.figures:
            parts = spec.split(":", 2)
            item = {"paper_id": parts[0], "image": parts[1] if len(parts) > 1 else ""}
            if len(parts) > 2:
                item["suggested_caption"] = parts[2]
            figures.append(item)
    info = copy_figures(args.job, figures=figures)
    logger.info(f"图片复制: {len(info['copied'])} 张")
    if not info["copied"]:
        logger.warning(info.get("note", "未复制任何图"))


def cmd_validate(args):
    from src.writer.job_validator import validate_job
    sys.argv = ["validate", "--job", args.job]
    result = validate_job(args.job)
    if not result["valid"]:
        for error in result["errors"]:
            logger.error(f"  - {error}")
        sys.exit(1)
    logger.info(f"✅ 写作任务 {args.job} 校验通过")


def main():
    ap = argparse.ArgumentParser(description="综述写作 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create")
    c.add_argument("--topic", default=None)
    c.add_argument("--input", default=None)
    c.add_argument("--target", default="phd_thesis")
    c.add_argument("--language", default="zh")
    c.set_defaults(func=cmd_create)

    m = sub.add_parser("match")
    m.add_argument("--job", required=True)
    m.add_argument("--categories", nargs="+", default=None,
                   help="按一个或多个中文分类目录筛选")
    m.add_argument("--category-mode", choices=["union", "intersection"], default="union")
    m.set_defaults(func=cmd_match)

    cp = sub.add_parser("confirm-papers"); cp.add_argument("--job", required=True)
    cp.add_argument("--papers", nargs="+", default=None, help="paper_id 列表")
    cp.add_argument("--paper-numbers", nargs="+", default=None, help="16 位 paper_number 列表")
    cp.set_defaults(func=cmd_confirm_papers)

    pw = sub.add_parser("prepare-workset", help="将 selected_papers 全文复制到 write/jobs/<job_id>/article/<paper_number>/")
    pw.add_argument("--job", required=True)
    pw.add_argument("--overwrite", action="store_true", help="若目标已存在则覆盖")
    pw.add_argument("--apply", action="store_true"); pw.set_defaults(func=cmd_prepare_workset)

    d = sub.add_parser("deep-read"); d.add_argument("--job", required=True)
    d.add_argument("--papers", nargs="+", default=None,
                   help="可选，默认从 selected_papers.json 取")
    d.add_argument("--force", action="store_true", help="覆盖已填文件（先备份）")
    d.set_defaults(func=cmd_deep_read)

    mdr = sub.add_parser("mark-deep-read"); mdr.add_argument("--job", required=True)
    mdr.set_defaults(func=cmd_mark_deep_read)

    s = sub.add_parser("story"); s.add_argument("--job", required=True)
    s.add_argument("--force", action="store_true", help="覆盖已填 story 文件（先备份）")
    s.set_defaults(func=cmd_story)

    ms = sub.add_parser("mark-story"); ms.add_argument("--job", required=True); ms.set_defaults(func=cmd_mark_story)

    t = sub.add_parser("tex"); t.add_argument("--job", required=True)
    t.add_argument("--title", default=None)
    t.add_argument("--force", action="store_true", help="覆盖已有 tex（先备份）")
    t.add_argument("--template-only", action="store_true", help="跳过前序校验，仅生成空模板")
    t.set_defaults(func=cmd_tex)

    mt = sub.add_parser("mark-tex"); mt.add_argument("--job", required=True); mt.set_defaults(func=cmd_mark_tex)

    f = sub.add_parser("copy-figures"); f.add_argument("--job", required=True)
    f.add_argument("--figures", nargs="+", default=None,
                   help="格式 paper_id:image 或 paper_id:image:caption")
    f.set_defaults(func=cmd_copy_figures)

    v = sub.add_parser("validate"); v.add_argument("--job", required=True); v.set_defaults(func=cmd_validate)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

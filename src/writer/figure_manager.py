"""图片复制与追踪：把确认使用的图从 job-local article workspace 复制到 job figures/

原则：
  1. 只有确认为使用的图才复制（默认从 figures 列表，不自动复制全部候选）；
  2. 候选图不自动进 TeX；
  3. 每张复制图必须有 source record（README.md 含 source_article_image）；
  4. TeX 中只能引用 write/jobs/<job_id>/figures 下的复制图。
"""
import json
import re
import shutil

from src.writer.job_manager import JobManager
from src.naming import validate_paper_id, validate_image_name, safe_child
from src.writer.bib_manager import job_local_bib_keys, resolve_work_dir


def copy_figures(job_id: str, figures: list[dict] | None = None,
                 jm: JobManager | None = None,
                 catalog: object | None = None) -> dict:
    """复制指定图到 write/jobs/<job_id>/figures/<paper_id>/ 并生成 source record README。

    figures: [{"paper_id","image","suggested_caption"?}]，为 None 时**不复制**任何图
            （避免把全部候选图当使用图）。调用方应传明确要用的图列表。
    """
    jm = jm or JobManager()
    # ``catalog`` is retained in the signature for callers/tests but no longer
    # used here: bib_key now comes from job-local article metadata.
    jdir = jm.job_dir(job_id)
    manifest_path = jdir / "planning" / "workset_manifest.json"
    if not manifest_path.exists():
        raise RuntimeError("workset_manifest.json not found. Run prepare-workset before copy-figures.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    article_by_pid = {
        item.get("paper_id"): resolve_work_dir(jdir, item.get("work_dir", ""))
        for item in manifest.get("copied", [])
        if item.get("paper_id") and item.get("work_dir")
    }

    if figures is None:
        return {"copied": [], "used_figures": [],
                "note": "未提供 figures 列表，未复制任何图（需明确指定要用的图）"}

    # bib_key from the job-local copied article metadata, matching export_job_bib.
    bib_map = job_local_bib_keys(article_by_pid)

    copied = []
    used = []
    skipped = []
    for item in figures:
        pid = item.get("paper_id")
        img = item.get("image")
        if not pid or not img:
            skipped.append({"paper_id": pid, "image": img, "reason": "缺少 paper_id 或 image"})
            continue
        # 防路径穿越：校验 pid + img，使用 safe_child 拼接
        try:
            validate_paper_id(pid)
            validate_image_name(img)
            article_dir = article_by_pid.get(pid)
            if article_dir is None:
                skipped.append({"paper_id": pid, "image": img, "reason": "paper not in prepared workset"})
                continue
            src = safe_child(article_dir, "images", img)
            dest_dir = safe_child(jdir, "figures", pid)
        except ValueError as e:
            skipped.append({"paper_id": pid, "image": img, "reason": str(e)})
            continue
        if not src.is_file():
            skipped.append({"paper_id": pid, "image": img, "reason": f"源文件不存在: {src}"})
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / img  # img 已通过 validate_image_name，不含路径分隔符
        shutil.copy2(src, dest)

        # source record README（按图追加，含 source_article_image）
        readme = dest_dir / "README.md"
        record = (
            f"\n## {img}\n"
            f"- copied_file: write/jobs/{jm.job_dir(job_id).name}/figures/{pid}/{img}\n"
            f"- source_article_image: article/{article_dir.name}/images/{img}\n"
            f"- paper_id: {pid}\n"
            f"- bib_key: {bib_map.get(pid, '')}\n"
            f"- source_markdown: article/{article_dir.name}/{pid}.md\n"
            f"- suggested_caption: {item.get('suggested_caption', '')}\n"
            f"- used_in_tex: false\n"
            f"- notes:\n"
        )
        if not readme.exists():
            readme.write_text("# Figure source record\n" + record, encoding="utf-8")
        else:
            with readme.open("a", encoding="utf-8") as f:
                f.write(record)

        copied.append(str(dest))
        used.append({"paper_id": pid, "image": img,
                     "tex_path": f"../figures/{pid}/{img}",
                     "source_article_image": f"article/{article_dir.name}/images/{img}"})

    jm.set_step(job_id, "figures_copied", True, extra={"used_figures": used})
    return {"copied": copied, "used_figures": used, "skipped": skipped}

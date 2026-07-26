"""Generate a small TeX article from a copied write/jobs article workspace."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

from config.settings import PROJECT_ROOT
from src.writer.bib import bib_key_for_entry, bibtex_for_entry, parse_blocks
from src.utils.naming import safe_child, validate_job_id
from src.utils.path_utils import normalize_repo_path
from src.utils.atomic_io import atomic_write_json


WRITE_DIR = PROJECT_ROOT / "write" / "jobs"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _find_one(folder: Path, pattern: str) -> Path:
    matches = sorted(folder.glob(pattern))
    if not matches:
        raise FileNotFoundError(f"missing {pattern} in {folder}")
    return matches[0]


def _tex_escape(value: Any) -> str:
    text = str(value or "")
    repl = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(repl.get(ch, ch) for ch in text)


def _catalog_text(catalog: dict, *path: str) -> str:
    cur: Any = catalog
    for key in path:
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(key)
    if isinstance(cur, list):
        return "; ".join(str(x) for x in cur if x)
    return str(cur or "")


def _load_article_entries(job_dir: Path, selected: dict) -> list[dict]:
    out: list[dict] = []
    article_dir = job_dir / "article"
    for item in selected.get("papers") or []:
        number = str(item.get("paper_number") or "")
        paper_name = str(item.get("paper_name") or "")
        folder = article_dir / number
        metadata = _read_json(_find_one(folder, "*.metadata.json"))
        catalog = _read_json(_find_one(folder, "*.catalog.json"))
        out.append({
            "paper_number": number,
            "paper_name": paper_name,
            "folder": folder,
            "metadata": metadata,
            "catalog": catalog,
            "bib_key": bib_key_for_entry({"paper_name": paper_name, "metadata": metadata}),
        })
    return out


def _metadata_doi(entry: dict) -> str:
    return str(((entry.get("metadata") or {}).get("identifiers") or {}).get("doi") or "").strip()


def _paper_label(entry: dict) -> str:
    catalog = entry.get("catalog") or {}
    metadata = entry.get("metadata") or {}
    # citation title comes from metadata; catalog title is content-only fallback.
    title = (
        (metadata.get("title") or {}).get("original")
        or (catalog.get("content_identity") or {}).get("content_title_zh")
        or entry.get("paper_name")
    )
    authors = metadata.get("authors") or []
    first = authors[0] if authors else {}
    fam = (first.get("family") or first.get("full_name") or "") if isinstance(first, dict) else str(first)
    author = str(fam).split(",")[0].strip()
    if author and len(authors) > 1:
        author = f"{author} et al."
    year = str(metadata.get("year") or "")
    return f"{author} ({year}) {title}"


def _section_text(entries: list[dict], title: str, language: str) -> dict[str, str]:
    keys = [e["bib_key"] for e in entries]
    cite_all = ",".join(keys[:3])
    topic = _tex_escape(title)
    if language == "en":
        intro = (
            "\\section{Introduction}\n"
            f"This mini article reviews {topic} through a catalog-first writing workflow. "
            "The selected papers provide complementary evidence on problem framing, methods, and limitations "
            f"\\cite{{{cite_all}}}.\n"
        )
        basis_lines = ["\\section{Literature Basis}"]
        for entry in entries:
            catalog = entry["catalog"]
            summary = (catalog.get("abstract") or {}).get("one_sentence_zh")
            methods = (catalog.get("methods") or {}).get("overview_zh")
            findings = "; ".join(x.get("finding_zh", "") for x in catalog.get("key_findings") or [])
            content = "; ".join(x for x in (summary, methods, findings) if x) or "content unavailable"
            basis_lines.append(f"{_tex_escape(_paper_label(entry))}: {_tex_escape(content)} \\cite{{{entry['bib_key']}}}.")
        discussion = (
            "\\section{Discussion}\n"
            "Across the selected literature, the catalog evidence suggests that a useful synthesis should separate "
            "physical mechanisms, model assumptions, and validation data. The copied article workspace keeps every "
            "claim traceable to formal metadata and catalog records.\n"
            "\\section{Conclusion}\n"
            f"The current evidence base supports a focused mini-review on {topic}, while deeper claims should be "
            "checked against the copied Markdown full texts before submission.\n"
        )
    else:
        intro = (
            "\\section{引言}\n"
            f"本文围绕“{topic}”进行一次 catalog-first 的小型综述写作测试。"
            "入选文献分别覆盖问题背景、方法框架和结果解释，能够支撑一个从初筛目录到 TeX 成文的闭环引用演示"
            f"\\cite{{{cite_all}}}。\n"
        )
        basis_lines = ["\\section{文献基础}"]
        for entry in entries:
            catalog = entry["catalog"]
            summary = (catalog.get("abstract") or {}).get("one_sentence_zh")
            methods = (catalog.get("methods") or {}).get("overview_zh")
            findings = "；".join(x.get("finding_zh", "") for x in catalog.get("key_findings") or [])
            uses = "；".join((catalog.get("writing_value") or {}).get("use_cases") or [])
            content = "；".join(x for x in (summary, methods, findings, uses) if x) or "内容不可用"
            basis_lines.append(f"{_tex_escape(_paper_label(entry))}：{_tex_escape(content)}\\cite{{{entry['bib_key']}}}。")
        discussion = (
            "\\section{讨论}\n"
            "从这些文献的 catalog 信息看，后续综述应把物理机制、模型假设、实验或观测证据分开组织，"
            "避免把不同尺度或不同数据来源的结论直接合并。当前写作目录只使用复制后的 article 数据，"
            "因此引用、图片和参考文献都可以回溯到正式入库后的 metadata 与 catalog。\n"
            "\\section{结论}\n"
            f"本轮测试表明，围绕“{topic}”的初筛目录可以稳定转化为一个含 BibTeX、章节文件和格式检查报告的 TeX 项目。"
            "真正用于论文正文前，仍应逐篇阅读 copied Markdown，补充更细的方程、实验条件和图表证据。\n"
        )
    return {
        "introduction.tex": intro,
        "literature_basis.tex": "\n\n".join(basis_lines) + "\n",
        "discussion.tex": discussion,
    }


def write_article(args: argparse.Namespace) -> dict:
    job_id = validate_job_id(args.job_id)
    job_dir = safe_child(Path(args.write_dir), job_id)
    selected = _read_json(job_dir / "selected_catalog.json")
    entries = _load_article_entries(job_dir, selected)
    if len(entries) < 3:
        raise ValueError("at least 3 selected papers are required to write the mini article")
    missing_doi = [entry["paper_number"] for entry in entries if not _metadata_doi(entry)]
    if missing_doi:
        raise ValueError(f"all selected papers must have metadata.identifiers.doi: {', '.join(missing_doi)}")

    tex_dir = job_dir / "tex"
    sections_dir = tex_dir / "sections"
    reports_dir = job_dir / "reports"
    if tex_dir.exists() and args.apply and args.overwrite:
        shutil.rmtree(tex_dir)
    if tex_dir.exists() and args.apply and not args.overwrite:
        raise FileExistsError(f"tex directory already exists: {tex_dir}")

    bib_entries = [bibtex_for_entry({"paper_name": e["paper_name"], "metadata": e["metadata"]}) for e in entries]
    bib_text = "\n\n".join(bib_entries) + "\n"
    bib_keys = sorted(parse_blocks(bib_text).keys())
    sections = _section_text(entries, args.title, args.language)
    title = _tex_escape(args.title)
    abstract = (
        "本文档由 MinerU v2 写作工作流根据 copied article metadata/catalog 生成，"
        "用于验证筛选、引用和格式检查闭环。"
        if args.language == "zh"
        else "This document is generated from copied article metadata/catalog to validate the writing workflow."
    )
    main = (
        "\\documentclass[UTF8]{ctexart}\n"
        "\\usepackage{graphicx}\n"
        "\\usepackage{hyperref}\n"
        f"\\title{{{title}}}\n"
        "\\author{MinerU catalog-first writing workflow}\n"
        "\\date{\\today}\n"
        "\\begin{document}\n"
        "\\maketitle\n"
        "\\begin{abstract}\n"
        f"{_tex_escape(abstract)}\n"
        "\\end{abstract}\n"
        "\\input{sections/introduction}\n"
        "\\input{sections/literature_basis}\n"
        "\\input{sections/discussion}\n"
        "\\bibliographystyle{plain}\n"
        "\\bibliography{references}\n"
        "\\end{document}\n"
    )
    prompt = (
        "# Catalog TeX Writer Prompt\n\n"
        "Use selected_catalog.json first, then copied article/<paper_number>/ catalog and metadata files. "
        "Do not read data/papers directly. Keep citations tied to references.bib keys.\n"
    )
    report = {
        "job_id": job_id,
        "title": args.title,
        "language": args.language,
        "dry_run": not args.apply,
        "paper_count": len(entries),
        "bib_keys": bib_keys,
        "tex_dir": normalize_repo_path(tex_dir),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    if not args.apply:
        return report

    sections_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (tex_dir / "main.tex").write_text(main, encoding="utf-8")
    (tex_dir / "references.bib").write_text(bib_text, encoding="utf-8")
    (tex_dir / "writing_prompt.md").write_text(prompt, encoding="utf-8")
    for name, content in sections.items():
        (sections_dir / name).write_text(content, encoding="utf-8")
    atomic_write_json(reports_dir / "write_article_report.json", report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a TeX mini article from a write job.")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--language", choices=["zh", "en"], default="zh")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--write-dir", type=Path, default=Path(WRITE_DIR))
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    if args.dry_run:
        args.apply = False
    report = write_article(args)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Validate a write/jobs/<job_id> TeX article project."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

from config.settings import PROJECT_ROOT
from src.bib import parse_blocks
from src.naming import safe_child, validate_job_id
from src.path_utils import normalize_repo_path
from src.utils.atomic_io import atomic_write_json


WRITE_DIR = PROJECT_ROOT / "write" / "jobs"
_CITE_RE = re.compile(r"\\cite\w*\s*\{([^}]+)\}")
_GRAPHICS_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\s*\{([^}]+)\}")
# 显式安全防护 token（字面量出现，便于防回流扫描识别）。
_RESTRICTED_PATH_TOKENS = ("data/papers", "data/paper_raw", "data/raw", "data/llm_work")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _citation_keys(tex: str) -> set[str]:
    keys: set[str] = set()
    for match in _CITE_RE.finditer(tex):
        keys.update(k.strip() for k in match.group(1).split(",") if k.strip())
    return keys


def _has_nonempty_doi(block: str) -> bool:
    return bool(re.search(r"\bdoi\s*=\s*[\{\"]\s*[^}\" ]+", block, flags=re.IGNORECASE))


def _graphics_exists(tex_dir: Path, ref: str) -> bool:
    target = (tex_dir / ref).resolve()
    if target.exists():
        return True
    for suffix in (".png", ".jpg", ".jpeg", ".pdf"):
        if target.with_suffix(suffix).exists():
            return True
    return False


def _restricted_token(text: str) -> str | None:
    norm = text.replace("\\", "/")
    for token in _RESTRICTED_PATH_TOKENS:
        if token in norm:
            return token
    return None


def _run_compile(tex_dir: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    output_dir = tex_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    latexmk = shutil.which("latexmk")
    xelatex = shutil.which("xelatex")
    if latexmk:
        cmd = [latexmk, "-xelatex", "-interaction=nonstopmode", "-halt-on-error", "-outdir=output", "main.tex"]
    elif xelatex:
        cmd = [xelatex, "-interaction=nonstopmode", "-halt-on-error", "-output-directory", "output", "main.tex"]
    else:
        warnings.append("LaTeX compiler not found; skipped compile")
        return errors, warnings
    result = subprocess.run(
        cmd,
        cwd=tex_dir,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    (output_dir / "compile.log").write_text((result.stdout or "") + "\n" + (result.stderr or ""), encoding="utf-8")
    if result.returncode != 0:
        errors.append(f"LaTeX compile failed with exit code {result.returncode}")
    return errors, warnings


def check_tex_project(args: argparse.Namespace) -> dict:
    job_id = validate_job_id(args.job_id)
    job_dir = safe_child(Path(args.write_dir), job_id)
    tex_dir = job_dir / "tex"
    article_dir = job_dir / "article"
    reports_dir = job_dir / "reports"
    errors: list[str] = []
    warnings: list[str] = []

    main_path = tex_dir / "main.tex"
    bib_path = tex_dir / "references.bib"
    if not main_path.exists():
        errors.append("missing tex/main.tex")
    if not bib_path.exists():
        errors.append("missing tex/references.bib")
    if not article_dir.exists():
        errors.append("missing article/")

    paper_dirs = sorted([p for p in article_dir.iterdir() if p.is_dir()]) if article_dir.exists() else []
    if len(paper_dirs) < 3:
        errors.append("article/ must contain at least 3 paper_number directories")
    for folder in paper_dirs:
        if not re.match(r"^\d{16}$", folder.name):
            errors.append(f"invalid article folder name: {folder.name}")
        for pattern in ("*.metadata.json", "*.catalog.json", "*.md", "*.pdf"):
            if not list(folder.glob(pattern)):
                errors.append(f"{folder.name} missing {pattern}")
        if not (folder / "images").is_dir():
            errors.append(f"{folder.name} missing images/")

    tex_texts: list[tuple[Path, str]] = []
    if tex_dir.exists():
        for path in sorted(tex_dir.rglob("*.tex")):
            text = _read_text(path)
            tex_texts.append((path, text))
            token = _restricted_token(text)
            if token:
                errors.append(f"{normalize_repo_path(path)} contains direct {token} path")
    bib_text = _read_text(bib_path) if bib_path.exists() else ""
    blocks = parse_blocks(bib_text)
    if len(blocks) < 3:
        errors.append("references.bib must contain at least 3 entries")
    for key, block in blocks.items():
        if not _has_nonempty_doi(block):
            errors.append(f"references.bib entry {key} missing nonempty doi")

    cited = set()
    for _, text in tex_texts:
        cited.update(_citation_keys(text))
    for key in sorted(cited - set(blocks.keys())):
        errors.append(f"\\cite{{{key}}} not found in references.bib")

    for path, text in tex_texts:
        for ref in _GRAPHICS_RE.findall(text):
            token = _restricted_token(ref)
            if token:
                errors.append(f"{normalize_repo_path(path)} includes direct {token} image path")
            elif not _graphics_exists(tex_dir, ref):
                errors.append(f"{normalize_repo_path(path)} image not found: {ref}")

    if args.compile and not errors:
        compile_errors, compile_warnings = _run_compile(tex_dir)
        errors.extend(compile_errors)
        warnings.extend(compile_warnings)

    report = {
        "job_id": job_id,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "paper_count": len(paper_dirs),
        "bib_count": len(blocks),
        "citation_count": len(cited),
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }
    reports_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(reports_dir / "format_check.json", report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check a write job TeX project.")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--compile", action="store_true")
    parser.add_argument("--write-dir", type=Path, default=Path(WRITE_DIR))
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    report = check_tex_project(args)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())

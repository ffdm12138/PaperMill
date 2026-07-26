"""Validate text-quality gates for a write/jobs/<job_id> TeX article."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

from config.settings import PROJECT_ROOT
from src.bib import parse_blocks
from src.naming import safe_child, validate_job_id
from src.path_utils import normalize_repo_path
from src.utils.atomic_io import atomic_write_json


WRITE_DIR = PROJECT_ROOT / "write" / "jobs"
REPORT_NAME = "write_quality_check_report.json"

_CITE_RE = re.compile(r"\\cite\w*\s*\{([^}]+)\}")
_SECTION_RE = re.compile(r"\\section\*?\s*\{([^}]*)\}", flags=re.IGNORECASE)
_POINTS_OUT_REPEAT_RE = re.compile(r"指出：[^。；\n]{0,160}指出")
_X_POINTS_OUT_X_RE = re.compile(
    r"(?P<subject>[\w\u4e00-\u9fff（）()，、·.\-~ ]{2,80})指出：\s*(?P=subject)"
)
_PLACEHOLDER_PATTERNS = [
    re.compile(r"smoke", flags=re.IGNORECASE),
    re.compile(r"闭环引用演示"),
    re.compile(r"本文档由\s*MinerU"),
    re.compile(r"MinerU\s+v2\s+写作工作流根据"),
    re.compile(r"用于验证筛选、引用和格式检查闭环"),
    re.compile(r"TEMPLATE_ONLY"),
]
_UNCERTAINTY_RE = re.compile(
    r"uncertainty|limitation|limitations|不确定性|局限性|局限",
    flags=re.IGNORECASE,
)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tex_files(tex_dir: Path) -> list[Path]:
    if not tex_dir.exists():
        return []
    return sorted(tex_dir.rglob("*.tex"))


def _citation_keys(tex: str) -> set[str]:
    keys: set[str] = set()
    for match in _CITE_RE.finditer(tex):
        keys.update(k.strip() for k in match.group(1).split(",") if k.strip())
    return keys


def _section_titles(tex: str) -> list[str]:
    return [m.group(1).strip() for m in _SECTION_RE.finditer(tex)]


def _has_named_section(files: list[Path], combined_tex: str, *names: str) -> bool:
    lowered_names = [name.lower() for name in names]
    for path in files:
        stem = path.stem.lower()
        if any(name in stem for name in lowered_names):
            return True
    for title in _section_titles(combined_tex):
        lowered = title.lower()
        if any(name in lowered for name in lowered_names):
            return True
    return False


def _first_match(patterns: list[re.Pattern[str]], text: str) -> str | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def check_write_quality_text(args: argparse.Namespace) -> dict[str, Any]:
    job_id = validate_job_id(args.job_id)
    write_dir = Path(args.write_dir)
    job_dir = safe_child(write_dir, job_id)
    tex_dir = job_dir / "tex"
    reports_dir = safe_child(job_dir, "reports")

    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}

    files = _tex_files(tex_dir)
    if not files:
        errors.append("missing tex/*.tex files")
    tex_parts: list[str] = []
    for path in files:
        try:
            tex_parts.append(_read_text(path))
        except FileNotFoundError:
            errors.append(f"missing TeX file: {normalize_repo_path(path)}")
    combined_tex = "\n\n".join(tex_parts)

    bib_path = tex_dir / "references.bib"
    if not bib_path.exists():
        errors.append("missing tex/references.bib")
        bib_text = ""
    else:
        bib_text = _read_text(bib_path)
    bib_blocks = parse_blocks(bib_text)
    cited = _citation_keys(combined_tex)
    missing_bib_citations = sorted(set(bib_blocks) - cited)

    repeat_hit = _POINTS_OUT_REPEAT_RE.search(combined_tex)
    x_hit = _X_POINTS_OUT_X_RE.search(combined_tex)
    if repeat_hit:
        errors.append(f"template sentence detected: {repeat_hit.group(0)[:120]}")
    if x_hit:
        errors.append(f"X指出：X template detected: {x_hit.group(0)[:120]}")

    placeholder_hit = _first_match(_PLACEHOLDER_PATTERNS, combined_tex)
    if placeholder_hit:
        errors.append(f"placeholder acceptance prose detected: {placeholder_hit[:120]}")

    if missing_bib_citations:
        errors.append("references.bib keys not cited in body: " + ", ".join(missing_bib_citations))

    has_introduction = _has_named_section(files, combined_tex, "introduction", "引言")
    has_conclusion = _has_named_section(files, combined_tex, "conclusion", "结论")
    if not has_introduction:
        errors.append("missing introduction/引言 section")
    if not has_conclusion:
        errors.append("missing conclusion/结论 section")

    has_uncertainty = bool(_UNCERTAINTY_RE.search(combined_tex))
    if not has_uncertainty:
        errors.append("missing uncertainty/limitation/不确定性/局限 paragraph")

    checks.update(
        {
            "template_sentence": not (repeat_hit or x_hit),
            "placeholder_text": placeholder_hit is None,
            "all_bib_keys_cited": not missing_bib_citations,
            "has_introduction": has_introduction,
            "has_conclusion": has_conclusion,
            "has_uncertainty_or_limitation": has_uncertainty,
        }
    )

    report = {
        "job_id": job_id,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "bib_count": len(bib_blocks),
        "citation_count": len(cited),
        "missing_bib_citations": missing_bib_citations,
        "checks": checks,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }
    reports_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(reports_dir / REPORT_NAME, report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check write job TeX text quality.")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--write-dir", type=Path, default=Path(WRITE_DIR))
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    report = check_write_quality_text(args)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())

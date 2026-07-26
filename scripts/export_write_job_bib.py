"""Export job-local references.bib from copied article metadata (fail-closed).

Reads ``write/jobs/<job_id>/selected_catalog.json`` plus each copied
``article/<paper_number>/*.metadata.json`` and writes
``write/jobs/<job_id>/tex/references.bib``.  Citation facts come ONLY from
Metadata; any paper whose metadata is missing or not citation-ready fails the
whole export with per-paper errors.  Keys match
``src.writer.bib.bib_key_for_entry`` (identical to write_catalog_tex_article).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

from config.settings import PROJECT_ROOT
from src.utils.atomic_io import atomic_write_text
from src.utils.naming import safe_child, validate_job_id
from src.utils.path_utils import normalize_repo_path
from src.writer.bib import bib_key_for_entry, bibtex_for_entry

WRITE_DIR = PROJECT_ROOT / "write" / "jobs"


def _failed(job_id: str, errors: list[str]) -> dict:
    return {"job_id": job_id, "passed": False, "errors": errors, "count": 0}


def export_job_references(args: argparse.Namespace) -> dict:
    job_id = validate_job_id(args.job_id)
    job_dir = safe_child(Path(args.write_dir), job_id)
    selected_path = job_dir / "selected_catalog.json"
    if not selected_path.exists():
        return _failed(job_id, [
            f"selected_catalog.json not found: {selected_path} — create the job "
            "with scripts/create_write_job.py first"
        ])
    try:
        selected = json.loads(selected_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _failed(job_id, [f"selected_catalog.json: invalid JSON ({exc})"])
    papers = selected.get("papers") or []
    wanted = set(args.paper_numbers or [])

    entries: list[dict] = []
    errors: list[str] = []
    matched: set[str] = set()
    for item in papers:
        number = str(item.get("paper_number") or "")
        paper_name = str(item.get("paper_name") or "")
        if wanted and number not in wanted:
            continue
        matched.add(number)
        folder = job_dir / "article" / number
        metadata_files = sorted(folder.glob("*.metadata.json"))
        if not metadata_files:
            errors.append(f"{number}: missing *.metadata.json under article/{number}")
            continue
        if len(metadata_files) > 1:
            # Citation facts must have exactly one source; picking one silently
            # would make the exported bib depend on filename ordering.
            names = ", ".join(sorted(p.name for p in metadata_files))
            errors.append(f"{number}: multiple *.metadata.json under article/{number} ({names})")
            continue
        try:
            metadata = json.loads(metadata_files[0].read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{number}: invalid JSON in {metadata_files[0].name} ({exc})")
            continue
        entry = {"paper_name": paper_name, "metadata": metadata}
        try:
            key = bib_key_for_entry(entry)
            block = bibtex_for_entry(entry)
        except (RuntimeError, ValueError) as exc:
            errors.append(f"{number}: {exc}")
            continue
        entries.append({"paper_number": number, "bib_key": key, "block": block})

    for number in sorted(wanted - matched):
        errors.append(f"{number}: requested paper_number is not in selected_catalog.json")

    if errors:
        return _failed(job_id, errors)
    if not entries:
        return _failed(job_id, ["no papers selected for bib export"])

    tex_dir = safe_child(job_dir, "tex")
    tex_dir.mkdir(parents=True, exist_ok=True)
    bib_path = tex_dir / "references.bib"
    atomic_write_text(
        bib_path, "\n\n".join(entry["block"] for entry in entries) + "\n"
    )
    return {
        "job_id": job_id,
        "passed": True,
        "errors": [],
        "count": len(entries),
        "bib_path": normalize_repo_path(bib_path),
        "entries": [
            {"paper_number": e["paper_number"], "bib_key": e["bib_key"]}
            for e in entries
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export write-job references.bib from job-local metadata only."
    )
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--write-dir", type=Path, default=Path(WRITE_DIR))
    parser.add_argument("--paper-numbers", nargs="+", default=None,
                        help="optional subset of paper_numbers to export")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    result = export_job_references(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

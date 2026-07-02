"""Report directory-boundary hygiene warnings without deleting user data."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import ALL_CATALOG_PATH, PAPER_RAW_DIR, PAPERS_DIR, PROJECT_ROOT
from src.path_utils import normalize_repo_path
from src.services.ingest_ids import PAPER_NUMBER_RE


WRITE_JOBS_DIR = PROJECT_ROOT / "write" / "jobs"
_REAL_ARTIFACT_SUFFIXES = {".pdf", ".md", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".tex", ".bib", ".log"}
_RESTRICTED_TOKENS = ("data/papers", "data/paper_raw", "data/raw", "data/llm_work")


def _read_json(path: Path, default: Any | None = None) -> Any:
    if not path.exists():
        if default is not None:
            return default
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _metadata_doi(metadata: dict) -> str:
    return str(((metadata.get("identifiers") or {}).get("doi")) or "").strip().lower()


def _metadata_file(folder: Path) -> Path | None:
    matches = sorted(folder.glob("*.metadata.json"))
    return matches[0] if matches else None


def _git_ignored(project_root: Path, path: Path) -> bool | None:
    git_dir = project_root / ".git"
    if not git_dir.exists():
        return None
    result = subprocess.run(
        ["git", "-C", str(project_root), "check-ignore", "-q", str(path)],
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def _contains_restricted_path(text: str) -> str | None:
    norm = text.replace("\\", "/")
    for token in _RESTRICTED_TOKENS:
        if token in norm:
            return token
    return None


def check_directory_hygiene(
    *,
    project_root: Path = PROJECT_ROOT,
    all_catalog_path: Path = ALL_CATALOG_PATH,
    papers_dir: Path = PAPERS_DIR,
    paper_raw_dir: Path = PAPER_RAW_DIR,
    write_jobs_dir: Path = WRITE_JOBS_DIR,
    write_root: Path | None = None,
) -> dict:
    warnings: list[str] = []
    catalog = _read_json(all_catalog_path, {"papers": []})
    papers = catalog.get("papers") or []

    seen_numbers: dict[str, str] = {}
    for entry in papers:
        number = str(entry.get("paper_number") or "")
        paper_id = str(entry.get("paper_id") or "")
        if number:
            if number in seen_numbers:
                warnings.append(f"duplicate paper_number in all.catalog: {number} ({seen_numbers[number]}, {paper_id})")
            seen_numbers[number] = paper_id
        folder = papers_dir / paper_id if paper_id else None
        if not folder or not folder.exists():
            warnings.append(f"all.catalog references missing formal paper folder: {paper_id or number}")
            continue
        meta_path = _metadata_file(folder)
        if not meta_path:
            warnings.append(f"formal paper missing metadata file: {normalize_repo_path(folder)}")
            continue
        metadata = _read_json(meta_path, {})
        doi = _metadata_doi(metadata)
        if not doi:
            warnings.append(f"formal paper metadata.identifiers.doi missing: {normalize_repo_path(meta_path)}")

    # paper_raw metadata-resolution hygiene (warnings only; never delete).
    if paper_raw_dir.exists():
        for folder in sorted(p for p in paper_raw_dir.iterdir()
                             if p.is_dir() and PAPER_NUMBER_RE.match(p.name)):
            # numbered-workspace hygiene only; legacy/untitled dedup is handled
            # by ingest_duplicate_guard.
            source_id = folder.name
            meta_path = _metadata_file(folder)
            if not meta_path:
                continue
            metadata = _read_json(meta_path, {})
            status = str(((metadata.get("metadata_match") or {}).get("status")) or "")
            has_md = (folder / f"{source_id}.md").exists()
            has_candidates = (folder / f"{source_id}.metadata.candidates.json").exists()
            has_resolve_report = (folder / f"{source_id}.metadata.resolve_report.json").exists()
            import_status_path = folder / ".import_status.json"
            import_status = ""
            if import_status_path.exists():
                import_status = str((_read_json(import_status_path, {}) or {}).get("status") or "")
            rel = normalize_repo_path(folder)
            if has_md and status == "unmatched":
                warnings.append(
                    f"paper_raw has markdown but metadata_match.status is unmatched: {rel}"
                )
            if has_candidates and (not has_resolve_report or status == "unmatched"):
                warnings.append(
                    f"paper_raw has unresolved metadata candidates (.metadata.candidates.json present): {rel}"
                )
            if import_status in {"metadata_candidates_found", "metadata_manual_review_required",
                                 "catalog_ready", "formalize_failed", "commit_failed"}:
                warnings.append(
                    f"paper_raw import_status stuck at {import_status}: {rel}"
                )

    if write_jobs_dir.exists():
        for path in write_jobs_dir.rglob("*"):
            if not path.is_file():
                continue
            rel = normalize_repo_path(path)
            if path.suffix.lower() in _REAL_ARTIFACT_SUFFIXES:
                ignored = _git_ignored(project_root, path)
                if ignored is False:
                    warnings.append(f"write/jobs real artifact is not gitignored: {rel}")
            if path.name == "selected_catalog.json":
                # selected_catalog must be strictly content-only: no path fields,
                # no bibliographic metadata. Enforce the boundary here.
                selected = _read_json(path, {})
                forbidden_keys = {
                    "metadata", "formal_paper_dir", "article_dir",
                    "source_dir", "folder_path", "main_md",
                    "metadata_file", "catalog_file",
                }
                for item in selected.get("papers") or []:
                    if not isinstance(item, dict):
                        continue
                    bad = sorted(k for k in item.keys() if k in forbidden_keys)
                    if bad:
                        warnings.append(
                            f"{rel} selected_catalog paper carries non-content keys: {bad}")
            if path.name == "prepare_article_report.json":
                # Path tracking lives in the report (not selected_catalog); guard
                # that article sources point only at the formal papers dir.
                rep = _read_json(path, {})
                for item in rep.get("papers") or []:
                    source = str(item.get("formal_paper_dir") or item.get("article_dir") or "")
                    token = _contains_restricted_path(source)
                    if token and token != "data/papers":
                        warnings.append(f"{rel} article source references non-formal path {token}: {source}")
            if path.name == "main.tex":
                text = path.read_text(encoding="utf-8", errors="ignore")
                token = _contains_restricted_path(text)
                if token:
                    warnings.append(f"{rel} contains direct {token} path")

    # Stale write runtime artifact guard. Scan direct write/ child directories
    # outside write/jobs/ and warn on real artifacts without deleting them.
    _write_root = write_root if write_root is not None else write_jobs_dir.parent
    if _write_root.exists():
        for child in sorted(_write_root.iterdir()):
            if not child.is_dir() or child.name == "jobs":
                continue
            for path in child.rglob("*"):
                if not path.is_file():
                    continue
                if path.suffix.lower() in _REAL_ARTIFACT_SUFFIXES:
                    rel = normalize_repo_path(path)
                    warnings.append(f"stale write runtime artifact present: {rel}")

    return {
        "valid": True,
        "warning_count": len(warnings),
        "warnings": warnings,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Report directory hygiene warnings without deleting user data.")
    parser.add_argument("--project-root", type=Path, default=Path(PROJECT_ROOT))
    parser.add_argument("--all-catalog", type=Path, default=Path(ALL_CATALOG_PATH))
    parser.add_argument("--papers-dir", type=Path, default=Path(PAPERS_DIR))
    parser.add_argument("--paper-raw-dir", type=Path, default=Path(PAPER_RAW_DIR))
    parser.add_argument("--write-jobs-dir", type=Path, default=Path(WRITE_JOBS_DIR))
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    report = check_directory_hygiene(
        project_root=args.project_root,
        all_catalog_path=args.all_catalog,
        papers_dir=args.papers_dir,
        paper_raw_dir=args.paper_raw_dir,
        write_jobs_dir=args.write_jobs_dir,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Canonical, side-effect-free repository hygiene policy."""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

LOCAL_TOOL_STATE_PREFIXES = (".workbuddy/", ".reasonix/", ".local/")
RUNTIME_DATA_PREFIXES = (
    "data/paper_raw/", "data/papers/", "data/raw/", "data/raw_all/",
    "data/staging/", "data/transactions/", "data/tmp/", "data/logs/",
    "data/jobs/", "data/import_work/", "data/llm_work/", "data/locks/", "data/cache/",
    "data/discovery/doi_candidates/", "data/discovery/pdf_fetch_logs/",
    "data/discovery/fetch_logs/", "data/discovery/keyword_notebooks/",
	"data/discovery/keyword_notebooks_retired/",
    "data/discovery/page_journals/", "data/discovery/receipts/",
    "data/discovery/pending_pages/", "data/discovery/locks/",
    "data/discovery/exports/", "data/discovery/queries/",
    "data/discovery/reports/", "data/discovery/logs/",
    "data/catalog/",
    "write/jobs/", "output/", "reports/", "logs/", "tmp/", "temp/",
)
RUNTIME_REPORT_PATHS = (
    "data/cleanup_report.json",
)
LOCAL_CREDENTIAL_NAMES = frozenset({
    ".env", ".env.local", "credentials.json", "token.json", "secrets.json",
    "service-account.json",
})
ALLOWED_RUNTIME_PLACEHOLDERS = frozenset({
    "data/paper_raw/.gitkeep", "data/papers/.gitkeep", "data/raw/.gitkeep",
    "data/raw_all/.gitkeep", "data/staging/.gitkeep",
    "data/transactions/.gitkeep", "data/tmp/.gitkeep", "data/logs/.gitkeep",
    "data/jobs/.gitkeep", "data/import_work/.gitkeep", "data/locks/.gitkeep",
    "data/catalog/.gitkeep",
    "write/jobs/.gitkeep", "reports/.gitkeep",
    "data/discovery/queries/.gitkeep",
    "data/discovery/doi_candidates/.gitkeep",
    "data/discovery/pdf_fetch_logs/.gitkeep",
    "data/discovery/queries/keywords.example.txt",
    "reports/report_template.md",
})


def normalize_repo_rel_path(path: str | Path) -> str:
    """Normalize a repository-relative path for case-insensitive policy checks."""
    value = str(path).replace("\\", "/").strip()
    value = re.sub(r"/+", "/", value)
    while value.startswith("./"):
        value = value[2:]
    return value.rstrip("/").casefold()


def is_allowed_runtime_placeholder(path: str | Path) -> bool:
    return normalize_repo_rel_path(path) in ALLOWED_RUNTIME_PLACEHOLDERS


def is_local_tool_state(path: str | Path) -> bool:
    rel = normalize_repo_rel_path(path) + "/"
    return rel.startswith(LOCAL_TOOL_STATE_PREFIXES)


def is_runtime_report(path: str | Path) -> bool:
    return normalize_repo_rel_path(path) in RUNTIME_REPORT_PATHS


def is_runtime_data_member(path: str | Path) -> bool:
    rel = normalize_repo_rel_path(path) + "/"
    return rel.startswith(RUNTIME_DATA_PREFIXES)


def _is_local_credential(path: str | Path) -> bool:
    return PurePosixPath(normalize_repo_rel_path(path)).name in LOCAL_CREDENTIAL_NAMES


def is_forbidden_snapshot_member(path: str | Path) -> bool:
    if is_allowed_runtime_placeholder(path):
        return False
    return (is_local_tool_state(path) or is_runtime_data_member(path)
            or is_runtime_report(path) or _is_local_credential(path))


def is_forbidden_git_member(path: str | Path) -> bool:
    if is_allowed_runtime_placeholder(path):
        return False
    return is_forbidden_snapshot_member(path)


# Compatibility for the first policy revision; callers should use the public name.
normalize_repository_path = normalize_repo_rel_path


def runtime_workspace_counts(paths: list[str]) -> dict[str, int]:
    raw: set[str] = set()
    papers: set[str] = set()
    for value in paths:
        parts = PurePosixPath(normalize_repo_rel_path(value)).parts
        if len(parts) >= 3 and parts[:2] == ("data", "paper_raw") and parts[2] != ".gitkeep":
            raw.add(parts[2])
        if len(parts) >= 3 and parts[:2] == ("data", "papers") and parts[2] != ".gitkeep":
            papers.add(parts[2])
    return {"paper_raw": len(raw), "papers": len(papers)}

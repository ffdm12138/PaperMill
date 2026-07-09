"""打包 git 跟踪的所有文件 + 轻量文本/结构文件为 audit snapshot zip。

这是 lightweight audit/handoff snapshot，不是纯源码 release 包，也不是完整数据备份。

契约：
- Git 仓库保持 source-only hygiene：真实文献资产、PDF、图片、运行时数据和日志不得被 git tracked。
- 但 `mineru_snapshot.zip` 会在包含所有程序代码、测试、文档、配置的基础上，
  额外扫描整个工作区中的轻量文本/结构文件（`.json` / `.md` / `.yaml` / `.toml` / `.csv`
  / `.bib` / `.tex` / `.py` / `.sh` / `.bat` 等），包括被 `.gitignore` 忽略的
  `data/papers`、`data/paper_raw` 等目录中的 catalog、metadata、markdown、source_records。
- zip **不包含** PDF、图片、日志、缓存、临时文件、数据库、模型权重、密钥和大文件。
- zip 中出现的轻量运行时样本不代表 git 污染。
- **Workspace sampling**: `data/paper_raw/` 和 `data/papers/` 各最多保留
  5 个样例 workspace（按目录名升序确定性选择）。
  完整数据备份请使用专门的备份/导出流程。
- **Secret scan**: 仅扫描进入 snapshot zip 的文件，不保证全仓无 secret。
  完整仓库 secret 扫描请使用独立的 hygiene task。

用法：
    python scripts/pack_repo.py                         # audit profile (默认)
    python scripts/pack_repo.py --profile source        # 仅 git-tracked source
    python scripts/pack_repo.py --name v2 --profile audit
"""
import argparse
from dataclasses import dataclass
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
ZIP_NAME_BASE = "mineru_snapshot"
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
_PAPER_NUMBER_RE = re.compile(r"^\d{16}$")
_WORKSPACE_SAMPLE_LIMIT = 5

# ── Packaging profiles ────────────────────────────────────────────
# audit (default): source code + git-ignored runtime samples
# source:          only git-tracked source + .gitkeep
PACK_PROFILE = "audit"

# ── Lightweight audit snapshot: allowlist / denylist ──────────────
# LIGHTWEIGHT_ALLOWED_SUFFIXES: text/structure files allowed in zip
# (both git-tracked AND extra-scanned workspace files).
# HEAVY_OR_BINARY_DENIED_SUFFIXES: heavy/binary files NEVER allowed.
# DENIED_PATH_PARTS: directory components whose contents are NEVER allowed.
# DENIED_NAMES: specific filenames NEVER allowed (secrets).
#
# The extra workspace scan (audit profile) collects all files matching
# LIGHTWEIGHT_ALLOWED_SUFFIXES from across the entire project tree,
# then filters through DENIED_PATH_PARTS / DENIED_NAMES / DENIED_SUFFIXES.
# This ensures .json/.md/.txt/.yaml etc. from data/papers, data/paper_raw,
# data/catalog, write/jobs, and any other directory can enter the zip,
# while PDFs, images, logs, caches, secrets, binaries, and large files stay out.

LIGHTWEIGHT_ALLOWED_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".csv",
    ".tsv",
    ".bib",
    ".tex",
    ".sty",
    ".cls",
    ".sh",
    ".bat",
    ".ps1",
    ".html",
    ".css",
    ".js",
    ".gitkeep",
    ".paper.number",
}

HEAVY_OR_BINARY_DENIED_SUFFIXES = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".svgz",
    ".zip",
    ".tar",
    ".gz",
    ".7z",
    ".rar",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
    ".tmp",
    ".bak",
    ".pkl",
    ".pickle",
    ".pt",
    ".pth",
    ".onnx",
    ".bin",
    ".npy",
    ".npz",
    ".parquet",
}

# Specific filenames NEVER allowed in zip regardless of profile.
DENIED_NAMES = {
    ".env",
    ".env.local",
    "credentials.json",
    "token.json",
    "secrets.json",
    "service-account.json",
    ".dockerignore",
}

# Path components whose contents are NEVER allowed in zip.
DENIED_PATH_PARTS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    "node_modules",
    ".venv",
    "venv",
    "output",
    "images",
    "cache",
    "tmp",
    "temp",
    "logs",
}

# Size limits (oversized files are skipped with a warning).
SINGLE_FILE_MAX_BYTES = 50 * 1024 * 1024   # 50 MB
ZIP_MAX_BYTES = 600 * 1024 * 1024           # 600 MB

SECRET_PLACEHOLDERS = {
    "your@email.com",
    "your_key_if_needed",
    "test@example.com",
    "test-openalex-key",
}


@dataclass(frozen=True)
class SecretFinding:
    """A single secret-like literal found during snapshot scanning.

    Only metadata about the finding is stored — the matched value is never
    kept in this object, logged, or printed.
    """
    rule: str
    path: str
    line: int


SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "openalex_email_assignment",
        re.compile(
            r"^[ \t]*(?:export[ \t]+|\$env:)?"
            r"OPENALEX_EMAIL[ \t]*=[ \t]*"
            r"[\"']?([^\"'\s#]+@[^\"'\s#]+)[\"']?",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        "openalex_api_key_assignment",
        re.compile(
            r"^[ \t]*(?:export[ \t]+|\$env:)?"
            r"OPENALEX_API_KEY[ \t]*=[ \t]*"
            r"[\"']?([A-Za-z0-9._\-]{10,})[\"']?",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    ("semantic_scholar_api_key_assignment", re.compile(r"SEMANTIC_SCHOLAR_API_KEY\s*=\s*[\"']([A-Za-z0-9_\-]{10,})[\"']", re.IGNORECASE)),
    ("authorization_bearer_literal", re.compile(r"Authorization\s*:\s*Bearer\s+([A-Za-z0-9._\-]{16,})", re.IGNORECASE)),
    ("bearer_literal", re.compile(r"Bearer\s+([A-Za-z0-9._\-]{24,})", re.IGNORECASE)),
    ("x_api_key_literal", re.compile(r"x-api-key\s*[:=]\s*[\"']?([A-Za-z0-9._\-]{16,})[\"']?", re.IGNORECASE)),
    ("wiley_tdm_token_literal", re.compile(r"Wiley-TDM-Client-Token\s*[:=]\s*[\"']?([A-Za-z0-9._\-]{12,})[\"']?", re.IGNORECASE)),
    ("elsevier_api_key_literal", re.compile(r"X-ELS-APIKey\s*[:=]\s*[\"']?([A-Za-z0-9._\-]{12,})[\"']?", re.IGNORECASE)),
    ("generic_api_key_assignment", re.compile(r"\bapi_key\s*=\s*[\"']([A-Za-z0-9._\-]{16,})[\"']", re.IGNORECASE)),
]


def scan_text_for_secrets(text: str, rel_path: str = "") -> list[SecretFinding]:
    """Return literal secret findings while allowing bare env-var names.

    The matched value is never stored in the finding — only the rule name,
    file path, and 1-indexed line number are returned.
    """
    findings: list[SecretFinding] = []
    for name, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(1) if match.groups() else match.group(0)
            if value in SECRET_PLACEHOLDERS or value.startswith("your_"):
                continue
            line = text[:match.start()].count("\n") + 1
            findings.append(SecretFinding(
                rule=name,
                path=rel_path,
                line=line,
            ))
    return findings


def scan_files_for_secrets(files: list[str]) -> list[SecretFinding]:
    """Scan files for hardcoded credential-like patterns.

    Test files (``tests/``) are excluded — they legitimately contain fake
    credentials for unit-test purposes. The dedicated hygiene test
    ``test_no_hardcoded_openalex_secrets.py`` separately covers non-test
    tracked files.
    """
    findings: list[SecretFinding] = []
    for rel in files:
        if rel.startswith("tests/"):
            continue
        path = PROJECT_ROOT / rel
        if not _lightweight_suffix_match(rel, path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        findings.extend(scan_text_for_secrets(text, rel))
    return findings


def _lightweight_suffix_match(rel: str, path: Path) -> bool:
    """True if rel/path has a lightweight allow-listed suffix.

    Python's ``Path.suffix`` treats dotfiles like ``.gitkeep`` as having no
    suffix (``Path("a/.gitkeep").suffix == ""``) and multi-dot names like
    ``0000000000000001.paper.number`` as suffix ``.number`` (not
    ``.paper.number``). So a bare ``path.suffix`` membership check misses
    ``.gitkeep`` and ``.paper.number`` markers. Handle those two edge cases
    explicitly; everything else falls through to the standard suffix set.
    """
    if path.name == ".gitkeep" or rel.endswith(".gitkeep"):
        return True
    if rel.endswith(".paper.number"):
        return True
    return path.suffix.lower() in LIGHTWEIGHT_ALLOWED_SUFFIXES


def _heavy_suffix_match(path: Path) -> bool:
    """Like _lightweight_suffix_match, but for the heavy/binary deny set.

    ``.paper.number`` is not heavy; guard against the same multi-dot suffix
    quirk以防 a future heavy suffix ending in ``.number`` mis-firing.
    """
    return path.suffix.lower() in HEAVY_OR_BINARY_DENIED_SUFFIXES


def _safe_for_zip(rel_path: str) -> bool:
    """路径能否安全写入 zip（不含 surrogate 且可 UTF-8 编码）。"""
    if _SURROGATE_RE.search(rel_path):
        return False
    try:
        rel_path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    try:
        zipfile.ZipInfo(rel_path)
    except Exception:
        return False
    return True


def _is_local_backup_artifact(path: Path) -> bool:
    name = path.name.lower()
    return (
        name.endswith(".bak")
        or ".bak." in name
        or name.endswith(".backup")
        or ".backup." in name
        or name.endswith(".tmp")
        or name.endswith(".temp")
        or ".bak_" in name
    )


def _should_pack(rel_path: str, *, require_lightweight: bool = False) -> bool:
    """Universal deny rules + optional lightweight suffix check.

    ``require_lightweight=False`` (default): for git-tracked files.
        Only universal denies apply — a git-tracked ``Dockerfile``
        (suffixless) passes through.

    ``require_lightweight=True``: for the extra workspace scan
        (audit profile).  The file MUST have a suffix in
        ``LIGHTWEIGHT_ALLOWED_SUFFIXES`` AND pass all universal denies.
    """
    path = Path(rel_path)
    rel = rel_path.replace("\\", "/")

    # 1. Skip local agent / research tooling state directories.
    if path.parts and path.parts[0] in {".reasonix"}:
        return False
    # 2. Skip test migration tombstone files.
    if rel.endswith("._deleted") or rel.endswith(".py._deleted"):
        return False
    # 3. Skip root scratch files (shell redirection accidents).
    _ROOT_SCRATCH = {"=", "keep_rank"}
    if rel in _ROOT_SCRATCH:
        return False
    if rel == "snapshot_manifest.json":
        return False
    # 4. Skip local backup / temporary artifacts.
    if _is_local_backup_artifact(path):
        return False
    # 5. Skip HEAVY_OR_BINARY_DENIED_SUFFIXES regardless of origin.
    if _heavy_suffix_match(path):
        return False
    # 6. Skip import/ directory (external-source PDFs).
    if path.parts and path.parts[0] == "import":
        return False

    # 7. Skip DENIED_PATH_PARTS — any component match blocks the file.
    for denied_part in DENIED_PATH_PARTS:
        if denied_part in path.parts:
            return False

    # 8. Skip DENIED_NAMES.
    if path.name in DENIED_NAMES:
        return False

    # 9. Skip write/ workspace (only .gitkeep / README.md).
    if path.parts and path.parts[0] == "write":
        _WRITE_KEEP = {"write/README.md", "write/.gitkeep", "write/jobs/.gitkeep"}
        if rel not in _WRITE_KEEP:
            return False

    # 10. Skip runtime data directories (non-gitkeep).
    _DATA_SKIP_DIRS = {
        "data/llm_work", "data/tmp", "data/logs", "data/jobs",
        "data/transactions", "data/jobs/upload_staging",
        "data/import_work",
        "data/discovery/doi_candidates", "data/discovery/pdf_fetch_logs",
        "data/discovery/fetch_logs", "data/discovery/queries",
        "data/discovery/reports", "data/discovery/keyword_notebooks",
        "data/discovery/pending_pages", "data/discovery/locks",
        "data/discovery/exports", "data/discovery/logs",
    }
    # Committable example files that live inside otherwise-skipped runtime dirs.
    _RUNTIME_DIR_KEEPERS = {".gitkeep", "keywords.example.txt"}
    for skip_dir in _DATA_SKIP_DIRS:
        if (rel.startswith(skip_dir + "/") or rel == skip_dir) \
                and path.name not in _RUNTIME_DIR_KEEPERS:
            return False

    # 11. Skip data/locks/*.lock files.
    if rel.startswith("data/locks/") and path.suffix == ".lock":
        return False

    # 12. Only allow reports/.gitkeep and template files.
    if rel.startswith("reports/"):
        if rel == "reports/.gitkeep" or "template" in path.name.lower():
            return True
        return False

    # 13. Skip locally generated catalog indexes / ledgers.
    _GENERATED_CATALOG_FILES = {
        "data/catalog/all.catalog.json",
        "data/catalog/paper_index.json",
        "data/catalog/paper_number_ledger.json",
        "data/catalog/paper_number_ledger.json.bak",
        "data/catalog/catalog_migration_report.json",
        "data/catalog/metadata_quality_report.json",
    }
    if rel in _GENERATED_CATALOG_FILES:
        return False

    # 14. Skip generated catalog runtime files.
    if rel.startswith("data/catalog/"):
        cname = path.name.lower()
        if cname.startswith("paper_number_ledger.bak_") and cname.endswith(".json"):
            return False
        if cname.endswith(".runtime.json"):
            return False

    # 15. Source profile: block runtime data dirs entirely (only .gitkeep).
    if PACK_PROFILE == "source":
        _SRC_DATA_DIRS = {"data/paper_raw", "data/papers", "data/raw", "data/raw_all"}
        if any(rel.startswith(d + "/") for d in _SRC_DATA_DIRS) or rel in _SRC_DATA_DIRS:
            return path.name == ".gitkeep"

    # 16. For extra-scanned (non-git) files: must be lightweight text.
    if require_lightweight:
        return _lightweight_suffix_match(rel, path)

    return _safe_for_zip(rel_path)


def git_tracked_files(profile: str = "audit") -> list[str]:
    """Return sorted list of files (relative paths) for the snapshot.

    1. All git-tracked files filtered through ``_should_pack(require_lightweight=False)``.
    2. **Audit profile only**: extra workspace-wide scan for lightweight
       text/structure files (``.json`` / ``.md`` / ``.yaml`` / etc.) that
       pass ``_should_pack(require_lightweight=True)``.
       This scans the entire project tree, so files in git-ignored directories
       like ``data/papers/`` or ``data/paper_raw/`` are included if they
       are lightweight text files.
    """
    try:
        # Only --cached (tracked files). Untracked-but-not-ignored files are
        # NOT included here: source profile must be git-tracked only, and the
        # audit profile's workspace scan below already picks up lightweight
        # untracked/gitignored files as a superset of --others --exclude-standard.
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "ls-files", "--cached", "-z"],
            capture_output=True, text=True, encoding="utf-8",
            check=False,
        )
        if result.returncode == 0:
            files = [
                f.replace("\\", "/")
                for f in result.stdout.split("\0")
                if f and (PROJECT_ROOT / f).exists()
            ]
            if files:
                safe = [f for f in files if _should_pack(f)]
                skipped = [f for f in files if not _should_pack(f)]
                for f in skipped:
                    print(f"  [SKIP] excluded from snapshot: {f!r}")

                # Extra (audit only): workspace-wide scan for lightweight
                # text/structure files (catalog, metadata, markdown, configs,
                # source_records, etc.) even if git-ignored.
                if profile == "audit":
                    extras: list[str] = []
                    for path in sorted(PROJECT_ROOT.rglob("*")):
                        if not path.is_file():
                            continue
                        rel = path.relative_to(PROJECT_ROOT).as_posix()
                        if _should_pack(rel, require_lightweight=True):
                            extras.append(rel)
                    if extras:
                        safe_set = set(safe)
                        new_count = sum(1 for e in extras if e not in safe_set)
                        safe = sorted(safe_set | set(extras))
                        if new_count:
                            print(f"  Added {new_count} lightweight file(s) from workspace")

                print(f"  Found {len(safe)} total files")
                return safe
        else:
            print(f"[WARN] git ls-files failed: {result.stderr}")
    except Exception as e:
        print(f"[WARN] git ls-files unavailable: {e}")

    files = _scan_repo_files()
    print(f"  Found {len(files)} files from filesystem scan")
    return files


def _scan_repo_files() -> list[str]:
    """Fallback for zip snapshots without .git metadata.

    Scans entire workspace and filters through
    ``_should_pack(require_lightweight=True)`` so that lightweight
    text/structure files (including those in git-ignored runtime dirs)
    are included.
    """
    out: list[str] = []
    for path in sorted(PROJECT_ROOT.rglob("*")):
        if not path.is_file():
            continue
        if path.name.startswith("mineru_snapshot") and path.suffix == ".zip":
            continue
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if not _should_pack(rel, require_lightweight=True):
            continue
        out.append(rel)
    return sorted(out)


# -- workspace sampling ---------------------------------------------------


def _is_papers_workspace(dir_path: Path) -> bool:
    """A directory is a papers workspace if it contains any core paper asset."""
    _CORE_GLOBS = ("*.metadata.json", "*.catalog.json", "*.paper.number", "*.pdf", "*.md")
    return any(list(dir_path.glob(g)) for g in _CORE_GLOBS)


def _selected_sample_workspaces(root: Path, limit: int = _WORKSPACE_SAMPLE_LIMIT) -> dict:
    """Return deterministic sample workspace selections for the snapshot."""
    result = {"paper_raw_selected": set(), "paper_raw_total": 0,
              "papers_selected": set(), "papers_total": 0}
    # paper_raw: only 16-digit subdirs
    paper_raw_dir = root / "data" / "paper_raw"
    if paper_raw_dir.exists():
        raw_dirs = sorted(
            [d for d in paper_raw_dir.iterdir()
             if d.is_dir() and _PAPER_NUMBER_RE.match(d.name)],
            key=lambda d: d.name
        )
        result["paper_raw_total"] = len(raw_dirs)
        result["paper_raw_selected"] = {
            d.relative_to(root).as_posix() for d in raw_dirs[:limit]
        }
    # papers: only subdirs containing core paper assets, skipping non-paper dirs
    papers_dir = root / "data" / "papers"
    _PAPERS_SKIP = {"images", "cache", "tmp", "logs", "reports", "__pycache__"}
    if papers_dir.exists():
        candidates = [
            d for d in papers_dir.iterdir()
            if d.is_dir() and d.name not in _PAPERS_SKIP
            and not d.name.startswith(".") and _is_papers_workspace(d)
        ]
        papers_dirs = sorted(candidates, key=lambda d: d.name)
        result["papers_total"] = len(papers_dirs)
        result["papers_selected"] = {
            d.relative_to(root).as_posix() for d in papers_dirs[:limit]
        }
    return result


def _resolve_workspace_prefix(rel_path: str) -> str | None:
    """Return workspace prefix like ``data/paper_raw/0000000000000001`` or None.

    Root-level files (depth < 4) are never considered workspace files.
    For ``data/paper_raw/``, only paths where the third component is a
    16-digit number are workspace files.  For ``data/papers/``, paths
    with at least 4 parts are workspace files.
    """
    path = Path(rel_path)
    if len(path.parts) < 4:
        return None
    if path.parts[0] == "data" and path.parts[1] == "paper_raw":
        if _PAPER_NUMBER_RE.match(path.parts[2]):
            return f"data/paper_raw/{path.parts[2]}"
        return None
    if path.parts[0] == "data" and path.parts[1] == "papers":
        return f"data/papers/{path.parts[2]}"
    return None


def _should_sample_keep(rel_path: str, sampling: dict) -> bool:
    """Return True if the file should be included in the snapshot.

    Files outside any workspace always pass.  Files inside a workspace
    must belong to a selected workspace.
    """
    ws = _resolve_workspace_prefix(rel_path)
    if ws is None:
        return True
    if ws.startswith("data/paper_raw/"):
        return ws in sampling["paper_raw_selected"]
    if ws.startswith("data/papers/"):
        return ws in sampling["papers_selected"]
    return True


def _verify_snapshot_sampling(zip_path: Path, sampling: dict) -> list[str]:
    """Post-pack self-check: verify workspace sampling constraints."""
    errors: list[str] = []
    raw_ws: set[str] = set()
    papers_ws: set[str] = set()
    manifest_count = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            if name == "snapshot_manifest.json":
                manifest_count += 1
                continue
            ws = _resolve_workspace_prefix(name)
            if ws is None:
                continue
            if ws.startswith("data/paper_raw/"):
                raw_ws.add(ws)
            elif ws.startswith("data/papers/"):
                papers_ws.add(ws)
    limit = _WORKSPACE_SAMPLE_LIMIT
    if len(raw_ws) > limit:
        errors.append(
            f"snapshot contains {len(raw_ws)} paper_raw workspaces (limit: {limit})"
        )
    if len(papers_ws) > limit:
        errors.append(
            f"snapshot contains {len(papers_ws)} papers workspaces (limit: {limit})"
        )
    if manifest_count != 1:
        errors.append(f"snapshot_manifest.json count is {manifest_count}, expected 1")
    if not manifest_count:
        errors.append("snapshot_manifest.json missing from zip")
    return errors


def main():
    parser = argparse.ArgumentParser(
        description="Build audit snapshot ZIP (source + optional runtime samples)",
    )
    parser.add_argument(
        "--name", type=str, default="",
        help="Suffix for zip filename, e.g. v2 -> mineru_snapshot_v2.zip",
    )
    parser.add_argument(
        "--profile", type=str, default="audit", choices=["audit", "source"],
        help='Packaging profile: "audit" (default) includes git-ignored runtime '
             'samples; "source" only includes git-tracked source + .gitkeep.',
    )
    args = parser.parse_args()

    global PACK_PROFILE
    PACK_PROFILE = args.profile

    suffix = args.name
    zip_name = f"{ZIP_NAME_BASE}_{suffix}.zip" if suffix else f"{ZIP_NAME_BASE}.zip"
    zip_path = PROJECT_ROOT / zip_name

    if PACK_PROFILE == "audit":
        print(f"  Profile: audit (source + runtime samples)")
    else:
        print(f"  Profile: source (git-tracked only)")

    files = git_tracked_files(profile=PACK_PROFILE)
    if not files:
        print("[ERROR] No tracked files, aborting")
        sys.exit(1)

    # Apply workspace sampling (always active; keeps snapshot lightweight)
    sampling = _selected_sample_workspaces(PROJECT_ROOT, limit=_WORKSPACE_SAMPLE_LIMIT)
    before = len(files)
    files = [f for f in files if _should_sample_keep(f, sampling)]
    dropped = before - len(files)
    if dropped:
        print(f"  [INFO] data/paper_raw sampled: {len(sampling['paper_raw_selected'])} of {sampling['paper_raw_total']} workspaces")
        print(f"  [INFO] data/papers sampled: {len(sampling['papers_selected'])} of {sampling['papers_total']} workspaces")
        print(f"         {dropped} file(s) from non-sampled workspaces excluded")

    # Secret scan on filtered list (only files entering the zip).
    # This scan is scoped to the snapshot — it does not guarantee the
    # full repository is secret-free.  Non-sampled workspaces are not
    # in the zip and are intentionally not scanned here.
    secret_findings = scan_files_for_secrets(files)
    if secret_findings:
        print("[ERROR] Secret-like literal(s) found; refusing to pack")
        for finding in secret_findings[:20]:
            print(f"  {finding.path}: {finding.rule} (line {finding.line})")
        if len(secret_findings) > 20:
            print(f"  ... {len(secret_findings) - 20} more")
        sys.exit(1)

    count = 0
    skipped = 0
    zip_total_bytes = 0
    oversized: list[str] = []

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(files):
            src = PROJECT_ROOT / f
            if not src.exists():
                print(f"  [SKIP] missing: {f}")
                skipped += 1
                continue
            if not _safe_for_zip(f):
                print(f"  [SKIP] unsafe path encoding: {f!r}")
                skipped += 1
                continue
            # Size limits (audit profile)
            file_size = src.stat().st_size
            if PACK_PROFILE == "audit":
                if file_size > SINGLE_FILE_MAX_BYTES:
                    print(f"  [SKIP] oversized ({file_size / 1024 / 1024:.1f} MB): {f}")
                    oversized.append(f)
                    skipped += 1
                    continue
            zf.write(src, f)
            zip_total_bytes += file_size
            count += 1
            # Total zip limit check
            if PACK_PROFILE == "audit" and zip_total_bytes > ZIP_MAX_BYTES:
                print(f"  [WARN] total zip size exceeds {ZIP_MAX_BYTES / 1024 / 1024:.0f} MB limit, stopping")
                break

        # Write snapshot manifest as last entry in the same session.
        # Sanitize workspace paths: surrogates or other encoding-unsafe
        # names (possible when a zip is decompressed cross-platform and
        # re-packed) must not crash the manifest write.
        def _manifest_safe_paths(paths: set[str]) -> tuple[list[str], int]:
            safe: list[str] = []
            skipped = 0
            for p in sorted(paths):
                if _safe_for_zip(p):
                    safe.append(p)
                else:
                    skipped += 1
            return safe, skipped

        raw_included, raw_unsafe = _manifest_safe_paths(sampling["paper_raw_selected"])
        papers_included, papers_unsafe = _manifest_safe_paths(sampling["papers_selected"])
        manifest = {
            "snapshot_type": "lightweight",
            "paper_raw_sample_limit": _WORKSPACE_SAMPLE_LIMIT,
            "paper_raw_total_detected": sampling["paper_raw_total"],
            "paper_raw_included": raw_included,
            "papers_sample_limit": _WORKSPACE_SAMPLE_LIMIT,
            "papers_total_detected": sampling["papers_total"],
            "papers_included": papers_included,
            "sampling_note": (
                "Only sampled data/paper_raw and data/papers workspaces are "
                "included.  Source data and git working tree are unchanged."
            ),
        }
        unsafe_total = raw_unsafe + papers_unsafe
        if unsafe_total:
            manifest["skipped_unsafe_manifest_paths"] = unsafe_total
        zf.writestr("snapshot_manifest.json",
                    json.dumps(manifest, indent=2, ensure_ascii=False))
        count += 1  # manifest counts toward total

    # Post-pack self-check
    check_errors = _verify_snapshot_sampling(zip_path, sampling)
    if check_errors:
        print("[ERROR] Snapshot self-check failed:")
        for e in check_errors:
            print(f"  {e}")
        sys.exit(1)

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"[OK] Packed: {zip_name} ({count} files, {size_mb:.1f} MB)")
    if skipped:
        print(f"     {skipped} file(s) skipped")
    if oversized:
        print(f"     {len(oversized)} oversized file(s) not included")
    if dropped:
        print(f"[INFO] data/paper_raw sampled: {len(sampling['paper_raw_selected'])} of {sampling['paper_raw_total']} workspaces")
        print(f"[INFO] data/papers sampled: {len(sampling['papers_selected'])} of {sampling['papers_total']} workspaces")
    print(f"     {zip_path}")


if __name__ == "__main__":
    main()

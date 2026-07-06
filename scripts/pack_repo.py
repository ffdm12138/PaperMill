"""打包 git 跟踪的所有文件 + 轻量文本/结构文件为 audit snapshot zip。

这是 lightweight audit snapshot，不是纯源码 release 包，也不是完整运行时备份。

契约：
- Git 仓库保持 source-only hygiene：真实文献资产、PDF、图片、运行时数据和日志不得被 git tracked。
- 但 `mineru_snapshot.zip` 会在包含所有程序代码、测试、文档、配置的基础上，
  额外扫描整个工作区中的轻量文本/结构文件（`.json` / `.md` / `.yaml` / `.toml` / `.csv`
  / `.bib` / `.tex` / `.py` / `.sh` / `.bat` 等），包括被 `.gitignore` 忽略的
  `data/papers`、`data/paper_raw` 等目录中的 catalog、metadata、markdown、source_records。
- zip **不包含** PDF、图片、日志、缓存、临时文件、数据库、模型权重、密钥和大文件。
- zip 中出现的轻量运行时样本不代表 git 污染。

用法：
    python scripts/pack_repo.py                         # audit profile (默认)
    python scripts/pack_repo.py --profile source        # 仅 git-tracked source
    python scripts/pack_repo.py --name v2 --profile audit
"""
import argparse
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
    }
    for skip_dir in _DATA_SKIP_DIRS:
        if (rel.startswith(skip_dir + "/") or rel == skip_dir) \
                and path.name != ".gitkeep":
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

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"[OK] Packed: {zip_name} ({count} files, {size_mb:.1f} MB)")
    if skipped:
        print(f"     {skipped} file(s) skipped")
    if oversized:
        print(f"     {len(oversized)} oversized file(s) not included")
    print(f"     {zip_path}")


if __name__ == "__main__":
    main()

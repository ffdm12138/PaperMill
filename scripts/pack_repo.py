"""打包 git 跟踪的所有文件为 zip，放在项目根目录。

用途：GitHub/Gitee 提交后有缓存延迟，zip 可直接下载分发。
排除自身和 .zip 文件，避免死循环。

用法：
    python scripts/pack_repo.py              # 生成 mineru_snapshot.zip
    python scripts/pack_repo.py --name v2   # 生成 mineru_snapshot_v2.zip
"""
import re
import subprocess
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
ZIP_NAME_BASE = "mineru_snapshot"
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


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


def _should_pack(rel_path: str) -> bool:
    path = Path(rel_path)
    rel = rel_path.replace("\\", "/")
    # 跳过备份文件
    if ".bak_" in path.name:
        return False
    # 跳过 PDF / 图片 / 大二进制文件（版权语料，不进 zip）
    _SKIP_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".docx", ".pptx", ".xlsx"}
    if path.suffix.lower() in _SKIP_SUFFIXES:
        return False
    # 跳过 import 目录（外源 PDF）
    if path.parts and path.parts[0] == "import":
        return False
    # 跳过 _test_batch 目录
    if path.parts and path.parts[0] == "_test_batch":
        return False
    # 跳过 write/ 工作区运行产物（硬排除，不依赖 .gitignore，覆盖 fallback 扫描）。
    # 只保留 write/README.md、write/.gitkeep、write/jobs/.gitkeep；其余（含
    # write/jobs/<job>/** 与 legacy write/<job>/** 下的 .tex/.md/.bib/.pdf 等）一律排除。
    if path.parts and path.parts[0] == "write":
        _WRITE_KEEP = {"write/README.md", "write/.gitkeep", "write/jobs/.gitkeep"}
        if rel not in _WRITE_KEEP:
            return False
    # 跳过数据目录中的版权文件（data/raw, data/papers），但保留 .gitkeep
    _DATA_SKIP_DIRS = {
        "data/raw",
        "data/paper_raw",
        "data/papers",
        "data/llm_work",
        "data/tmp",
        "data/logs",
        "data/jobs",
        "data/transactions",
        "data/jobs/upload_staging",
        "data/discovery/doi_candidates",
        "data/discovery/pdf_fetch_logs",
        "data/discovery/fetch_logs",
        "data/import_work",
    }
    for skip_dir in _DATA_SKIP_DIRS:
        if (rel.startswith(skip_dir + "/") or rel == skip_dir) \
                and path.name != ".gitkeep":
            return False
    if rel.startswith("data/locks/") and path.suffix == ".lock":
        return False
    if rel.startswith("reports/") and path.suffix.lower() in {".json", ".err"}:
        return False
    # 跳过本地生成的 catalog 索引/账本（含真实库内容，绝不进快照）。
    # 源码快照只提交对应 *.template.json 空模板与 .gitkeep。
    # 这条规则在无 .git 元数据、只能走文件系统扫描时同样生效。
    _GENERATED_CATALOG_FILES = {
        "data/catalog/all.catalog.json",
        "data/catalog/paper_index.json",
        "data/catalog/paper_number_ledger.json",
        "data/catalog/catalog_migration_report.json",
        "data/catalog/metadata_quality_report.json",
    }
    if rel in _GENERATED_CATALOG_FILES:
        return False
    return _safe_for_zip(rel_path)


def git_tracked_files() -> list[str]:
    """返回 git 跟踪文件 + 未忽略的新文件（相对路径）"""
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
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
                if len(safe) < len(files):
                    for f in files:
                        if not _should_pack(f):
                            print(f"  [SKIP] excluded from snapshot: {f!r}")
                print(f"  Found {len(safe)} files from git ls-files")
                return safe
        else:
            print(f"[WARN] git ls-files failed: {result.stderr}")
    except Exception as e:
        print(f"[WARN] git ls-files unavailable: {e}")

    files = _scan_repo_files()
    print(f"  Found {len(files)} files from filesystem scan")
    return files


def _scan_repo_files() -> list[str]:
    """Fallback for zip snapshots without .git metadata。

    跳过无法安全写入 zip 的路径（含 surrogate、UTF-8 编码失败等），打印 SKIP。
    """
    out = []
    excluded_dirs = {".git", "__pycache__", ".pytest_cache"}
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(PROJECT_ROOT).parts
        if any(part in excluded_dirs for part in rel_parts):
            continue
        if path.suffix in {".pyc", ".tmp", ".lock"}:
            continue
        if path.name.startswith("mineru_snapshot") and path.suffix == ".zip":
            continue
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if not _should_pack(rel):
            print(f"  [SKIP] excluded from snapshot: {rel!r}")
            continue
        out.append(rel)
    return sorted(out)


def main():
    suffix = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--name" else ""
    zip_name = f"{ZIP_NAME_BASE}_{suffix}.zip" if suffix else f"{ZIP_NAME_BASE}.zip"
    zip_path = PROJECT_ROOT / zip_name

    files = git_tracked_files()
    if not files:
        print("[ERROR] No tracked files, aborting")
        sys.exit(1)

    count = 0
    skipped = 0
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
            zf.write(src, f)
            count += 1

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"[OK] Packed: {zip_name} ({count} files, {size_mb:.1f} MB)")
    if skipped:
        print(f"     {skipped} file(s) skipped")
    print(f"     {zip_path}")


if __name__ == "__main__":
    main()

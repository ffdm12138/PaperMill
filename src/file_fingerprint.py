"""文件指纹：sha256 计算，用于去重与误跳过防护"""
import hashlib
from pathlib import Path
from datetime import datetime


def compute_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """分块计算文件 sha256，避免大文件一次性读入内存"""
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def compute_md5(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Compute a file md5 in chunks for compatibility duplicate reports."""
    h = hashlib.md5()
    with Path(path).open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def compute_file_hashes(path: Path) -> dict:
    p = Path(path)
    return {
        "md5": compute_md5(p),
        "sha256": compute_sha256(p),
        "file_size": p.stat().st_size,
    }


def file_meta(path: Path) -> dict:
    """采集文件元信息：sha256/size/mtime"""
    p = Path(path)
    st = p.stat()
    return {
        "md5": compute_md5(p),
        "sha256": compute_sha256(p),
        "file_size": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
    }

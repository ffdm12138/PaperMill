import hashlib
from pathlib import Path

from src.utils.file_allocation import allocate_unique_path


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_allocate_unique_path_reuses_same_sha(tmp_path):
    target = tmp_path / "paper.pdf"
    data = b"same"
    target.write_bytes(data)
    path, reused = allocate_unique_path(target, _sha(data))
    assert path == target
    assert reused is True


def test_allocate_unique_path_loops_after_sha8_conflict(tmp_path):
    target = tmp_path / "paper.pdf"
    new_sha = _sha(b"new")
    target.write_bytes(b"old")
    (tmp_path / f"paper_{new_sha[:8]}.pdf").write_bytes(b"other")

    path, reused = allocate_unique_path(target, new_sha)

    assert path.name == f"paper_{new_sha[:8]}_2.pdf"
    assert reused is False

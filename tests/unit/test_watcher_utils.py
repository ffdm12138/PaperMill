"""测试 watcher 文件稳定性检测与临时文件识别"""
import time
import tempfile
from pathlib import Path
from src.watcher_utils import is_file_stable, is_uploading_temp


def test_is_file_stable_static_file():
    """静止文件应判定为稳定"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "test.pdf"
        p.write_bytes(b"hello world")
        assert is_file_stable(p, wait_seconds=0.1) is True


def test_is_file_stable_nonexistent():
    assert is_file_stable(Path("/nonexistent/x.pdf"), wait_seconds=0.1) is False


def test_is_file_stable_empty_file():
    """空文件不应被判定为稳定"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "empty.pdf"
        p.write_bytes(b"")
        assert is_file_stable(p, wait_seconds=0.1) is False


def test_is_uploading_temp():
    assert is_uploading_temp(Path("x.pdf.uploading")) is True
    assert is_uploading_temp(Path("x.PDF.UPLOADING")) is True
    assert is_uploading_temp(Path("x.part")) is True
    assert is_uploading_temp(Path("x.tmp")) is True
    assert is_uploading_temp(Path("x.pdf")) is False

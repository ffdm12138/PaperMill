"""测试 cleaner locate_markdown 确定性选择 + extract 路径安全"""
import tempfile
import pytest
from pathlib import Path
from src.cleaner import MinerUOutputCleaner


def _make_md(path: Path, content: str = "# test"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_exact_method_stem():
    """指定 method + stem 时优先 exact path"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        # 标准结构
        _make_md(d / "mypaper" / "auto" / "mypaper.md", "# auto content")
        _make_md(d / "mypaper" / "ocr" / "mypaper.md", "# ocr content")
        c = MinerUOutputCleaner()
        found = c.locate_markdown(d, method="auto", stem="mypaper")
        assert found is not None
        assert "auto" in str(found)
        assert "auto content" in found.read_text(encoding="utf-8")


def test_exact_ocr_method():
    """指定 ocr 方法时选 ocr 目录"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _make_md(d / "paper" / "auto" / "paper.md", "# auto")
        _make_md(d / "paper" / "ocr" / "paper.md", "# ocr")
        c = MinerUOutputCleaner()
        found = c.locate_markdown(d, method="ocr", stem="paper")
        assert found is not None
        assert "ocr" in str(found)
        assert "ocr" in found.read_text(encoding="utf-8")


def test_multi_method_no_method_specified_fails():
    """多个 method 候选且未指定 method → 返回 None"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _make_md(d / "x" / "auto" / "x.md", "# a")
        _make_md(d / "x" / "ocr" / "x.md", "# o")
        c = MinerUOutputCleaner()
        found = c.locate_markdown(d)
        assert found is None  # 无法确定


def test_single_md_returns_it():
    """唯一 .md 无论位置都返回"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _make_md(d / "deep" / "nested" / "only.md", "# only")
        c = MinerUOutputCleaner()
        found = c.locate_markdown(d)
        assert found is not None
        assert found.name == "only.md"


def test_single_method_candidate_returns_it():
    """只有一个在 method 目录下的候选 → 选它"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _make_md(d / "top.md", "# i am at top, not method")
        _make_md(d / "stem" / "auto" / "stem.md", "# real content")
        c = MinerUOutputCleaner()
        found = c.locate_markdown(d)
        assert found is not None
        assert found.parent.name == "auto"


def test_no_method_candidates_returns_none():
    """多个候选都不在 method 目录 → 返回 None"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _make_md(d / "a.md", "# a")
        _make_md(d / "sub" / "b.md", "# b")
        c = MinerUOutputCleaner()
        found = c.locate_markdown(d)
        assert found is None


def test_extract_rejects_malicious_paper_id():
    """extract 拒绝路径穿越 paper_id"""
    with tempfile.TemporaryDirectory() as td:
        c = MinerUOutputCleaner()
        res = c.extract(td, "../../etc", overwrite=False)
        assert res["success"] is False
        assert "Invalid paper_id" in res["error"]

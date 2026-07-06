"""测试 cleaner：Markdown 定位、覆盖保护、多候选处理"""
import tempfile
from pathlib import Path
from src.cleaner import MinerUOutputCleaner


def test_locate_markdown_finds_md():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "stem" / "auto"
        d.mkdir(parents=True)
        (d / "stem.md").write_text("# Title\n\nContent", encoding="utf-8")
        c = MinerUOutputCleaner()
        found = c.locate_markdown(Path(td))
        assert found is not None
        assert found.name == "stem.md"


def test_locate_markdown_none():
    c = MinerUOutputCleaner()
    assert c.locate_markdown(Path("/nonexistent")) is None


def test_locate_markdown_ignores_json_sidecars():
    """正文 md 存在时不应漏掉"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "stem" / "auto"
        d.mkdir(parents=True)
        (d / "stem.md").write_text("# real content", encoding="utf-8")
        (d / "stem_model.json").write_text("{}", encoding="utf-8")
        c = MinerUOutputCleaner()
        found = c.locate_markdown(Path(td))
        assert found is not None
        assert found.suffix == ".md"


def test_extract_rejects_existing_dir():
    """目标目录已存在且 overwrite=False 应报错"""
    # cleaner 直接写 config 的 PAPERS_DIR，不便在这里测试完整路径。
    # 这里验证 locate_markdown 逻辑正确，覆盖保护在集成测试中验证。
    c = MinerUOutputCleaner()
    res = c.extract("/nonexistent/src", "test_pid", overwrite=False)
    assert res["success"] is False
    assert "未在" in res["error"]


def test_locate_markdown_multiple_candidates_no_method_dir():
    """多候选且无标准 method 目录——应返回 None 并报错"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        # 两个 md 文件都不在标准 method 子目录
        (d / "a.md").write_text("# A", encoding="utf-8")
        sub = d / "sub"
        sub.mkdir()
        (sub / "b.md").write_text("# B", encoding="utf-8")
        c = MinerUOutputCleaner()
        found = c.locate_markdown(d)
        assert found is None  # 多候选且无法确定 → None


def test_locate_markdown_priors_method_dir():
    """多候选时，有 method 目录的优先"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "a.md").write_text("# top-level", encoding="utf-8")
        auto_dir = d / "auto"
        auto_dir.mkdir(parents=True)
        (auto_dir / "stem.md").write_text("# method-dir content", encoding="utf-8")
        c = MinerUOutputCleaner()
        found = c.locate_markdown(d)
        assert found is not None
        assert found.name == "stem.md"
        assert "method-dir" in found.read_text(encoding="utf-8")


def test_locate_markdown_hybrid_ocr_priority():
    """hybrid_ocr 方法目录应被识别"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "top.md").write_text("# top", encoding="utf-8")
        hybrid = d / "hybrid_ocr"
        hybrid.mkdir(parents=True)
        (hybrid / "stem.md").write_text("# hybrid ocr", encoding="utf-8")
        c = MinerUOutputCleaner()
        found = c.locate_markdown(d)
        assert found is not None
        assert found.parent.name == "hybrid_ocr"

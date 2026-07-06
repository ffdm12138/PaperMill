"""测试 cleaner hybrid_auto/vlm_auto 目录兼容 + backend 优先级"""
import tempfile
from pathlib import Path
from src.cleaner import MinerUOutputCleaner


def _make(path: Path, content: str = "# test"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_method_auto_finds_hybrid_auto():
    """method=auto + backend=hybrid-engine 应命中 hybrid_auto"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "stem"
        _make(d / "hybrid_auto" / "stem.md", "# hybrid auto content")
        c = MinerUOutputCleaner()
        found = c.locate_markdown(d, method="auto", stem="stem",
                                  backend="hybrid-engine")
        assert found is not None
        assert found.parent.name == "hybrid_auto"


def test_method_auto_finds_vlm_auto():
    """method=auto + backend=vlm-engine 应命中 vlm_auto"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "stem"
        _make(d / "vlm_auto" / "stem.md", "# vlm auto content")
        c = MinerUOutputCleaner()
        found = c.locate_markdown(d, method="auto", stem="stem",
                                  backend="vlm-engine")
        assert found is not None
        assert found.parent.name == "vlm_auto"


def test_method_ocr_only_hybrid_auto_exists_fails():
    """method=ocr 但只有 hybrid_auto → 应失败，不 fallback"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "stem"
        _make(d / "hybrid_auto" / "stem.md", "# wrong method")
        c = MinerUOutputCleaner()
        found = c.locate_markdown(d, method="ocr", stem="stem")
        assert found is None  # ocr 不接受 hybrid_auto


def test_auto_and_hybrid_auto_prioritizes_by_backend():
    """auto + hybrid_auto 同时存在，backend=hybrid-engine → 选 hybrid_auto"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "stem"
        _make(d / "auto" / "stem.md", "# plain auto")
        _make(d / "hybrid_auto" / "stem.md", "# hybrid auto")
        c = MinerUOutputCleaner()
        found = c.locate_markdown(d, method="auto", stem="stem",
                                  backend="hybrid-engine")
        assert found is not None
        assert found.parent.name == "hybrid_auto"


def test_auto_and_hybrid_auto_no_backend_returns_none():
    """auto + hybrid_auto 同时存在，backend=None → 返回 None（无法确定）"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "stem"
        _make(d / "auto" / "stem.md", "# plain auto")
        _make(d / "hybrid_auto" / "stem.md", "# hybrid auto")
        c = MinerUOutputCleaner()
        found = c.locate_markdown(d, method="auto", stem="stem", backend=None)
        assert found is None  # 无法确定优先级


def test_method_dirs_map():
    """_method_dirs 返回正确的目录映射（hybrid 优先）"""
    c = MinerUOutputCleaner()
    # 默认：hybrid_* 在前
    dirs = c._method_dirs("auto")
    assert dirs[0] == "hybrid_auto"
    assert "auto" in dirs
    assert "hybrid_auto" in c._method_dirs("auto")
    assert "hybrid_ocr" in c._method_dirs("ocr")
    assert "hybrid_txt" in c._method_dirs("txt")
    # vlm-engine 时 vlm_* 插入首位
    vdirs = c._method_dirs("auto", backend="vlm-engine")
    assert vdirs[0] == "vlm_auto"
    # pipeline 时原生目录优先
    pdirs = c._method_dirs("auto", backend="pipeline")
    assert pdirs[0] == "auto"


# ---- Phase 3 收敛：method 给定但 stem 未给时，目录名不得反向决定语义 ----

def test_method_given_but_stem_absent_rejects_wrong_dir():
    """method=ocr 但目录里只有 hybrid_auto → 硬约束失败返回 None，不反向命中。

    守护「文件系统结构偷偷决定逻辑」：method 是唯一 truth，
    目录名不匹配 method 时不得 fallback。
    """
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _make(d / "hybrid_auto" / "stem.md", "# wrong method content")
        c = MinerUOutputCleaner()
        # method 给定、stem 未给：只接受 method 对应目录
        found = c.locate_markdown(d, method="ocr")
        assert found is None


def test_method_given_but_stem_absent_matches_correct_dir():
    """method=ocr 且目录里有 hybrid_ocr → 命中（正向匹配成功）。"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _make(d / "hybrid_ocr" / "stem.md", "# ocr content")
        c = MinerUOutputCleaner()
        found = c.locate_markdown(d, method="ocr", backend="hybrid-engine")
        assert found is not None
        assert found.parent.name == "hybrid_ocr"


def test_method_given_but_stem_absent_rejects_auto_when_ocr():
    """method=ocr，同时存在 hybrid_auto 与 hybrid_ocr → 只取 ocr，不取 auto。

    这是「目录名决定语义」最易出错处：多候选时不得错选其它 method 目录。
    """
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _make(d / "hybrid_auto" / "stem.md", "# auto content")
        _make(d / "hybrid_ocr" / "stem.md", "# ocr content")
        c = MinerUOutputCleaner()
        found = c.locate_markdown(d, method="ocr", backend="hybrid-engine")
        assert found is not None
        assert found.parent.name == "hybrid_ocr"
        assert "ocr content" in found.read_text(encoding="utf-8")

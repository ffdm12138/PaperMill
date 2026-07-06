"""Phase 1 验收：cleaner method 目录硬约束——method=auto/txt 不得命中 hybrid_ocr。

守护「文件系统结构偷偷决定逻辑」：source_dir 自身为 method 目录时，
必须与声明的 method/backend 一致，mismatch 直接返回 None。
"""
import tempfile
from pathlib import Path
from src.cleaner import MinerUOutputCleaner


def _make(path: Path, content: str = "# test"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _dir_case(td, dirname, method, backend):
    """source_dir 本身就是 method 目录，stem.md 直接放在里面。"""
    d = Path(td) / dirname
    _make(d / "doc.md", f"# {dirname}")
    c = MinerUOutputCleaner()
    return c.locate_markdown(d, method=method, stem="doc", backend=backend)


def test_hybrid_ocr_method_auto_rejected():
    """A. source_dir=hybrid_ocr, method=auto, backend=hybrid-engine -> None"""
    with tempfile.TemporaryDirectory() as td:
        assert _dir_case(td, "hybrid_ocr", "auto", "hybrid-engine") is None


def test_hybrid_ocr_method_txt_rejected():
    """B. source_dir=hybrid_ocr, method=txt, backend=hybrid-engine -> None"""
    with tempfile.TemporaryDirectory() as td:
        assert _dir_case(td, "hybrid_ocr", "txt", "hybrid-engine") is None


def test_hybrid_ocr_method_ocr_accepted():
    """C. source_dir=hybrid_ocr, method=ocr, backend=hybrid-engine -> 命中"""
    with tempfile.TemporaryDirectory() as td:
        found = _dir_case(td, "hybrid_ocr", "ocr", "hybrid-engine")
        assert found is not None
        assert found.parent.name == "hybrid_ocr"


def test_hybrid_auto_method_auto_accepted():
    """D. source_dir=hybrid_auto, method=auto, backend=hybrid-engine -> 命中"""
    with tempfile.TemporaryDirectory() as td:
        found = _dir_case(td, "hybrid_auto", "auto", "hybrid-engine")
        assert found is not None
        assert found.parent.name == "hybrid_auto"


def test_plain_auto_method_auto_hybrid_engine_accepted():
    """E. source_dir=auto, method=auto, backend=hybrid-engine -> 命中（pipeline 兼容）"""
    with tempfile.TemporaryDirectory() as td:
        found = _dir_case(td, "auto", "auto", "hybrid-engine")
        assert found is not None


def test_vlm_auto_method_auto_hybrid_engine_rejected():
    """F. source_dir=vlm_auto, method=auto, backend=hybrid-engine -> None"""
    with tempfile.TemporaryDirectory() as td:
        assert _dir_case(td, "vlm_auto", "auto", "hybrid-engine") is None


def test_vlm_auto_method_auto_vlm_engine_accepted():
    """G. source_dir=vlm_auto, method=auto, backend=vlm-engine -> 命中"""
    with tempfile.TemporaryDirectory() as td:
        found = _dir_case(td, "vlm_auto", "auto", "vlm-engine")
        assert found is not None
        assert found.parent.name == "vlm_auto"


def test_hybrid_txt_method_auto_rejected():
    """method=auto 也不得命中 hybrid_txt"""
    with tempfile.TemporaryDirectory() as td:
        assert _dir_case(td, "hybrid_txt", "auto", "hybrid-engine") is None


def test_method_from_dirname_mapping():
    """反向映射覆盖全部已知目录名。"""
    m = MinerUOutputCleaner._method_from_dirname
    assert m("hybrid_auto") == ("auto", "hybrid-engine")
    assert m("hybrid_ocr") == ("ocr", "hybrid-engine")
    assert m("hybrid_txt") == ("txt", "hybrid-engine")
    assert m("auto") == ("auto", "pipeline")
    assert m("ocr") == ("ocr", "pipeline")
    assert m("txt") == ("txt", "pipeline")
    assert m("vlm_auto") == ("auto", "vlm-engine")
    assert m("unknown_dir") == (None, None)

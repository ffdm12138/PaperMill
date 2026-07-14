"""测试 paper_name 命名、校验、路径穿越防护 + job_id 校验"""
import pytest
from src.naming import (sanitize_paper_name, validate_paper_name, validate_job_id,
                        validate_image_name, safe_child)


def test_sanitize_replaces_illegal():
    assert sanitize_paper_name("a/b:c?d") == "a_b_c_d"
    assert sanitize_paper_name("  x  y ") == "x_y"
    assert sanitize_paper_name("") == "untitled"


def test_validate_paper_name_accepts():
    assert validate_paper_name("1999_dery_吹雪体相模型") == "1999_dery_吹雪体相模型"
    assert validate_paper_name("paper_2023") == "paper_2023"


# ---- validate_job_id ----

@pytest.mark.parametrize("bad_job", [
    "../../etc/passwd",
    "001_test/../../malicious",
    "job\\..\\secret",
    "..\\..\\etc",
    "a/b/c",
    "",
])
def test_validate_job_id_rejects(bad_job):
    with pytest.raises(ValueError):
        validate_job_id(bad_job)


def test_validate_job_id_accepts():
    assert validate_job_id("001_风吹雪升华参数化_a1b2c3") == "001_风吹雪升华参数化_a1b2c3"
    assert validate_job_id("042_test_job_abcdef") == "042_test_job_abcdef"


# ---- safe_child 路径穿越防护 ----

def test_safe_child_rejects_escape():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        (base / "safe").mkdir()
        # 正常路径 OK
        assert safe_child(base, "safe") == (base / "safe").resolve()
        # 穿越应抛错
        with pytest.raises(ValueError):
            safe_child(base, "../../etc")
        with pytest.raises(ValueError):
            safe_child(base, "sub/../../../etc")


def test_safe_child_rejects_unicode_tricks():
    """路径穿越变体不应绕过 safe_child（真实穿越才拦截）"""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        # 真实路径穿越：多层 ..
        with pytest.raises(ValueError):
            safe_child(base, "..", "..", "etc")
        # 含 .. 的单个 segment
        with pytest.raises(ValueError):
            safe_child(base, "sub/../../etc")


def test_safe_child_allows_normal_path():
    """正常多级路径不受影响"""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        (base / "a" / "b").mkdir(parents=True)
        p = safe_child(base, "a", "b")
        assert p == (base / "a" / "b").resolve()


# ---- validate_image_name ----

@pytest.mark.parametrize("bad_img", [
    "../../../etc/passwd.png",
    "a/b/c.jpg",
    "shell;rm -rf.png",
    "has space.png",
    "no_ext",
    "evil.exe",
    "中文.png",
    "",
])
def test_validate_image_name_rejects(bad_img):
    with pytest.raises(ValueError):
        validate_image_name(bad_img)


def test_validate_image_name_accepts():
    assert validate_image_name("figure_01.png") == "figure_01.png"
    assert validate_image_name("img-Ab_2.jpg") == "img-Ab_2.jpg"
    assert validate_image_name("chart_3.2.webp") == "chart_3.2.webp"
    assert validate_image_name("logo_v2+1.bmp") == "logo_v2+1.bmp"

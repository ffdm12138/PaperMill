"""测试配置健壮性：env_int/env_str 非法值不回退崩溃"""
import os
import pytest


def test_env_int_bad_value_falls_back():
    """环境变量 MINERU_MAX_WORKERS=abc 时不崩，回退默认"""
    os.environ["MINERU_MAX_WORKERS"] = "abc"
    import importlib
    import config.settings
    importlib.reload(config.settings)
    assert config.settings.MINERU_MAX_WORKERS == 1  # 回退默认
    del os.environ["MINERU_MAX_WORKERS"]
    importlib.reload(config.settings)


def test_env_int_negative_falls_back():
    """环境变量 MINERU_MAX_UPLOAD_SIZE=-1 回退默认"""
    os.environ["MINERU_MAX_UPLOAD_SIZE"] = "-1"
    import importlib
    import config.settings
    importlib.reload(config.settings)
    # -1 < min_val=1，应回退
    assert config.settings.MAX_UPLOAD_SIZE == 500 * 1024 * 1024
    del os.environ["MINERU_MAX_UPLOAD_SIZE"]
    importlib.reload(config.settings)


def test_env_port_out_of_range_falls_back():
    """API_PORT=99999 超出范围回退默认"""
    os.environ["MINERU_API_PORT"] = "99999"
    import importlib
    import config.settings
    importlib.reload(config.settings)
    assert config.settings.API_PORT == 8080
    del os.environ["MINERU_API_PORT"]
    importlib.reload(config.settings)


def test_research_domain_default_is_empty():
    """默认 RESEARCH_DOMAIN 为空字符串（不硬编码风吹雪）"""
    import importlib
    import config.settings
    # Reset env
    for k in list(os.environ):
        if k.startswith("MINERU_RESEARCH"):
            del os.environ[k]
    importlib.reload(config.settings)
    assert config.settings.RESEARCH_DOMAIN == ""


def test_research_domain_from_env():
    """环境变量 MINERU_RESEARCH_DOMAIN 生效"""
    os.environ["MINERU_RESEARCH_DOMAIN"] = "测试领域"
    import importlib
    import config.settings
    importlib.reload(config.settings)
    assert config.settings.RESEARCH_DOMAIN == "测试领域"
    del os.environ["MINERU_RESEARCH_DOMAIN"]
    importlib.reload(config.settings)

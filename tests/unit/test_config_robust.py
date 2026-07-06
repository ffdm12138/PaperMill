"""测试配置健壮性：env_int/env_str 非法值不回退崩溃。

Uses monkeypatch.setenv (not os.environ[...] = ...) so env vars are always
restored at teardown — no order-dependency pollution in the full suite.
Tests _env_int / _env_str directly to avoid importlib.reload side effects
(directory creation, CUDA_PATH injection) that leak global state.
"""
import pytest


def test_env_int_bad_value_falls_back(monkeypatch):
    """环境变量 MINERU_MAX_WORKERS=abc 时不崩，回退默认"""
    monkeypatch.setenv("MINERU_MAX_WORKERS", "abc")
    from config.settings import _env_int
    assert _env_int("MINERU_MAX_WORKERS", 1, min_val=1) == 1


def test_env_int_negative_falls_back(monkeypatch):
    """环境变量 MINERU_MAX_UPLOAD_SIZE=-1 回退默认"""
    monkeypatch.setenv("MINERU_MAX_UPLOAD_SIZE", "-1")
    from config.settings import _env_int
    # -1 < min_val=1，应回退
    assert _env_int("MINERU_MAX_UPLOAD_SIZE", 500 * 1024 * 1024, min_val=1) == 500 * 1024 * 1024


def test_env_port_out_of_range_falls_back(monkeypatch):
    """API_PORT=99999 超出范围回退默认"""
    monkeypatch.setenv("MINERU_API_PORT", "99999")
    from config.settings import _env_int
    assert _env_int("MINERU_API_PORT", 8080, min_val=1, max_val=65535) == 8080


def test_research_domain_default_is_empty(monkeypatch):
    """默认 RESEARCH_DOMAIN 为空字符串（不硬编码风吹雪）"""
    monkeypatch.delenv("MINERU_RESEARCH_DOMAIN", raising=False)
    from config.settings import _env_str
    assert _env_str("MINERU_RESEARCH_DOMAIN", "") == ""


def test_research_domain_from_env(monkeypatch):
    """环境变量 MINERU_RESEARCH_DOMAIN 生效"""
    monkeypatch.setenv("MINERU_RESEARCH_DOMAIN", "测试领域")
    from config.settings import _env_str
    assert _env_str("MINERU_RESEARCH_DOMAIN", "") == "测试领域"

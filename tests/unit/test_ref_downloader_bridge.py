"""RefDownloaderResolver 测试（mock subprocess，不调用真实 CLI）。"""
import subprocess

from src.fetch.resolvers.ref_downloader_bridge import RefDownloaderResolver
from src.fetch.resolvers.base import ResolveContext


def test_not_installed_returns_action_hint():
    """CLI 不可用时返回 requires_user_action=True。"""
    r = RefDownloaderResolver()
    ctx = ResolveContext(doi="10.1/test")
    result = r._not_installed("10.1/test")
    assert result.success is True
    assert result.requires_user_action is True
    assert "pip install" in result.action_hint


def test_resolve_cli_not_found(monkeypatch):
    """FileNotFoundError → action_hint。"""
    def mock_run(*args, **kwargs):
        raise FileNotFoundError("ref-downloader not found")
    monkeypatch.setattr(subprocess, "run", mock_run)
    r = RefDownloaderResolver()
    result = r.resolve(ResolveContext(doi="10.1/test"))
    assert result.requires_user_action is True
    assert "pip install" in result.action_hint


def test_resolve_cli_success(monkeypatch):
    """CLI 返回有效 JSON → FetchResult success。"""
    def mock_run(*args, **kwargs):
        class FakeResult:
            returncode = 0
            stdout = '{"pdf_path": "/tmp/test.pdf", "pdf_url": ""}'
            stderr = ""
        return FakeResult()
    monkeypatch.setattr(subprocess, "run", mock_run)
    r = RefDownloaderResolver()
    result = r.resolve(ResolveContext(doi="10.1/test"))
    assert result.success is True
    assert result.output_path == "/tmp/test.pdf"


def test_resolve_cli_failure(monkeypatch):
    """CLI timeout → action_hint（不会崩溃）。"""
    def mock_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ref-downloader", timeout=300)
    monkeypatch.setattr(subprocess, "run", mock_run)
    r = RefDownloaderResolver()
    result = r.resolve(ResolveContext(doi="10.1/test"))
    assert result.requires_user_action is True  # fallback to not_installed

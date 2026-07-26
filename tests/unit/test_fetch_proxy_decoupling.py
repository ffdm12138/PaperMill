"""共享代理模块测试：确认 TDM/publisher resolver 使用统一 proxy 模块。

不访问网络，不依赖本机代理环境变量。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

TDM_PATH = ROOT / "src" / "fetch" / "resolvers" / "tdm_resolvers.py"


def test_tdm_resolvers_use_pdf_transport_not_metadata_proxy():
    """TDM resolver 必须从共享 proxy 模块获取代理，而非各自重复解析。"""
    src = TDM_PATH.read_text(encoding="utf-8")
    assert "from src.fetch.pdf_transport import fetch_url_direct_then_proxy" in src
    assert "fetch_url_direct_then_proxy(" in src
    assert "from src.fetch.proxy import get_fetch_proxies" not in src
    assert "get_fetch_proxies()" not in src
    # 不应残留对已删除 Sci-Hub 模块代理函数的直接调用
    assert "_get_proxies()" not in src


def test_proxy_module_importable_and_returns_none_when_unset(monkeypatch):
    import src.fetch.proxy as proxy_mod

    # 不依赖本机代理环境：直接打桩 FETCH_PROXY
    monkeypatch.setattr(proxy_mod, "FETCH_PROXY", "", raising=False)
    assert proxy_mod.get_fetch_proxies() is None


def test_proxy_module_returns_dict_when_set(monkeypatch):
    import src.fetch.proxy as proxy_mod

    monkeypatch.setattr(proxy_mod, "FETCH_PROXY", "http://127.0.0.1:7890", raising=False)
    proxies = proxy_mod.get_fetch_proxies()
    assert proxies == {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}


def test_scihub_never_in_any_access_mode():
    """Sci-Hub 已从项目中彻底移除，任何 AccessMode 都不得启用它。"""
    from src.fetch.access_policy import AccessMode, AccessPolicy

    for mode in AccessMode:
        enabled = AccessPolicy(mode=mode).enabled_resolver_names()
        assert "scihub" not in enabled, f"{mode} 仍含 scihub"


def test_discovery_resolve_crossref_uses_proxy():
    """resolve_crossref HTTP now goes through the unified ProviderClient; the
    shared proxy is wired on RequestsTransport in provider_client (not per
    module).  Asserts the module has no direct requests/proxy usage and that
    the transport carries the proxy."""
    src = (ROOT / "src" / "discovery" / "resolve_crossref.py").read_text(encoding="utf-8")
    assert "from src.fetch.proxy import get_fetch_proxies" not in src
    assert "proxies=get_fetch_proxies()" not in src
    assert "import requests" not in src
    transport_src = (ROOT / "src" / "discovery" / "providers" / "provider_client.py").read_text(encoding="utf-8")
    assert "from src.fetch.proxy import get_fetch_proxies" in transport_src
    assert "RequestsTransport(proxies=get_fetch_proxies())" in transport_src


def test_discovery_search_openalex_uses_proxy():
    """search_openalex HTTP goes through the unified ProviderClient; the
    shared proxy lives on RequestsTransport in provider_client."""
    src = (ROOT / "src" / "discovery" / "search_openalex.py").read_text(encoding="utf-8")
    assert "from src.fetch.proxy import get_fetch_proxies" not in src
    assert "proxies=get_fetch_proxies()" not in src
    assert "import requests" not in src
    transport_src = (ROOT / "src" / "discovery" / "providers" / "provider_client.py").read_text(encoding="utf-8")
    assert "RequestsTransport(proxies=get_fetch_proxies())" in transport_src


def test_metadata_resolver_has_no_direct_http():
    """metadata_resolve.resolver 不得直连 requests——OpenAlex/Crossref HTTP 统一走
    ProviderClient(代理由 RequestsTransport 强制注入,见上方 transport 测试)。"""
    src = (ROOT / "src" / "metadata_resolve" / "resolver.py").read_text(encoding="utf-8")
    assert "import requests" not in src
    assert "requests.get" not in src
    assert "proxies=get_fetch_proxies()" not in src


def test_metadata_enrichment_service_has_no_direct_http():
    """metadata_resolve.enrichment 不得直连 requests——Crossref HTTP 统一走
    ProviderClient(代理由 RequestsTransport 强制注入,见上方 transport 测试)。"""
    src = (ROOT / "src" / "metadata_resolve" / "enrichment.py").read_text(encoding="utf-8")
    assert "import requests" not in src
    assert "requests.get" not in src
    assert "proxies=get_fetch_proxies()" not in src


def test_provider_transport_ignores_ambient_proxy_env(monkeypatch):
    """RequestsTransport uses an explicit Session with trust_env=False, so
    ambient HTTP(S)_PROXY never leaks into discovery HTTP; the configured
    FETCH_PROXY dict is passed explicitly on every request."""
    from src.discovery.providers.provider_client import (
        RequestsTransport,
        RequestSpec,
    )

    class FakeResponse:
        status_code = 200
        headers: dict = {}
        content = b"{}"

    created: list = []

    class FakeSession:
        def __init__(self):
            self.trust_env = True
            self.calls = []
            created.append(self)

        def get(self, url, params=None, headers=None, timeout=None, proxies=None):
            self.calls.append({"url": url, "proxies": proxies})
            return FakeResponse()

    import requests
    monkeypatch.setattr(requests, "Session", FakeSession)

    proxies = {"http": "http://127.0.0.1:1", "https": "http://127.0.0.1:1"}
    transport = RequestsTransport(proxies=proxies)
    spec = RequestSpec(
        provider="openalex",
        purpose="discovery_page",
        url="https://example.org/works",
    )
    transport.send(spec, 5.0)

    assert len(created) == 1
    assert created[0].trust_env is False
    assert created[0].calls[0]["proxies"] == proxies

    # Direct mode: proxies=None is passed explicitly; the session still
    # ignores ambient environment configuration.
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient-proxy:9")
    direct = RequestsTransport(proxies=None)
    direct.send(spec, 5.0)
    assert created[1].trust_env is False
    assert created[1].calls[0]["proxies"] is None

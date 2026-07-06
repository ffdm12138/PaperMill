"""共享代理模块测试：确认 TDM/publisher resolver 使用统一 proxy 模块。

不访问网络，不依赖本机代理环境变量。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

TDM_PATH = ROOT / "src" / "fetch" / "resolvers" / "tdm_resolvers.py"


def test_tdm_resolvers_use_shared_proxy():
    """TDM resolver 必须从共享 proxy 模块获取代理，而非各自重复解析。"""
    src = TDM_PATH.read_text(encoding="utf-8")
    assert "from src.fetch.proxy import get_fetch_proxies" in src
    assert "get_fetch_proxies()" in src
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
    """resolve_crossref 的 3 处 requests.get 都必须传 proxies。"""
    src = (ROOT / "src" / "discovery" / "resolve_crossref.py").read_text(encoding="utf-8")
    assert "from src.fetch.proxy import get_fetch_proxies" in src
    assert "proxies=get_fetch_proxies()" in src
    # 3 处请求
    assert src.count("proxies=get_fetch_proxies()") >= 3


def test_discovery_search_openalex_uses_proxy():
    """search_openalex 的 requests.get 必须传 proxies。"""
    src = (ROOT / "src" / "discovery" / "search_openalex.py").read_text(encoding="utf-8")
    assert "from src.fetch.proxy import get_fetch_proxies" in src
    assert "proxies=get_fetch_proxies()" in src


def test_metadata_resolver_uses_proxy():
    """metadata_resolver 的 _requests.get 调用必须传 proxies。"""
    src = (ROOT / "src" / "services" / "metadata_resolver.py").read_text(encoding="utf-8")
    assert "from src.fetch.proxy import get_fetch_proxies" in src
    assert "proxies=get_fetch_proxies()" in src


def test_metadata_enrichment_service_uses_proxy():
    """metadata_enrichment_service 的 requests.get 必须传 proxies。"""
    src = (ROOT / "src" / "services" / "metadata_enrichment_service.py").read_text(encoding="utf-8")
    assert "from src.fetch.proxy import get_fetch_proxies" in src
    assert "proxies=get_fetch_proxies()" in src

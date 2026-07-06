"""Resolver registry — 统一管理所有 PDF resolver 的注册和构建。

设计目标：
- 所有 resolver class 集中在 src/fetch/resolvers/。
- fetch_pipeline.py 不再有行内 bridge class。
- 新增 resolver 只需在 RESOLVER_REGISTRY 注册。
"""
from src.fetch.access_policy import AccessPolicy
from src.fetch.resolvers.browser_resolvers import BrowserAssistedResolver
from src.fetch.resolvers.custom_resolvers import ExternalCommandResolver
from src.fetch.resolvers.header_based_resolver import HeaderBasedDoiResolver
from src.fetch.resolvers.institutional_resolvers import (
    InstitutionalBrowserResolver,
    PublisherTDMResolver,
)
from src.fetch.resolvers.local_resolvers import LocalManualResolver
from src.fetch.resolvers.original_link_resolver import OriginalLinkResolver
from src.fetch.resolvers.oa_resolvers import (
    ArxivResolver,
    OpenAlexResolver,
    PublisherOAResolver,
    SemanticScholarResolver,
    UnpaywallResolver,
)
from src.fetch.resolvers.preprint_resolvers import BiorxivResolver, PmcOaResolver
from src.fetch.resolvers.ref_downloader_bridge import RefDownloaderResolver
from src.fetch.resolvers.sciengine_resolver import SciEngineResolver
from src.fetch.resolvers.tdm_resolvers import (
    ElsevierTdmResolver,
    SpringerDirectResolver,
    WileyTdmResolver,
)
from src.fetch.resolvers.base import PdfResolver


# ── 注册表 ────────────────────────────────────────

RESOLVER_REGISTRY: dict[str, type[PdfResolver]] = {
    # Original metadata links (highest priority)
    "original_link": OriginalLinkResolver,
    # OA
    "unpaywall": UnpaywallResolver,
    "openalex": OpenAlexResolver,
    "semantic_scholar": SemanticScholarResolver,
    "arxiv": ArxivResolver,
    "publisher_oa": PublisherOAResolver,
    "springer_direct": SpringerDirectResolver,
    # Publisher-specific
    "sciengine_direct": SciEngineResolver,
    # Preprint / PMC
    "biorxiv": BiorxivResolver,
    "pmc_oa": PmcOaResolver,
    # TDM（需 token）
    "wiley_tdm": WileyTdmResolver,
    "elsevier_tdm": ElsevierTdmResolver,
    # Institutional
    "publisher_tdm": PublisherTDMResolver,
    "institutional_browser": InstitutionalBrowserResolver,
    # Browser assisted
    "browser_assisted": BrowserAssistedResolver,
    # Local
    "local_manual": LocalManualResolver,
    "custom": ExternalCommandResolver,
    "header_based": HeaderBasedDoiResolver,
    # Bridge
    "ref_downloader": RefDownloaderResolver,
}


def build_resolvers(policy: AccessPolicy) -> list:
    """根据 access policy 构建 resolver 实例列表。"""
    resolvers = []
    for name in policy.enabled_resolver_names():
        cls = RESOLVER_REGISTRY.get(name)
        if cls:
            if cls is ExternalCommandResolver:
                argv = (policy.extra or {}).get("custom_command_argv") or []
                resolvers.append(cls(command_argv=argv))
            elif cls is HeaderBasedDoiResolver:
                extra = policy.extra or {}
                resolvers.append(cls(
                    base_url=extra.get("base_url", ""),
                    url_template=extra.get("url_template", ""),
                    headers=extra.get("headers", {}),
                    timeout=int(extra.get("timeout_seconds") or policy.timeout_seconds),
                ))
            else:
                resolvers.append(cls())
    return resolvers

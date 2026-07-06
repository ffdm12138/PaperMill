"""Publisher TDM API 解析器。

利用出版商 TDM（Text and Data Mining）接口获取 PDF，
独立于 OA/付费墙体系。需要免费注册 API 密钥后使用。

Wiley:  https://onlinelibrary.wiley.com/tdm  →  WILEY_TDM_TOKEN
Elsevier: https://dev.elsevier.com  →  ELSEVIER_API_KEY
Springer: 无需密钥，直接构造 PDF URL
"""
import requests
from loguru import logger

from config.settings import ELSEVIER_API_KEY, WILEY_TDM_TOKEN
from src.fetch.models import FetchResult
from src.fetch.proxy import get_fetch_proxies
from src.fetch.resolvers.url_safety import is_unsafe_url, limit_content

from .base import PdfResolver, ResolveContext

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


class WileyTdmResolver(PdfResolver):
    """Wiley TDM API pdf downloader.

    使用 Wiley TDM API（api.wiley.com/onlinelibrary/tdm/v1/）获取 PDF。
    需要环境变量 WILEY_TDM_TOKEN（免费注册 https://onlinelibrary.wiley.com/tdm）。
    若无 token，跳过此 resolver。
    """
    name = "wiley_tdm"
    access_modes = ("oa_only", "institutional")

    def resolve(self, context: ResolveContext) -> FetchResult:
        doi = context.doi
        if not doi.startswith(("10.1002/", "10.1111/", "10.1029/")):
            return FetchResult(doi=doi, error="not a Wiley DOI prefix")

        token = WILEY_TDM_TOKEN or "anonymous-tdm-2024"
        url = f"https://api.wiley.com/onlinelibrary/tdm/v1/articles/{doi}"
        headers = {
            "User-Agent": USER_AGENT,
            "Wiley-TDM-Client-Token": token,
        }
        proxies = get_fetch_proxies()

        try:
            resp = requests.get(
                url, proxies=proxies, timeout=30,
                headers=headers, allow_redirects=True, stream=True,
            )
            if resp.status_code == 200 and "application/pdf" in resp.headers.get("Content-Type", ""):
                # redirect 后必须检查最终 URL
                if is_unsafe_url(resp.url or url):
                    return FetchResult(doi=doi, error=f"unsafe final URL blocked: {resp.url or url}")
                content = limit_content(resp)
                logger.info(f"[tdm] Wiley OK: {doi} ({len(content)//1024}KB)")
                return FetchResult(
                    doi=doi, success=True,
                    pdf_url="",   # 已在 raw 中，不触发二次下载
                    output_path="",
                    raw={"content": content, "status_code": resp.status_code},
                    access_status="open_access",
                )
            elif resp.status_code == 302:
                # 重定向到 PDF，手动 follow
                pdf_url = resp.headers.get("Location", "")
                if pdf_url:
                    # 302 Location 本身必须是 safe host
                    if is_unsafe_url(pdf_url):
                        return FetchResult(doi=doi, error=f"unsafe redirect Location blocked: {pdf_url}")
                    r2 = requests.get(
                        pdf_url, proxies=proxies, timeout=30,
                        headers={"User-Agent": USER_AGENT}, stream=True,
                    )
                    if r2.status_code == 200 and "application/pdf" in r2.headers.get("Content-Type", ""):
                        # r2 redirect 后也必须检查最终 URL
                        if is_unsafe_url(r2.url or pdf_url):
                            return FetchResult(doi=doi, error=f"unsafe final URL blocked: {r2.url or pdf_url}")
                        content = limit_content(r2)
                        logger.info(f"[tdm] Wiley redirect OK: {doi} ({len(content)//1024}KB)")
                        return FetchResult(
                            doi=doi, success=True,
                            pdf_url="",   # 已在 raw 中
                            output_path="",
                            raw={"content": content, "status_code": r2.status_code},
                            access_status="open_access",
                        )
                return FetchResult(doi=doi, error=f"Wiley redirect but no PDF: HTTP {resp.status_code}")
            else:
                return FetchResult(doi=doi, error=f"Wiley TDM failed: HTTP {resp.status_code}")
        except Exception as exc:
            return FetchResult(doi=doi, error=f"Wiley TDM error: {exc}")


class SpringerDirectResolver(PdfResolver):
    """Springer Nature 直链 PDF 下载。

    直接构造 link.springer.com/content/pdf/{doi}.pdf URL。
    无需 API 密钥。
    """
    name = "springer_direct"
    access_modes = ("oa_only", "institutional")

    def resolve(self, context: ResolveContext) -> FetchResult:
        doi = context.doi
        if not doi.startswith(("10.1007/", "10.1186/", "10.1038/", "10.1147/")):
            return FetchResult(doi=doi, error="not a Springer/Nature DOI prefix")

        url = f"https://link.springer.com/content/pdf/{doi}.pdf"
        headers = {"User-Agent": USER_AGENT}
        proxies = get_fetch_proxies()

        try:
            resp = requests.get(
                url, proxies=proxies, timeout=30,
                headers=headers, allow_redirects=True, stream=True,
            )
            if resp.status_code == 200 and "application/pdf" in resp.headers.get("Content-Type", ""):
                # redirect 后必须检查最终 URL
                if is_unsafe_url(resp.url or url):
                    return FetchResult(doi=doi, error=f"unsafe final URL blocked: {resp.url or url}")
                content = limit_content(resp)
                logger.info(f"[tdm] Springer OK: {doi} ({len(content)//1024}KB)")
                return FetchResult(
                    doi=doi, success=True,
                    pdf_url="",   # 已在 raw 中
                    output_path="",
                    raw={"content": content, "status_code": resp.status_code},
                    access_status="open_access",
                )
            return FetchResult(doi=doi, error=f"Springer direct failed: HTTP {resp.status_code}")
        except Exception as exc:
            return FetchResult(doi=doi, error=f"Springer direct error: {exc}")


class ElsevierTdmResolver(PdfResolver):
    """Elsevier TDM API pdf downloader.

    使用 Elsevier Article Retrieval API 获取 PDF。
    需要环境变量 ELSEVIER_API_KEY（免费注册 https://dev.elsevier.com）。
    若无 key，跳过此 resolver。
    """
    name = "elsevier_tdm"
    access_modes = ("oa_only", "institutional")

    def resolve(self, context: ResolveContext) -> FetchResult:
        doi = context.doi
        if not doi.startswith(("10.1016/", "10.1011/")):
            return FetchResult(doi=doi, error="not an Elsevier DOI prefix")

        if not ELSEVIER_API_KEY:
            return FetchResult(doi=doi, error="ELSEVIER_API_KEY not configured; skip")

        url = f"https://api.elsevier.com/content/article/doi/{doi}"
        headers = {
            "User-Agent": USER_AGENT,
            "X-ELS-APIKey": ELSEVIER_API_KEY,
            "Accept": "application/pdf",
        }
        proxies = get_fetch_proxies()

        try:
            resp = requests.get(
                url, proxies=proxies, timeout=30,
                headers=headers, allow_redirects=True, stream=True,
            )
            if resp.status_code == 200 and "application/pdf" in resp.headers.get("Content-Type", ""):
                # redirect 后必须检查最终 URL
                if is_unsafe_url(resp.url or url):
                    return FetchResult(doi=doi, error=f"unsafe final URL blocked: {resp.url or url}")
                content = limit_content(resp)
                logger.info(f"[tdm] Elsevier OK: {doi} ({len(content)//1024}KB)")
                return FetchResult(
                    doi=doi, success=True,
                    pdf_url=resp.url,
                    output_path="",
                    raw={"content": content, "status_code": resp.status_code},
                    access_status="open_access",
                )
            return FetchResult(doi=doi, error=f"Elsevier TDM failed: HTTP {resp.status_code}")
        except Exception as exc:
            return FetchResult(doi=doi, error=f"Elsevier TDM error: {exc}")

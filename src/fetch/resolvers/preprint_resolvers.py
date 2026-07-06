"""Preprint platform resolvers：bioRxiv / medRxiv / PMC OA。

通过各平台公开 API 查询 PDF URL，无需认证。
"""
import json
import re

import requests
from loguru import logger

from src.fetch.models import FetchResult
from src.fetch.proxy import get_fetch_proxies
from src.fetch.resolvers.base import PdfResolver, ResolveContext


class BiorxivResolver(PdfResolver):
    """bioRxiv / medRxiv API 查询 PDF URL。

    API: GET https://api.biorxiv.org/details/doi/{doi}
    返回 JSON: collection[0].pdf_url
    """
    name = "biorxiv"
    access_modes = ("oa_only", "institutional")

    def resolve(self, context: ResolveContext) -> FetchResult:
        doi = context.doi
        try:
            resp = requests.get(
                f"https://api.biorxiv.org/details/doi/{doi}",
                timeout=20,
                headers={"User-Agent": "Mozilla/5.0"},
                proxies=get_fetch_proxies(),
            )
            resp.raise_for_status()
            data = resp.json()
            collection = data.get("collection") or []
            if not collection:
                return FetchResult(doi=doi, error="biorxiv: no entries")
            pdf_rel = (collection[0].get("pdf_rel") or "").strip()
            if not pdf_rel:
                return FetchResult(doi=doi, error="biorxiv: no pdf_rel")
            pdf_url = f"https://www.biorxiv.org/content/{pdf_rel}.full.pdf"
            return FetchResult(
                doi=doi, success=True, source="biorxiv",
                resolver=self.name, access_mode="oa_only",
                access_status="open_access", pdf_url=pdf_url,
                metadata={"biorxiv_data": collection[0]},
            )
        except Exception as exc:
            logger.debug(f"biorxiv lookup failed for {doi}: {exc}")
            return FetchResult(doi=doi, error=f"biorxiv: {exc}")


class PmcOaResolver(PdfResolver):
    """PMC OA 服务查询 PDF URL。

    API: GET https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id=doi:{doi}
    返回 XML，从中提取 PDF 链接。
    """
    name = "pmc_oa"
    access_modes = ("oa_only", "institutional")

    def resolve(self, context: ResolveContext) -> FetchResult:
        doi = context.doi
        try:
            resp = requests.get(
                f"https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi",
                params={"id": f"doi:{doi}"},
                timeout=20,
                headers={"User-Agent": "Mozilla/5.0"},
                proxies=get_fetch_proxies(),
            )
            resp.raise_for_status()
            # 从 XML 中提取 PDF 链接
            xml = resp.text
            # 查找 <link format="pdf" href="..."/>
            m = re.search(r'<link[^>]+format="pdf"[^>]+href="([^"]+)"', xml)
            if not m:
                return FetchResult(doi=doi, error="pmc_oa: no PDF link in response")
            pdf_url = m.group(1)
            if pdf_url.startswith("/"):
                pdf_url = "https://www.ncbi.nlm.nih.gov" + pdf_url
            return FetchResult(
                doi=doi, success=True, source="pmc_oa",
                resolver=self.name, access_mode="oa_only",
                access_status="open_access", pdf_url=pdf_url,
                metadata={"raw_xml_sample": xml[:500]},
            )
        except Exception as exc:
            logger.debug(f"pmc_oa lookup failed for {doi}: {exc}")
            return FetchResult(doi=doi, error=f"pmc_oa: {exc}")

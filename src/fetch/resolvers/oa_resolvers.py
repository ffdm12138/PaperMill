"""OA resolver 包装：将现有 fetch_* 函数包装为 PdfResolver 子类。

不删除旧模块 (fetch_unpaywall.py etc.)，保持向后兼容。
"""
from src.fetch.fetch_arxiv import ARXIV_DOI_PREFIX, arxiv_id_from_metadata, resolve_arxiv_pdf
from src.fetch.fetch_openalex import resolve_openalex_pdf
from src.fetch.fetch_publisher import resolve_publisher_pdf
from src.fetch.fetch_semantic_scholar import resolve_semantic_scholar_pdf
from src.fetch.fetch_unpaywall import resolve_unpaywall

from .base import PdfResolver, ResolveContext


class UnpaywallResolver(PdfResolver):
    name = "unpaywall"
    access_modes = ("oa_only", "institutional")

    def resolve(self, context: ResolveContext) -> dict:
        result = resolve_unpaywall(context.doi)
        if result.success:
            result.resolver = self.name
            result.access_mode = "oa_only"
            result.access_status = "open_access"
        return result


class OpenAlexResolver(PdfResolver):
    name = "openalex"
    access_modes = ("oa_only", "institutional")

    def resolve(self, context: ResolveContext) -> dict:
        result = resolve_openalex_pdf(context.doi)
        if result.success:
            result.resolver = self.name
            result.access_mode = "oa_only"
            result.access_status = "open_access"
        return result


class SemanticScholarResolver(PdfResolver):
    name = "semantic_scholar"
    access_modes = ("oa_only", "institutional")

    def resolve(self, context: ResolveContext) -> dict:
        result = resolve_semantic_scholar_pdf(context.doi)
        if result.success:
            result.resolver = self.name
            result.access_mode = "oa_only"
            result.access_status = "open_access"
        return result


class ArxivResolver(PdfResolver):
    name = "arxiv"
    access_modes = ("oa_only", "institutional")

    def applies_to(self, context: ResolveContext) -> bool:
        # The resolver only ever builds a URL from an arXiv id already present
        # in metadata; without one it can never succeed. Deciding that here
        # keeps 'no arXiv id' out of the attempt log for every non-preprint.
        return bool(arxiv_id_from_metadata(context.metadata or {})) or \
            str(context.doi or "").lower().startswith(ARXIV_DOI_PREFIX)

    def resolve(self, context: ResolveContext) -> dict:
        meta = context.metadata or {}
        result = resolve_arxiv_pdf(context.doi, metadata=meta)
        if result.success:
            result.resolver = self.name
            result.access_mode = "oa_only"
            result.access_status = "open_access"
        return result


class PublisherOAResolver(PdfResolver):
    name = "publisher_oa"
    access_modes = ("oa_only", "institutional")

    def resolve(self, context: ResolveContext) -> dict:
        result = resolve_publisher_pdf(context.doi)
        if result.success:
            result.resolver = self.name
            result.access_mode = "oa_only"
            result.access_status = "open_access"
        return result

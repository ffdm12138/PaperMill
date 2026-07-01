"""Institutional resolver 存根。

不实现自动化登录/下载。只返回 ``requires_user_action=True``，
提示用户使用机构权限手动获取 PDF。
"""
from ..models import FetchResult
from .base import PdfResolver, ResolveContext


class PublisherTDMResolver(PdfResolver):
    name = "publisher_tdm"
    access_modes = ("institutional",)

    def resolve(self, context: ResolveContext) -> FetchResult:
        doi = context.doi
        return FetchResult(
            doi=doi,
            success=True,
            source="publisher_tdm",
            resolver=self.name,
            access_mode="institutional",
            access_status="institutional_access",
            requires_user_action=True,
            landing_url=f"https://doi.org/{doi}",
            action_hint=(
                f"打开 {doi} 所在出版商网站，用机构登录态下载 PDF，"
                f"放入 data/raw/ 根目录后运行 "
                f"python scripts/stage_raw_pdfs_to_paper_raw.py --move --apply"
            ),
            metadata={
                "doi": doi,
                "access_mode": "institutional",
                "resolver": self.name,
            },
        )


class InstitutionalBrowserResolver(PdfResolver):
    name = "institutional_browser"
    access_modes = ("institutional",)

    def resolve(self, context: ResolveContext) -> FetchResult:
        return FetchResult(
            doi=context.doi,
            success=True,
            source="institutional_browser",
            resolver=self.name,
            access_mode="institutional",
            access_status="institutional_access",
            requires_user_action=True,
            landing_url=f"https://doi.org/{context.doi}",
            action_hint=(
                f"用浏览器打开 https://doi.org/{context.doi}，"
                f"通过机构登录态访问，手动下载 PDF，"
                f"然后运行 python scripts/stage_raw_pdfs_to_paper_raw.py --move --apply"
            ),
        )

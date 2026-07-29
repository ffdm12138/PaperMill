"""PDF resolver 统一接口。

每个 resolver 继承 ``PdfResolver``，实现 ``resolve(context) -> FetchResult``。
通过 ``enabled(policy)`` 判断是否在当前 access policy 下启用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.fetch.models import FetchResult


@dataclass
class ResolveContext:
    doi: str = ""
    title: str = ""
    year: int | None = None
    domain_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    source_record: dict[str, Any] = field(default_factory=dict)
    access_policy: Any = None


class PdfResolver:
    name: str = "base"
    access_modes: tuple[str, ...] = ()

    def enabled(self, policy) -> bool:
        return self.name in policy.enabled_resolver_names()

    def applies_to(self, context: ResolveContext) -> bool:
        """Return False when this resolver provably cannot serve *context*.

        This is a purely local decision — DOI prefix, identifiers already in
        metadata — and MUST NOT perform any I/O.  Its only job is to keep a
        resolver from spending a network round trip to learn something the
        DOI already said.  A resolver that cannot decide locally returns True
        and lets ``resolve`` answer.

        Deciding here rather than inside ``resolve`` keeps the chain honest:
        a skipped resolver never appears as a failed attempt, so the attempt
        log shows real failures instead of constant no-ops.
        """
        return True

    def resolve(self, context: ResolveContext) -> FetchResult:
        raise NotImplementedError

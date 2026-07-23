"""The single typed provider-page dispatch boundary for discovery lanes.

The coordinator hands each executor an immutable :class:`LaneExecutionSpec`.
This module is the only place that translates that already-resolved spec into
an OpenAlex or Crossref adapter call.  It deliberately accepts neither loose
provider/query parameters nor a callback that can re-derive request identity.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from src.discovery.execution.lane_models import LaneExecutionSpec
from src.discovery.providers.provider_client import ProviderClient
from src.discovery.providers.provider_models import DiscoveryPage


class ProviderPageFetcher:
    """Production adapter for one typed provider-page request."""

    def fetch(
        self,
        spec: LaneExecutionSpec,
        cursor: str,
        client: ProviderClient,
    ) -> DiscoveryPage:
        """Fetch one page using only fields frozen in *spec*."""
        if spec.key.provider == "openalex":
            from src.discovery.search_openalex import search_openalex_page

            return search_openalex_page(spec, cursor, client)
        if spec.key.provider == "crossref":
            from src.discovery.resolve_crossref import search_crossref_page

            return search_crossref_page(spec, cursor, client)
        raise ValueError(f"unknown provider: {spec.key.provider!r}")


@dataclass(frozen=True)
class CallbackProviderPageFetcher(ProviderPageFetcher):
    """Typed fake/test adapter.

    Tests receive the same immutable spec and batch-bound client as production.
    This keeps all fakes on the actual contract instead of reproducing the old
    bag-of-keyword-arguments interface.
    """

    callback: Callable[[LaneExecutionSpec, str, ProviderClient], DiscoveryPage]

    def fetch(
        self,
        spec: LaneExecutionSpec,
        cursor: str,
        client: ProviderClient,
    ) -> DiscoveryPage:
        return self.callback(spec, cursor, client)

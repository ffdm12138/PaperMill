"""Discovery v4 provider adapters — OpenAlex and Crossref HTTP clients.

Adapters receive typed ``ProviderPageRequestV4`` input and return
``ProviderPageJournalV4`` results.  They never guess fields from
notebook dicts.

Import directly from submodules:
    from src.discovery.providers.provider_client import ProviderClient, ProviderRuntime
"""

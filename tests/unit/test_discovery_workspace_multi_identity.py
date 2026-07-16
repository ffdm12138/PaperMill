from __future__ import annotations

from pathlib import Path

import pytest

from src.discovery.workspace_index import DiscoveryIdentityRef, DiscoveryWorkspaceIndex


pytestmark = pytest.mark.unit


def _ref(*, provider: str, candidate: str, source: bool = False,
         receipt: bool = False) -> DiscoveryIdentityRef:
    return DiscoveryIdentityRef(
        paper_number="0000000000000001", scope="paper_raw",
        workspace_path=Path("0000000000000001"), provider=provider,
        keyword_id="keyword", page_id=f"page-{candidate}", candidate_id=candidate,
        normalized_doi="10.9200/shared",
        source_record_path=Path(f"{provider}.source.json") if source else None,
        receipt_path=Path(f"{provider}.receipt.json") if receipt else None,
    )


def _lookup(index: DiscoveryWorkspaceIndex, provider: str, candidate: str):
    return index.lookup(
        provider=provider, keyword_id="keyword", page_id=f"page-{candidate}",
        candidate_id=candidate, normalized_doi="10.9200/shared")


@pytest.mark.parametrize("receipt_order", [("openalex", "crossref"), ("crossref", "openalex")])
def test_same_paper_number_preserves_multiple_identities_across_freeze(receipt_order):
    index = DiscoveryWorkspaceIndex()
    index.add_or_merge(_ref(provider="openalex", candidate="a", source=True))
    index.add_or_merge(_ref(provider="crossref", candidate="b", source=True))

    for provider in receipt_order:
        candidate = "a" if provider == "openalex" else "b"
        index.add_or_merge(_ref(provider=provider, candidate=candidate, receipt=True))
        assert len(_lookup(index, "openalex", "a")) == 1
        assert len(_lookup(index, "crossref", "b")) == 1

    before = {
        "a": tuple(_lookup(index, "openalex", "a")),
        "b": tuple(_lookup(index, "crossref", "b")),
    }
    assert len(index.refs) == 2
    frozen = index.freeze()
    after = {
        "a": tuple(_lookup(frozen, "openalex", "a")),
        "b": tuple(_lookup(frozen, "crossref", "b")),
    }

    assert after == before
    assert len(frozen.refs) == 2
    assert all(ref.source_record_path and ref.receipt_path for ref in frozen.refs)

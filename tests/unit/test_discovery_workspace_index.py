"""Pure in-memory discovery workspace index contracts."""
from pathlib import Path

import pytest

from src.discovery.workspace_index import DiscoveryIdentityRef, DiscoveryWorkspaceIndex


def _ref(number: str = "0000000000000001") -> DiscoveryIdentityRef:
    return DiscoveryIdentityRef(
        paper_number=number, scope="paper_raw", workspace_path=Path(number),
        provider="crossref", keyword_id="kw", page_id="page", candidate_id="candidate",
        normalized_doi="10.1000/index",
    )


def test_lookup_and_copy_are_in_memory():
    index = DiscoveryWorkspaceIndex([_ref()])
    assert index.lookup(candidate_id="candidate", page_id="page", keyword_id="kw",
                        provider="crossref", normalized_doi="10.1000/index")[0].paper_number.endswith("1")
    copied = index.copy()
    copied.add_or_merge(_ref("0000000000000002"))
    assert index.workspace_count == 1
    assert copied.workspace_count == 2


def test_frozen_index_rejects_mutation():
    frozen = DiscoveryWorkspaceIndex([_ref()]).freeze()
    with pytest.raises(TypeError):
        frozen.add_or_merge(_ref("0000000000000002"))

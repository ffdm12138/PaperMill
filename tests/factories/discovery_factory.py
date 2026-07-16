"""Canonical synthetic discovery candidate factory."""
from __future__ import annotations

from typing import Any


def create_discovery_candidate(*, doi: str = "10.1000/candidate",
                               candidate_id: str = "candidate-1",
                               page_id: str = "page-1", keyword_id: str = "keyword-1",
                               provider: str = "crossref") -> dict[str, Any]:
    return {
        "title": "Synthetic discovery candidate", "year": 2026, "doi": doi,
        "provider": provider,
        "discovery_context": {
            "candidate_id": candidate_id, "page_id": page_id, "keyword_id": keyword_id,
            "provider": provider, "normalized_doi": doi,
        },
    }

from __future__ import annotations

from copy import deepcopy

from src.metadata.source_records import metadata_source_rel_path
from src.metadata.schema import empty_metadata


def make_minimal_metadata(
    *,
    paper_number: str = "0000000000000001",
    title: str = "Example Paper",
    doi: str = "10.0000/example",
    source_type: str = "manual_pdf",
    provider: str = "manual",
    match_status: str = "matched",
) -> dict:
    """Return a minimal metadata v2.0 object that follows current contracts."""
    metadata = deepcopy(empty_metadata(paper_number, source_type=source_type))
    metadata["title"]["original"] = title
    metadata["authors"] = [
        {
            "full_name": "Doe Jane",
            "family": "Doe",
            "given": "Jane",
            "orcid": "",
            "affiliation": "",
        }
    ]
    metadata["first_author"] = {"family": "Doe", "display": "Doe Jane"}
    metadata["year"] = 2024
    metadata["container"]["journal"] = "Example Journal"
    metadata["identifiers"]["doi"] = doi
    metadata["source"].update(
        {
            "kind": source_type,
            "provider": provider,
            "raw_record_path": metadata_source_rel_path(provider),
        }
    )
    del match_status  # Match state lives only in the independent receipt.
    return metadata

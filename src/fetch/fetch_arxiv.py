"""Conservative arXiv PDF URL resolver from known metadata."""
import re

from src.fetch.models import FetchResult


ARXIV_ID_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$|^[a-z\-]+(\.[A-Z]{2})?/\d{7}(v\d+)?$", re.I)

#: DataCite prefix arXiv mints for its own preprints (e.g. 10.48550/arXiv.2101.00001).
ARXIV_DOI_PREFIX = "10.48550/"


def arxiv_id_from_metadata(metadata: dict | None) -> str:
    """Return a valid arXiv id from *metadata*, or "" when absent.

    Shared with ``ArxivResolver.applies_to`` so the "is there an arXiv id at
    all" question is answered in exactly one place.
    """
    metadata = metadata or {}
    external = metadata.get("externalIds") or metadata.get("external_ids") or {}
    arxiv_id = external.get("ArXiv") or external.get("arXiv") or metadata.get("arxiv_id") or ""
    arxiv_id = str(arxiv_id or "").strip()
    return arxiv_id if arxiv_id and ARXIV_ID_RE.match(arxiv_id) else ""


def resolve_arxiv_pdf(doi: str, metadata: dict | None = None) -> FetchResult:
    metadata = metadata or {}
    arxiv_id = arxiv_id_from_metadata(metadata)
    if not arxiv_id:
        # An arXiv-minted DOI carries the id in its suffix.
        normalized = str(doi or "").strip()
        if normalized.lower().startswith(ARXIV_DOI_PREFIX):
            suffix = normalized[len(ARXIV_DOI_PREFIX):]
            candidate = suffix.split(":", 1)[-1].removeprefix("arXiv.").removeprefix("arxiv.")
            if ARXIV_ID_RE.match(candidate):
                arxiv_id = candidate
    if not arxiv_id:
        return FetchResult(doi=doi, source="arxiv", metadata=metadata, error="no arXiv id")
    return FetchResult(
        doi=doi,
        success=True,
        source="arxiv",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}.pdf",
        oa_status="green",
        metadata=metadata,
    )

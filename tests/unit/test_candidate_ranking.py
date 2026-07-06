from src.discovery.models import PaperCandidate, normalize_doi, normalize_title
from src.discovery.rank_candidates import dedupe_and_rank_candidates


def test_normalizers_stabilize_doi_and_title():
    assert normalize_doi("https://doi.org/10.1000/ABC") == "10.1000/abc"
    assert normalize_title("A  Test: Paper!") == "a test paper"


def test_dedupe_merges_by_doi_and_ranks_oa_multi_source():
    candidates = [
        PaperCandidate(
            title="Blowing snow sublimation",
            year=1999,
            doi="10.1000/SNOW",
            source="openalex",
            citation_count=10,
        ),
        PaperCandidate(
            title="Blowing snow sublimation",
            year=1999,
            doi="10.1000/snow",
            source="crossref",
            pdf_url="https://example.org/paper.pdf",
            open_access=True,
            citation_count=5,
        ),
    ]
    ranked = dedupe_and_rank_candidates(candidates, query="blowing snow sublimation")
    assert len(ranked) == 1
    assert ranked[0].doi == "10.1000/snow"
    assert ranked[0].pdf_url.endswith(".pdf")
    assert set(ranked[0].raw["sources"]) == {"openalex", "crossref"}
    assert ranked[0].confidence > 0.7


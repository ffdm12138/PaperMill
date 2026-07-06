import src.discovery.pipeline as pipeline_mod
from src.discovery import resolve_crossref, search_openalex
from src.discovery.models import CandidateBatch, PaperCandidate
from src.discovery.pipeline import discover_papers
from src.discovery.search_openalex import parse_openalex_work


def test_candidate_batch_round_trip():
    batch = CandidateBatch(
        original_query="snow",
        expanded_queries=["snow", "blowing snow"],
        sources=["openalex"],
        candidates=[PaperCandidate(title="T", doi="https://doi.org/10.1/A", source="openalex")],
    )
    restored = CandidateBatch.from_dict(batch.to_dict())
    assert restored.candidates[0].doi == "10.1/a"
    assert restored.expanded_queries == ["snow", "blowing snow"]


def test_parse_openalex_work_extracts_oa_pdf():
    work = {
        "id": "https://openalex.org/W1",
        "display_name": "Snow Paper",
        "publication_year": 2020,
        "doi": "https://doi.org/10.2/snow",
        "cited_by_count": 4,
        "authorships": [{"author": {"display_name": "A Author"}}],
        "primary_location": {
            "pdf_url": "https://example.org/snow.pdf",
            "source": {"display_name": "Journal"},
        },
        "open_access": {"is_oa": True, "oa_url": "https://example.org/landing"},
    }
    candidate = parse_openalex_work(work, query="snow", domain_id="blowing_snow_physics")
    assert candidate.doi == "10.2/snow"
    assert candidate.pdf_url.endswith(".pdf")
    assert candidate.open_access is True


def test_parse_crossref_item_extracts_doi_title_year_authors():
    from src.discovery.resolve_crossref import parse_crossref_item

    item = {
        "DOI": "10.3/snow",
        "title": ["Snow Paper"],
        "container-title": ["Journal of Snow"],
        "is-referenced-by-count": 7,
        "issued": {"date-parts": [[2021, 3]]},
        "author": [{"given": "A", "family": "Author"}],
        "URL": "https://doi.org/10.3/snow",
    }
    candidate = parse_crossref_item(item, query="snow")
    assert candidate.source == "crossref"
    assert candidate.source_id == "10.3/snow"
    assert candidate.doi == "10.3/snow"
    assert candidate.title == "Snow Paper"
    assert candidate.year == 2021
    assert candidate.venue == "Journal of Snow"
    assert candidate.citation_count == 7
    assert candidate.authors == ["A Author"]


def test_search_crossref_network_error_returns_empty(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(resolve_crossref.requests, "get", boom)
    assert resolve_crossref.search_crossref("snow") == []


def test_pipeline_uses_openalex_and_crossref_only(monkeypatch):
    # Semantic Scholar must no longer be imported into the discovery pipeline.
    assert not hasattr(pipeline_mod, "search_semantic_scholar")

    seen = {"openalex": 0, "crossref": 0}

    openalex_cand = PaperCandidate(title="OA Snow", doi="10.9/oa", source="openalex")
    crossref_cand = PaperCandidate(title="CR Snow", doi="10.9/cr", source="crossref")

    monkeypatch.setattr(pipeline_mod, "search_openalex", lambda q, domain_id=None, limit=15: (seen.__setitem__("openalex", seen["openalex"] + 1), [openalex_cand])[1])
    monkeypatch.setattr(pipeline_mod, "search_crossref", lambda q, domain_id=None, limit=15: (seen.__setitem__("crossref", seen["crossref"] + 1), [crossref_cand])[1])
    monkeypatch.setattr(pipeline_mod, "expand_query", lambda q, domain_id=None: {"expanded_queries": [q]})
    monkeypatch.setattr(pipeline_mod, "_fill_missing_dois", lambda c, limit=10: None)
    monkeypatch.setattr(pipeline_mod, "dedupe_and_rank_candidates", lambda c, query, max_candidates: c)

    batch = discover_papers("snow", output_dir=__import__("tempfile").mkdtemp())

    assert batch.sources == ["openalex", "crossref"]
    assert seen["openalex"] == 1
    assert seen["crossref"] == 1
import json

import pytest

import src.discovery.pipeline as pipeline_mod
from src.discovery import resolve_crossref, search_openalex
from src.discovery.models import CandidateBatch, PaperCandidate
from src.discovery.pipeline import discover_papers
from src.discovery.search_openalex import parse_openalex_work
from src.services.openalex_credentials import OpenAlexCredentials, safe_request_error_summary
from src.services.v2_library import empty_metadata


pytestmark = pytest.mark.unit


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


# ── OpenAlex credential pure-function tests ───────────────────────

def test_openalex_headers_with_credentials():
    creds = OpenAlexCredentials(api_key="test-key-12345")
    h = search_openalex._headers(creds)
    assert h["Authorization"] == "Bearer test-key-12345"


def test_openalex_headers_without_credentials():
    creds = OpenAlexCredentials()
    h = search_openalex._headers(creds)
    assert "Authorization" not in h


def test_openalex_params_with_email():
    creds = OpenAlexCredentials(email="discover@test.org")
    p = search_openalex._params("snow", 10, creds)
    assert p["mailto"] == "discover@test.org"


def test_openalex_params_without_email():
    creds = OpenAlexCredentials()
    p = search_openalex._params("snow", 10, creds)
    assert "mailto" not in p


# ── OpenAlex integration tests ────────────────────────────────────

def test_search_openalex_loads_credentials_once(monkeypatch):
    """load_openalex_credentials must be called exactly once per search."""
    call_count = 0

    def counting_loader(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return OpenAlexCredentials()

    monkeypatch.setattr(search_openalex, "load_openalex_credentials", counting_loader)
    monkeypatch.setattr(
        search_openalex.requests, "get",
        lambda *a, **k: type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: {"results": []}})(),
    )

    result = search_openalex.search_openalex("snow")
    assert call_count == 1, f"Expected 1 call, got {call_count}"
    assert result == []


def test_search_openalex_requests_passes_credentials(monkeypatch):
    creds = OpenAlexCredentials(email="req@test.org", api_key="req-test-key")
    monkeypatch.setattr(
        search_openalex, "load_openalex_credentials",
        lambda *a, **k: creds,
    )

    captured_params = None
    captured_headers = None

    def capture_get(url, **kwargs):
        nonlocal captured_params, captured_headers
        captured_params = kwargs.get("params", {})
        captured_headers = kwargs.get("headers", {})
        return type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: {"results": []}})()

    monkeypatch.setattr(search_openalex.requests, "get", capture_get)

    search_openalex.search_openalex("snow")
    assert captured_params is not None
    assert captured_params.get("mailto") == "req@test.org"
    assert captured_headers.get("Authorization") == "Bearer req-test-key"


def test_search_openalex_error_does_not_leak_credentials(monkeypatch):
    """A requests exception containing credentials in its message must not
    leak them into logs or the return value."""
    creds = OpenAlexCredentials(email="leak-check@test.org", api_key="leak-check-key-99999")

    class LeakyException(Exception):
        def __init__(self):
            super().__init__(
                "GET https://api.openalex.org/works?mailto=leak-check@test.org "
                "Authorization: Bearer leak-check-key-99999"
            )

    monkeypatch.setattr(
        search_openalex, "load_openalex_credentials",
        lambda *a, **k: creds,
    )
    monkeypatch.setattr(
        search_openalex.requests, "get",
        lambda *a, **k: (_ for _ in ()).throw(LeakyException()),
    )

    # Capture log output
    log_lines = []
    monkeypatch.setattr(
        search_openalex.logger, "warning",
        lambda msg, *args: log_lines.append(msg.format(*args) if args else msg),
    )

    result = search_openalex.search_openalex("snow")
    assert result == []

    # Check no credentials leaked into logs
    log_text = "\n".join(log_lines)
    assert "leak-check@test.org" not in log_text, f"Email leaked into log: {log_text}"
    assert "leak-check-key-99999" not in log_text, f"API key leaked into log: {log_text}"


# ── Crossref tests ────────────────────────────────────────────────

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


def test_pipeline_uses_openalex_and_crossref_only(monkeypatch, tmp_path):
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

    batch = discover_papers("snow", output_dir=tmp_path)

    assert batch.sources == ["openalex", "crossref"]
    assert seen["openalex"] == 1
    assert seen["crossref"] == 1


def test_discovery_merges_openalex_crossref_same_doi(monkeypatch, tmp_path):
    openalex_cand = PaperCandidate(title="Shared DOI", doi="10.1000/shared", source="openalex")
    crossref_cand = PaperCandidate(title="Shared DOI", doi="10.1000/shared", source="crossref")

    monkeypatch.setattr(pipeline_mod, "search_openalex", lambda q, domain_id=None, limit=15: [openalex_cand])
    monkeypatch.setattr(pipeline_mod, "search_crossref", lambda q, domain_id=None, limit=15: [crossref_cand])
    monkeypatch.setattr(pipeline_mod, "expand_query", lambda q, domain_id=None: {"expanded_queries": [q]})
    monkeypatch.setattr(pipeline_mod, "_fill_missing_dois", lambda c, limit=10: None)

    batch = discover_papers("snow", output_dir=tmp_path)

    assert len(batch.candidates) == 1
    assert batch.candidates[0].doi == "10.1000/shared"
    assert set(batch.candidates[0].source.split(",")) == {"openalex", "crossref"}


def test_discovery_hide_existing_filters_jsonl_only(monkeypatch, tmp_path):
    paper_raw = tmp_path / "paper_raw" / "0000000000000001"
    paper_raw.mkdir(parents=True)
    meta = empty_metadata("0000000000000001", source_type="network_search")
    meta["identifiers"]["doi"] = "10.1000/existing"
    (paper_raw / "0000000000000001.metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    existing = PaperCandidate(title="Existing", doi="10.1000/existing", source="openalex")
    unique = PaperCandidate(title="Unique", doi="10.1000/unique", source="crossref")

    monkeypatch.setattr(pipeline_mod, "search_openalex", lambda q, domain_id=None, limit=15: [existing])
    monkeypatch.setattr(pipeline_mod, "search_crossref", lambda q, domain_id=None, limit=15: [unique])
    monkeypatch.setattr(pipeline_mod, "expand_query", lambda q, domain_id=None: {"expanded_queries": [q]})
    monkeypatch.setattr(pipeline_mod, "_fill_missing_dois", lambda c, limit=10: None)

    batch = discover_papers(
        "snow",
        output_dir=tmp_path / "out",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        hide_existing=True,
    )

    assert [candidate.doi for candidate in batch.candidates] == ["10.1000/unique"]
    summary_path = next((tmp_path / "out").glob("*_summary.json"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["existing_duplicates_detected"] == 1
    assert summary["hidden_existing_duplicates"] == 1
    assert summary["visible_candidates"] == 1

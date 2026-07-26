import pytest

from src.discovery import resolve_crossref, search_openalex
from src.discovery.models import PaperCandidate
from src.discovery.providers.provider_models import DiscoveryPage, ProviderSearchRequest
from src.discovery.search_openalex import parse_openalex_work
from src.fetch.openalex_credentials import OpenAlexCredentials, safe_request_error_summary


pytestmark = pytest.mark.unit


def _install_runtime(monkeypatch, transport, *, max_retries: int = 0) -> None:
    """Install ``transport`` as the process-wide ProviderRuntime singleton.

    Replaces the legacy ``search_openalex.requests`` / ``resolve_crossref.requests``
    mocks: both modules now route HTTP through the unified ProviderClient.
    """
    from src.discovery.providers.provider_client import ProviderRuntime
    from src.utils.rate_limit import default_config
    from tests.helpers.fake_provider import FakeClock, FakeSleeper

    cfg = default_config()
    cfg["global"]["paper_interval_seconds"] = 0.0
    cfg["global"]["jitter_seconds"] = 0.0
    for p in cfg.get("providers", ()):
        cfg["providers"][p]["min_interval_seconds"] = 0.0
    runtime = ProviderRuntime(
        config=cfg, transport=transport, max_retries=max_retries,
        sleeper=FakeSleeper(FakeClock()), clock=FakeClock(),
    )
    monkeypatch.setattr(ProviderRuntime, "_instance", runtime)


class _CapturingTransport:
    """Records every RequestSpec seen; returns ``response`` or raises ``exc``."""

    def __init__(self, response=None, exc: Exception | None = None) -> None:
        self.response = response
        self.exc = exc
        self.specs: list = []

    def send(self, spec, timeout_seconds):
        self.specs.append(spec)
        if self.exc is not None:
            raise self.exc
        return self.response


def test_provider_identity_is_retained_on_request_and_page():
    request = ProviderSearchRequest(
        keyword_id="0123456789abcdef",
        keyword_zh="风吹雪",
        query_id="fedcba9876543210",
        query="blowing snow",
        query_language="en",
        provider="openalex",
        lane="backfill",
    )
    page = DiscoveryPage(
        provider=request.provider,
        keyword_zh=request.keyword_zh,
        query_id=request.query_id,
        query=request.query,
        query_language=request.query_language,
        lane=request.lane,
    )
    payload = page.to_dict()
    assert payload["query_id"] == request.query_id
    assert payload["query"] == request.query
    assert payload["query_language"] == request.query_language


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
    from tests.helpers.fake_provider import http_response
    _install_runtime(monkeypatch, _CapturingTransport(response=http_response(200, {"results": []})))

    result = search_openalex.search_openalex("snow")
    assert call_count == 1, f"Expected 1 call, got {call_count}"
    assert result == []


def test_search_openalex_requests_passes_credentials(monkeypatch):
    creds = OpenAlexCredentials(email="req@test.org", api_key="req-test-key")
    monkeypatch.setattr(
        search_openalex, "load_openalex_credentials",
        lambda *a, **k: creds,
    )
    from tests.helpers.fake_provider import http_response
    transport = _CapturingTransport(response=http_response(200, {"results": []}))
    _install_runtime(monkeypatch, transport)

    search_openalex.search_openalex("snow")
    spec = transport.specs[0]
    assert spec.params.get("mailto") == "req@test.org"
    assert spec.headers.get("Authorization") == "Bearer req-test-key"


def test_search_openalex_error_does_not_leak_credentials(monkeypatch):
    """A transport exception containing credentials in its message must not
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
    _install_runtime(monkeypatch, _CapturingTransport(exc=LeakyException()))

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
    class Boom(Exception):
        pass

    _install_runtime(monkeypatch, _CapturingTransport(exc=Boom("network down")))
    assert resolve_crossref.search_crossref("snow") == []



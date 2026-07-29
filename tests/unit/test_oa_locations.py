"""Tests for OA candidate ranking.

The behaviour under test is the fix for a measured failure: providers rank
the publisher copy first, the publisher refuses this network, and collapsing
the location list to one URL therefore threw away the repository copy that
does download.
"""
import pytest

from src.fetch.oa_locations import (
    PdfCandidate,
    all_candidates_blocked,
    candidate_urls,
    candidates_from_openalex,
    candidates_from_semantic_scholar,
    candidates_from_unpaywall,
    rank_candidates,
)


# A real case: Wiley refuses this egress, the Brazilian repository copy of the
# same article returns a valid PDF.
WILEY_PDF = "https://onlinelibrary.wiley.com/doi/pdfdirect/10.1002/esp.3310"
REPO_PDF = "https://repositorio.ufc.br/bitstream/riufc/1/article.pdf"


# ── 1. repository copies outrank blocked publisher copies ─────────────

def test_repository_copy_outranks_blocked_publisher_copy():
    payload = {
        "is_oa": True,
        "oa_status": "bronze",
        "best_oa_location": {"host_type": "publisher", "url_for_pdf": WILEY_PDF},
        "oa_locations": [
            {"host_type": "publisher", "url_for_pdf": WILEY_PDF},
            {"host_type": "repository", "url_for_pdf": REPO_PDF},
        ],
    }
    ranked = candidates_from_unpaywall(payload)
    assert candidate_urls(ranked)[0] == REPO_PDF, "the reachable copy must be tried first"
    assert WILEY_PDF in candidate_urls(ranked), "the publisher copy stays as a fallback"


def test_repository_outranks_publisher_even_when_neither_is_blocked():
    ranked = rank_candidates([
        PdfCandidate(url="https://journal.example.org/a.pdf", host_type="publisher", is_direct_pdf=True),
        PdfCandidate(url="https://repo.example.edu/a.pdf", host_type="repository", is_direct_pdf=True),
    ])
    assert candidate_urls(ranked)[0] == "https://repo.example.edu/a.pdf"


# ── 2. direct PDF links outrank landing pages ─────────────────────────

def test_direct_pdf_outranks_landing_page_on_same_host():
    payload = {
        "is_oa": True,
        "best_oa_location": {
            "host_type": "repository",
            "url": "https://repo.example.edu/record/1",
            "url_for_pdf": "https://repo.example.edu/record/1/file.pdf",
        },
    }
    urls = candidate_urls(candidates_from_unpaywall(payload))
    assert urls == [
        "https://repo.example.edu/record/1/file.pdf",
        "https://repo.example.edu/record/1",
    ]


# ── 3. published version outranks accepted/submitted ──────────────────

def test_published_version_outranks_accepted_manuscript():
    ranked = rank_candidates([
        PdfCandidate(url="https://repo.example.edu/accepted.pdf", host_type="repository",
                     version="acceptedVersion", is_direct_pdf=True),
        PdfCandidate(url="https://repo.example.edu/published.pdf", host_type="repository",
                     version="publishedVersion", is_direct_pdf=True),
    ])
    assert candidate_urls(ranked)[0] == "https://repo.example.edu/published.pdf"


# ── 4. deduplication keeps the first, richer occurrence ───────────────

def test_duplicate_urls_are_collapsed():
    payload = {
        "is_oa": True,
        "best_oa_location": {"host_type": "repository", "url_for_pdf": REPO_PDF},
        "oa_locations": [
            {"host_type": "repository", "url_for_pdf": REPO_PDF},
            {"host_type": "repository", "url_for_pdf": REPO_PDF},
        ],
    }
    assert candidate_urls(candidates_from_unpaywall(payload)) == [REPO_PDF]


def test_empty_and_missing_urls_are_dropped():
    payload = {"is_oa": True, "best_oa_location": {"url_for_pdf": "", "url": None},
               "oa_locations": [{}, {"url": "   "}]}
    assert candidates_from_unpaywall(payload) == []


# ── 5. OpenAlex: all open locations, not just primary_location ────────

def test_openalex_keeps_every_open_location():
    work = {
        "open_access": {"is_oa": True, "oa_url": "https://doi.org/10.1/x"},
        "primary_location": {
            "is_oa": True,
            "pdf_url": WILEY_PDF,
            "source": {"type": "journal"},
        },
        "locations": [
            {"is_oa": True, "pdf_url": REPO_PDF, "source": {"type": "repository"}},
        ],
    }
    urls = candidate_urls(candidates_from_openalex(work))
    assert urls[0] == REPO_PDF
    assert WILEY_PDF in urls


def test_openalex_skips_closed_locations():
    work = {
        "open_access": {"is_oa": True},
        "locations": [
            {"is_oa": False, "pdf_url": "https://paywall.example.org/a.pdf"},
            {"is_oa": True, "pdf_url": "https://repo.example.edu/a.pdf",
             "source": {"type": "repository"}},
        ],
    }
    assert candidate_urls(candidates_from_openalex(work)) == ["https://repo.example.edu/a.pdf"]


def test_openalex_source_type_maps_onto_host_type():
    work = {
        "open_access": {"is_oa": True},
        "locations": [
            {"is_oa": True, "pdf_url": "https://p.example.org/a.pdf", "source": {"type": "journal"}},
            {"is_oa": True, "pdf_url": "https://r.example.edu/a.pdf", "source": {"type": "repository"}},
        ],
    }
    ranked = candidates_from_openalex(work)
    assert [c.host_type for c in ranked] == ["repository", "publisher"]


# ── 6. Semantic Scholar ───────────────────────────────────────────────

def test_semantic_scholar_single_pdf():
    data = {"isOpenAccess": True, "openAccessPdf": {"url": REPO_PDF, "license": "CC-BY"}}
    ranked = candidates_from_semantic_scholar(data)
    assert candidate_urls(ranked) == [REPO_PDF]
    assert ranked[0].license == "CC-BY"


@pytest.mark.parametrize("data", [{}, {"openAccessPdf": None}, {"openAccessPdf": {"url": ""}}])
def test_semantic_scholar_without_pdf(data):
    assert candidates_from_semantic_scholar(data) == []


# ── 7. blocked-host reporting ─────────────────────────────────────────

def test_all_candidates_blocked_detects_publisher_only_paper():
    payload = {"is_oa": True, "best_oa_location": {"host_type": "publisher", "url_for_pdf": WILEY_PDF}}
    assert all_candidates_blocked(candidates_from_unpaywall(payload)) is True


def test_all_candidates_blocked_is_false_when_a_reachable_copy_exists():
    payload = {
        "is_oa": True,
        "best_oa_location": {"host_type": "publisher", "url_for_pdf": WILEY_PDF},
        "oa_locations": [{"host_type": "repository", "url_for_pdf": REPO_PDF}],
    }
    assert all_candidates_blocked(candidates_from_unpaywall(payload)) is False


def test_all_candidates_blocked_is_false_for_empty_input():
    assert all_candidates_blocked([]) is False


# ── 8. persistence shape ──────────────────────────────────────────────

def test_to_dict_exposes_host_and_blocked_flag_without_raw_payload():
    candidate = PdfCandidate(url=WILEY_PDF, host_type="publisher", is_direct_pdf=True,
                             extra={"secret": "must not leak"})
    payload = candidate.to_dict()
    assert payload["host"] == "onlinelibrary.wiley.com"
    assert payload["blocked_host"] is True
    assert "extra" not in payload and "secret" not in str(payload)

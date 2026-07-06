from src.fetch.fetch_arxiv import resolve_arxiv_pdf
from src.fetch.fetch_pipeline import safe_doi_slug
from src.fetch.models import FetchResult


def test_fetch_result_round_trip_and_doi_normalization():
    result = FetchResult(doi="https://doi.org/10.1000/ABC", success=True, source="unpaywall")
    restored = FetchResult.from_dict(result.to_dict())
    assert restored.doi == "10.1000/abc"
    assert restored.success is True


def test_safe_doi_slug_is_filesystem_friendly():
    assert safe_doi_slug("10.1000/ABC DEF") == "10.1000_abc_def"


def test_arxiv_resolver_requires_known_arxiv_id():
    ok = resolve_arxiv_pdf("10.1/x", {"externalIds": {"ArXiv": "2401.12345"}})
    missing = resolve_arxiv_pdf("10.1/x", {})
    assert ok.success is True
    assert ok.pdf_url.endswith("2401.12345.pdf")
    assert missing.success is False


def test_fetch_result_new_fields_round_trip():
    result = FetchResult(
        doi="10.1000/abc",
        success=True,
        source="unpaywall",
        status_code=200,
        open_access=True,
        fetched_at="2026-06-27T00:00:00+00:00",
        raw={"maybe_landing_page": True, "k": "v"},
    )
    restored = FetchResult.from_dict(result.to_dict())
    assert restored.status_code == 200
    assert restored.open_access is True
    assert restored.fetched_at == "2026-06-27T00:00:00+00:00"
    assert restored.raw == {"maybe_landing_page": True, "k": "v"}


def test_fetch_result_legacy_sidecar_compat():
    """旧 sidecar（仅含原 12 字段）经 from_dict 后兼容字段填充。"""
    legacy = {
        "doi": "10.1000/abc",
        "success": True,
        "source": "unpaywall",
        "pdf_url": "https://example.org/p.pdf",
        "output_path": "",
        "sidecar_path": "",
        "oa_status": "gold",
        "license": "cc-by",
        "sha256": "",
        "error": "",
        "downloaded_at": "2026-06-27T00:00:00+00:00",
        "metadata": {"k": "v"},
    }
    restored = FetchResult.from_dict(legacy)
    assert restored.status_code is None
    assert restored.open_access is None
    # fetched_at 与 downloaded_at 同步
    assert restored.fetched_at == restored.downloaded_at == "2026-06-27T00:00:00+00:00"
    # raw 与 metadata 互为兼容来源
    assert restored.raw == restored.metadata == {"k": "v"}


def test_fetch_result_fetched_at_synced_with_downloaded_at():
    result = FetchResult(doi="10.1/x", success=True, source="unpaywall")
    assert result.fetched_at == result.downloaded_at
    assert result.fetched_at


"""FetchResult access metadata + 新旧 sidecar 兼容测试。"""
from src.fetch.models import FetchResult


def test_new_fields_default():
    r = FetchResult(doi="10.1/x")
    assert r.access_mode == "oa_only"
    assert r.resolver == ""
    assert r.resolver_chain == []
    assert r.landing_url == ""
    assert r.is_direct_pdf is None
    assert r.requires_user_action is False
    assert r.action_hint == ""
    assert r.access_status == ""
    assert r.supplementary_urls == []
    assert r.has_supplementary is None


def test_new_fields_round_trip():
    r = FetchResult(
        doi="10.1/x", success=True, source="openalex",
        access_mode="oa_only", resolver="openalex",
        resolver_chain=["unpaywall", "openalex"],
        landing_url="https://doi.org/10.1/x",
        is_direct_pdf=True, access_status="open_access",
        supplementary_urls=["https://example.org/suppl.zip"],
        has_supplementary=True,
    )
    restored = FetchResult.from_dict(r.to_dict())
    assert restored.access_mode == "oa_only"
    assert restored.resolver == "openalex"
    assert restored.resolver_chain == ["unpaywall", "openalex"]
    assert restored.landing_url == "https://doi.org/10.1/x"
    assert restored.is_direct_pdf is True
    assert restored.access_status == "open_access"
    assert restored.supplementary_urls == ["https://example.org/suppl.zip"]
    assert restored.has_supplementary is True


def test_legacy_sidecar_compat():
    """旧 sidecar 缺新字段，from_dict 用默认值填充。"""
    legacy = {
        "doi": "10.1/x",
        "success": True,
        "source": "unpaywall",
        "pdf_url": "https://example.org/p.pdf",
        "metadata": {"access_mode": "institutional"},
    }
    restored = FetchResult.from_dict(legacy)
    # 新字段用默认值
    assert restored.resolver == ""
    assert restored.resolver_chain == []
    assert restored.landing_url == ""
    assert restored.is_direct_pdf is None
    assert restored.requires_user_action is False
    # access_mode 从 metadata 回退
    assert restored.access_mode == "institutional"


def test_supplementary_fields():
    r = FetchResult(doi="10.1/x", supplementary_urls=["https://example.org/s1"])
    assert r.has_supplementary is None
    assert len(r.supplementary_urls) == 1
    d = r.to_dict()
    assert "supplementary_urls" in d
    assert "has_supplementary" in d

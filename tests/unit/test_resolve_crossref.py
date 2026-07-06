"""resolve_crossref 兼容接口测试：纯 monkeypatch mock，不访问真实网络。"""
from src.discovery import resolve_crossref
from src.discovery.resolve_crossref import (
    get_crossref_work_by_doi,
    resolve_crossref_by_title,
    resolve_doi_by_title,
)


def _crossref_response(items):
    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    return FakeResponse({"message": {"items": items}})


def test_resolve_crossref_by_title_ranks_by_similarity_and_year(monkeypatch):
    items = [
        # 标题接近 + 年份接近
        {"DOI": "10.1/a", "title": ["Blowing Snow Sublimation Study"], "issued": {"date-parts": [[2020]]}},
        # 标题相差大 + 年份远
        {"DOI": "10.1/b", "title": ["Completely Unrelated Ocean Topic"], "issued": {"date-parts": [[2001]]}},
    ]
    monkeypatch.setattr(
        resolve_crossref.requests,
        "get",
        lambda *args, **kwargs: _crossref_response(items),
    )
    ranked = resolve_crossref_by_title("blowing snow sublimation", year=2020, limit=5)
    assert len(ranked) == 2
    assert ranked[0].doi == "10.1/a"
    assert ranked[0].confidence >= 0.0
    assert ranked[0].confidence >= ranked[1].confidence


def test_resolve_crossref_by_title_returns_empty_on_network_error(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(resolve_crossref.requests, "get", boom)
    assert resolve_crossref_by_title("anything") == []


def test_get_crossref_work_by_doi_returns_message(monkeypatch):
    payload = {"message": {"DOI": "10.1/x", "title": ["t"]}}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    monkeypatch.setattr(resolve_crossref.requests, "get", lambda *args, **kwargs: FakeResponse())
    work = get_crossref_work_by_doi("https://doi.org/10.1/x")
    assert work == payload["message"]


def test_get_crossref_work_by_doi_returns_none_on_error(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(resolve_crossref.requests, "get", boom)
    assert get_crossref_work_by_doi("10.1/x") is None


def test_get_crossref_work_by_doi_rejects_empty_doi():
    assert get_crossref_work_by_doi("") is None


def test_resolve_doi_by_title_contract_preserved(monkeypatch):
    items = [
        {"DOI": "10.1/a", "title": ["Blowing Snow Sublimation Study"], "issued": {"date-parts": [[2020]]}},
    ]
    monkeypatch.setattr(
        resolve_crossref.requests,
        "get",
        lambda *args, **kwargs: _crossref_response(items),
    )
    best = resolve_doi_by_title("blowing snow sublimation study", year=2020)
    assert best is not None
    assert best.doi == "10.1/a"

    # 标题差距大时应返回 None
    items_bad = [{"DOI": "10.1/b", "title": ["Unrelated Ocean"], "issued": {"date-parts": [[2001]]}}]
    monkeypatch.setattr(
        resolve_crossref.requests,
        "get",
        lambda *args, **kwargs: _crossref_response(items_bad),
    )
    assert resolve_doi_by_title("blowing snow sublimation study", year=2020) is None

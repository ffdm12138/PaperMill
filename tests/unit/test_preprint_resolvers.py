"""Preprint resolver 测试（mock requests，不访问真实网络）。"""
from unittest.mock import patch

import pytest
from src.fetch.resolvers.preprint_resolvers import BiorxivResolver, PmcOaResolver
from src.fetch.resolvers.base import ResolveContext


class FakeResponse:
    def __init__(self, content, status=200):
        self._content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")
        return None

    def json(self):
        import json
        return json.loads(self._content)

    @property
    def text(self):
        return self._content


def test_biorxiv_success(monkeypatch):
    """bioRxiv 返回 PDF URL。"""
    def mock_get(url, **kw):
        return FakeResponse(
            '{"collection": [{"pdf_rel": "10.1101/2020.01.01.123456v1"}]}'
        )
    monkeypatch.setattr("src.fetch.resolvers.preprint_resolvers.requests.get", mock_get)

    r = BiorxivResolver()
    result = r.resolve(ResolveContext(doi="10.1101/2020.01.01.123456"))
    assert result.success is True
    assert "full.pdf" in result.pdf_url
    assert "biorxiv.org" in result.pdf_url


def test_biorxiv_no_entries(monkeypatch):
    """bioRxiv 空结果。"""
    def mock_get(url, **kw):
        return FakeResponse('{"collection": []}')
    monkeypatch.setattr("src.fetch.resolvers.preprint_resolvers.requests.get", mock_get)

    r = BiorxivResolver()
    result = r.resolve(ResolveContext(doi="10.1101/xxxx"))
    assert result.success is False
    assert "no entries" in result.error


def test_pmc_oa_success(monkeypatch):
    """PMC OA 返回 PDF URL。"""
    def mock_get(url, **kw):
        xml = '<links><link format="pdf" href="/pmc/articles/PMC12345/pdf/main.pdf"/></links>'
        return FakeResponse(xml)
    monkeypatch.setattr("src.fetch.resolvers.preprint_resolvers.requests.get", mock_get)

    r = PmcOaResolver()
    result = r.resolve(ResolveContext(doi="10.1/pmc_test"))
    assert result.success is True
    assert "ncbi.nlm.nih.gov" in result.pdf_url
    assert "PMC12345" in result.pdf_url


def test_pmc_oa_no_pdf(monkeypatch):
    """PMC OA 无 PDF 链接。"""
    def mock_get(url, **kw):
        return FakeResponse("<links></links>")
    monkeypatch.setattr("src.fetch.resolvers.preprint_resolvers.requests.get", mock_get)

    r = PmcOaResolver()
    result = r.resolve(ResolveContext(doi="10.1/pmc_missing"))
    assert result.success is False

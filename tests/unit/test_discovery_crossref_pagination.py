"""Unit tests for Crossref cursor pagination (search_crossref_page)."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from src.discovery.resolve_crossref import search_crossref_page


pytestmark = pytest.mark.unit


def _crossref_response(items, next_cursor, total=None):
    return {
        "message": {
            "items": items,
            "next-cursor": next_cursor,
            "total-results": total if total is not None else len(items),
        }
    }


def _item(doi, title="T"):
    return {
        "DOI": doi,
        "title": [title],
        "container-title": ["Journal"],
        "author": [],
        "URL": f"https://doi.org/{doi}",
        "is-referenced-by-count": 0,
    }


class TestCrossrefPage:
    def test_success_with_next_cursor(self):
        data = _crossref_response([_item("10.1/a"), _item("10.1/b")], next_cursor="CR2")
        mock_resp = MagicMock()
        mock_resp.json.return_value = data
        mock_resp.headers = {}
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        with patch("src.discovery.resolve_crossref.requests.get", return_value=mock_resp):
            page = search_crossref_page(
                "boundary layer",
                original_keyword="boundary layer",
                lane="backfill",
                page_size=50,
                cursor="CR1",
            )
        assert page.status == "success"
        assert page.next_cursor == "CR2"
        assert page.returned_count == 2
        assert page.exhausted is False

    def test_exhausted_when_no_next_cursor(self):
        data = _crossref_response([_item("10.1/a")], next_cursor=None)
        mock_resp = MagicMock()
        mock_resp.json.return_value = data
        mock_resp.headers = {}
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        with patch("src.discovery.resolve_crossref.requests.get", return_value=mock_resp):
            page = search_crossref_page(
                "boundary layer",
                original_keyword="boundary layer",
                lane="backfill",
                page_size=50,
                cursor="CR1",
            )
        assert page.exhausted is True

    def test_short_page_with_next_cursor_not_exhausted(self):
        """A short page that still carries next-cursor must NOT be marked exhausted."""
        data = _crossref_response([_item("10.1/a")], next_cursor="CR2")
        mock_resp = MagicMock()
        mock_resp.json.return_value = data
        mock_resp.headers = {}
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        with patch("src.discovery.resolve_crossref.requests.get", return_value=mock_resp):
            page = search_crossref_page(
                "boundary layer",
                original_keyword="boundary layer",
                lane="backfill",
                page_size=50,
                cursor="CR1",
            )
        assert page.exhausted is False

    def test_failure_does_not_advance_cursor(self):
        mock_resp = MagicMock()
        mock_resp.headers = {}
        mock_resp.status_code = 503
        mock_resp.raise_for_status.side_effect = Exception("service unavailable")
        with patch("src.discovery.resolve_crossref.requests.get", return_value=mock_resp):
            page = search_crossref_page(
                "boundary layer",
                original_keyword="boundary layer",
                lane="backfill",
                page_size=50,
                cursor="CR1",
            )
        assert page.status == "failed"
        assert page.next_cursor is None
        assert page.request_cursor == "CR1"

    def test_safe_error_strips_url_query(self):
        """Error sanitization must not leak URL params (which may carry keys)."""
        with patch(
            "src.discovery.resolve_crossref.requests.get",
            side_effect=ConnectionError("GET https://api.crossref.org/works?secret=ABCDEF timeout"),
        ):
            page = search_crossref_page(
                "boundary layer",
                original_keyword="boundary layer",
                lane="backfill",
                page_size=50,
                cursor="CR1",
            )
        assert page.status == "failed"
        assert "ABCDEF" not in (page.safe_error or "")

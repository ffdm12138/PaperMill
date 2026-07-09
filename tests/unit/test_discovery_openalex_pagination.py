"""Unit tests for OpenAlex cursor pagination (search_openalex_page)."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from src.discovery.provider_models import DiscoveryPage
from src.discovery.search_openalex import search_openalex_page


pytestmark = pytest.mark.unit


def _openalex_response(results, next_cursor, count=None):
    """Build a fake OpenAlex /works JSON response."""
    return {
        "meta": {"next_cursor": next_cursor, "count": count if count is not None else len(results)},
        "results": results,
    }


def _work(doi, title="T"):
    return {
        "id": f"https://openalex.org/W{doi}",
        "display_name": title,
        "doi": f"https://doi.org/{doi}",
        "publication_year": 2020,
        "authorships": [],
        "primary_location": {},
        "open_access": {"is_oa": False},
    }


class TestOpenAlexPage:
    def test_success_with_next_cursor(self):
        data = _openalex_response([_work("10.1/a"), _work("10.1/b")], next_cursor="CURSOR2")
        mock_resp = MagicMock()
        mock_resp.json.return_value = data
        mock_resp.headers = {}
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        with patch("src.discovery.search_openalex.requests.get", return_value=mock_resp):
            page = search_openalex_page(
                "boundary layer",
                original_keyword="boundary layer",
                lane="backfill",
                page_size=25,
                cursor="CURSOR1",
            )
        assert page.status == "success"
        assert page.next_cursor == "CURSOR2"
        assert page.returned_count == 2
        assert page.exhausted is False
        assert page.request_cursor == "CURSOR1"

    def test_success_exhausted_when_no_next_cursor(self):
        data = _openalex_response([_work("10.1/a")], next_cursor=None)
        mock_resp = MagicMock()
        mock_resp.json.return_value = data
        mock_resp.headers = {}
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        with patch("src.discovery.search_openalex.requests.get", return_value=mock_resp):
            page = search_openalex_page(
                "boundary layer",
                original_keyword="boundary layer",
                lane="refresh",
                page_size=25,
                cursor="*",
            )
        assert page.status == "success"
        assert page.exhausted is True
        assert page.next_cursor is None

    def test_failure_does_not_return_cursor(self):
        mock_resp = MagicMock()
        mock_resp.headers = {}
        mock_resp.status_code = 500
        mock_resp.raise_for_status.side_effect = Exception("server error")
        with patch("src.discovery.search_openalex.requests.get", return_value=mock_resp):
            page = search_openalex_page(
                "boundary layer",
                original_keyword="boundary layer",
                lane="backfill",
                page_size=25,
                cursor="CURSOR1",
            )
        assert page.status == "failed"
        assert page.next_cursor is None
        assert page.request_cursor == "CURSOR1"
        assert page.exhausted is False
        assert page.safe_error is not None

    def test_network_exception_returns_failed_page(self):
        with patch("src.discovery.search_openalex.requests.get", side_effect=ConnectionError("refused")):
            page = search_openalex_page(
                "boundary layer",
                original_keyword="boundary layer",
                lane="backfill",
                page_size=25,
                cursor="CURSOR1",
            )
        assert page.status == "failed"
        assert page.next_cursor is None

    def test_refresh_uses_star_cursor(self):
        data = _openalex_response([_work("10.1/a")], next_cursor="NEXT")
        mock_resp = MagicMock()
        mock_resp.json.return_value = data
        mock_resp.headers = {}
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        with patch("src.discovery.search_openalex.requests.get", return_value=mock_resp) as mock_get:
            search_openalex_page(
                "boundary layer",
                original_keyword="boundary layer",
                lane="refresh",
                page_size=25,
                cursor="*",
            )
        sent_params = mock_get.call_args.kwargs.get("params", {})
        assert sent_params.get("cursor") == "*"

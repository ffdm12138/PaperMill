from __future__ import annotations

import argparse

import pytest

import scripts.fetch_pdf_for_paper_raw as fetch_cli


pytestmark = pytest.mark.unit


def _args(resolver: str, *, base_url: str = "", url_template: str = "") -> argparse.Namespace:
    return argparse.Namespace(resolver=resolver, base_url=base_url, url_template=url_template, timeout=30)


def test_header_based_policy_records_masked_headers_only_as_config():
    policy = fetch_cli._build_policy(_args("header-based"), {"Cookie": "secret"})

    assert policy.enabled_resolver_names() == ["header_based"]
    assert policy.extra["headers"] == {"Cookie": "secret"}

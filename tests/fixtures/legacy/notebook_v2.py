"""Intentionally unsupported discovery notebook v2 fixtures.

This module is the only ordinary test fixture that constructs the retired
notebook container. Active code never imports it.
"""
from __future__ import annotations

from typing import Any


RETIRED_QUERY_CONTAINER_FIELD = "expansions"


def v2_notebook_payload(keyword_id_value: str) -> dict[str, Any]:
    return {
        "schema_version": "2.0",
        "keyword_id": keyword_id_value,
        RETIRED_QUERY_CONTAINER_FIELD: {},
        "lifetime_statistics": {},
    }


def inject_retired_query_container(payload: dict[str, Any]) -> dict[str, Any]:
    payload[RETIRED_QUERY_CONTAINER_FIELD] = {}
    return payload

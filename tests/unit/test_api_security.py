from __future__ import annotations

import importlib
import sys

import pytest
from fastapi.testclient import TestClient

import config
import config.settings as settings
import src.server as server


def test_no_api_key_keeps_localhost_default(monkeypatch):
    monkeypatch.setattr(server, "MINERU_API_KEY", "")
    client = TestClient(server.app)

    resp = client.get("/status")

    assert resp.status_code == 200


def test_api_key_required_when_configured(monkeypatch):
    monkeypatch.setattr(server, "MINERU_API_KEY", "test-key")
    client = TestClient(server.app)

    assert client.get("/status").status_code == 401
    assert client.get("/status", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/status", headers={"X-API-Key": "test-key"}).status_code == 200


def test_security_headers_present(monkeypatch):
    monkeypatch.setattr(server, "MINERU_API_KEY", "")
    client = TestClient(server.app)

    resp = client.get("/status")

    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "no-referrer"


def test_input_length_constraints(monkeypatch):
    monkeypatch.setattr(server, "MINERU_API_KEY", "")
    client = TestClient(server.app)

    assert client.post("/prompt/plan-reading", json={"question": "x" * 4001}).status_code == 422
    assert client.post(
        "/prompt/read-fulltext",
        json={"question": "ok", "paper_names": [f"id{i}" for i in range(101)]},
    ).status_code == 422
    assert client.post("/write/jobs", json={"topic": "x" * 1001}).status_code == 422


def _reload_settings(monkeypatch, *, host: str, key: str = "", unsafe: str = ""):
    monkeypatch.setenv("MINERU_API_HOST", host)
    if key:
        monkeypatch.setenv("MINERU_API_KEY", key)
    else:
        monkeypatch.delenv("MINERU_API_KEY", raising=False)
    if unsafe:
        monkeypatch.setenv("MINERU_ALLOW_UNAUTHENTICATED_PUBLIC_API", unsafe)
    else:
        monkeypatch.delenv("MINERU_ALLOW_UNAUTHENTICATED_PUBLIC_API", raising=False)
    sys.modules.pop("config.settings", None)
    try:
        return importlib.import_module("config.settings")
    finally:
        sys.modules["config.settings"] = settings
        config.settings = settings


def test_public_host_without_key_fails(monkeypatch):
    with pytest.raises(RuntimeError):
        _reload_settings(monkeypatch, host="0.0.0.0")


def test_public_host_with_key_allowed(monkeypatch):
    loaded = _reload_settings(monkeypatch, host="0.0.0.0", key="test-key")

    assert loaded.API_HOST == "0.0.0.0"
    assert loaded.MINERU_API_KEY == "test-key"


def test_public_host_unsafe_override_warns(monkeypatch):
    with pytest.warns(RuntimeWarning):
        loaded = _reload_settings(monkeypatch, host="0.0.0.0", unsafe="true")

    assert loaded.MINERU_ALLOW_UNAUTHENTICATED_PUBLIC_API is True

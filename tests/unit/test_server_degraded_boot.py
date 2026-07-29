"""Regression guards: the API server must boot degraded, not crash, when no
active discovery v4 workspace exists (fresh runtime-zero install before
``init_discovery_workspace.py`` has run) — and must FAIL CLOSED with a
typed corrupt state when the discovery runtime is damaged.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import src.discovery.runtime_context as runtime_context
from src.catalog_folders.reader import create_safe_catalog_reader
from src.writer.job_manager import JobManager


pytestmark = pytest.mark.unit


@pytest.fixture
def no_active_generation(monkeypatch):
    """Force discovery runtime resolution to the fresh-install state."""

    def _raise(*args, **kwargs):
        raise runtime_context.DiscoveryRuntimeNotInitialized(
            "no active discovery v4 workspace: test fixture"
        )

    monkeypatch.setattr(
        runtime_context, "resolve_active_runtime", _raise
    )


@pytest.fixture
def corrupt_active_generation(monkeypatch):
    """Force discovery runtime resolution to a damaged-state error."""

    def _raise(*args, **kwargs):
        raise runtime_context.DiscoveryRuntimeCorrupt(
            "active generation pointer is not strict V4: test fixture"
        )

    monkeypatch.setattr(
        runtime_context, "resolve_active_runtime", _raise
    )


def test_create_safe_catalog_reader_degrades_without_active_generation(
    no_active_generation,
):
    # Fresh install: the reader stays fully usable; notebook taxonomy is
    # simply not injected.
    reader = create_safe_catalog_reader()
    status = reader.status()
    assert "writer_category_safe" in status
    assert "discovery_runtime" not in status
    assert "discovery_unavailable_reason" not in status


def test_create_safe_catalog_reader_fails_closed_on_corrupt_runtime(
    corrupt_active_generation,
):
    # Damaged discovery state is production corruption, not a fresh
    # install: the reader must not silently degrade.
    with pytest.raises(runtime_context.DiscoveryRuntimeCorrupt):
        create_safe_catalog_reader()


def test_create_safe_catalog_reader_healthy_with_active_generation(
    monkeypatch, tmp_path,
):
    notebook_root = tmp_path / "keyword_notebooks"
    notebook_root.mkdir()
    monkeypatch.setattr(
        runtime_context,
        "resolve_active_runtime",
        lambda **_: runtime_context.DiscoveryRuntimeContext(
            workspace=None,
            notebook_root=notebook_root,
            page_journal_root=tmp_path,
            reports_root=tmp_path,
            locks_root=tmp_path,
        ),
    )
    reader = create_safe_catalog_reader()
    assert "writer_category_safe" in reader.status()


def _make_client(monkeypatch, tmp_path):
    import src.server as server

    monkeypatch.setattr(server, "catalog", None)
    monkeypatch.setattr(server, "library", None)
    monkeypatch.setattr(server, "prompt_builder", None)
    monkeypatch.setattr(
        server, "job_manager", JobManager(write_dir=tmp_path / "write" / "jobs")
    )
    # Isolated empty catalog root so browsing does not touch real runtime data.
    catalog_root = tmp_path / "catalog"
    (catalog_root / "all").mkdir(parents=True)
    import config.settings as settings

    monkeypatch.setattr(settings, "CATALOG_FOLDER_ROOT", catalog_root)
    return TestClient(server.app)


def test_server_serves_unrelated_apis_without_active_generation(
    no_active_generation, monkeypatch, tmp_path,
):
    client = _make_client(monkeypatch, tmp_path)

    response = client.get("/status")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "running"
    assert body["discovery"] == "uninitialized"
    assert body["discovery_reason"]

    response = client.get("/status/discovery")
    assert response.status_code == 503
    assert response.json()["detail"]["state"] == "uninitialized"

    response = client.post("/write/jobs", json={"topic": "degraded boot"})
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    assert (tmp_path / "write" / "jobs" / job_id).is_dir()

    response = client.get("/catalog/all")
    assert response.status_code == 200
    assert response.json() == {"papers": []}


def test_server_reports_typed_corrupt_discovery(
    corrupt_active_generation, monkeypatch, tmp_path,
):
    client = _make_client(monkeypatch, tmp_path)

    response = client.get("/status")
    assert response.status_code == 200
    assert response.json()["discovery"] == "corrupt"

    response = client.get("/status/discovery")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["state"] == "corrupt"
    assert detail["reason"]

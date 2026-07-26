from __future__ import annotations

import json

from src.workspace.evidence import inspect_workspace_evidence
from src.workspace.readiness import IngestProfile, evaluate_metadata_staged
from tests.factories.paper_raw_factory import (
    create_manual_pdf_workspace,
    create_network_metadata_workspace,
)


def _readiness(folder):
    return evaluate_metadata_staged(inspect_workspace_evidence(folder))


def test_receipt_does_not_guess_profile_when_workflow_is_missing(tmp_path):
    folder = create_network_metadata_workspace(tmp_path)
    manifest = folder / "stage_manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload.pop("workflow_path", None)
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = _readiness(folder)

    assert result.profile is None
    assert not result.ready
    assert "unknown_profile" in result.missing


def test_pdf_does_not_guess_profile_when_workflow_is_missing(tmp_path):
    folder = create_manual_pdf_workspace(tmp_path)
    manifest = folder / "stage_manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload.pop("workflow_path", None)
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = _readiness(folder)

    assert result.profile is None
    assert not result.ready


def test_unknown_workflow_is_repair_required(tmp_path):
    folder = create_network_metadata_workspace(tmp_path)
    manifest = folder / "stage_manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["workflow_path"] = "mystery_ingest"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = _readiness(folder)

    assert not result.ready
    assert any(issue.category == "unknown_workflow_path" for issue in result.issues)


def test_manual_pdf_profile_allows_no_discovery_receipt(tmp_path):
    result = _readiness(create_manual_pdf_workspace(tmp_path))

    assert result.profile is IngestProfile.MANUAL_PDF
    assert result.ready
    assert "discovery_receipt" not in result.missing


def test_network_metadata_profile_requires_discovery_receipt(tmp_path):
    folder = create_network_metadata_workspace(tmp_path)
    next(folder.glob("*.discovery_receipt.json")).unlink()

    result = _readiness(folder)

    assert result.profile is IngestProfile.NETWORK_METADATA
    assert not result.ready
    assert "discovery_receipt" in result.missing

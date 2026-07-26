from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta

import pytest

import scripts.smoke_mineru_conversion as smoke
from src.mineru import smoke as mineru_smoke


PN = "0000000000000208"


def _ready_runtime():
    return {
        "verdict": "READY_FOR_CONVERSION",
        "runtime": {"require_gpu": True},
        "torch_cuda": {"cuda_available": True},
        "service_identity": {"verdict": "managed_ready"},
        "warnings": [],
    }


def _patch_runtime_env(monkeypatch):
    """Patch runtime_config_from_env so validate_smoke_report's config matches."""
    monkeypatch.setattr(smoke, "runtime_config_from_env", lambda: type("Cfg", (), {
        "api_url": "http://127.0.0.1:8000",
        "runner": type("Runner", (), {"value": "cli_api_proxy"})(),
    })())
    monkeypatch.setattr(mineru_smoke, "runtime_config_from_env", lambda: type("Cfg", (), {
        "api_url": "http://127.0.0.1:8000",
        "runner": type("Runner", (), {"value": "cli_api_proxy"})(),
    })())


def test_smoke_readiness_only_does_not_run_convert(monkeypatch, tmp_path, capsys):
    """Without --apply, smoke must not call _run_convert and produces a
    readiness-only report that cannot unlock formal batch conversion."""
    monkeypatch.delenv("MINERU_RUNNER", raising=False)
    monkeypatch.delenv("MINERU_API_URL", raising=False)
    monkeypatch.delenv("MINERU_ALLOW_CPU", raising=False)
    _patch_runtime_env(monkeypatch)
    paper_raw = tmp_path / "paper_raw"
    folder = paper_raw / PN
    folder.mkdir(parents=True)
    (folder / f"{PN}.md").write_text("ok", encoding="utf-8")
    (folder / f"{PN}.conversion.json").write_text(json.dumps({
        "status": "converted",
        "runner": "cli_api_proxy",
        "backend": "hybrid-engine",
    }), encoding="utf-8")

    monkeypatch.setattr(smoke, "verify_gpu_runtime", lambda api_url: _ready_runtime())
    monkeypatch.setattr(smoke, "snapshot_mineru_api", lambda api_url: {
        "api_available": True,
        "status": "healthy",
        "completed_tasks": 1,
        "failed_tasks": 0,
    })

    def fake_run(argv):
        raise AssertionError("_run_convert must not be called in readiness-only mode")

    class FakeConverter:
        def __init__(self, paper_raw_dir):
            pass

        def inspect_conversion(self, paper_number):
            return {"state": "converted_current", "reason": "ok"}

    monkeypatch.setattr(smoke, "_run_convert", fake_run)
    monkeypatch.setattr(smoke, "PaperRawConverter", FakeConverter)

    report_path = tmp_path / "smoke.json"
    saved = sys.argv
    sys.argv = [
        "smoke_mineru_conversion.py",
        "--paper-number", PN,
        "--paper-raw-dir", str(paper_raw),
        "--report", str(report_path),
    ]
    try:
        rc = smoke.main()
    finally:
        sys.argv = saved

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["mode"] == "readiness_only"
    assert payload["applied"] is False
    assert payload["side_effects"] is False
    assert payload["verdict"] == "SMOKE_READINESS_READY"

    # Readiness-only reports must NOT unlock batch conversion.
    validation = mineru_smoke.validate_smoke_report(report_path)
    assert validation["ok"] is False
    assert any("--apply" in e or "live apply" in e for e in validation["errors"])


def test_smoke_conversion_passes_skip_smoke_check(monkeypatch, tmp_path, capsys):
    """With --apply, smoke calls _run_convert with --apply --skip-smoke-check
    and produces a live_apply report that can unlock formal batch conversion."""
    monkeypatch.delenv("MINERU_RUNNER", raising=False)
    monkeypatch.delenv("MINERU_API_URL", raising=False)
    monkeypatch.delenv("MINERU_ALLOW_CPU", raising=False)
    _patch_runtime_env(monkeypatch)
    paper_raw = tmp_path / "paper_raw"
    folder = paper_raw / PN
    folder.mkdir(parents=True)
    (folder / f"{PN}.md").write_text("ok", encoding="utf-8")
    (folder / f"{PN}.conversion.json").write_text(json.dumps({
        "status": "converted",
        "runner": "cli_api_proxy",
        "backend": "hybrid-engine",
    }), encoding="utf-8")
    captured = []

    monkeypatch.setattr(smoke, "verify_gpu_runtime", lambda api_url: _ready_runtime())
    monkeypatch.setattr(smoke, "snapshot_mineru_api", lambda api_url: {
        "api_available": True,
        "status": "healthy",
        "completed_tasks": 2,
        "failed_tasks": 0,
    })

    def fake_run(argv):
        captured.append(argv)
        return 0, {"items": [{"paper_number": PN, "status": "converted"}]}

    class FakeConverter:
        def __init__(self, paper_raw_dir):
            pass

        def inspect_conversion(self, paper_number):
            return {"state": "converted_current", "reason": "ok"}

    monkeypatch.setattr(smoke, "_run_convert", fake_run)
    monkeypatch.setattr(smoke, "PaperRawConverter", FakeConverter)

    report_path = tmp_path / "smoke.json"
    saved = sys.argv
    sys.argv = [
        "smoke_mineru_conversion.py",
        "--paper-number", PN,
        "--paper-raw-dir", str(paper_raw),
        "--apply",
        "--report", str(report_path),
    ]
    try:
        rc = smoke.main()
    finally:
        sys.argv = saved

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert "--skip-smoke-check" in captured[0]
    assert "--apply" in captured[0]
    assert payload["runner"] == "cli_api_proxy"
    assert payload["mode"] == "live_apply"
    assert payload["applied"] is True
    assert payload["side_effects"] is True
    assert payload["verdict"] in {"SMOKE_CONVERTED", "SMOKE_NO_GPU_ACTIVITY_BUT_CONVERTED_TEXT_ONLY"}

    # Live-apply reports must unlock formal batch conversion.
    validation = mineru_smoke.validate_smoke_report(report_path)
    assert validation["ok"] is True


def test_text_only_smoke_fails_without_ready_runtime(monkeypatch, tmp_path, capsys):
    """When runtime is not ready, smoke fails even with --apply and must not
    call _run_convert."""
    _patch_runtime_env(monkeypatch)
    paper_raw = tmp_path / "paper_raw"
    folder = paper_raw / PN
    folder.mkdir(parents=True)
    (folder / f"{PN}.md").write_text("ok", encoding="utf-8")
    (folder / f"{PN}.conversion.json").write_text(json.dumps({
        "status": "converted",
        "runner": "cli_api_proxy",
    }), encoding="utf-8")

    runtime = _ready_runtime()
    runtime["verdict"] = "CUDA_NOT_AVAILABLE"
    monkeypatch.setattr(smoke, "verify_gpu_runtime", lambda api_url: runtime)
    monkeypatch.setattr(smoke, "snapshot_mineru_api", lambda api_url: {
        "api_available": True,
        "status": "healthy",
        "completed_tasks": 0,
        "failed_tasks": 0,
    })

    class FakeConverter:
        def __init__(self, paper_raw_dir):
            pass

        def inspect_conversion(self, paper_number):
            return {"state": "converted_current", "reason": "ok"}

    def fake_run(argv):
        raise AssertionError("_run_convert must not be called when runtime not ready")

    monkeypatch.setattr(smoke, "_run_convert", fake_run)
    monkeypatch.setattr(smoke, "PaperRawConverter", FakeConverter)

    saved = sys.argv
    sys.argv = [
        "smoke_mineru_conversion.py",
        "--paper-number", PN,
        "--paper-raw-dir", str(paper_raw),
        "--apply",
        "--report", str(tmp_path / "smoke.json"),
    ]
    try:
        rc = smoke.main()
    finally:
        sys.argv = saved

    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["verdict"] == "SMOKE_FAILED"
    # Even though --apply was passed, runtime failure means no side effects.
    assert payload["mode"] == "live_apply"
    assert payload["applied"] is True


def test_readiness_only_report_does_not_unlock_batch(monkeypatch, tmp_path):
    """A readiness-only report must not pass validate_smoke_report even if
    runtime/service are ready."""
    _patch_runtime_env(monkeypatch)
    report_path = tmp_path / "smoke_readiness.json"
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "readiness_only",
        "applied": False,
        "side_effects": False,
        "paper_number": PN,
        "api_url": "http://127.0.0.1:8000",
        "runner": "cli_api_proxy",
        "runtime_verdict": "READY_FOR_CONVERSION",
        "service_identity": {"verdict": "managed_ready"},
        "verdict": "SMOKE_READINESS_READY",
    }
    report_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    validation = mineru_smoke.validate_smoke_report(report_path)
    assert validation["ok"] is False
    assert any("--apply" in e or "live apply" in e for e in validation["errors"])


def test_legacy_smoke_report_without_mode_fails(monkeypatch, tmp_path):
    """Legacy reports lacking mode/applied/side_effects must fail validation."""
    _patch_runtime_env(monkeypatch)
    report_path = tmp_path / "smoke_legacy.json"
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "verdict": "SMOKE_CONVERTED",
        "api_url": "http://127.0.0.1:8000",
        "runner": "cli_api_proxy",
        "runtime_verdict": "READY_FOR_CONVERSION",
        "service_identity": {"verdict": "managed_ready"},
    }
    report_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    validation = mineru_smoke.validate_smoke_report(report_path)
    assert validation["ok"] is False
    assert any("--apply" in e or "live apply" in e for e in validation["errors"])


def test_live_apply_skipped_already_current_unlocks_batch(monkeypatch, tmp_path):
    """A live_apply report with SMOKE_SKIPPED_ALREADY_CURRENT must pass
    validation (it was produced with --apply even though conversion was
    skipped)."""
    _patch_runtime_env(monkeypatch)
    report_path = tmp_path / "smoke_skipped.json"
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "live_apply",
        "applied": True,
        "side_effects": True,
        "paper_number": PN,
        "api_url": "http://127.0.0.1:8000",
        "runner": "cli_api_proxy",
        "runtime_verdict": "READY_FOR_CONVERSION",
        "service_identity": {"verdict": "managed_ready"},
        "verdict": "SMOKE_SKIPPED_ALREADY_CURRENT",
    }
    report_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    validation = mineru_smoke.validate_smoke_report(report_path)
    assert validation["ok"] is True


def test_smoke_required_message_includes_apply():
    """smoke_required_message must tell the user to use --apply."""
    msg = mineru_smoke.smoke_required_message()
    assert "--apply" in msg
    assert "cannot unlock formal batch conversion" in msg or "readiness-only" in msg

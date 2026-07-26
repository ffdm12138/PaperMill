"""Tests for the staged conversion-then-resolve SOP script."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sys
from pathlib import Path

import pytest

import scripts.run_paper_raw_gpu_conversion_then_resolve as sop


pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]
PN = "0000000000000208"


def _install_phase_mocks(monkeypatch, *, convert_argv=None, resolve_argv=None,
                         convert_report=None, resolve_report=None):
    """Replace the phase mains with fakes that print JSON and record argv."""
    captured = {"convert": [], "resolve": []}

    def fake_convert_main():
        captured["convert"].append(list(sys.argv))
        if convert_report is not None:
            print(json.dumps(convert_report))
        return 0

    def fake_resolve_main():
        captured["resolve"].append(list(sys.argv))
        if resolve_report is not None:
            print(json.dumps(resolve_report))
        return 0

    import scripts.convert_paper_raw_gpu as gpu
    import scripts.resolve_paper_raw_metadata as resolve_cli
    monkeypatch.setattr(gpu, "main", fake_convert_main)
    monkeypatch.setattr(resolve_cli, "main", fake_resolve_main)
    monkeypatch.setattr(sop, "_ensure_api", lambda start=True: {"ok": True, "started": start})
    return captured


def _install_api_tracker(monkeypatch, *, convert_report=None, resolve_report=None):
    """Track whether start_services vs snapshot was used for the API phase."""
    calls = {"start_services": 0, "check_api_only": 0}
    captured = {"convert": [], "resolve": []}

    def fake_convert_main():
        captured["convert"].append(list(sys.argv))
        if convert_report is not None:
            print(json.dumps(convert_report))
        return 0

    def fake_resolve_main():
        captured["resolve"].append(list(sys.argv))
        if resolve_report is not None:
            print(json.dumps(resolve_report))
        return 0

    import scripts.convert_paper_raw_gpu as gpu
    import scripts.resolve_paper_raw_metadata as resolve_cli
    monkeypatch.setattr(gpu, "main", fake_convert_main)
    monkeypatch.setattr(resolve_cli, "main", fake_resolve_main)

    def fake_ensure(start=True):
        if start:
            calls["start_services"] += 1
            return {"ok": True, "started": True}
        calls["check_api_only"] += 1
        return {"ok": False, "started": False, "api_health": {"api_available": False}}

    monkeypatch.setattr(sop, "_ensure_api", fake_ensure)
    return captured, calls


def _run_sop(argv: list[str]) -> tuple[int, dict]:
    saved = sys.argv
    sys.argv = argv
    import io
    import contextlib
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = sop.main()
    finally:
        sys.argv = saved
    return rc, json.loads(buf.getvalue())


def _valid_smoke(path: Path) -> None:
    path.write_text(json.dumps({
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "live_apply",
        "applied": True,
        "side_effects": True,
        "verdict": "SMOKE_CONVERTED",
        "api_url": "http://127.0.0.1:8000",
        "runner": "cli_api_proxy",
        "runtime_verdict": "READY_FOR_CONVERSION",
        "service_identity": {"verdict": "managed_ready"},
    }), encoding="utf-8")


def test_dry_run_routes_flags_and_aggregates(monkeypatch, tmp_path):
    convert_report = {
        "applied": False,
        "items": [
            {"paper_number": PN, "status": "planned", "metadata_ready_for_commit": False},
        ],
    }
    resolve_report = {
        "applied": False,
        "items": [
            {"paper_number": PN, "decision": "auto_matched", "applied_status": ""},
        ],
    }
    captured = _install_phase_mocks(
        monkeypatch, convert_report=convert_report, resolve_report=resolve_report,
    )

    rc, result = _run_sop([
        "run.py", "--paper-number", PN,
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--dry-run",
    ])

    assert rc == 0
    conv_argv = captured["convert"][0]
    assert "--dry-run" in conv_argv
    assert "--apply" not in conv_argv
    assert "--prefer-markdown" in captured["resolve"][0]
    assert result["applied"] is False
    assert result["summary"]["conversion"]["planned"] == 1
    assert result["summary"]["still_blocked_for_formalize"] == [PN]


def test_apply_forwards_apply(monkeypatch, tmp_path):
    convert_report = {"applied": True, "items": [
        {"paper_number": PN, "status": "converted", "metadata_ready_for_commit": False},
    ]}
    resolve_report = {"applied": True, "items": [
        {"paper_number": PN, "decision": "matched", "applied_status": "matched"},
    ]}
    captured = _install_phase_mocks(
        monkeypatch, convert_report=convert_report, resolve_report=resolve_report,
    )

    rc, result = _run_sop([
        "run.py", "--paper-number", PN,
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--apply",
    ])

    assert rc == 0
    assert result["applied"] is True
    assert "--apply" in captured["convert"][0]
    assert "--apply" in captured["resolve"][0]
    assert result["summary"]["conversion"]["converted"] == 1
    assert result["summary"]["metadata_resolution_after_convert"]["matched"] == 1
    assert result["summary"]["still_blocked_for_formalize"] == []


def test_convert_only_skips_resolve(monkeypatch, tmp_path):
    captured = _install_phase_mocks(monkeypatch, convert_report={"items": []})
    rc, result = _run_sop([
        "run.py", "--paper-number", PN,
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--convert-only", "--dry-run",
    ])
    assert rc == 0
    assert captured["convert"] and not captured["resolve"]
    assert "metadata_resolution" not in result["phases"]


def test_resolve_only_skips_convert(monkeypatch, tmp_path):
    captured = _install_phase_mocks(monkeypatch, resolve_report={"items": []})
    rc, result = _run_sop([
        "run.py", "--paper-number", PN,
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--resolve-only", "--dry-run",
    ])
    assert rc == 0
    assert captured["resolve"] and not captured["convert"]
    assert "conversion" not in result["phases"]


def test_dry_run_does_not_start_api(monkeypatch, tmp_path):
    convert_report = {"applied": False, "items": [
        {"paper_number": PN, "status": "planned", "metadata_ready_for_commit": False},
    ]}
    captured, calls = _install_api_tracker(monkeypatch, convert_report=convert_report)

    rc, result = _run_sop([
        "run.py", "--paper-number", PN,
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--dry-run",
    ])

    assert rc == 0
    assert calls["start_services"] == 0
    assert calls["check_api_only"] == 1
    assert result["phases"]["conversion"]["rc"] == 0
    assert captured["convert"]


def test_apply_starts_api(monkeypatch, tmp_path):
    convert_report = {"applied": True, "items": [
        {"paper_number": PN, "status": "converted", "metadata_ready_for_commit": True},
    ]}
    captured, calls = _install_api_tracker(monkeypatch, convert_report=convert_report)

    rc, result = _run_sop([
        "run.py", "--paper-number", PN,
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--apply",
    ])

    assert rc == 0
    assert calls["start_services"] == 1
    assert calls["check_api_only"] == 0


def test_apply_aborts_when_api_unavailable(monkeypatch, tmp_path):
    def fake_ensure(start=True):
        if start:
            return {"ok": False, "started": True, "error": "would not start"}
        return {"ok": False, "started": False}

    import scripts.convert_paper_raw_gpu as gpu
    monkeypatch.setattr(gpu, "main", lambda: (_ for _ in ()).throw(AssertionError("convert must not run on abort")))
    monkeypatch.setattr(sop, "_ensure_api", fake_ensure)

    rc, result = _run_sop([
        "run.py", "--paper-number", PN,
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--apply",
    ])

    assert rc == 2
    assert result["ok"] is False


def test_all_apply_blocks_without_smoke(monkeypatch, tmp_path):
    import scripts.convert_paper_raw_gpu as gpu
    monkeypatch.setattr(gpu, "main", lambda: (_ for _ in ()).throw(AssertionError("convert must not run")))
    monkeypatch.setattr("src.mineru.smoke.DEFAULT_SMOKE_REPORT", tmp_path / "missing_smoke.json")

    rc, result = _run_sop([
        "run.py", "--all",
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--apply",
    ])

    assert rc == 2
    assert result["ok"] is False
    assert "smoke_check" in result


def test_all_apply_with_valid_smoke_forwards_report(monkeypatch, tmp_path):
    monkeypatch.delenv("MINERU_RUNNER", raising=False)
    monkeypatch.delenv("MINERU_API_URL", raising=False)
    monkeypatch.delenv("MINERU_ALLOW_CPU", raising=False)
    smoke = tmp_path / "smoke.json"
    _valid_smoke(smoke)
    convert_report = {"applied": True, "items": [
        {"paper_number": PN, "status": "converted", "metadata_ready_for_commit": True},
    ]}
    captured = _install_phase_mocks(monkeypatch, convert_report=convert_report, resolve_report={"items": []})

    rc, result = _run_sop([
        "run.py", "--all",
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--apply",
        "--smoke-report", str(smoke),
    ])

    assert rc == 0
    assert "--smoke-report" in captured["convert"][0]
    assert str(smoke) in captured["convert"][0]
    assert result["summary"]["conversion"]["converted"] == 1


def test_all_apply_with_valid_default_smoke_does_not_require_explicit_report(monkeypatch, tmp_path):
    monkeypatch.delenv("MINERU_RUNNER", raising=False)
    monkeypatch.delenv("MINERU_API_URL", raising=False)
    monkeypatch.delenv("MINERU_ALLOW_CPU", raising=False)
    smoke = tmp_path / "reports" / "smoke_mineru_conversion.json"
    smoke.parent.mkdir(parents=True)
    _valid_smoke(smoke)
    monkeypatch.setattr("src.mineru.smoke.DEFAULT_SMOKE_REPORT", smoke)
    convert_report = {"applied": True, "items": [
        {"paper_number": PN, "status": "converted", "metadata_ready_for_commit": True},
    ]}
    captured = _install_phase_mocks(monkeypatch, convert_report=convert_report, resolve_report={"items": []})

    rc, result = _run_sop([
        "run.py", "--all",
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--apply",
    ])

    assert rc == 0
    assert "--smoke-report" not in captured["convert"][0]
    assert result["summary"]["conversion"]["converted"] == 1

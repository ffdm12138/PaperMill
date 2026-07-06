from __future__ import annotations

import sys
from pathlib import Path

import pytest

import scripts.check_mineru_processes as proc_check
import scripts.convert_paper_raw_batch as batch
from src.converter import MinerUConverter


pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _health(ok=True, message="ok", *, api_available=None):
    return type("Health", (), {
        "ok": ok,
        "message": message,
        "nvidia_smi": ok,
        "api_available": api_available,
    })()


def _torch_health():
    return type("TorchHealth", (), {
        "ok": True,
        "message": "ok",
        "torch_version": "2.4.0+cu121",
        "torch_cuda_version": "12.1",
        "cuda_available": True,
        "device_count": 1,
        "device_name": "NVIDIA Test",
    })()


def test_start_fast_api_mode_checks_health_before_start_and_sets_cuda():
    text = (_REPO_ROOT / "start_fast_api_mode.bat").read_text(encoding="utf-8")

    assert "scripts\\start_mineru_services.py --wait" in text
    assert "--api-url http://127.0.0.1:8000" in text
    assert "set CUDA_VISIBLE_DEVICES=0" in text
    assert "set MINERU_RUNNER=cli_api_proxy" in text
    assert "set MINERU_API_URL=http://127.0.0.1:8000" in text


def test_runtime_failure_reports_unavailable_cli_api_proxy(monkeypatch):
    monkeypatch.setenv("MINERU_RUNNER", "cli_api_proxy")
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "true")
    monkeypatch.setattr("src.mineru_runtime.preflight_gpu", lambda: _health())
    monkeypatch.setattr("src.mineru_runtime.preflight_torch_cuda", lambda: _torch_health())
    monkeypatch.setattr(
        "src.mineru_runtime.preflight_mineru_api",
        lambda api_url: _health(False, "connection refused", api_available=False),
    )
    from src.mineru_runtime import runtime_config_from_env

    runtime = batch._runtime_snapshot(runtime_config_from_env())

    assert "mineru-api unavailable" in batch._runtime_failure(runtime)


def test_batch_api_unavailable_does_not_call_converter(monkeypatch, tmp_path):
    paper_raw = tmp_path / "paper_raw"
    folder = paper_raw / "0000000000000001"
    folder.mkdir(parents=True)
    monkeypatch.setenv("MINERU_RUNNER", "cli_api_proxy")
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "true")
    monkeypatch.setattr("src.mineru_runtime.preflight_gpu", lambda: _health())
    monkeypatch.setattr("src.mineru_runtime.preflight_torch_cuda", lambda: _torch_health())
    monkeypatch.setattr(
        "src.mineru_runtime.preflight_mineru_api",
        lambda api_url: _health(False, "connection refused", api_available=False),
    )

    class FakeConverter:
        def __init__(self, paper_raw_dir):
            self.paper_raw_dir = paper_raw_dir

        def inspect_conversion(self, *args, **kwargs):
            return {
                "state": "not_converted",
                "reason": "not converted",
                "markdown": "",
                "images_dir": "",
                "pdf_sha256": "",
                "manifest": None,
            }

        def convert(self, *args, **kwargs):
            raise AssertionError("convert should not be called when api preflight fails")

    monkeypatch.setattr(batch, "PaperRawConverter", FakeConverter)
    saved = sys.argv
    sys.argv = ["convert_paper_raw_batch.py", "--paper-raw-dir", str(paper_raw), "--all", "--apply"]
    try:
        rc = batch.main()
    finally:
        sys.argv = saved

    assert rc == 1


def test_mineru_converter_cli_api_proxy_checks_health_before_subprocess(monkeypatch, tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF")
    monkeypatch.setenv("MINERU_RUNNER", "cli_api_proxy")
    monkeypatch.setenv("MINERU_API_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(
        "src.converter.preflight_mineru_api",
        lambda api_url: _health(False, "connection refused", api_available=False),
    )
    monkeypatch.setattr(
        "src.converter.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("subprocess should not run")),
    )

    result = MinerUConverter(timeout=1, log_dir="").convert(pdf, tmp_path / "out")

    assert result["success"] is False
    assert result["runner"] == "cli_api_proxy"
    assert "mineru-api unavailable" in result["error"]


def test_check_mineru_processes_warns_on_multiple_api(monkeypatch, capsys):
    monkeypatch.setattr(proc_check.shutil, "which", lambda name: None)
    monkeypatch.setattr(proc_check, "_find_mineru_processes", lambda: [
        {"pid": "100", "name": "python.exe", "cmdline": "mineru-api --port 8000", "kind": "mineru-api"},
        {"pid": "101", "name": "python.exe", "cmdline": "mineru-api --port 8000", "kind": "mineru-api"},
    ])
    monkeypatch.setattr(
        "scripts.check_mineru_processes.preflight_gpu",
        lambda: _health(False, "no gpu"),
    )
    monkeypatch.setattr("scripts.check_mineru_processes.preflight_torch_cuda", lambda: _torch_health())
    monkeypatch.setattr(
        "scripts.check_mineru_processes.snapshot_mineru_api",
        lambda api_url=None: {"api_available": False, "error": "unreachable"},
    )
    monkeypatch.setattr("scripts.check_mineru_processes.runtime_config_from_env", lambda: type("Cfg", (), {
        "runner": type("Runner", (), {"value": "cli_api_proxy"})(),
        "require_gpu": True,
        "cuda_path": "",
        "cuda_visible_devices": "0",
        "api_url": "http://127.0.0.1:8000",
    })())
    monkeypatch.setattr("scripts.check_mineru_processes.verify_gpu_runtime", lambda api_url=None: {
        "verdict": "API_HEALTHY_BUT_FAILED_TASKS",
        "service_identity": {"verdict": "managed_ready", "pid_file": "pid", "pid": 100,
                             "identity": {"cmdline": "mineru-api --port 8000"}},
        "mineru_api_health": {"api_available": True, "status": "healthy",
                              "completed_tasks": 0, "failed_tasks": 10,
                              "queued_tasks": 2, "processing_tasks": 0,
                              "version": "3.4.0"},
        "gpu_processes": {"available": False, "processes": [], "error": "nvidia-smi not available"},
        "warnings": ["conversion tasks have failed"],
    })

    saved = sys.argv
    sys.argv = ["check_mineru_processes.py"]
    try:
        rc = proc_check.main()
    finally:
        sys.argv = saved
    out = capsys.readouterr().out

    assert rc == 0
    assert "multiple mineru-api processes detected" in out


def test_snapshot_mineru_api_parses_failed_tasks(monkeypatch):
    """snapshot_mineru_api parses /health JSON and the warning fires when the
    API is healthy but only failed tasks exist."""
    from src import mineru_runtime as rt

    class _FakeResp:
        status_code = 200
        content = b'{"status":"healthy","version":"3.4.0","queued_tasks":0,"processing_tasks":0,"completed_tasks":0,"failed_tasks":10}'

        def json(self):
            import json as _json
            return _json.loads(self.content)

    import src.mineru_runtime as _rt
    monkeypatch.setattr(_rt, "runtime_config_from_env", lambda: type("Cfg", (), {
        "runner": type("R", (), {"value": "cli_api_proxy"})(),
        "api_url": "http://127.0.0.1:8000",
    }))

    fake_requests = type("Req", (), {"get": lambda *a, **k: _FakeResp()})()
    monkeypatch.setitem(__import__("sys").modules, "requests", fake_requests)

    snap = rt.snapshot_mineru_api("http://127.0.0.1:8000")
    assert snap["api_available"] is True
    assert snap["status"] == "healthy"
    assert snap["failed_tasks"] == 10
    assert snap["completed_tasks"] == 0
    assert "conversion tasks have failed" in rt.mineru_api_failed_task_warning(snap)


def test_check_mineru_processes_json_snapshot(monkeypatch, capsys):
    monkeypatch.setattr(proc_check.shutil, "which", lambda name: None)
    monkeypatch.setattr(proc_check, "_find_mineru_processes", lambda: [])
    monkeypatch.setattr("scripts.check_mineru_processes.preflight_gpu", lambda: _health(False, "no gpu"))
    monkeypatch.setattr("scripts.check_mineru_processes.preflight_torch_cuda", lambda: _torch_health())
    monkeypatch.setattr("scripts.check_mineru_processes.snapshot_nvidia_smi", lambda: {"available": False})
    monkeypatch.setattr(
        "scripts.check_mineru_processes.snapshot_mineru_api",
        lambda api_url=None: {"api_available": True, "status": "healthy",
                              "completed_tasks": 0, "failed_tasks": 10,
                              "queued_tasks": 0, "processing_tasks": 0},
    )
    monkeypatch.setattr("scripts.check_mineru_processes.describe_runtime", lambda cfg: {"runner": "cli_api_proxy"})
    monkeypatch.setattr("scripts.check_mineru_processes.runtime_config_from_env", lambda: type("Cfg", (), {
        "runner": type("Runner", (), {"value": "cli_api_proxy"})(),
        "require_gpu": True,
        "cuda_path": "",
        "cuda_visible_devices": "0",
        "api_url": "http://127.0.0.1:8000",
    })())
    monkeypatch.setattr("scripts.check_mineru_processes.verify_gpu_runtime", lambda api_url=None: {
        "verdict": "API_HEALTHY_BUT_FAILED_TASKS",
        "service_identity": {"verdict": "managed_ready", "pid_file": "pid", "pid": 100,
                             "identity": {"cmdline": "mineru-api --port 8000"}},
        "mineru_api_health": {"api_available": True, "status": "healthy",
                              "completed_tasks": 0, "failed_tasks": 10,
                              "queued_tasks": 2, "processing_tasks": 0,
                              "version": "3.4.0"},
        "gpu_processes": {"available": False, "processes": [], "error": "nvidia-smi not available"},
        "warnings": ["conversion tasks have failed"],
    })

    saved = sys.argv
    sys.argv = ["check_mineru_processes.py", "--json"]
    try:
        rc = proc_check.main()
    finally:
        sys.argv = saved
    out = capsys.readouterr().out

    assert rc == 0
    import json
    payload = json.loads(out)
    assert "gpu" in payload
    assert "mineru_processes" in payload
    assert "runtime" in payload
    assert "lock" in payload
    assert payload["mineru_api_health"]["api_available"] is True
    assert payload["mineru_api_health"]["failed_tasks"] == 10
    assert "conversion tasks have failed" in payload["mineru_api_warning"]


def test_check_mineru_processes_human_shows_api_task_counts(monkeypatch, capsys):
    monkeypatch.setattr(proc_check.shutil, "which", lambda name: None)
    monkeypatch.setattr(proc_check, "_find_mineru_processes", lambda: [])
    monkeypatch.setattr("scripts.check_mineru_processes.preflight_gpu", lambda: _health(False, "no gpu"))
    monkeypatch.setattr("scripts.check_mineru_processes.preflight_torch_cuda", lambda: _torch_health())
    monkeypatch.setattr("scripts.check_mineru_processes.snapshot_nvidia_smi", lambda: {"available": False})
    monkeypatch.setattr(
        "scripts.check_mineru_processes.snapshot_mineru_api",
        lambda api_url=None: {"api_available": True, "status": "healthy",
                              "completed_tasks": 0, "failed_tasks": 10,
                              "queued_tasks": 2, "processing_tasks": 0,
                              "version": "3.4.0"},
    )
    monkeypatch.setattr("scripts.check_mineru_processes.describe_runtime", lambda cfg: {"runner": "cli_api_proxy"})
    monkeypatch.setattr("scripts.check_mineru_processes.runtime_config_from_env", lambda: type("Cfg", (), {
        "runner": type("Runner", (), {"value": "cli_api_proxy"})(),
        "require_gpu": True,
        "cuda_path": "",
        "cuda_visible_devices": "0",
        "api_url": "http://127.0.0.1:8000",
    })())
    monkeypatch.setattr("scripts.check_mineru_processes.verify_gpu_runtime", lambda api_url=None: {
        "verdict": "API_HEALTHY_BUT_FAILED_TASKS",
        "service_identity": {"verdict": "managed_ready", "pid_file": "pid", "pid": 100,
                             "identity": {"cmdline": "mineru-api --port 8000"}},
        "mineru_api_health": {"api_available": True, "status": "healthy",
                              "completed_tasks": 0, "failed_tasks": 10,
                              "queued_tasks": 2, "processing_tasks": 0,
                              "version": "3.4.0"},
        "gpu_processes": {"available": False, "processes": [], "error": "nvidia-smi not available"},
        "warnings": ["conversion tasks have failed"],
    })

    saved = sys.argv
    sys.argv = ["check_mineru_processes.py"]
    try:
        rc = proc_check.main()
    finally:
        sys.argv = saved
    out = capsys.readouterr().out

    assert rc == 0
    assert "[4] mineru-api health" in out
    assert "[6] verdict" in out
    assert "failed_tasks: 10" in out
    assert "completed_tasks: 0" in out
    assert "conversion tasks have failed" in out

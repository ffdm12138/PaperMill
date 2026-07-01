from __future__ import annotations

import json
import sys

import scripts.start_mineru_services as start_cli
import src.mineru_service_manager as sm


def _patch_paths(monkeypatch, tmp_path):
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(sm, "LOG_DIR", log_dir)
    monkeypatch.setattr(sm, "MINERU_API_PID_FILE", log_dir / "mineru_api.pid")
    monkeypatch.setattr(sm, "MINERU_API_LOG_FILE", log_dir / "mineru_api.log")
    monkeypatch.setattr(sm, "WEB_PID_FILE", log_dir / "mineru_web.pid")
    monkeypatch.setattr(sm, "WEB_LOG_FILE", log_dir / "mineru_web.log")
    return log_dir


def test_start_reuses_healthy_service_without_popen(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(sm, "health_ok", lambda api_url: True)
    monkeypatch.setattr(sm.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Popen called")))

    result = sm.start_services(wait=True)

    assert result["ok"] is True
    assert result["action"] == "reused"
    assert result["health"] == "ok"


def test_start_fails_when_port_occupied_but_health_failed(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(sm, "health_ok", lambda api_url: False)
    monkeypatch.setattr(sm, "port_is_open", lambda host, port: True)
    monkeypatch.setattr(sm.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Popen called")))

    result = sm.start_services(port=8000, api_url="http://127.0.0.1:8000")

    assert result["ok"] is False
    assert result["action"] == "failed"
    assert "occupied" in result["message"]


def test_start_builds_command_env_pid_and_log(monkeypatch, tmp_path):
    log_dir = _patch_paths(monkeypatch, tmp_path)
    captured = {}

    class FakePopen:
        pid = 12345

        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs["env"]
            captured["stdout"] = kwargs["stdout"]

    monkeypatch.setattr(sm, "health_ok", lambda api_url: False)
    monkeypatch.setattr(sm, "port_is_open", lambda host, port: False)
    monkeypatch.setattr(sm.subprocess, "Popen", FakePopen)

    result = sm.start_services(
        port=8000,
        api_url="http://127.0.0.1:8000",
        cuda_visible_devices="0",
        cuda_path=r"C:\CUDA\v12.6",
        wait=False,
    )
    captured["stdout"].close()

    assert result["action"] == "started"
    assert captured["cmd"] == ["mineru-api", "--port", "8000", "--enable-vlm-preload", "true"]
    assert captured["env"]["MINERU_RUNNER"] == "cli_api_proxy"
    assert captured["env"]["MINERU_REQUIRE_GPU"] == "true"
    assert captured["env"]["MINERU_API_URL"] == "http://127.0.0.1:8000"
    assert captured["env"]["CUDA_VISIBLE_DEVICES"] == "0"
    assert (log_dir / "mineru_api.pid").read_text(encoding="utf-8") == "12345"
    assert (log_dir / "mineru_api.log").exists()


def test_stop_only_stops_mineru_api_pid(monkeypatch, tmp_path):
    log_dir = _patch_paths(monkeypatch, tmp_path)
    (log_dir).mkdir(parents=True)
    (log_dir / "mineru_api.pid").write_text("100", encoding="utf-8")
    stopped = []
    monkeypatch.setattr(sm, "process_cmdline", lambda pid: "python.exe -m src.server")
    monkeypatch.setattr(sm, "_terminate_pid", lambda pid, force=False: stopped.append(pid) or True)
    monkeypatch.setattr(sm, "find_processes_containing", lambda token: [])

    result = sm.stop_services()

    assert stopped == []
    assert result["action"] == "not_running"
    assert "not mineru-api" in result["message"]


def test_stop_all_mineru_api_does_not_stop_other_python(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    stopped = []
    monkeypatch.setattr(sm, "read_pid", lambda pid_file=sm.MINERU_API_PID_FILE: None)
    monkeypatch.setattr(sm, "find_processes_containing", lambda token: [
        {"pid": "200", "cmdline": "python -m src.server"},
        {"pid": "201", "cmdline": "mineru-api --port 8000"},
    ] if token == "mineru-api" else [])
    monkeypatch.setattr(sm, "_terminate_pid", lambda pid, force=False: stopped.append(pid) or True)

    result = sm.stop_services(all_mineru_api=True)

    assert stopped == [201]
    assert result["action"] == "stopped"


def test_start_cli_json_output(monkeypatch, capsys):
    monkeypatch.setattr(start_cli, "start_services", lambda **kwargs: {
        "ok": True,
        "action": "reused",
        "mineru_api_url": "http://127.0.0.1:8000",
        "health": "ok",
        "message": "ok",
    })
    saved = sys.argv
    sys.argv = ["start_mineru_services.py", "--json"]
    try:
        rc = start_cli.main()
    finally:
        sys.argv = saved

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "reused"

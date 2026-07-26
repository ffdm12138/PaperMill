from __future__ import annotations

import json
import sys

import pytest

import scripts.start_mineru_services as start_cli
import src.mineru.service_manager as sm


def _patch_paths(monkeypatch, tmp_path):
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(sm, "LOG_DIR", log_dir)
    monkeypatch.setattr(sm, "MINERU_API_PID_FILE", log_dir / "mineru_api.pid")
    monkeypatch.setattr(sm, "MINERU_API_LOG_FILE", log_dir / "mineru_api.log")
    monkeypatch.setattr(sm, "WEB_PID_FILE", log_dir / "mineru_web.pid")
    monkeypatch.setattr(sm, "WEB_LOG_FILE", log_dir / "mineru_web.log")
    return log_dir


def test_start_reuses_managed_healthy_service_without_popen(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(sm, "classify_mineru_api_service", lambda **kwargs: {
        "verdict": "managed_ready",
        "healthy": True,
        "pid": 123,
        "identity": {"cmdline": "mineru-api --port 8000", "is_mineru_api": True},
        "warnings": [],
    })
    monkeypatch.setattr(sm.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Popen called")))

    result = sm.start_services(wait=True)

    assert result["ok"] is True
    assert result["action"] == "reused"
    assert result["health"] == "ok"
    assert "check_mineru_processes.py" in result["next_command"]
    assert "smoke_mineru_conversion.py" in result["next_command"]
    assert "--apply" in result["next_command"]
    assert "run_paper_raw_gpu_conversion_then_resolve.py" in result["next_command"]


def test_start_refuses_healthy_unmanaged_without_restart(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(sm, "classify_mineru_api_service", lambda **kwargs: {
        "verdict": "healthy_but_unmanaged",
        "healthy": True,
        "pid": None,
        "identity": {},
        "warnings": ["healthy API has no pid file"],
    })
    monkeypatch.setattr(sm.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Popen called")))

    result = sm.start_services(wait=True)

    assert result["ok"] is False
    assert result["action"] == "failed"
    assert "no safe PID" in result["message"]


def test_start_restart_if_stale_only_stops_pid_file_mineru_api(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    calls = {"classify": 0, "stopped": []}

    def fake_classify(**kwargs):
        calls["classify"] += 1
        if calls["classify"] == 1:
            return {
                "verdict": "healthy_but_unmanaged",
                "healthy": True,
                "pid": 321,
                "identity": {"cmdline": "mineru-api --port 8000", "is_mineru_api": True},
                "warnings": ["exe mismatch"],
            }
        return {"verdict": "not_running", "healthy": False, "pid": None, "identity": {}, "warnings": []}

    class FakePopen:
        pid = 654

        def __init__(self, cmd, **kwargs):
            kwargs["stdout"].close()

    monkeypatch.setattr(sm, "classify_mineru_api_service", fake_classify)
    monkeypatch.setattr(sm, "_terminate_pid", lambda pid, force=False, wait_seconds=8.0: calls["stopped"].append(pid) or True)
    monkeypatch.setattr(sm, "port_is_open", lambda host, port: False)
    monkeypatch.setattr(sm, "health_ok", lambda api_url: True)
    monkeypatch.setattr(sm.subprocess, "Popen", FakePopen)

    result = sm.start_services(wait=True, restart_if_stale=True)

    assert calls["stopped"] == [321]
    assert result["action"] == "restarted"


def test_start_fails_when_port_occupied_but_health_failed(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(sm, "health_ok", lambda api_url: False)
    monkeypatch.setattr(sm, "port_is_open", lambda host, port: True)
    monkeypatch.setattr(sm.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Popen called")))

    result = sm.start_services(port=8000, api_url="http://127.0.0.1:8000")

    assert result["ok"] is False
    assert result["action"] == "failed"
    assert "occupied" in result["message"]


def test_start_rejects_api_url_port_mismatch(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(sm.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Popen called")))

    result = sm.start_services(port=8000, api_url="http://127.0.0.1:8001")

    assert result["ok"] is False
    assert result["action"] == "failed"
    assert "does not match --port" in result["message"]


def test_start_builds_command_env_pid_and_log(monkeypatch, tmp_path):
    log_dir = _patch_paths(monkeypatch, tmp_path)
    captured = {}
    # Simulate the real failure mode: mineru-api is NOT on PATH (which()=None),
    # but the env's Scripts/ dir holds mineru-api.exe. find_mineru_api_exe()
    # must fall back to that absolute path.
    fake_scripts = tmp_path / "fakeenv" / "Scripts"
    fake_scripts.mkdir(parents=True)
    fake_exe = fake_scripts / "mineru-api.exe"
    fake_exe.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(sm.shutil, "which", lambda name: None)
    monkeypatch.setattr(sm.sys, "executable", str(tmp_path / "fakeenv" / "python.exe"))

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

    expected_exe = str(fake_exe)
    assert result["action"] == "started"
    assert result["mineru_api_exe"] == expected_exe
    assert captured["cmd"][0] == expected_exe
    assert captured["cmd"][1:] == ["--port", "8000", "--enable-vlm-preload", "true"]
    assert captured["env"]["MINERU_RUNNER"] == "cli_api_proxy"
    assert captured["env"]["MINERU_REQUIRE_GPU"] == "true"
    assert captured["env"]["MINERU_API_URL"] == "http://127.0.0.1:8000"
    assert captured["env"]["CUDA_VISIBLE_DEVICES"] == "0"
    # Scripts dir must be on PATH so mineru-api resolves inside the subprocess.
    assert str(fake_scripts) in captured["env"]["PATH"]
    assert (log_dir / "mineru_api.pid").read_text(encoding="utf-8") == "12345"
    assert (log_dir / "mineru_api.log").exists()


def test_wait_true_fails_when_api_never_ready(monkeypatch, tmp_path):
    """With wait=True, a never-ready API must return ok=False (not ok=True)."""
    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(sm, "health_ok", lambda api_url: False)
    monkeypatch.setattr(sm, "port_is_open", lambda host, port: False)

    class FakePopen:
        pid = 555

        def __init__(self, cmd, **kwargs):
            kwargs["stdout"].close()

    monkeypatch.setattr(sm.subprocess, "Popen", FakePopen)

    result = sm.start_services(wait=True, wait_seconds=0.0)

    assert result["action"] == "started"
    assert result["health"] == "not_ready"
    assert result["ok"] is False
    assert "did not become ready" in result["message"]


def test_wait_false_allows_not_ready_ok(monkeypatch, tmp_path):
    """Without --wait, a freshly-started (not yet ready) API is acceptable: ok=True."""
    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(sm, "health_ok", lambda api_url: False)
    monkeypatch.setattr(sm, "port_is_open", lambda host, port: False)

    class FakePopen:
        pid = 556

        def __init__(self, cmd, **kwargs):
            kwargs["stdout"].close()

    monkeypatch.setattr(sm.subprocess, "Popen", FakePopen)

    result = sm.start_services(wait=False)

    assert result["health"] == "not_ready"
    assert result["ok"] is True
    assert "not ready yet" in result["message"]


def test_find_mineru_api_exe_falls_back_to_scripts_dir(monkeypatch, tmp_path):
    fake_scripts = tmp_path / "fakeenv" / "Scripts"
    fake_scripts.mkdir(parents=True)
    fake_exe = fake_scripts / "mineru-api.exe"
    fake_exe.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(sm.shutil, "which", lambda name: None)
    monkeypatch.setattr(sm.sys, "executable", str(tmp_path / "fakeenv" / "python.exe"))

    assert sm.find_mineru_api_exe() == str(fake_exe)


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


# --- Phase 3: service state machine tests ---


def test_wait_reclassify_success_after_start(monkeypatch, tmp_path):
    """After wait reports health ok, reclassify must confirm managed_ready."""
    _patch_paths(monkeypatch, tmp_path)
    calls = {"classify": 0}

    def fake_classify(**kwargs):
        calls["classify"] += 1
        if calls["classify"] == 1:
            return {"verdict": "not_running", "healthy": False, "pid": None, "identity": {}, "warnings": []}
        return {"verdict": "managed_ready", "healthy": True, "pid": 654, "identity": {"is_mineru_api": True}, "warnings": []}

    class FakePopen:
        pid = 654

        def __init__(self, cmd, **kwargs):
            kwargs["stdout"].close()

    monkeypatch.setattr(sm, "classify_mineru_api_service", fake_classify)
    monkeypatch.setattr(sm, "health_ok", lambda api_url: True)
    monkeypatch.setattr(sm, "port_is_open", lambda host, port: False)
    monkeypatch.setattr(sm.subprocess, "Popen", FakePopen)

    result = sm.start_services(wait=True, restart_if_stale=False)

    assert result["ok"] is True
    assert result["health"] == "ok"
    assert result["service_verdict_before"] == "not_running"
    assert result["service_verdict_after"] == "managed_ready"


def test_wait_health_ok_but_identity_not_managed_fails(monkeypatch, tmp_path):
    """If health ok but after-classify is not managed_ready, ok must be False."""
    _patch_paths(monkeypatch, tmp_path)
    calls = {"classify": 0}

    def fake_classify(**kwargs):
        calls["classify"] += 1
        if calls["classify"] == 1:
            return {"verdict": "not_running", "healthy": False, "pid": None, "identity": {}, "warnings": []}
        return {"verdict": "healthy_but_unmanaged", "healthy": True, "pid": None, "identity": {}, "warnings": []}

    class FakePopen:
        pid = 654

        def __init__(self, cmd, **kwargs):
            kwargs["stdout"].close()

    monkeypatch.setattr(sm, "classify_mineru_api_service", fake_classify)
    monkeypatch.setattr(sm, "health_ok", lambda api_url: True)
    monkeypatch.setattr(sm, "port_is_open", lambda host, port: False)
    monkeypatch.setattr(sm.subprocess, "Popen", FakePopen)

    result = sm.start_services(wait=True, restart_if_stale=False)

    assert result["ok"] is False
    assert "health is ok but service identity is not managed_ready" in result["message"]


def test_unmanaged_no_safe_pid_restart_if_stale_does_not_kill(monkeypatch, tmp_path):
    """healthy_but_unmanaged with no pid and --restart-if-stale must not kill or start."""
    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(sm, "classify_mineru_api_service", lambda **kwargs: {
        "verdict": "healthy_but_unmanaged",
        "healthy": True,
        "pid": None,
        "identity": {},
        "warnings": ["healthy API has no pid file"],
    })
    killed = []

    def fake_terminate(pid, **kwargs):
        killed.append(pid)
        return True

    monkeypatch.setattr(sm, "_terminate_pid", fake_terminate)
    monkeypatch.setattr(sm.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Popen called")))

    result = sm.start_services(wait=True, restart_if_stale=True)

    assert result["ok"] is False
    assert killed == []
    assert "no safe PID" in result["message"]


def test_stale_non_live_pid_deletes_pidfile_and_reclassifies(monkeypatch, tmp_path):
    """healthy_but_stale_pid with non-live pid: delete pid file, reclassify."""
    _patch_paths(monkeypatch, tmp_path)
    calls = {"classify": 0}

    def fake_classify(**kwargs):
        calls["classify"] += 1
        if calls["classify"] == 1:
            return {
                "verdict": "healthy_but_stale_pid",
                "healthy": True,
                "pid": 999,
                "identity": {"live": False, "is_mineru_api": True},
                "warnings": ["pid file points to non-live PID 999"],
            }
        elif calls["classify"] == 2:
            # After stale pid file removal: reclassify as not_running, so we can start
            return {"verdict": "not_running", "healthy": False, "pid": None, "identity": {}, "warnings": []}
        else:
            # After Popen + wait: classify must confirm managed_ready
            return {"verdict": "managed_ready", "healthy": True, "pid": 654, "identity": {"is_mineru_api": True}, "warnings": []}

    class FakePopen:
        pid = 654

        def __init__(self, cmd, **kwargs):
            kwargs["stdout"].close()

    monkeypatch.setattr(sm, "classify_mineru_api_service", fake_classify)
    monkeypatch.setattr(sm, "health_ok", lambda api_url: True)
    monkeypatch.setattr(sm, "port_is_open", lambda host, port: False)
    monkeypatch.setattr(sm.subprocess, "Popen", FakePopen)

    result = sm.start_services(wait=True, restart_if_stale=False)

    # After removing stale pid and reclassifying as not_running, it should start
    assert calls["classify"] >= 2
    assert result["ok"] is True
    assert result["service_verdict_before"] == "not_running"


def test_port_occupied_health_failed_message_includes_next_steps(monkeypatch, tmp_path):
    """Port occupied but health failed must include actionable message."""
    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(sm, "classify_mineru_api_service", lambda **kwargs: {
        "verdict": "unhealthy",
        "healthy": False,
        "pid": None,
        "identity": {},
        "warnings": [],
    })
    monkeypatch.setattr(sm, "health_ok", lambda api_url: False)
    monkeypatch.setattr(sm, "port_is_open", lambda host, port: True)
    monkeypatch.setattr(sm.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Popen called")))

    result = sm.start_services(port=8000, api_url="http://127.0.0.1:8000")

    assert result["ok"] is False
    assert "/health is unavailable" in result["message"]
    assert "stop_mineru_services.py --all-mineru-api" in result["message"]


def test_restart_if_stale_safe_pid_action_is_restarted(monkeypatch, tmp_path):
    """--restart-if-stale with safe pid must result in action= restarted."""
    _patch_paths(monkeypatch, tmp_path)
    calls = {"classify": 0, "stopped": []}

    def fake_classify(**kwargs):
        calls["classify"] += 1
        if calls["classify"] == 1:
            return {
                "verdict": "healthy_but_unmanaged",
                "healthy": True,
                "pid": 321,
                "identity": {"cmdline": "mineru-api --port 8000", "is_mineru_api": True},
                "warnings": ["exe mismatch"],
            }
        return {"verdict": "managed_ready", "healthy": True, "pid": 654, "identity": {"is_mineru_api": True}, "warnings": []}

    class FakePopen:
        pid = 654

        def __init__(self, cmd, **kwargs):
            kwargs["stdout"].close()

    monkeypatch.setattr(sm, "classify_mineru_api_service", fake_classify)
    monkeypatch.setattr(sm, "_terminate_pid", lambda pid, force=False, wait_seconds=8.0: calls["stopped"].append(pid) or True)
    monkeypatch.setattr(sm, "port_is_open", lambda host, port: False)
    monkeypatch.setattr(sm, "health_ok", lambda api_url: True)
    monkeypatch.setattr(sm.subprocess, "Popen", FakePopen)

    result = sm.start_services(wait=True, restart_if_stale=True)

    assert calls["stopped"] == [321]
    assert result["action"] == "restarted"
    assert result["service_verdict_before"] == "healthy_but_unmanaged"
    assert result["service_verdict_after"] == "managed_ready"


# ── Process-probe backends ────────────────────────────────────────────


class _Completed:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def test_iter_processes_falls_back_to_pwsh_cim_when_wmic_is_missing(monkeypatch):
    """Windows 11 24H2+ ships without wmic; the CIM lane must take over."""
    monkeypatch.setattr(sm.os, "name", "nt")
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        if cmd[0] == "wmic":
            raise FileNotFoundError("wmic")
        return _Completed("mineru-api --port 8000\tmineru-api.exe\t4242\n")

    monkeypatch.setattr(sm.subprocess, "run", fake_run)
    procs = sm.iter_processes()

    assert [cmd[0] for cmd in seen] == ["wmic", "pwsh"]
    assert procs == [{"pid": "4242", "name": "mineru-api.exe",
                      "cmdline": "mineru-api --port 8000"}]


def test_iter_processes_raises_when_every_backend_fails(monkeypatch):
    """A broken probe must never look like 'no processes are running'."""
    monkeypatch.setattr(sm.os, "name", "nt")
    monkeypatch.setattr(sm.subprocess, "run",
                        lambda cmd, **kwargs: (_ for _ in ()).throw(FileNotFoundError(cmd[0])))

    with pytest.raises(sm.ProcessProbeError):
        sm.iter_processes()


def test_iter_processes_treats_an_empty_table_as_probe_failure(monkeypatch):
    """This process is always in the table, so zero rows means the probe lied."""
    monkeypatch.setattr(sm.os, "name", "nt")
    monkeypatch.setattr(sm.subprocess, "run", lambda cmd, **kwargs: _Completed("\n"))

    with pytest.raises(sm.ProcessProbeError):
        sm.iter_processes()


def test_probe_failure_keeps_the_pid_file_and_refuses_to_start(monkeypatch, tmp_path):
    """Unverifiable PID: refuse, never delete the pid file as if it were stale."""
    _patch_paths(monkeypatch, tmp_path)
    sm.MINERU_API_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    sm.write_pid(4242, sm.MINERU_API_PID_FILE)
    monkeypatch.setattr(sm, "health_ok", lambda api_url, timeout=2.0: True)
    monkeypatch.setattr(sm, "port_is_open", lambda host, port, timeout=1.0: True)
    monkeypatch.setattr(sm, "iter_processes",
                        lambda: (_ for _ in ()).throw(sm.ProcessProbeError("no backend")))
    monkeypatch.setattr(sm.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Popen called")))

    service = sm.classify_mineru_api_service(
        pid_file=sm.MINERU_API_PID_FILE, api_url="http://127.0.0.1:8000",
        expected_exe="mineru-api", expected_port=8000)
    assert service["verdict"] == "probe_unavailable"

    result = sm.start_services(wait=False)
    assert result["ok"] is False
    assert sm.MINERU_API_PID_FILE.exists(), "pid file must survive a probe failure"


def test_stop_services_refuses_to_kill_an_unverifiable_pid(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    sm.MINERU_API_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    sm.write_pid(4242, sm.MINERU_API_PID_FILE)
    monkeypatch.setattr(sm, "iter_processes",
                        lambda: (_ for _ in ()).throw(sm.ProcessProbeError("no backend")))
    monkeypatch.setattr(sm, "_terminate_pid",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not kill")))

    result = sm.stop_services()
    assert result["ok"] is False
    assert "cannot verify PID 4242" in result["message"]

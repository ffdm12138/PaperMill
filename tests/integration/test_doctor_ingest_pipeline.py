import json

import scripts.doctor_ingest_pipeline as doctor


def test_doctor_writes_report_and_skips_preflight_without_sources(tmp_path, monkeypatch):
    calls: list[str] = []

    def fake_run_step(name, cmd, *, cwd, env=None, timeout=None):
        calls.append(name)
        return {
            "name": name,
            "command": cmd,
            "returncode": 0,
            "blocking": False,
            "stdout": "{}",
            "stderr": "",
        }

    monkeypatch.setattr(doctor, "_run_step", fake_run_step)
    report_path = tmp_path / "reports" / "doctor.json"

    rc = doctor.main([
        "--project-root", str(tmp_path),
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--report-path", str(report_path),
    ])

    assert rc == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["valid"] is True
    assert calls == [
        "check_directory_hygiene",
        "validate_v2_library",
        "audit_metadata_quality",
        "audit_ingest_duplicates",
        "pytest_ingest_subset",
    ]
    preflight = next(step for step in report["steps"] if step["name"] == "preflight_paper_raw_import")
    assert preflight["skipped"] is True


def test_doctor_returns_nonzero_on_blocking_step(tmp_path, monkeypatch):
    paper_raw = tmp_path / "paper_raw" / "0000000000000001"
    paper_raw.mkdir(parents=True)

    def fake_run_step(name, cmd, *, cwd, env=None, timeout=None):
        return {
            "name": name,
            "command": cmd,
            "returncode": 1 if name == "preflight_paper_raw_import" else 0,
            "blocking": name == "preflight_paper_raw_import",
            "stdout": "",
            "stderr": "blocked",
        }

    monkeypatch.setattr(doctor, "_run_step", fake_run_step)
    report_path = tmp_path / "doctor.json"

    rc = doctor.main([
        "--project-root", str(tmp_path),
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--report-path", str(report_path),
        "--skip-tests",
    ])

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert rc == 1
    assert report["valid"] is False
    assert report["blocking_count"] == 1


def test_doctor_pytest_uses_isolated_env_and_timeout(tmp_path, monkeypatch):
    """pytest step must run with PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 and a timeout."""
    captured = {}

    class _FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        if "-m" in cmd and "pytest" in cmd:
            captured["env"] = kwargs.get("env")
            captured["timeout"] = kwargs.get("timeout")
        return _FakeProc()

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    report_path = tmp_path / "doctor.json"

    doctor.main([
        "--project-root", str(tmp_path),
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--report-path", str(report_path),
    ])

    assert captured.get("env", {}).get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1"
    assert captured.get("timeout") == 300


def test_doctor_pytest_timeout_is_blocking(tmp_path, monkeypatch):
    """A pytest TimeoutExpired must become a blocking step, not hang the doctor."""
    class _FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        if "-m" in cmd and "pytest" in cmd:
            raise doctor.subprocess.TimeoutExpired(cmd=cmd, timeout=300, output="", stderr="")
        return _FakeProc()

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    report_path = tmp_path / "doctor.json"

    rc = doctor.main([
        "--project-root", str(tmp_path),
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--report-path", str(report_path),
    ])

    report = json.loads(report_path.read_text(encoding="utf-8"))
    pytest_step = next(s for s in report["steps"] if s["name"] == "pytest_ingest_subset")
    assert pytest_step["blocking"] is True
    assert pytest_step["returncode"] == 124
    assert pytest_step["error"] == "timed out after 300s"
    assert report["valid"] is False
    assert rc == 1

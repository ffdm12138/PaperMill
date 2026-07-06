"""Tests for convert_paper_raw_batch formal runtime gates."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sys
from pathlib import Path

import pytest

import scripts.convert_paper_raw_batch as batch
import scripts.convert_paper_raw_gpu as wrapper


pytestmark = pytest.mark.unit

PN1 = "0000000000000001"
PN2 = "0000000000000002"

def _health(ok=True, message="ok", *, api_available=None):
    return type("Health", (), {
        "ok": ok,
        "message": message,
        "nvidia_smi": ok,
        "api_available": api_available,
    })()


def _torch_health(ok=True, message="ok"):
    return type("TorchHealth", (), {
        "ok": ok,
        "message": message,
        "torch_version": "2.4.0+cu121" if ok else "",
        "torch_cuda_version": "12.1" if ok else "",
        "cuda_available": ok,
        "device_count": 1 if ok else 0,
        "device_name": "NVIDIA Test" if ok else "",
    })()


def _paper_raw(root: Path, *source_ids: str) -> Path:
    paper_raw = root / "paper_raw"
    paper_raw.mkdir(parents=True)
    for sid in source_ids:
        (paper_raw / sid).mkdir(parents=True)
    return paper_raw


def _add_conversion_assets(paper_raw: Path, *source_ids: str) -> None:
    for sid in source_ids:
        folder = paper_raw / sid
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{sid}.pdf").write_bytes(b"%PDF-test")
        (folder / f"{sid}.metadata.json").write_text(
            json.dumps({"paper_number": sid, "paper_raw_id": sid, "metadata_match": {"status": "unmatched"}}),
            encoding="utf-8",
        )


def _patch_preflights(monkeypatch, *, api_available=True):
    monkeypatch.setattr("src.mineru_runtime.preflight_gpu", lambda: _health())
    monkeypatch.setattr("src.mineru_runtime.preflight_torch_cuda", lambda: _torch_health())
    monkeypatch.setattr(
        "src.mineru_runtime.preflight_mineru_api",
        lambda api_url: _health(api_available, "api ok" if api_available else "api down", api_available=api_available),
    )
    monkeypatch.setattr(
        "src.mineru_runtime.snapshot_mineru_api",
        lambda api_url=None, timeout=5.0: {"api_available": api_available, "status": "healthy" if api_available else ""},
    )


def _run_batch(monkeypatch, paper_raw: Path, *args: str) -> int:
    saved = sys.argv
    sys.argv = ["convert_paper_raw_batch.py", "--paper-raw-dir", str(paper_raw), *args]
    try:
        return batch.main()
    finally:
        sys.argv = saved


def _run_wrapper(monkeypatch, paper_raw: Path, *args: str) -> int:
    saved = sys.argv
    sys.argv = ["convert_paper_raw_gpu.py", "--paper-raw-dir", str(paper_raw), *args]
    try:
        return wrapper.main()
    finally:
        sys.argv = saved


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


def test_batch_cli_runner_warns_on_dry_run(monkeypatch, tmp_path, capsys):
    paper_raw = _paper_raw(tmp_path, PN1, PN2)
    _add_conversion_assets(paper_raw, PN1, PN2)
    monkeypatch.setenv("MINERU_RUNNER", "cli")
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "true")
    monkeypatch.delenv("MINERU_ALLOW_CPU", raising=False)
    _patch_preflights(monkeypatch)

    rc = _run_batch(monkeypatch, paper_raw, "--all", "--dry-run")
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert rc == 0
    assert "Direct convert_paper_raw_batch.py call detected" in combined
    assert "cli_api_proxy" in combined
    assert "MinerU runtime:" in combined
    assert "require_gpu: true" in combined
    assert "torch_cuda:" in combined


def test_batch_cli_api_proxy_no_cold_start_warning(monkeypatch, tmp_path, capsys):
    paper_raw = _paper_raw(tmp_path, PN1, PN2)
    _add_conversion_assets(paper_raw, PN1, PN2)
    monkeypatch.setenv("MINERU_RUNNER", "cli_api_proxy")
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "true")
    _patch_preflights(monkeypatch)

    rc = _run_batch(monkeypatch, paper_raw, "--all", "--dry-run")
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert rc == 0
    assert "cold-start" not in combined.lower()


def test_batch_allow_cpu_warns_and_summary_shows_debug_fallback(monkeypatch, tmp_path, capsys):
    paper_raw = _paper_raw(tmp_path, PN1)
    _add_conversion_assets(paper_raw, PN1)
    monkeypatch.setenv("MINERU_RUNNER", "cli_api_proxy")
    _patch_preflights(monkeypatch)

    rc = _run_batch(monkeypatch, paper_raw, "--all", "--dry-run", "--allow-cpu")
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert rc == 0
    assert "--allow-cpu enables debug-only CPU/no-GPU fallback" in combined
    assert "require_gpu: false" in combined
    assert "allow_cpu: true" in combined


def test_gpu_wrapper_hard_overrides_legacy_env(monkeypatch, tmp_path, capsys):
    paper_raw = _paper_raw(tmp_path, PN1)
    _add_conversion_assets(paper_raw, PN1)
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "false")
    monkeypatch.setenv("MINERU_ALLOW_CPU", "true")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "1")
    _patch_preflights(monkeypatch)

    rc = _run_wrapper(monkeypatch, paper_raw, "--all", "--dry-run")
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert rc == 0
    assert "Formal GPU MinerU conversion wrapper active." in combined
    assert "CUDA_VISIBLE_DEVICES=0" in combined
    assert "Direct convert_paper_raw_batch.py call detected" not in combined
    assert "require_gpu: true" in combined
    assert "allow_cpu: false" in combined
    assert "cuda_visible_devices: 0" in combined


def test_gpu_wrapper_rejects_allow_cpu(monkeypatch, tmp_path, capsys):
    paper_raw = _paper_raw(tmp_path)

    rc = _run_wrapper(monkeypatch, paper_raw, "--all", "--dry-run", "--allow-cpu")
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert rc == 2
    assert "--allow-cpu is debug-only" in combined


def test_direct_batch_gpu_false_apply_hard_fails(monkeypatch, tmp_path, capsys):
    paper_raw = _paper_raw(tmp_path, PN1)
    _add_conversion_assets(paper_raw, PN1)
    monkeypatch.setenv("MINERU_RUNNER", "cli_api_proxy")
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "false")
    monkeypatch.delenv("MINERU_ALLOW_CPU", raising=False)
    _patch_preflights(monkeypatch)

    rc = _run_batch(monkeypatch, paper_raw, "--all", "--apply", "--smoke-report", str(tmp_path / "missing_smoke.json"))
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert rc == 2
    assert "formal conversion requires GPU" in combined


def test_multi_source_cli_apply_hard_fails_without_escape_hatch(monkeypatch, tmp_path, capsys):
    paper_raw = _paper_raw(tmp_path, PN1, PN2)
    _add_conversion_assets(paper_raw, PN1, PN2)
    monkeypatch.setenv("MINERU_RUNNER", "cli")
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "true")
    _patch_preflights(monkeypatch)

    rc = _run_batch(monkeypatch, paper_raw, "--all", "--apply", "--smoke-report", str(tmp_path / "missing_smoke.json"))
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert rc == 2
    assert "cannot use MINERU_RUNNER=cli for multiple sources" in combined


def test_batch_report_includes_runtime_and_torch_cuda(monkeypatch, tmp_path):
    paper_raw = _paper_raw(tmp_path, PN1)
    _add_conversion_assets(paper_raw, PN1)
    report_path = tmp_path / "convert_report.json"
    monkeypatch.setenv("MINERU_RUNNER", "cli_api_proxy")
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "true")
    _patch_preflights(monkeypatch)

    rc = _run_wrapper(monkeypatch, paper_raw, "--all", "--dry-run", "--report", str(report_path))

    assert rc == 0
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["runtime"]["require_gpu"] is True
    assert payload["runtime"]["cuda_visible_devices"] == "0"
    assert "torch_cuda" in payload["runtime"]
    assert isinstance(payload, dict)
    assert "items" in payload
    assert "api_health_before" in payload
    assert payload["items"][0]["runtime"]["require_gpu"] is True
    assert "summary" in payload
    assert "stage" in payload["items"][0]
    assert "duration_seconds" in payload["items"][0]


def test_missing_metadata_shell_blocks_apply_before_converter(monkeypatch, tmp_path):
    paper_raw = _paper_raw(tmp_path, PN1)
    (paper_raw / PN1 / f"{PN1}.pdf").write_bytes(b"%PDF-test")
    report_path = tmp_path / "convert_report.json"
    convert_calls = []

    class FakeConverter:
        def __init__(self, paper_raw_dir):
            pass

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
            convert_calls.append(args)
            raise AssertionError("missing metadata shell must block before conversion")

    monkeypatch.setattr(batch, "PaperRawConverter", FakeConverter)

    rc = _run_batch(monkeypatch, paper_raw, "--all", "--apply", "--report", str(report_path))

    assert rc == 1
    assert convert_calls == []
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    item = payload["items"][0]
    assert item["status"] == "failed"
    assert item["has_pdf"] is True
    assert item["has_metadata_shell"] is False
    assert item["metadata_required_for_conversion"] is True
    assert "missing metadata.json shell" in item["reason"]


def test_missing_metadata_shell_dry_run_does_not_run_runtime_preflight(monkeypatch, tmp_path):
    paper_raw = _paper_raw(tmp_path, PN1)
    (paper_raw / PN1 / f"{PN1}.pdf").write_bytes(b"%PDF-test")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("runtime preflight should not run for asset-gated dry-run")

    monkeypatch.setattr("src.mineru_runtime.preflight_gpu", fail_if_called)
    monkeypatch.setattr("src.mineru_runtime.preflight_torch_cuda", fail_if_called)
    monkeypatch.setattr("src.mineru_runtime.preflight_mineru_api", fail_if_called)

    rc = _run_batch(monkeypatch, paper_raw, "--all", "--dry-run")

    assert rc == 0


def test_gpu_wrapper_all_apply_requires_smoke(monkeypatch, tmp_path, capsys):
    paper_raw = _paper_raw(tmp_path, PN1)
    _add_conversion_assets(paper_raw, PN1)
    monkeypatch.setenv("MINERU_RUNNER", "cli_api_proxy")
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "true")
    monkeypatch.setattr("src.mineru_smoke.DEFAULT_SMOKE_REPORT", tmp_path / "missing_smoke.json")

    rc = _run_wrapper(monkeypatch, paper_raw, "--all", "--apply")
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert rc == 2
    assert "Batch conversion requires a recent successful single-paper live smoke test" in combined


def test_gpu_wrapper_valid_smoke_allows_formal_batch(monkeypatch, tmp_path):
    paper_raw = _paper_raw(tmp_path, PN1)
    _add_conversion_assets(paper_raw, PN1)
    smoke = tmp_path / "smoke.json"
    _valid_smoke(smoke)
    monkeypatch.setenv("MINERU_RUNNER", "cli_api_proxy")
    monkeypatch.setenv("MINERU_API_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "true")

    class FakeConverter:
        def __init__(self, paper_raw_dir):
            pass

        def inspect_conversion(self, *args, **kwargs):
            return {
                "state": "converted_current",
                "reason": "conversion manifest is current",
                "markdown": "x.md",
                "images_dir": "images",
                "pdf_sha256": "",
                "manifest": {},
            }

    monkeypatch.setattr(batch, "PaperRawConverter", FakeConverter)

    rc = _run_wrapper(monkeypatch, paper_raw, "--all", "--apply", "--smoke-report", str(smoke))

    assert rc == 0


def test_gpu_wrapper_valid_default_smoke_allows_formal_batch(monkeypatch, tmp_path):
    paper_raw = _paper_raw(tmp_path, PN1)
    _add_conversion_assets(paper_raw, PN1)
    smoke = tmp_path / "reports" / "smoke_mineru_conversion.json"
    smoke.parent.mkdir(parents=True)
    _valid_smoke(smoke)
    monkeypatch.setattr("src.mineru_smoke.DEFAULT_SMOKE_REPORT", smoke)
    monkeypatch.setenv("MINERU_RUNNER", "cli_api_proxy")
    monkeypatch.setenv("MINERU_API_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "true")

    class FakeConverter:
        def __init__(self, paper_raw_dir):
            pass

        def inspect_conversion(self, *args, **kwargs):
            return {
                "state": "converted_current",
                "reason": "conversion manifest is current",
                "markdown": "x.md",
                "images_dir": "images",
                "pdf_sha256": "",
                "manifest": {},
            }

    monkeypatch.setattr(batch, "PaperRawConverter", FakeConverter)

    rc = _run_wrapper(monkeypatch, paper_raw, "--all", "--apply")

    assert rc == 0


def test_gpu_wrapper_single_paper_apply_not_blocked_by_missing_smoke(monkeypatch, tmp_path):
    paper_raw = _paper_raw(tmp_path, PN1)
    _add_conversion_assets(paper_raw, PN1)
    monkeypatch.setenv("MINERU_RUNNER", "cli_api_proxy")
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "true")
    called = []
    monkeypatch.setattr(batch, "main", lambda: called.append(list(sys.argv)) or 0)

    rc = _run_wrapper(monkeypatch, paper_raw, "--paper-number", PN1, "--apply")

    assert rc == 0
    assert called


def test_lower_level_batch_all_apply_warns_not_hard_fails_on_missing_smoke(monkeypatch, tmp_path, capsys):
    paper_raw = _paper_raw(tmp_path, PN1)
    _add_conversion_assets(paper_raw, PN1)
    monkeypatch.setenv("MINERU_RUNNER", "cli_api_proxy")
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "true")

    class FakeConverter:
        def __init__(self, paper_raw_dir):
            pass

        def inspect_conversion(self, *args, **kwargs):
            return {
                "state": "converted_current",
                "reason": "conversion manifest is current",
                "markdown": "x.md",
                "images_dir": "images",
                "pdf_sha256": "",
                "manifest": {},
            }

    monkeypatch.setattr(batch, "PaperRawConverter", FakeConverter)

    rc = _run_batch(monkeypatch, paper_raw, "--all", "--apply", "--smoke-report", str(tmp_path / "missing_smoke.json"))
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert rc == 0
    assert "without a valid smoke report" in combined


def test_batch_failed_item_reports_lock_owner(monkeypatch, tmp_path):
    paper_raw = _paper_raw(tmp_path, PN1)
    _add_conversion_assets(paper_raw, PN1)
    monkeypatch.setenv("MINERU_RUNNER", "cli_api_proxy")
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "true")
    _patch_preflights(monkeypatch)
    monkeypatch.setattr(batch, "_runtime_failure", lambda runtime: "")
    report_path = tmp_path / "convert_report.json"

    class FakeConverter:
        def __init__(self, paper_raw_dir):
            pass

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
            return {"success": False, "error": "MinerU lock busy: held by PID 46416"}

    monkeypatch.setattr(batch, "PaperRawConverter", FakeConverter)
    monkeypatch.setattr("src.mineru_lock.read_mineru_lock_status", lambda **kwargs: {
        "owner_pid": 46416,
        "paper_number": PN2,
        "age_seconds": 2935,
        "owner_live": True,
        "verdict": "LOCK_STUCK_SUSPECTED",
    })

    rc = _run_batch(
        monkeypatch,
        paper_raw,
        "--all",
        "--apply",
        "--report", str(report_path),
        "--lock-wait-timeout-seconds", "1",
        "--lock-stuck-warn-seconds", "1",
    )

    assert rc == 1
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    item = payload["items"][0]
    assert item["status"] == "failed"
    assert item["lock_owner_pid"] == 46416
    assert item["lock_owner_paper_number"] == PN2
    assert item["lock_owner_live"] is True
    assert item["lock_wait_warning"] == "LOCK_STUCK_SUSPECTED"

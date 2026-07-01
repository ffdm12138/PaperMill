"""Tests for convert_paper_raw_batch formal runtime gates."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import scripts.convert_paper_raw_batch as batch
import scripts.convert_paper_raw_gpu as wrapper


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


def _patch_preflights(monkeypatch, *, api_available=True):
    monkeypatch.setattr("src.mineru_runtime.preflight_gpu", lambda: _health())
    monkeypatch.setattr("src.mineru_runtime.preflight_torch_cuda", lambda: _torch_health())
    monkeypatch.setattr(
        "src.mineru_runtime.preflight_mineru_api",
        lambda api_url: _health(api_available, "api ok" if api_available else "api down", api_available=api_available),
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


def test_batch_cli_runner_warns_on_dry_run(monkeypatch, tmp_path, capsys):
    paper_raw = _paper_raw(tmp_path, "000001", "000002")
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
    paper_raw = _paper_raw(tmp_path, "000001", "000002")
    monkeypatch.setenv("MINERU_RUNNER", "cli_api_proxy")
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "true")
    _patch_preflights(monkeypatch)

    rc = _run_batch(monkeypatch, paper_raw, "--all", "--dry-run")
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert rc == 0
    assert "cold-start" not in combined.lower()


def test_batch_allow_cpu_warns_and_summary_shows_debug_fallback(monkeypatch, tmp_path, capsys):
    paper_raw = _paper_raw(tmp_path, "000001")
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
    paper_raw = _paper_raw(tmp_path, "000001")
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
    paper_raw = _paper_raw(tmp_path, "000001")
    monkeypatch.setenv("MINERU_RUNNER", "cli_api_proxy")
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "false")
    monkeypatch.delenv("MINERU_ALLOW_CPU", raising=False)
    _patch_preflights(monkeypatch)

    rc = _run_batch(monkeypatch, paper_raw, "--all", "--apply")
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert rc == 2
    assert "formal conversion requires GPU" in combined


def test_multi_source_cli_apply_hard_fails_without_escape_hatch(monkeypatch, tmp_path, capsys):
    paper_raw = _paper_raw(tmp_path, "000001", "000002")
    monkeypatch.setenv("MINERU_RUNNER", "cli")
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "true")
    _patch_preflights(monkeypatch)

    rc = _run_batch(monkeypatch, paper_raw, "--all", "--apply")
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert rc == 2
    assert "cannot use MINERU_RUNNER=cli for multiple sources" in combined


def test_batch_report_includes_runtime_and_torch_cuda(monkeypatch, tmp_path):
    paper_raw = _paper_raw(tmp_path, "000001")
    report_path = tmp_path / "convert_report.json"
    monkeypatch.setenv("MINERU_RUNNER", "cli_api_proxy")
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "true")
    _patch_preflights(monkeypatch)

    rc = _run_wrapper(monkeypatch, paper_raw, "--all", "--dry-run", "--report", str(report_path))

    assert rc == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report[0]["runtime"]["require_gpu"] is True
    assert report[0]["runtime"]["cuda_visible_devices"] == "0"
    assert "torch_cuda" in report[0]["runtime"]

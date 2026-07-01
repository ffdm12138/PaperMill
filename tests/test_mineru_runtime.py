import os
import json

import pytest

from src.mineru_runtime import (
    MinerURunner,
    MinerURuntimeConfig,
    _join_cuda_bin,
    build_mineru_env,
    describe_runtime,
    preflight_gpu,
    preflight_torch_cuda,
    runtime_config_from_env,
)


def test_join_cuda_bin_windows():
    assert _join_cuda_bin(r"C:\CUDA\v12.6") == r"C:\CUDA\v12.6\bin"
    assert _join_cuda_bin(r"C:\CUDA\v12.6\\") == r"C:\CUDA\v12.6\bin"


def test_join_cuda_bin_posix():
    assert _join_cuda_bin("/usr/local/cuda") == "/usr/local/cuda/bin"
    assert _join_cuda_bin("/usr/local/cuda/") == "/usr/local/cuda/bin"


def test_build_mineru_env_prepends_cuda_bin():
    config = MinerURuntimeConfig(cuda_path=r"C:\CUDA\v12.6")
    env = build_mineru_env(config, base_env={"PATH": r"C:\Windows"})

    assert env["CUDA_PATH"] == r"C:\CUDA\v12.6"
    assert env["PATH"].startswith(r"C:\CUDA\v12.6\bin")


def test_build_mineru_env_posix_cuda_bin():
    config = MinerURuntimeConfig(cuda_path="/usr/local/cuda")
    env = build_mineru_env(config, base_env={"PATH": "/usr/bin"})

    assert env["CUDA_PATH"] == "/usr/local/cuda"
    assert env["PATH"].startswith("/usr/local/cuda/bin")


def test_build_mineru_env_injects_cuda_visible_devices():
    config = MinerURuntimeConfig(cuda_visible_devices="0")
    env = build_mineru_env(config, base_env={"PATH": "/usr/bin"})

    assert env["CUDA_VISIBLE_DEVICES"] == "0"


def test_runtime_config_from_env(monkeypatch):
    monkeypatch.setenv("MINERU_RUNNER", "api")
    monkeypatch.setenv("MINERU_API_URL", "http://127.0.0.1:9000")
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "true")

    config = runtime_config_from_env()

    assert config.runner == MinerURunner.API
    assert config.api_url == "http://127.0.0.1:9000"
    assert config.require_gpu is True


def test_runtime_config_requires_gpu_by_default(monkeypatch):
    monkeypatch.delenv("MINERU_REQUIRE_GPU", raising=False)
    monkeypatch.delenv("MINERU_ALLOW_CPU", raising=False)
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)

    config = runtime_config_from_env()

    assert config.require_gpu is True
    assert config.allow_cpu is False
    assert config.cuda_visible_devices == "0"


def test_runtime_config_allow_cpu_escape_hatch(monkeypatch):
    monkeypatch.delenv("MINERU_REQUIRE_GPU", raising=False)
    monkeypatch.setenv("MINERU_ALLOW_CPU", "true")
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)

    config = runtime_config_from_env()

    assert config.require_gpu is False
    assert config.allow_cpu is True
    assert config.cuda_visible_devices == ""


def test_runtime_config_require_gpu_overrides_allow_cpu(monkeypatch):
    monkeypatch.setenv("MINERU_ALLOW_CPU", "true")
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "true")
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)

    config = runtime_config_from_env()

    assert config.require_gpu is True
    assert config.allow_cpu is True
    assert config.cuda_visible_devices == ""


def test_describe_runtime_serializes_runner():
    data = describe_runtime(MinerURuntimeConfig(runner=MinerURunner.CLI))

    assert data["runner"] == "cli"


def test_require_gpu_true_fails_when_nvidia_smi_missing(monkeypatch):
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "true")
    monkeypatch.setattr("src.mineru_runtime.shutil.which", lambda name: None)

    health = preflight_gpu()

    assert health.ok is False
    assert health.nvidia_smi is False
    assert "GPU is required for MinerU ingest conversion" in health.message
    assert "MINERU_ALLOW_CPU=true" in health.message


def test_allow_cpu_missing_nvidia_smi_is_debug_fallback(monkeypatch):
    monkeypatch.delenv("MINERU_REQUIRE_GPU", raising=False)
    monkeypatch.setenv("MINERU_ALLOW_CPU", "true")
    monkeypatch.setattr("src.mineru_runtime.shutil.which", lambda name: None)

    health = preflight_gpu()

    assert health.ok is True
    assert health.nvidia_smi is False
    assert "CPU/debug fallback active" in health.message


def test_require_gpu_true_fails_when_nvidia_smi_fails(monkeypatch):
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "true")
    monkeypatch.setattr("src.mineru_runtime.shutil.which", lambda name: "nvidia-smi")

    class Result:
        returncode = 1

    monkeypatch.setattr("src.mineru_runtime.subprocess.run", lambda *args, **kwargs: Result())

    health = preflight_gpu()

    assert health.ok is False
    assert health.nvidia_smi is False
    assert "GPU is required for MinerU ingest conversion" in health.message
    assert "nvidia-smi failed" in health.message


def test_torch_cuda_preflight_success(monkeypatch):
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "true")
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    captured = {}

    class Result:
        returncode = 0
        stdout = json.dumps({
            "ok": True,
            "torch_version": "2.4.0+cu121",
            "torch_cuda_version": "12.1",
            "cuda_available": True,
            "device_count": 1,
            "device_name": "NVIDIA Test",
        })
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env") or {}
        return Result()

    monkeypatch.setattr("src.mineru_runtime.subprocess.run", fake_run)

    health = preflight_torch_cuda()

    assert health.ok is True
    assert health.torch_cuda_version == "12.1"
    assert health.device_count == 1
    assert captured["cmd"][0] == os.sys.executable
    assert captured["env"]["CUDA_VISIBLE_DEVICES"] == "0"


def test_torch_cuda_preflight_fail_closed_when_cuda_unavailable(monkeypatch):
    monkeypatch.setenv("MINERU_REQUIRE_GPU", "true")

    class Result:
        returncode = 0
        stdout = json.dumps({
            "ok": True,
            "torch_version": "2.4.0",
            "torch_cuda_version": "",
            "cuda_available": False,
            "device_count": 0,
            "device_name": "",
        })
        stderr = ""

    monkeypatch.setattr("src.mineru_runtime.subprocess.run", lambda *args, **kwargs: Result())

    health = preflight_torch_cuda()

    assert health.ok is False
    assert "torch.version.cuda is empty" in health.message
    assert "torch.cuda.is_available() is false" in health.message
    assert health.cuda_available is False


def test_torch_cuda_preflight_debug_fallback_skips_probe(monkeypatch):
    monkeypatch.delenv("MINERU_REQUIRE_GPU", raising=False)
    monkeypatch.setenv("MINERU_ALLOW_CPU", "true")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("torch subprocess probe should be skipped")

    monkeypatch.setattr("src.mineru_runtime.subprocess.run", fail_if_called)

    health = preflight_torch_cuda()

    assert health.ok is True
    assert "CPU/debug fallback active" in health.message

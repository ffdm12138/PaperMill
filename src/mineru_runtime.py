"""MinerU runtime configuration and preflight helpers."""
from __future__ import annotations

import os
import json
import shutil
import subprocess
import sys
import urllib.parse
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

from src.path_utils import is_windows_abs_path


class MinerURunner(str, Enum):
    CLI = "cli"
    API = "api"                  # 未实现的 HTTP upload adapter（纯 API 模式）
    CLI_API_PROXY = "cli_api_proxy"  # CLI + --api-url → 常驻 mineru-api


@dataclass
class MinerURuntimeConfig:
    runner: MinerURunner = MinerURunner.CLI
    api_url: str = "http://127.0.0.1:8000"
    require_gpu: bool = True
    allow_cpu: bool = False
    cuda_path: str = ""
    cuda_visible_devices: str = ""
    backend: str = "hybrid-engine"
    effort: str = "medium"
    method: str = "auto"


@dataclass
class MinerURuntimeHealth:
    ok: bool
    runner: str
    message: str = ""
    nvidia_smi: bool | None = None
    cli_available: bool | None = None
    api_available: bool | None = None


@dataclass
class MinerUCudaHealth:
    ok: bool
    message: str
    torch_version: str = ""
    torch_cuda_version: str = ""
    cuda_available: bool | None = None
    device_count: int | None = None
    device_name: str = ""


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def activate_formal_gpu_env() -> None:
    """Activate the formal MinerU GPU conversion environment in-process.

    Formal entrypoints must validate smoke/runtime state against the same
    runner and API URL they will use for conversion.  Keep this helper early in
    those mains, before ``runtime_config_from_env()`` or smoke validation.
    """
    os.environ["MINERU_REQUIRE_GPU"] = "true"
    os.environ.pop("MINERU_ALLOW_CPU", None)
    os.environ["CUDA_VISIBLE_DEVICES"] = "0"
    os.environ["MINERU_BACKEND"] = "hybrid-engine"
    os.environ["MINERU_METHOD"] = "auto"
    os.environ["MINERU_EFFORT"] = "medium"
    os.environ["MINERU_RUNNER"] = "cli_api_proxy"
    os.environ.setdefault("MINERU_API_URL", "http://127.0.0.1:8000")


def runtime_config_from_env() -> MinerURuntimeConfig:
    runner = os.environ.get("MINERU_RUNNER", "cli").strip().lower() or "cli"
    valid = {r.value for r in MinerURunner}
    if runner not in valid:
        raise ValueError(f"invalid MINERU_RUNNER: {runner}. Valid: {sorted(valid)}")
    allow_cpu = _env_bool("MINERU_ALLOW_CPU", False)
    if allow_cpu:
        cuda_visible_devices = os.environ.get("CUDA_VISIBLE_DEVICES", "").strip()
    else:
        cuda_visible_devices = os.environ.get("CUDA_VISIBLE_DEVICES", "0").strip() or "0"
    return MinerURuntimeConfig(
        runner=MinerURunner(runner),
        api_url=os.environ.get("MINERU_API_URL", "http://127.0.0.1:8000").strip()
        or "http://127.0.0.1:8000",
        require_gpu=_env_bool("MINERU_REQUIRE_GPU", not allow_cpu),
        allow_cpu=allow_cpu,
        cuda_path=os.environ.get("CUDA_PATH", "").strip(),
        cuda_visible_devices=cuda_visible_devices,
        backend=os.environ.get("MINERU_BACKEND", "hybrid-engine").strip() or "hybrid-engine",
        effort=os.environ.get("MINERU_EFFORT", "medium").strip() or "medium",
        method=os.environ.get("MINERU_METHOD", "auto").strip() or "auto",
    )


def _join_cuda_bin(cuda_path: str) -> str:
    """Join CUDA bin directory preserving native path separator style.

    On Windows, ``os.path.join(r"C:\\CUDA\\v12.6", "bin")`` produces
    ``C:\\CUDA\\v12.6/bin`` (mixed slashes) when the test runs on a POSIX
    Python.  Conversely, ``os.path.join("/usr/local/cuda", "bin")`` on a
    Windows Python produces ``/usr/local/cuda\\bin``.

    This helper keeps Windows drive-letter paths with backslashes and
    POSIX paths with forward slashes, regardless of the host platform.
    """
    cuda_path = cuda_path.rstrip("\\/")
    if is_windows_abs_path(cuda_path):
        return cuda_path + "\\bin"
    # POSIX path or relative path — always use forward slash to avoid
    # backslash leakage on Windows Python.
    return cuda_path + "/bin"


def build_mineru_env(config: MinerURuntimeConfig | None = None, base_env: dict | None = None) -> dict:
    config = config or runtime_config_from_env()
    env = dict(base_env or os.environ)

    # 硬编码 CUDA_PATH 默认值，不依赖 shell 环境变量。
    # lmdeploy / mineru-api 启动时强制要求 CUDA_PATH 存在。
    _CUDA_FALLBACK = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.6"
    cuda = config.cuda_path or _CUDA_FALLBACK
    env["CUDA_PATH"] = cuda
    cuda_bin = _join_cuda_bin(cuda)
    path_parts = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
    if cuda_bin and cuda_bin not in path_parts:
        env["PATH"] = os.pathsep.join([cuda_bin] + path_parts)

    if config.cuda_visible_devices:
        env["CUDA_VISIBLE_DEVICES"] = config.cuda_visible_devices

    # 修复 SSL_CERT_FILE：如果环境变量指向不存在的文件，移除它，
    # 否则 httpx/ssl 会报 FileNotFoundError 导致 mineru CLI 启动失败。
    ssl_cert = env.get("SSL_CERT_FILE", "")
    if ssl_cert and not Path(ssl_cert).exists():
        env.pop("SSL_CERT_FILE", None)
    return env


def preflight_gpu(require_gpu: bool | None = None) -> MinerURuntimeHealth:
    config = runtime_config_from_env()
    required = config.require_gpu if require_gpu is None else require_gpu
    nvidia_smi = shutil.which("nvidia-smi") is not None
    if not nvidia_smi:
        if required:
            message = (
                "GPU is required for MinerU ingest conversion, but nvidia-smi was not found. "
                "Set up NVIDIA/CUDA, or explicitly set MINERU_ALLOW_CPU=true only for debugging."
            )
        else:
            message = (
                "CPU/debug fallback active: GPU check skipped because nvidia-smi was not found. "
                "This is not formal ingest SOP."
            )
        return MinerURuntimeHealth(
            ok=not required,
            runner=config.runner.value,
            message=message,
            nvidia_smi=False,
        )
    try:
        result = subprocess.run(
            ["nvidia-smi"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        ok = result.returncode == 0
    except Exception:
        ok = False
    if ok:
        message = "nvidia-smi ok"
    elif required:
        message = (
            "GPU is required for MinerU ingest conversion, but nvidia-smi failed. "
            "Set up NVIDIA/CUDA, or explicitly set MINERU_ALLOW_CPU=true only for debugging."
        )
    else:
        message = (
            "CPU/debug fallback active: GPU check skipped because nvidia-smi failed. "
            "This is not formal ingest SOP."
        )
    return MinerURuntimeHealth(
        ok=ok or not required,
        runner=config.runner.value,
        message=message,
        nvidia_smi=ok,
    )


def preflight_torch_cuda(require_gpu: bool | None = None) -> MinerUCudaHealth:
    """Verify CUDA availability from the current Python/Torch environment."""
    config = runtime_config_from_env()
    required = config.require_gpu if require_gpu is None else require_gpu
    if not required:
        return MinerUCudaHealth(
            ok=True,
            message="CPU/debug fallback active: torch CUDA check skipped or unavailable.",
        )

    probe = r"""
import json
try:
    import torch
    cuda_available = bool(torch.cuda.is_available())
    device_count = int(torch.cuda.device_count())
    device_name = ""
    if cuda_available and device_count > 0:
        device_name = str(torch.cuda.get_device_name(0))
    print(json.dumps({
        "ok": True,
        "torch_version": str(getattr(torch, "__version__", "") or ""),
        "torch_cuda_version": str(getattr(torch.version, "cuda", "") or ""),
        "cuda_available": cuda_available,
        "device_count": device_count,
        "device_name": device_name,
    }, ensure_ascii=False))
except Exception as exc:
    print(json.dumps({
        "ok": False,
        "error": str(exc),
        "torch_version": "",
        "torch_cuda_version": "",
        "cuda_available": None,
        "device_count": None,
        "device_name": "",
    }, ensure_ascii=False))
    raise SystemExit(2)
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=build_mineru_env(config),
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return MinerUCudaHealth(ok=False, message="torch CUDA preflight timed out")
    except Exception as exc:
        return MinerUCudaHealth(ok=False, message=f"torch CUDA preflight failed: {exc}")

    payload: dict
    try:
        payload = json.loads((result.stdout or "").strip().splitlines()[-1])
    except Exception:
        payload = {}

    torch_version = str(payload.get("torch_version") or "")
    torch_cuda_version = str(payload.get("torch_cuda_version") or "")
    cuda_available = payload.get("cuda_available")
    device_count_raw = payload.get("device_count")
    try:
        device_count = int(device_count_raw) if device_count_raw is not None else None
    except (TypeError, ValueError):
        device_count = None
    device_name = str(payload.get("device_name") or "")

    if result.returncode != 0:
        error = str(payload.get("error") or result.stderr or "torch CUDA probe failed").strip()
        return MinerUCudaHealth(
            ok=False,
            message=f"torch CUDA preflight failed: {error}",
            torch_version=torch_version,
            torch_cuda_version=torch_cuda_version,
            cuda_available=cuda_available if isinstance(cuda_available, bool) else None,
            device_count=device_count,
            device_name=device_name,
        )
    errors: list[str] = []
    if not torch_version:
        errors.append("torch version is empty")
    if not torch_cuda_version:
        errors.append("torch.version.cuda is empty")
    if cuda_available is not True:
        errors.append("torch.cuda.is_available() is false")
    if device_count is None or device_count < 1:
        errors.append("torch.cuda.device_count() < 1")
    if errors:
        return MinerUCudaHealth(
            ok=False,
            message="; ".join(errors),
            torch_version=torch_version,
            torch_cuda_version=torch_cuda_version,
            cuda_available=cuda_available if isinstance(cuda_available, bool) else None,
            device_count=device_count,
            device_name=device_name,
        )
    return MinerUCudaHealth(
        ok=True,
        message="torch CUDA ok",
        torch_version=torch_version,
        torch_cuda_version=torch_cuda_version,
        cuda_available=True,
        device_count=device_count,
        device_name=device_name,
    )


def preflight_mineru_cli(mineru_exe: str = "mineru") -> MinerURuntimeHealth:
    config = runtime_config_from_env()
    try:
        result = subprocess.run(
            [mineru_exe, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=build_mineru_env(config),
            timeout=10,
        )
        ok = result.returncode == 0
        msg = (result.stdout or result.stderr or "").strip()
    except Exception as exc:
        ok = False
        msg = str(exc)
    return MinerURuntimeHealth(ok=ok, runner=MinerURunner.CLI.value, message=msg, cli_available=ok)


def preflight_mineru_api(api_url: str | None = None, timeout: float = 5.0) -> MinerURuntimeHealth:
    config = runtime_config_from_env()
    url = (api_url or config.api_url).rstrip("/")
    try:
        import requests

        response = requests.get(f"{url}/health", timeout=timeout)
        if response.status_code >= 400:
            response = requests.get(url, timeout=timeout)
        ok = response.status_code < 400
        msg = f"HTTP {response.status_code}"
    except Exception as exc:
        ok = False
        msg = str(exc)
    required = config.require_gpu or config.runner == MinerURunner.API
    return MinerURuntimeHealth(
        ok=ok or not required,
        runner=MinerURunner.API.value,
        message=msg,
        api_available=ok,
    )


def snapshot_mineru_api(api_url: str | None = None, timeout: float = 5.0) -> dict:
    """Fetch a structured mineru-api health snapshot.

    Returns:
        dict with ``api_available`` plus (when available) ``status``, ``version``,
        ``queued_tasks``, ``processing_tasks``, ``completed_tasks``,
        ``failed_tasks``. On any failure returns ``{"api_available": False,
        "error": ...}``. A healthy API that has only ever failed tasks
        (``failed_tasks > 0 and completed_tasks == 0``) is surfaced so callers
        can warn before launching a batch conversion.
    """
    config = runtime_config_from_env()
    url = (api_url or config.api_url).rstrip("/")
    try:
        import requests

        response = requests.get(f"{url}/health", timeout=timeout)
        if response.status_code >= 400:
            return {"api_available": False, "error": f"HTTP {response.status_code}"}
        data = response.json() if response.content else {}
    except Exception as exc:
        return {"api_available": False, "error": str(exc)}
    if not isinstance(data, dict):
        return {"api_available": False, "error": "non-object /health body"}
    return {
        "api_available": True,
        "status": str(data.get("status") or ""),
        "version": str(data.get("version") or ""),
        "queued_tasks": int(data.get("queued_tasks") or 0),
        "processing_tasks": int(data.get("processing_tasks") or 0),
        "completed_tasks": int(data.get("completed_tasks") or 0),
        "failed_tasks": int(data.get("failed_tasks") or 0),
    }


def mineru_api_failed_task_warning(snapshot: dict | None) -> str:
    """Return a warning string when the API is healthy but tasks have only
    failed (no completions). Empty string otherwise."""
    if not snapshot or not snapshot.get("api_available"):
        return ""
    if snapshot.get("status") and snapshot.get("status") != "healthy":
        return ""
    failed = int(snapshot.get("failed_tasks") or 0)
    completed = int(snapshot.get("completed_tasks") or 0)
    if failed > 0 and completed == 0:
        return ("mineru-api is healthy but conversion tasks have failed; "
                "check per-task error/report before batch conversion.")
    return ""


def list_gpu_processes() -> dict:
    """列出 GPU 上的 compute 进程（pid, process_name, used_memory）。

    Returns:
        dict: {
            "available": bool,
            "processes": [{"pid": int, "process_name": str, "used_memory_mb": int}, ...],
            "error": str (仅失败时),
        }
    """
    import datetime as _dt
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return {"available": False, "processes": [], "error": "nvidia-smi not found"}
    try:
        result = subprocess.run(
            [nvidia_smi, "--query-compute-apps=pid,process_name,used_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15,
        )
        if result.returncode != 0:
            return {"available": False, "processes": [],
                    "error": f"nvidia-smi exit {result.returncode}"}
        procs = []
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            try:
                procs.append({
                    "pid": int(parts[0].strip()),
                    "process_name": parts[1].strip(),
                    "used_memory_mb": int(parts[2].strip()),
                })
            except (ValueError, IndexError):
                continue
        return {"available": True, "processes": procs}
    except Exception as exc:
        return {"available": False, "processes": [], "error": str(exc)}


def describe_runtime(config: MinerURuntimeConfig | None = None) -> dict:
    config = config or runtime_config_from_env()
    data = asdict(config)
    data["runner"] = config.runner.value
    return data


def verify_gpu_runtime(api_url: str = "http://127.0.0.1:8000") -> dict:
    """Return a formal MinerU GPU conversion readiness snapshot.

    ``/health`` is only liveness. A ready verdict also requires CUDA/torch and
    a managed mineru-api identity.
    """
    config = runtime_config_from_env()
    warnings: list[str] = []
    gpu_health = preflight_gpu()
    torch_health = preflight_torch_cuda()
    api_health = snapshot_mineru_api(api_url)
    gpu_processes = list_gpu_processes()
    try:
        from src.mineru_service_manager import (
            MINERU_API_PID_FILE,
            classify_mineru_api_service,
            find_mineru_api_exe,
        )

        parsed = urllib.parse.urlparse(api_url)
        expected_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        service = classify_mineru_api_service(
            pid_file=MINERU_API_PID_FILE,
            api_url=api_url,
            expected_exe=find_mineru_api_exe(),
            expected_port=int(expected_port),
        )
    except Exception as exc:
        service = {
            "verdict": "healthy_but_unmanaged" if api_health.get("api_available") else "not_running",
            "warnings": [str(exc)],
            "identity": {},
        }

    if gpu_health.ok is False or torch_health.ok is False:
        verdict = "CUDA_NOT_AVAILABLE"
    elif not api_health.get("api_available"):
        verdict = "NO_MINERU_API"
    elif service.get("verdict") != "managed_ready":
        verdict = "API_HEALTHY_BUT_UNMANAGED"
    elif int(api_health.get("failed_tasks") or 0) > 0 and int(api_health.get("completed_tasks") or 0) == 0:
        verdict = "API_HEALTHY_BUT_FAILED_TASKS"
    else:
        verdict = "READY_FOR_CONVERSION"

    api_warning = mineru_api_failed_task_warning(api_health)
    if api_warning:
        warnings.append(api_warning)
    warnings.extend(str(w) for w in service.get("warnings") or [])
    if not gpu_health.ok:
        warnings.append(gpu_health.message)
    if not torch_health.ok:
        warnings.append(torch_health.message)

    return {
        "verdict": verdict,
        "runtime": describe_runtime(config),
        "nvidia_smi_available": bool(getattr(gpu_health, "nvidia_smi", False)),
        "gpu_processes": gpu_processes,
        "torch_cuda_available": bool(torch_health.cuda_available),
        "torch_cuda_device_count": torch_health.device_count,
        "torch_cuda": {
            "ok": torch_health.ok,
            "message": torch_health.message,
            "torch_version": torch_health.torch_version,
            "torch_cuda_version": torch_health.torch_cuda_version,
            "cuda_available": torch_health.cuda_available,
            "device_count": torch_health.device_count,
            "device_name": torch_health.device_name,
        },
        "cuda_visible_devices": config.cuda_visible_devices,
        "mineru_api_health": api_health,
        "service_identity": service,
        "warnings": [w for w in warnings if w],
    }


def snapshot_nvidia_smi() -> dict:
    """采集 nvidia-smi GPU 快照（单次采样）。

    无 nvidia-smi 时不抛异常，返回 ``{"available": False}``。

    Returns:
        dict: {
            "available": bool,
            "timestamp": str,
            "gpus": [{"name": str, "memory_used_mb": int, "memory_total_mb": int,
                       "gpu_util_pct": int, "memory_util_pct": int}, ...],
            "error": str (仅失败时),
        }
    """
    import datetime as _dt
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return {"available": False, "timestamp": _dt.datetime.now().isoformat(),
                "error": "nvidia-smi not found"}
    try:
        result = subprocess.run(
            [nvidia_smi,
             "--query-gpu=name,memory.used,memory.total,utilization.gpu,utilization.memory",
             "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        if result.returncode != 0:
            return {"available": False, "timestamp": _dt.datetime.now().isoformat(),
                    "error": f"nvidia-smi exit {result.returncode}: {result.stderr.strip()[-200:]}"}
        gpus = []
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 5:
                continue
            try:
                gpus.append({
                    "name": parts[0],
                    "memory_used_mb": int(float(parts[1])),
                    "memory_total_mb": int(float(parts[2])),
                    "gpu_util_pct": int(float(parts[3])),
                    "memory_util_pct": int(float(parts[4])),
                })
            except (ValueError, IndexError):
                continue
        return {"available": True, "timestamp": _dt.datetime.now().isoformat(), "gpus": gpus}
    except Exception as exc:
        return {"available": False, "timestamp": _dt.datetime.now().isoformat(),
                "error": str(exc)}

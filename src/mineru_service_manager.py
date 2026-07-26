"""Start/stop helpers for the local persistent mineru-api service."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from config.settings import CUDA_PATH_DEFAULT, DATA_DIR, PROJECT_ROOT
from src.mineru_runtime import build_mineru_env, list_gpu_processes, preflight_gpu, preflight_torch_cuda, runtime_config_from_env, snapshot_mineru_api


def find_mineru_api_exe() -> str:
    """Robustly resolve the mineru-api executable.

    ``start_mineru_services.py`` is often launched directly with the conda env's
    ``python.exe`` (e.g. ``C:\\Users\\Admin\\.conda\\envs\\mineru\\python.exe``),
    which does NOT put the env's ``Scripts/`` directory on PATH. A bare
    ``"mineru-api"`` then fails with ``FileNotFoundError``. This helper mirrors
    ``src.converter._find_mineru_exe`` for the ``mineru`` CLI.

    Resolution order:
    1. ``shutil.which("mineru-api")``
    2. ``shutil.which("mineru-api.exe")``
    3. ``Path(sys.executable).parent / "Scripts" / "mineru-api.exe"``
    4. ``Path(sys.executable).parent / "mineru-api.exe"``
    5. fallback ``"mineru-api"`` (let subprocess resolve via PATH)
    """
    for name in ("mineru-api", "mineru-api.exe"):
        found = shutil.which(name)
        if found:
            return found
    py_dir = Path(sys.executable).parent
    for cand in (py_dir / "Scripts" / "mineru-api.exe", py_dir / "mineru-api.exe"):
        if cand.exists():
            return str(cand)
    return "mineru-api"


LOG_DIR = DATA_DIR / "logs"
MINERU_API_PID_FILE = LOG_DIR / "mineru_api.pid"
MINERU_API_LOG_FILE = LOG_DIR / "mineru_api.log"
WEB_PID_FILE = LOG_DIR / "mineru_web.pid"
WEB_LOG_FILE = LOG_DIR / "mineru_web.log"

MINERU_READY_NEXT_COMMAND = (
    "python scripts/check_mineru_processes.py && "
    "python scripts/smoke_mineru_conversion.py --paper-number <id> --apply && "
    "python scripts/run_paper_raw_gpu_conversion_then_resolve.py --all --apply"
)


def _api_url_for_port(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _host_port(api_url: str) -> tuple[str, int]:
    parsed = urllib.parse.urlparse(api_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, int(port)


def health_ok(api_url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{api_url.rstrip('/')}/health", timeout=timeout) as response:
            return 200 <= int(response.status) < 300
    except (OSError, urllib.error.URLError, ValueError):
        return False


def port_is_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def read_pid(pid_file: Path = MINERU_API_PID_FILE) -> int | None:
    try:
        text = pid_file.read_text(encoding="utf-8").strip()
        return int(text) if text else None
    except Exception:
        return None


def write_pid(pid: int, pid_file: Path = MINERU_API_PID_FILE) -> None:
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(pid), encoding="utf-8")


def remove_pid_file(pid_file: Path = MINERU_API_PID_FILE) -> bool:
    try:
        if pid_file.exists():
            pid_file.unlink()
            return True
    except Exception:
        return False
    return not pid_file.exists()


def _norm_path(value: str | Path | None) -> str:
    if not value:
        return ""
    try:
        return str(Path(value).resolve()).replace("\\", "/").lower()
    except Exception:
        return str(value).replace("\\", "/").lower()


def _iter_processes() -> list[dict]:
    procs: list[dict] = []
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["wmic", "process", "get", "ProcessId,Name,CommandLine", "/format:csv"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line or line.lower().startswith("node,"):
                    continue
                parts = line.rsplit(",", 2)
                if len(parts) < 3:
                    continue
                procs.append({
                    "pid": parts[-1].strip(),
                    "name": parts[-2].strip(),
                    "cmdline": parts[0].strip(),
                })
        except Exception:
            pass
    else:
        try:
            result = subprocess.run(
                ["ps", "-eo", "pid=,comm=,args="],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split(None, 2)
                if len(parts) >= 3:
                    procs.append({"pid": parts[0], "name": parts[1], "cmdline": parts[2]})
        except Exception:
            pass
    return procs


def find_processes_containing(token: str) -> list[dict]:
    token_l = token.lower()
    return [p for p in _iter_processes() if token_l in str(p.get("cmdline", "")).lower()]


def process_cmdline(pid: int) -> str:
    pid_s = str(pid)
    for proc in _iter_processes():
        if str(proc.get("pid")) == pid_s:
            return str(proc.get("cmdline") or "")
    return ""


def inspect_mineru_api_process(pid: int) -> dict:
    """Return best-effort identity for a mineru-api process.

    Windows does not reliably expose another process' environment without
    elevated tooling, so env is intentionally best-effort. The stable identity
    fields are pid/exe/cmdline/create_time plus live/mineru-api flags.
    """
    identity = {
        "pid": pid,
        "live": False,
        "is_mineru_api": False,
        "exe": "",
        "name": "",
        "cmdline": "",
        "cwd": "",
        "create_time": "",
        "env": {},
    }
    if pid <= 0:
        return identity
    pid_s = str(pid)
    for proc in _iter_processes():
        if str(proc.get("pid")) != pid_s:
            continue
        cmdline = str(proc.get("cmdline") or "")
        identity.update({
            "live": True,
            "exe": str(proc.get("exe") or proc.get("executable") or ""),
            "name": str(proc.get("name") or ""),
            "cmdline": cmdline,
            "cwd": str(proc.get("cwd") or ""),
            "create_time": str(proc.get("create_time") or ""),
            "is_mineru_api": "mineru-api" in cmdline.lower(),
        })
        break
    return identity


def _cmdline_port(cmdline: str) -> int | None:
    patterns = [
        r"(?:^|\s)--port(?:=|\s+)(\d+)",
        r"(?:^|\s)-p(?:=|\s+)(\d+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, cmdline)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
    return None


def _identity_matches_expected(identity: dict, expected_exe: str) -> bool:
    cmdline = str(identity.get("cmdline") or "").lower()
    if "mineru-api" not in cmdline:
        return False
    expected = _norm_path(expected_exe)
    exe = _norm_path(identity.get("exe"))
    if expected_exe in {"mineru-api", "mineru-api.exe"}:
        return True
    if expected and exe and exe == expected:
        return True
    if expected and expected in cmdline.replace("\\", "/"):
        return True
    return False


def classify_mineru_api_service(
    *,
    pid_file: Path,
    api_url: str,
    expected_exe: str,
    expected_port: int,
) -> dict:
    """Classify whether the current healthy mineru-api is managed by us."""
    health = health_ok(api_url)
    pid = read_pid(pid_file)
    identity = inspect_mineru_api_process(pid) if pid is not None else {}
    api_port = _host_port(api_url)[1]
    warnings: list[str] = []

    if not health:
        verdict = "unhealthy" if port_is_open(*_host_port(api_url)) else "not_running"
        return {
            "verdict": verdict,
            "healthy": False,
            "pid": pid,
            "pid_file": str(pid_file),
            "identity": identity,
            "expected_exe": expected_exe,
            "expected_port": expected_port,
            "api_url": api_url,
            "warnings": warnings,
        }
    if pid is None:
        return {
            "verdict": "healthy_but_unmanaged",
            "healthy": True,
            "pid": None,
            "pid_file": str(pid_file),
            "identity": identity,
            "expected_exe": expected_exe,
            "expected_port": expected_port,
            "api_url": api_url,
            "warnings": ["healthy API has no pid file"],
        }
    if not identity.get("live"):
        return {
            "verdict": "healthy_but_stale_pid",
            "healthy": True,
            "pid": pid,
            "pid_file": str(pid_file),
            "identity": identity,
            "expected_exe": expected_exe,
            "expected_port": expected_port,
            "api_url": api_url,
            "warnings": [f"pid file points to non-live PID {pid}"],
        }
    if not identity.get("is_mineru_api"):
        return {
            "verdict": "healthy_but_stale_pid",
            "healthy": True,
            "pid": pid,
            "pid_file": str(pid_file),
            "identity": identity,
            "expected_exe": expected_exe,
            "expected_port": expected_port,
            "api_url": api_url,
            "warnings": [f"pid file points to PID {pid}, but it is not mineru-api"],
        }

    cmd_port = _cmdline_port(str(identity.get("cmdline") or ""))
    if api_port != expected_port:
        warnings.append(f"api_url port {api_port} does not match expected port {expected_port}")
    if cmd_port is not None and cmd_port != expected_port:
        warnings.append(f"mineru-api command port {cmd_port} does not match expected port {expected_port}")
    if not _identity_matches_expected(identity, expected_exe):
        warnings.append("mineru-api executable/cmdline does not match current environment")

    verdict = "managed_ready" if not warnings else "healthy_but_unmanaged"
    return {
        "verdict": verdict,
        "healthy": True,
        "pid": pid,
        "pid_file": str(pid_file),
        "identity": identity,
        "expected_exe": expected_exe,
        "expected_port": expected_port,
        "api_url": api_url,
        "warnings": warnings,
    }


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _terminate_pid(pid: int, *, force: bool = False, wait_seconds: float = 8.0) -> bool:
    if os.name == "nt":
        cmd = ["taskkill", "/PID", str(pid)]
        if force:
            cmd.append("/F")
        subprocess.run(cmd, capture_output=True, timeout=10)
    else:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)
    if not force:
        return _terminate_pid(pid, force=True, wait_seconds=3.0)
    return not _pid_alive(pid)


def _service_env(
    *,
    api_url: str,
    cuda_visible_devices: str,
    cuda_path: str,
) -> dict:
    env = build_mineru_env(runtime_config_from_env(), base_env=os.environ)
    env["MINERU_RUNNER"] = "cli_api_proxy"
    env["MINERU_REQUIRE_GPU"] = "true"
    env["MINERU_API_URL"] = api_url
    env["CUDA_VISIBLE_DEVICES"] = cuda_visible_devices
    env["CUDA_PATH"] = cuda_path
    env["MINERU_BACKEND"] = env.get("MINERU_BACKEND") or "hybrid-engine"
    env["MINERU_METHOD"] = env.get("MINERU_METHOD") or "auto"
    env["MINERU_EFFORT"] = env.get("MINERU_EFFORT") or "medium"
    # The env's Scripts/ directory holds mineru-api.exe (and the mineru CLI).
    # When this process is launched with a bare env python.exe the Scripts dir
    # is NOT on PATH, so mineru-api cannot be resolved. Prepend it explicitly.
    env_scripts = Path(sys.executable).parent / "Scripts"
    if env_scripts.exists():
        scripts_dir = str(env_scripts)
        if scripts_dir not in env.get("PATH", ""):
            env["PATH"] = scripts_dir + os.pathsep + env.get("PATH", "")
    # CUDA bin is already prepended by build_mineru_env's _join_cuda_bin; only
    # add it here if it somehow is not present (cross-platform path style safe).
    cuda_bin = str(Path(cuda_path) / "bin")
    if cuda_bin not in env.get("PATH", ""):
        env["PATH"] = cuda_bin + os.pathsep + env.get("PATH", "")
    return env


def start_services(
    *,
    port: int = 8000,
    api_url: str | None = None,
    cuda_visible_devices: str = "0",
    cuda_path: str = CUDA_PATH_DEFAULT,
    vlm_preload: bool = True,
    wait: bool = False,
    wait_seconds: float = 60.0,
    web: bool = False,
    restart_if_stale: bool = False,
) -> dict:
    api_url = api_url or _api_url_for_port(port)
    host, api_port = _host_port(api_url)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    mineru_api_exe = find_mineru_api_exe()

    if api_port != port:
        return {
            "ok": False,
            "action": "failed",
            "mineru_api_url": api_url,
            "pid": None,
            "pid_file": str(MINERU_API_PID_FILE),
            "log_file": str(MINERU_API_LOG_FILE),
            "health": "failed",
            "message": (
                f"api_url port {api_port} does not match --port {port}; "
                "refusing to start/probe different mineru-api ports."
            ),
        }

    service = classify_mineru_api_service(
        pid_file=MINERU_API_PID_FILE,
        api_url=api_url,
        expected_exe=mineru_api_exe,
        expected_port=port,
    )
    service_verdict_before = service["verdict"]
    if service["verdict"] == "managed_ready":
        return {
            "ok": True,
            "action": "reused",
            "mineru_api_url": api_url,
            "pid": service.get("pid"),
            "pid_file": str(MINERU_API_PID_FILE),
            "log_file": str(MINERU_API_LOG_FILE),
            "health": "ok",
            "port_open": True,
            "service": service,
            "service_verdict_before": service_verdict_before,
            "service_verdict_after": service_verdict_before,
            "message": (
                "mineru-api is healthy and managed; reused existing service. "
                "Do not infer readiness from old mineru_api.log lines. "
                "Current /health plus managed service identity are authoritative."
            ),
            "next_command": MINERU_READY_NEXT_COMMAND,
        }
    if service.get("healthy"):
        # healthy_but_stale_pid with non-live pid: delete stale pid file and
        # reclassify before deciding whether to start or restart.
        if service["verdict"] == "healthy_but_stale_pid" and not service.get("identity", {}).get("live"):
            remove_pid_file(MINERU_API_PID_FILE)
            service = classify_mineru_api_service(
                pid_file=MINERU_API_PID_FILE,
                api_url=api_url,
                expected_exe=mineru_api_exe,
                expected_port=port,
            )
            service_verdict_before = service["verdict"]
        # Only continue healthy-state handling if the service is still healthy
        # after any stale pid cleanup. If it became not_running, fall through to
        # the port_is_open check and start path below.
        if service.get("healthy"):
            if service["verdict"] == "managed_ready":
                return {
                    "ok": True,
                    "action": "reused",
                    "mineru_api_url": api_url,
                    "pid": service.get("pid"),
                    "pid_file": str(MINERU_API_PID_FILE),
                    "log_file": str(MINERU_API_LOG_FILE),
                    "health": "ok",
                    "port_open": True,
                    "service": service,
                    "service_verdict_before": service_verdict_before,
                    "service_verdict_after": service["verdict"],
                    "message": (
                        "mineru-api is healthy and managed after stale pid cleanup; "
                        "reused existing service. Do not infer readiness from old "
                        "mineru_api.log lines. Current /health plus managed service "
                        "identity are authoritative."
                    ),
                    "next_command": MINERU_READY_NEXT_COMMAND,
                }
            if restart_if_stale and service.get("identity", {}).get("is_mineru_api"):
                pid = service.get("pid")
                stopped = _terminate_pid(int(pid), force=False) if pid is not None else False
                remove_pid_file(MINERU_API_PID_FILE)
                if not stopped:
                    return {
                        "ok": False,
                        "action": "failed",
                        "mineru_api_url": api_url,
                        "pid": pid,
                        "pid_file": str(MINERU_API_PID_FILE),
                        "log_file": str(MINERU_API_LOG_FILE),
                        "health": "failed",
                        "port_open": True,
                        "service": service,
                        "service_verdict_before": service_verdict_before,
                        "service_verdict_after": "failed",
                        "message": (
                            "existing mineru-api is healthy but stale/unmanaged; "
                            "failed to stop pid-file-managed process. Run "
                            "stop_mineru_services.py --all-mineru-api or manually "
                            "stop the process."
                        ),
                    }
                # fall through to start a fresh managed service
            elif service["verdict"] == "healthy_but_unmanaged" and not service.get("pid"):
                # healthy but no safe PID to stop — must not start a second service
                return {
                    "ok": False,
                    "action": "failed",
                    "mineru_api_url": api_url,
                    "pid": None,
                    "pid_file": str(MINERU_API_PID_FILE),
                    "log_file": str(MINERU_API_LOG_FILE),
                    "health": "failed",
                    "port_open": True,
                    "service": service,
                    "service_verdict_before": service_verdict_before,
                    "service_verdict_after": service_verdict_before,
                    "message": (
                        "existing healthy mineru-api is unmanaged and has no safe "
                        "PID to stop; run stop_mineru_services.py --all-mineru-api "
                        "or manually stop the process."
                    ),
                }
            else:
                return {
                    "ok": False,
                    "action": "failed",
                    "mineru_api_url": api_url,
                    "pid": service.get("pid"),
                    "pid_file": str(MINERU_API_PID_FILE),
                    "log_file": str(MINERU_API_LOG_FILE),
                    "health": "failed",
                    "port_open": True,
                    "service": service,
                    "service_verdict_before": service_verdict_before,
                    "service_verdict_after": service_verdict_before,
                    "message": (
                        "existing mineru-api is healthy but unmanaged/stale; "
                        "restart required. Run stop_mineru_services.py "
                        "--all-mineru-api or manually stop the process."
                    ),
                }

    if port_is_open(host, api_port):
        return {
            "ok": False,
            "action": "failed",
            "mineru_api_url": api_url,
            "pid": None,
            "pid_file": str(MINERU_API_PID_FILE),
            "log_file": str(MINERU_API_LOG_FILE),
            "health": "failed",
            "port_open": True,
            "service_verdict_before": service_verdict_before,
            "service_verdict_after": "failed",
            "message": (
                f"port {api_port} is occupied but {api_url}/health is "
                "unavailable; not starting a second mineru-api. Run "
                "stop_mineru_services.py --all-mineru-api, stop the occupying "
                "process, or use another --port."
            ),
        }

    cmd = [mineru_api_exe, "--port", str(port)]
    if vlm_preload:
        cmd.extend(["--enable-vlm-preload", "true"])
    env = _service_env(
        api_url=api_url,
        cuda_visible_devices=cuda_visible_devices,
        cuda_path=cuda_path,
    )
    log_handle = MINERU_API_LOG_FILE.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    log_handle.close()
    write_pid(proc.pid, MINERU_API_PID_FILE)

    health = "not_ready"
    if wait:
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if health_ok(api_url):
                health = "ok"
                break
            time.sleep(2.0)

    web_result = None
    if web:
        web_result = start_web_service()

    # With --wait, the caller expects the API to actually be ready: a not_ready
    # result is a failure. Without --wait, fire-and-forget: not_ready is acceptable.
    initial_ok = (health == "ok") if wait else (health in {"ok", "not_ready"})

    # After wait reports health ok, reclassify to confirm managed service
    # identity. health_ok alone is not sufficient; the service must be
    # managed_ready.
    service_verdict_after = "unknown"
    if health == "ok":
        after_service = classify_mineru_api_service(
            pid_file=MINERU_API_PID_FILE,
            api_url=api_url,
            expected_exe=mineru_api_exe,
            expected_port=port,
        )
        service_verdict_after = after_service["verdict"]
        if wait and service_verdict_after != "managed_ready":
            initial_ok = False
            health = "not_ready"

    if health == "ok":
        message = (
            "mineru-api started. Do not infer readiness from old mineru_api.log "
            "lines. Current /health plus managed service identity are authoritative."
        )
    elif wait and service_verdict_after not in ("managed_ready", "unknown"):
        message = (
            f"mineru-api health is ok but service identity is not "
            f"managed_ready: {service_verdict_after}."
        )
    elif wait:
        message = "mineru-api did not become ready before wait timeout."
    else:
        message = "mineru-api started but is not ready yet."
    # The process was launched via Popen, so action reflects launch semantics
    # (started/restarted) regardless of readiness. ok=False captures failures.
    if restart_if_stale and service_verdict_before in ("healthy_but_unmanaged", "healthy_but_stale_pid"):
        action = "restarted"
    else:
        action = "started"
    return {
        "ok": initial_ok,
        "action": action,
        "mineru_api_url": api_url,
        "mineru_api_exe": mineru_api_exe,
        "cmd": cmd,
        "cuda_visible_devices": cuda_visible_devices,
        "cuda_path": cuda_path,
        "mineru_runner": env.get("MINERU_RUNNER"),
        "pid": proc.pid,
        "pid_file": str(MINERU_API_PID_FILE),
        "log_file": str(MINERU_API_LOG_FILE),
        "health": health,
        "port_open": health == "ok",
        "service_verdict_before": service_verdict_before,
        "service_verdict_after": service_verdict_after,
        "message": message,
        "next_command": MINERU_READY_NEXT_COMMAND,
        "web": web_result,
    }


def start_web_service() -> dict:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_handle = WEB_LOG_FILE.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "src.server"],
        cwd=str(PROJECT_ROOT),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    log_handle.close()
    write_pid(proc.pid, WEB_PID_FILE)
    return {"ok": True, "pid": proc.pid, "pid_file": str(WEB_PID_FILE), "log_file": str(WEB_LOG_FILE)}


def stop_services(
    *,
    force: bool = False,
    all_mineru_api: bool = False,
    web: bool = False,
) -> dict:
    stopped: list[int] = []
    warnings: list[str] = []
    targets: list[int] = []

    pid = read_pid(MINERU_API_PID_FILE)
    if pid is not None:
        cmdline = process_cmdline(pid)
        if "mineru-api" in cmdline.lower():
            targets.append(pid)
        else:
            warnings.append(f"pid file points to PID {pid}, but command line is not mineru-api; skipped")
    elif all_mineru_api:
        for proc in find_processes_containing("mineru-api"):
            if "mineru-api" not in str(proc.get("cmdline", "")).lower():
                continue
            try:
                targets.append(int(proc["pid"]))
            except (KeyError, TypeError, ValueError):
                pass
    else:
        found = find_processes_containing("mineru-api")
        if found:
            warnings.append("mineru-api process exists but no pid file was found; use --all-mineru-api to stop all")

    if all_mineru_api:
        for proc in find_processes_containing("mineru-api"):
            if "mineru-api" not in str(proc.get("cmdline", "")).lower():
                continue
            try:
                p = int(proc["pid"])
            except (KeyError, TypeError, ValueError):
                continue
            if p not in targets:
                targets.append(p)

    for target in targets:
        try:
            if _terminate_pid(target, force=force):
                stopped.append(target)
            else:
                warnings.append(f"failed to stop PID {target}")
        except Exception as exc:
            warnings.append(f"failed to stop PID {target}: {exc}")

    pid_removed = remove_pid_file(MINERU_API_PID_FILE) if stopped or pid is None else False
    web_result = None
    if web:
        web_result = stop_web_service(force=force)

    ok = not any(w.startswith("failed to stop") for w in warnings)
    if stopped:
        action = "stopped"
    elif warnings:
        action = "failed" if not ok else "not_running"
    else:
        action = "not_running"
    return {
        "ok": ok,
        "action": action,
        "stopped_pids": stopped,
        "pid_file_removed": pid_removed,
        "message": "; ".join(warnings) if warnings else ("stopped mineru-api" if stopped else "mineru-api is not running"),
        "web": web_result,
    }


def stop_web_service(*, force: bool = False) -> dict:
    pid = read_pid(WEB_PID_FILE)
    if pid is None:
        return {"ok": True, "action": "not_running", "stopped_pids": [], "pid_file_removed": True}
    cmdline = process_cmdline(pid)
    if "src.server" not in cmdline and "-m src.server" not in cmdline:
        return {"ok": False, "action": "failed", "stopped_pids": [], "pid_file_removed": False,
                "message": f"web pid file points to PID {pid}, but command line is not src.server"}
    stopped = _terminate_pid(pid, force=force)
    return {
        "ok": stopped,
        "action": "stopped" if stopped else "failed",
        "stopped_pids": [pid] if stopped else [],
        "pid_file_removed": remove_pid_file(WEB_PID_FILE) if stopped else False,
    }


def dumps_result(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)


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



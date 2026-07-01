"""Start/stop helpers for the local persistent mineru-api service."""
from __future__ import annotations

import json
import os
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
from src.mineru_runtime import build_mineru_env, runtime_config_from_env


LOG_DIR = DATA_DIR / "logs"
MINERU_API_PID_FILE = LOG_DIR / "mineru_api.pid"
MINERU_API_LOG_FILE = LOG_DIR / "mineru_api.log"
WEB_PID_FILE = LOG_DIR / "mineru_web.pid"
WEB_LOG_FILE = LOG_DIR / "mineru_web.log"


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
) -> dict:
    api_url = api_url or _api_url_for_port(port)
    host, api_port = _host_port(api_url)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if health_ok(api_url):
        return {
            "ok": True,
            "action": "reused",
            "mineru_api_url": api_url,
            "pid": read_pid(MINERU_API_PID_FILE),
            "pid_file": str(MINERU_API_PID_FILE),
            "log_file": str(MINERU_API_LOG_FILE),
            "health": "ok",
            "message": "mineru-api is already healthy; reused existing service.",
            "next_command": "python scripts/convert_paper_raw_gpu.py --source-id 000001 --apply",
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
            "message": f"port {api_port} is occupied but {api_url}/health is not available; refusing to start another mineru-api.",
        }

    cmd = ["mineru-api", "--port", str(port)]
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

    return {
        "ok": health in {"ok", "not_ready"},
        "action": "started",
        "mineru_api_url": api_url,
        "pid": proc.pid,
        "pid_file": str(MINERU_API_PID_FILE),
        "log_file": str(MINERU_API_LOG_FILE),
        "health": health,
        "message": "mineru-api started." if health == "ok" else "mineru-api started but is not ready yet.",
        "next_command": "python scripts/convert_paper_raw_gpu.py --source-id 000001 --apply",
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

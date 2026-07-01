"""MinerU 进程与 GPU 状态检查脚本。

用法:
    python scripts/check_mineru_processes.py            # 仅检查，不杀进程
    python scripts/check_mineru_processes.py --kill-stale     # 清理已死锁
    python scripts/check_mineru_processes.py --kill-all-mineru # 终止所有 MinerU 进程
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

from src.mineru_runtime import (
    describe_runtime,
    preflight_gpu,
    preflight_torch_cuda,
    runtime_config_from_env,
    snapshot_nvidia_smi,
)
from src.mineru_lock import read_mineru_lock_status, clear_stale_mineru_lock, LOCK_PATH


def _run_cmd(cmd: list[str], timeout: int = 15) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return -1, "", str(e)


def _find_mineru_processes() -> list[dict]:
    """查找所有 MinerU 相关进程（python + mineru + mineru-api）。"""
    procs = []
    # Windows: use wmic / tasklist
    try:
        import subprocess as sp
        r = sp.run(["wmic", "process", "get", "ProcessId,Name,CommandLine", "/format:csv"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            lower = line.lower()
            # 匹配 mineru / python 进程，且命令行包含 mineru 相关
            if "mineru" in lower or ("python" in lower and ("mineru" in lower or "watcher" in lower or "batch_convert" in lower or "benchmark" in lower or "server" in lower)):
                try:
                    parts = line.rsplit(",", 2)
                    if len(parts) >= 3:
                        pid = parts[-1].strip()
                        name = parts[-2].strip()
                        cmdline = parts[0].strip() if len(parts) > 1 else ""
                        cmd_lower = cmdline.lower()
                        if "mineru-api" in cmd_lower:
                            kind = "mineru-api"
                        elif "watcher" in cmd_lower:
                            kind = "watcher"
                        elif "src.server" in cmd_lower:
                            kind = "python -m src.server"
                        elif "mineru" in cmd_lower:
                            kind = "mineru CLI"
                        else:
                            kind = "mineru-related"
                        procs.append({"pid": pid, "name": name, "cmdline": cmdline, "kind": kind})
                except (ValueError, IndexError):
                    pass
    except Exception:
        pass
    return procs


def _kill_by_pid(pid: int) -> bool:
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def _is_pid_alive(pid: int) -> bool:
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                           capture_output=True, text=True, timeout=8)
        return str(pid) in r.stdout
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="MinerU 进程与 GPU 状态检查")
    parser.add_argument("--kill-stale", action="store_true",
                        help="清理 stale lock 和已死 PID")
    parser.add_argument("--kill-all-mineru", action="store_true",
                        help="终止所有 MinerU 相关进程")
    args = parser.parse_args()

    print("=" * 60)
    print("MinerU 进程与 GPU 状态检查")
    print("=" * 60)

    # ── 1. GPU 总览 ──
    print("\n[1] nvidia-smi GPU 总览")
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        rc, out, err = _run_cmd(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,"
                                 "utilization.gpu,utilization.memory,temperature.gpu,power.draw",
                                 "--format=csv,noheader,nounits"])
        if rc == 0 and out.strip():
            print(f"  {out.strip()}")
        else:
            print(f"  nvidia-smi query failed: {err}")
    else:
        print("  nvidia-smi not found")

    # ── 2. GPU 进程 ──
    print("\n[2] GPU Processes (nvidia-smi)")
    if nvidia_smi:
        rc, out, err = _run_cmd(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                                 "--format=csv,noheader,nounits"])
        if rc == 0 and out.strip():
            for line in out.strip().splitlines():
                print(f"  {line.strip()}")
        else:
            print("  (no compute processes on GPU or query not supported)")
    else:
        print("  nvidia-smi not available")

    # ── 3. MinerU 相关进程 ──
    print("\n[3] MinerU 相关系统进程")
    procs = _find_mineru_processes()
    if procs:
        for p in procs:
            cmd = p["cmdline"][:120] if p["cmdline"] else "(unknown)"
            print(f"  PID {p['pid']:>6}  {p['name']:<20}  {p.get('kind', 'mineru-related'):<22}  {cmd}")
        mineru_api_count = sum(1 for p in procs if p.get("kind") == "mineru-api")
        if mineru_api_count > 1:
            print("  WARNING: multiple mineru-api processes detected. Keep only one persistent mineru-api to avoid GPU OOM.")
    else:
        print("  (no MinerU-related processes found)")

    # ── 4. 环境变量 ──
    print("\n[4] MinerU 环境变量")
    for var in ["MINERU_REQUIRE_GPU", "MINERU_RUNNER", "MINERU_API_URL",
                "MINERU_ALLOW_CPU", "CUDA_PATH", "CUDA_VISIBLE_DEVICES", "MINERU_LOCK_TIMEOUT",
                "MINERU_BACKEND", "MINERU_EFFORT", "MINERU_METHOD"]:
        val = os.environ.get(var, "")
        print(f"  {var}={val or '(not set)'}")

    # ── 5. Runtime config ──
    print("\n[5] Runtime config (from config/settings.py)")
    config = runtime_config_from_env()
    print(f"  runner: {config.runner.value}")
    print(f"  require_gpu: {config.require_gpu}")
    print(f"  cuda_path: {config.cuda_path or '(not set)'}")
    print(f"  cuda_visible_devices: {config.cuda_visible_devices or '(not set)'}")
    gpu_health = preflight_gpu()
    print(f"  preflight_gpu: ok={gpu_health.ok} msg={gpu_health.message} nvidia_smi={gpu_health.nvidia_smi}")
    torch_health = preflight_torch_cuda()
    print(f"  torch_cuda: ok={torch_health.ok} msg={torch_health.message}")
    print(f"  torch.cuda.is_available(): {torch_health.cuda_available}")
    print(f"  torch.version.cuda: {torch_health.torch_cuda_version or '(empty)'}")
    print(f"  torch.cuda.device_count(): {torch_health.device_count}")
    print(f"  torch.cuda.get_device_name(0): {torch_health.device_name or '(empty)'}")

    # ── 6. Lock 状态 ──
    print("\n[6] MinerU Lock 状态")
    lock_status = read_mineru_lock_status()
    if lock_status["locked"]:
        print(f"  LOCKED: pid={lock_status['owner_pid']} command={lock_status.get('command','?')}")
        print(f"  started_at: {lock_status.get('started_at','?')}")
        print(f"  age_seconds: {lock_status.get('age_seconds','?')}")
        if lock_status.get("stale"):
            print(f"  *** STALE LOCK (PID not alive) ***")
    else:
        print(f"  unlocked")
    print(f"  lock file: {LOCK_PATH}")

    # ── 7. Actions ──
    if args.kill_stale:
        print("\n[Action] --kill-stale")
        lock_status = read_mineru_lock_status()
        if lock_status.get("stale"):
            clear_stale_mineru_lock()
            print("  stale lock cleared")
        else:
            print("  no stale lock found")
        # 也列出可能 stale 的进程
        for p in procs:
            try:
                pid = int(p["pid"])
                if not _is_pid_alive(pid):
                    print(f"  PID {pid} appears dead, cleaning reference")
            except ValueError:
                pass

    if args.kill_all_mineru:
        print("\n[Action] --kill-all-mineru")
        for p in procs:
            try:
                pid = int(p["pid"])
                if pid == os.getpid():
                    print(f"  SKIP self: PID {pid}")
                    continue
                print(f"  KILL PID {pid}: {p['name']} {p['cmdline'][:100]}")
                _kill_by_pid(pid)
                time.sleep(0.3)
            except ValueError:
                pass
        # 清理 lock
        if LOCK_PATH.exists():
            clear_stale_mineru_lock()
            print("  lock file cleaned")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

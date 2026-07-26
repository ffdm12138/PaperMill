"""进程存活探测（单源）。

Windows 下经 ``tasklist`` 查询，POSIX 下经 ``os.kill(pid, 0)``。查询失败
一律视为不存活（fail-safe：调用方只用它判断锁持有者是否残留）。
"""
from __future__ import annotations

import os
import subprocess
import sys


def is_pid_alive(pid: int) -> bool:
    """检查 PID 是否存活（Windows）。"""
    try:
        if sys.platform == "win32":
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=8,
            )
            return str(pid) in r.stdout
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False

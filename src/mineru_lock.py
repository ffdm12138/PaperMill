"""MinerU 全局转换锁 — 防止多个 MinerU 进程同时占用 GPU。

使用方法:
    from src.mineru_lock import MinerULock

    lock = MinerULock()
    if not lock.acquire(timeout=3600):
        raise RuntimeError("Another MinerU conversion is running")
    try:
        # ... do conversion ...
    finally:
        lock.release()

状态查询:
    from src.mineru_lock import read_mineru_lock_status, clear_stale_mineru_lock
    status = read_mineru_lock_status()
    if status["stale"]:
        clear_stale_mineru_lock()
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from loguru import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

# 锁文件路径
LOCK_DIR = Path(__file__).parent.parent / "data" / "locks"
LOCK_PATH = LOCK_DIR / "mineru_convert.lock"

# 环境变量配置
LOCK_TIMEOUT_DEFAULT = int(os.environ.get("MINERU_LOCK_TIMEOUT", "3600"))
LOCK_WAIT_TIMEOUT_ENV = "MINERU_LOCK_WAIT_TIMEOUT_SECONDS"
LOCK_STUCK_WARN_ENV = "MINERU_LOCK_STUCK_WARN_SECONDS"
LOCK_STUCK_WARN_DEFAULT = 1800

LOCK_FREE = "LOCK_FREE"
LOCK_ACTIVE = "LOCK_ACTIVE"
LOCK_OWNER_DEAD = "LOCK_OWNER_DEAD"
LOCK_STUCK_SUSPECTED = "LOCK_STUCK_SUSPECTED"


def _ensure_lock_dir():
    LOCK_DIR.mkdir(parents=True, exist_ok=True)


def _is_pid_alive(pid: int) -> bool:
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


def _get_current_command() -> str:
    """尝试获取当前 Python 进程的命令行。"""
    try:
        return " ".join(sys.argv)
    except Exception:
        return "unknown"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


class MinerULock:
    """跨进程 MinerU 转换锁。

    同一时间只允许一个 MinerU 转换任务运行。
    锁文件记录 PID / command / started_at 用于诊断。
    """

    def __init__(
        self,
        timeout: int | None = None,
        poll_interval: float = 1.0,
        context: dict | None = None,
        stuck_warn_seconds: int | None = None,
    ):
        self._timeout = timeout if timeout is not None else _env_int(
            LOCK_WAIT_TIMEOUT_ENV,
            _env_int("MINERU_LOCK_TIMEOUT", LOCK_TIMEOUT_DEFAULT),
        )
        self._poll_interval = poll_interval
        self._context = dict(context or {})
        self._stuck_warn_seconds = (
            stuck_warn_seconds if stuck_warn_seconds is not None
            else _env_int(LOCK_STUCK_WARN_ENV, LOCK_STUCK_WARN_DEFAULT)
        )
        self._acquired = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    def acquire(self, timeout: int | None = None) -> bool:
        """获取锁。如果已被占用，等待直到超时。

        Returns:
            True 获取成功，False 超时。
        """
        wait_timeout = timeout if timeout is not None else self._timeout
        _ensure_lock_dir()
        deadline = time.time() + wait_timeout
        pid = os.getpid()
        cmd = _get_current_command()
        cwd = os.getcwd()

        while time.time() < deadline:
            try:
                # 原子创建：如果文件已存在且 PID 仍活，则等待
                if LOCK_PATH.exists():
                    existing = read_mineru_lock_status(stuck_warn_seconds=self._stuck_warn_seconds)
                    if existing.get("stale"):
                        # stale lock — 清理后重试
                        logger.warning(f"Clearing stale lock (PID {existing.get('owner_pid')} not alive)")
                        clear_stale_mineru_lock()
                        continue
                    # PID still alive — wait
                    owner = existing.get("owner_pid", "?")
                    logger.info(
                        f"MinerU lock held by PID {owner} "
                        f"(age={existing.get('age_seconds', '?')}s). Waiting..."
                    )
                    if existing.get("verdict") == LOCK_STUCK_SUSPECTED:
                        logger.warning(
                            "MinerU lock stuck suspected: PID {} age={}s paper_number={} stage={}",
                            owner,
                            existing.get("age_seconds", "?"),
                            existing.get("paper_number") or "",
                            existing.get("stage") or "",
                        )
                    time.sleep(self._poll_interval)
                    continue

                # 写入锁文件
                now = datetime.now().isoformat()
                lock_data = {
                    "pid": pid,
                    "command": cmd,
                    "cwd": cwd,
                    "started_at": now,
                    "created_at": now,
                    "updated_at": now,
                    "stage": self._context.get("stage") or "submit",
                    **self._context,
                }
                tmp_path = LOCK_PATH.with_suffix(".tmp")
                tmp_path.write_text(
                    json.dumps(lock_data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                # 原子重命名
                try:
                    tmp_path.replace(LOCK_PATH)
                except OSError:
                    # 竞态：另一个进程先创建了，检查是否是自己
                    tmp_path.unlink(missing_ok=True)
                    if LOCK_PATH.exists():
                        existing = read_mineru_lock_status(stuck_warn_seconds=self._stuck_warn_seconds)
                        if existing.get("owner_pid") == pid:
                            self._acquired = True
                            return True
                        time.sleep(self._poll_interval)
                        continue

                self._acquired = True
                return True

            except Exception as e:
                logger.warning(f"Lock acquire error: {e}")
                time.sleep(self._poll_interval)

        return False

    def release(self) -> None:
        """释放锁。"""
        if not self._acquired:
            return
        try:
            if LOCK_PATH.exists():
                existing = read_mineru_lock_status(stuck_warn_seconds=self._stuck_warn_seconds)
                if existing.get("owner_pid") == os.getpid():
                    LOCK_PATH.unlink()
        except Exception as exc:
            logger.debug("mineru lock release cleanup raced: {}", exc)
        finally:
            self._acquired = False

    def __enter__(self):
        if not self.acquire():
            status = read_mineru_lock_status(stuck_warn_seconds=self._stuck_warn_seconds)
            raise RuntimeError(
                f"Cannot acquire MinerU lock. Held by PID {status.get('owner_pid', '?')} "
                f"(age={status.get('age_seconds', '?')}s)"
            )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False


def clear_stale_mineru_lock() -> bool:
    """清理 stale lock（锁文件存在但 PID 已死）。"""
    status = read_mineru_lock_status()
    if status.get("stale") or (status["locked"] and status.get("owner_pid") is not None and
                                not _is_pid_alive(status["owner_pid"])):
        try:
            LOCK_PATH.unlink(missing_ok=True)
            logger.info("Stale mineru lock cleared")
            return True
        except Exception:
            pass
    return False


def _empty_lock_status() -> dict:
    return {
        "lock_present": False,
        "locked": False,
        "lock_path": str(LOCK_PATH),
        "owner_pid": None,
        "owner_live": False,
        "command": None,
        "started_at": None,
        "created_at": None,
        "updated_at": None,
        "age_seconds": None,
        "paper_number": "",
        "paper_raw_id": "",
        "runner": "",
        "api_url": "",
        "backend": "",
        "method": "",
        "stage": "",
        "stale": False,
        "verdict": LOCK_FREE,
    }


def _lock_verdict(*, lock_present: bool, owner_live: bool, age_seconds: float | None, stuck_warn_seconds: int) -> str:
    if not lock_present:
        return LOCK_FREE
    if not owner_live:
        return LOCK_OWNER_DEAD
    if age_seconds is not None and age_seconds > stuck_warn_seconds:
        return LOCK_STUCK_SUSPECTED
    return LOCK_ACTIVE


def read_mineru_lock_status(*, stuck_warn_seconds: int | None = None) -> dict:
    """Read the current conversion lock status without acquiring it."""
    if not LOCK_PATH.exists():
        return _empty_lock_status()
    warn_seconds = stuck_warn_seconds if stuck_warn_seconds is not None else _env_int(
        LOCK_STUCK_WARN_ENV,
        LOCK_STUCK_WARN_DEFAULT,
    )
    try:
        data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        owner_pid = data.get("pid")
        started_at = str(data.get("started_at") or data.get("created_at") or "")
        try:
            start_dt = datetime.fromisoformat(started_at)
            age = (datetime.now() - start_dt).total_seconds()
        except (ValueError, TypeError):
            age = None
        owner_live = bool(owner_pid is not None and _is_pid_alive(owner_pid))
        stale = not owner_live
        rounded_age = round(age, 1) if age is not None else None
        return {
            "lock_present": True,
            "locked": not stale,
            "lock_path": str(LOCK_PATH),
            "owner_pid": owner_pid,
            "owner_live": owner_live,
            "command": data.get("command"),
            "started_at": started_at,
            "created_at": data.get("created_at") or started_at,
            "updated_at": data.get("updated_at") or started_at,
            "age_seconds": rounded_age,
            "paper_number": data.get("paper_number") or "",
            "paper_raw_id": data.get("paper_raw_id") or "",
            "runner": data.get("runner") or "",
            "api_url": data.get("api_url") or "",
            "backend": data.get("backend") or "",
            "method": data.get("method") or "",
            "stage": data.get("stage") or "",
            "stale": stale,
            "verdict": _lock_verdict(
                lock_present=True,
                owner_live=owner_live,
                age_seconds=rounded_age,
                stuck_warn_seconds=warn_seconds,
            ),
        }
    except Exception:
        status = _empty_lock_status()
        status.update({
            "lock_present": True,
            "stale": True,
            "verdict": LOCK_OWNER_DEAD,
        })
        return status


def update_mineru_lock_stage(stage: str, **extra) -> None:
    """Best-effort update of the current process lock owner context."""
    if not LOCK_PATH.exists():
        return
    try:
        data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    if data.get("pid") != os.getpid():
        return
    data["stage"] = stage
    data["updated_at"] = datetime.now().isoformat()
    data.update({k: v for k, v in extra.items() if v not in (None, "")})
    tmp_path = LOCK_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(LOCK_PATH)

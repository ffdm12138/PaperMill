"""MinerU 3.4 文档转换引擎

调用 mineru CLI 将PDF/DOCX/PPTX/XLSX/图片 转换为 Markdown
"""
import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from loguru import logger
from src.mineru.runtime import (
    MinerURunner, build_mineru_env, preflight_gpu, preflight_mineru_api,
    preflight_torch_cuda, runtime_config_from_env, snapshot_nvidia_smi,
)
from src.mineru.lock import MinerULock, read_mineru_lock_status, update_mineru_lock_stage


def _find_mineru_exe() -> str:
    """跨平台查找 mineru 可执行文件"""
    # 1. 优先 PATH 查找
    for name in ("mineru", "mineru.exe"):
        found = shutil.which(name)
        if found:
            return found
    # 2. fallback: Python 环境目录
    _py_dir = Path(os.sys.executable).parent
    for cand in (_py_dir / "mineru.exe",
                 _py_dir / "Scripts" / "mineru.exe",
                 _py_dir.parent / "Scripts" / "mineru.exe"):
        if cand.exists():
            return str(cand)
    return "mineru"  # 最后尝试直接调命令名，让 subprocess 按 PATH 解析


MINERU_EXE = _find_mineru_exe()

def mineru_available() -> bool:
    """检查 mineru CLI 是否可用"""
    try:
        r = subprocess.run([MINERU_EXE, "--version"], capture_output=True,
                           encoding="utf-8", errors="replace", timeout=10)
        return r.returncode == 0
    except Exception:
        return False


class MinerUConverter:
    """MinerU 3.4 文档转换器"""

    def __init__(self, proxy: str = None, timeout: int | None = None,
                 log_dir: str | Path | None = None,
                 lock_timeout: int | None = None,
                 lock_stuck_warn_seconds: int | None = None):
        """
        Args:
            proxy: 代理地址，如 "http://127.0.0.1:7890"，None则不走代理
            timeout: Deprecated compatibility parameter. Ignored for MinerU
                     conversion; conversion is intentionally unbounded.
            log_dir: 性能日志输出目录。None 时从 config 读取 MINERU_LOG_DIR；
                     设为 "" 禁用日志写入
        """
        self.proxy = proxy
        self._deprecated_timeout = timeout
        from config.settings import MINERU_LOG_DIR
        if log_dir is None:
            self._log_dir = MINERU_LOG_DIR
        elif log_dir == "":
            self._log_dir = None
        else:
            self._log_dir = Path(log_dir)
        self._lock_timeout = lock_timeout
        self._lock_stuck_warn_seconds = lock_stuck_warn_seconds

    def _get_env(self) -> dict:
        """构建环境变量"""
        env = build_mineru_env(runtime_config_from_env(), base_env=os.environ)
        if self.proxy:
            env["HTTP_PROXY"] = self.proxy
            env["HTTPS_PROXY"] = self.proxy
        return env

    def convert(
        self,
        input_path: str | Path,
        output_dir: str | Path,
        backend: str = "hybrid-engine",
        method: str = "auto",
        lang: str = "ch",
        effort: str = "medium",
        api_url: str | None = None,
        paper_name: str = "",
    ) -> dict:
        """统一转换入口。

        Args:
            input_path: 输入文件路径 (PDF/DOCX/PPTX/XLSX/图片)
            output_dir: 输出目录
            backend: 解析后端 "pipeline" | "vlm-engine" | "hybrid-engine"
            method: 解析方法 "auto" | "ocr" | "txt"
            lang: OCR语言 "ch" | "en" 等
            effort: hybrid-engine解析强度 "medium" | "high"
            api_url: mineru-api 地址。
                     - 留空且 MINERU_RUNNER=cli → 纯 CLI（冷启动）
                     - 留空且 MINERU_RUNNER=cli_api_proxy → CLI + --api-url
                     - 留空且 MINERU_RUNNER=api → HTTP adapter（未实现，报错）
                     - 显式传入 → 优先使用
            paper_name: 可选 paper_name 用于性能日志标识

        Returns:
            dict: {
                "success": bool,
                "markdown": str,
                "md_path": str,
                "output_dir": str,
                "source_file": str,
                "runner": "cli" | "cli_api_proxy" | "api",
                "elapsed_seconds": float (仅 success),
                "error": str (失败时),
            }
        """
        config = runtime_config_from_env()
        runner = config.runner

        # ── cli: 拒绝无意中传入的 api_url（防止悄悄走未实现 HTTP adapter）──
        if runner == MinerURunner.CLI:
            if api_url:
                return {
                    "success": False,
                    "error": (
                        f"MINERU_RUNNER=cli 不支持 --api-url。"
                        f"如果要复用常驻 mineru-api 服务，请设置 "
                        f"MINERU_RUNNER=cli_api_proxy MINERU_API_URL={api_url}"
                    ),
                    "backend": backend, "method": method,
                    "effort": effort, "runner": "cli",
                }
            return self.convert_via_cli(input_path, output_dir, backend, method,
                                        lang, effort, paper_name=paper_name)

        # ── cli_api_proxy: CLI + --api-url ──
        if runner == MinerURunner.CLI_API_PROXY:
            proxy_url = api_url or config.api_url
            health = preflight_mineru_api(proxy_url)
            if not health.api_available:
                return {
                    "success": False,
                    "error": f"mineru-api unavailable at {proxy_url}: {health.message}",
                    "backend": backend,
                    "method": method,
                    "effort": effort,
                    "runner": "cli_api_proxy",
                }
            return self.convert_via_cli(
                input_path, output_dir, backend, method, lang, effort,
                paper_name=paper_name, api_url=proxy_url,
            )

        # ── api (HTTP adapter, not implemented) ──
        if runner == MinerURunner.API:
            effective_api_url = api_url or config.api_url
            return self.convert_via_api(input_path, output_dir, backend, method,
                                        lang, effort, effective_api_url)

    def _write_timing_log(
        self,
        *,
        paper_name: str,
        input_path: Path,
        file_size: int,
        start_time: float,
        end_time: float,
        gpu_snapshot_before: dict | None,
        gpu_snapshot_after: dict | None,
        cmd: list[str],
        return_code: int | None,
        stdout_tail: str,
        stderr_tail: str,
        output_dir: str,
        md_path: str,
        images_count: int,
        success: bool,
        error: str,
        backend: str,
        method: str,
        effort: str,
        lang: str,
        runner: str,
    ) -> None:
        """将单次 MinerU 转换的性能日志写入 JSON 文件（失败软降级）。"""
        if self._log_dir is None:
            return
        config = runtime_config_from_env()
        gpu_health = preflight_gpu()
        torch_health = preflight_torch_cuda()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_record = {
            "paper_name": paper_name,
            "pdf_path": str(input_path),
            "file_size": file_size,
            "runner": runner,
            "backend": backend,
            "method": method,
            "effort": effort,
            "lang": lang,
            "api_url": config.api_url if runner == "api" else None,
            "MINERU_REQUIRE_GPU": config.require_gpu,
            "MINERU_ALLOW_CPU": config.allow_cpu,
            "CUDA_PATH": config.cuda_path,
            "CUDA_VISIBLE_DEVICES": config.cuda_visible_devices,
            "preflight_gpu": {
                "ok": gpu_health.ok,
                "message": gpu_health.message,
                "nvidia_smi": getattr(gpu_health, "nvidia_smi", None),
            },
            "torch_cuda": {
                "ok": torch_health.ok,
                "message": torch_health.message,
                "torch_version": torch_health.torch_version,
                "torch_cuda_version": torch_health.torch_cuda_version,
                "cuda_available": torch_health.cuda_available,
                "device_count": torch_health.device_count,
                "device_name": torch_health.device_name,
            },
            "mineru_command": cmd,
            "conversion_timeout_enabled": False,
            "conversion_timeout_seconds": None,
            "start_time": datetime.fromtimestamp(start_time).isoformat(),
            "end_time": datetime.fromtimestamp(end_time).isoformat(),
            "elapsed_seconds": round(end_time - start_time, 2),
            "return_code": return_code,
            "stdout_tail": stdout_tail[-1000:] if stdout_tail else "",
            "stderr_tail": stderr_tail[-1000:] if stderr_tail else "",
            "output_dir": output_dir,
            "markdown_path": md_path,
            "images_count": images_count,
            "success": success,
            "error": error,
            "nvidia_smi_before": gpu_snapshot_before,
            "nvidia_smi_after": gpu_snapshot_after,
        }
        try:
            run_dir = self._log_dir / "mineru_runs"
            run_dir.mkdir(parents=True, exist_ok=True)
            # 使用 paper_name + timestamp 作为文件名前缀
            safe_pid = paper_name.replace("/", "_").replace("\\", "_")
            out_path = run_dir / f"{safe_pid}_{timestamp}.json"
            # 原子写入
            tmp_path = out_path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(log_record, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(out_path)
        except Exception:
            logger.warning(f"[converter] 无法写入性能日志 (paper_name={paper_name})", exc_info=True)

    def convert_via_cli(
        self,
        input_path: str | Path,
        output_dir: str | Path,
        backend: str = "hybrid-engine",
        method: str = "auto",
        lang: str = "ch",
        effort: str = "medium",
        paper_name: str = "",
        api_url: str | None = None,
    ) -> dict:
        """通过 mineru CLI 子进程转换。

        所有返回分支都带 backend/method/effort/runner 四字段，便于调用方
        （v2 paper_raw converter / 测试）直接取用，不依赖
        传入参数回填。

        Args:
            paper_name: 可选 paper_name 用于性能日志标识；空时使用 input_path 文件名。
            api_url: 非空时附加 --api-url <api_url>，启用 CLI API proxy 模式。
                     此时 runner 记为 "cli_api_proxy"。
        """
        input_path = Path(input_path)
        output_dir = Path(output_dir)
        file_size = input_path.stat().st_size if input_path.exists() else 0
        pid = paper_name or input_path.stem
        t0 = time.time()
        effective_runner = "cli_api_proxy" if api_url else "cli"

        def _fail(error: str) -> dict:
            return {
                "success": False, "error": error,
                "backend": backend, "method": method,
                "effort": effort, "runner": effective_runner,
            }

        def _fail_with_log(
            error: str,
            gpu_before: dict | None = None,
            gpu_after: dict | None = None,
            cmd_log: list[str] | None = None,
            stdout_log: str = "",
            stderr_log: str = "",
            return_code_log: int | None = None,
        ) -> dict:
            self._write_timing_log(
                paper_name=pid,
                input_path=input_path,
                file_size=file_size,
                start_time=t0,
                end_time=time.time(),
                gpu_snapshot_before=gpu_before,
                gpu_snapshot_after=gpu_after,
                cmd=cmd_log or [],
                return_code=return_code_log,
                stdout_tail=stdout_log,
                stderr_tail=stderr_log,
                output_dir=str(output_dir),
                md_path="",
                images_count=0,
                success=False,
                error=error,
                backend=backend, method=method,
                effort=effort, lang=lang, runner=effective_runner,
            )
            return _fail(error)

        if not input_path.exists():
            return _fail(f"文件不存在: {input_path}")
        gpu_health = preflight_gpu()
        if not gpu_health.ok:
            return _fail_with_log(f"GPU preflight failed: {gpu_health.message}")
        torch_health = preflight_torch_cuda()
        if not torch_health.ok:
            return _fail_with_log(f"Torch CUDA preflight failed: {torch_health.message}")

        logger.info(f"[converter] runner={effective_runner} | {input_path.name} (backend={backend}, method={method})")

        cmd = [
            MINERU_EXE,
            "-p", str(input_path),
            "-o", str(output_dir),
            "-b", backend,
            "-m", method,
            "-l", lang,
        ]
        if api_url:
            cmd.extend(["--api-url", api_url])
        if backend == "hybrid-engine":
            cmd.extend(["--effort", effort])

        # ── 全局 MinerU 锁：防止多个 mineru 进程同时占 GPU ──
        mineru_lock = MinerULock(
            timeout=self._lock_timeout,
            stuck_warn_seconds=self._lock_stuck_warn_seconds,
            context={
                "paper_number": pid,
                "paper_raw_id": pid,
                "runner": effective_runner,
                "api_url": api_url or "",
                "backend": backend,
                "method": method,
                "effort": effort,
                "stage": "submit",
            },
        )
        lock_acquired = mineru_lock.acquire()
        if not lock_acquired:
            lock_status = read_mineru_lock_status(stuck_warn_seconds=self._lock_stuck_warn_seconds)
            return _fail_with_log(
                f"MinerU lock busy: held by PID {lock_status.get('owner_pid', '?')} "
                f"(age={lock_status.get('age_seconds', '?')}s). "
                f"Stop other conversions or run: python scripts/check_mineru_processes.py --kill-stale",
                gpu_before=None,
            )

        gpu_before = snapshot_nvidia_smi()

        try:
            try:
                update_mineru_lock_stage("run")
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=self._get_env(),
                )

                update_mineru_lock_stage("write")
                gpu_after = snapshot_nvidia_smi()
                elapsed = time.time() - t0

                if result.returncode != 0:
                    error_msg = result.stderr[-500:] if result.stderr else "未知错误"
                    logger.error(f"转换失败: {error_msg}")
                    return _fail_with_log(
                        error_msg,
                        gpu_before=gpu_before, gpu_after=gpu_after,
                        cmd_log=cmd, stdout_log=result.stdout, stderr_log=result.stderr,
                        return_code_log=result.returncode,
                    )

                # 查找生成的Markdown文件
                # 产品固定 hybrid-engine，优先 hybrid_auto / hybrid_ocr / hybrid_txt
                stem = input_path.stem
                from src.mineru.cleaner import MinerUOutputCleaner
                cl = MinerUOutputCleaner()
                # 先试 locate_markdown（支持 hybrid_* 变体）
                md_path = cl.locate_markdown(
                    output_dir, method=method, stem=stem, backend=backend)
                if md_path is None:
                    md_path = cl.locate_markdown(
                        output_dir / stem, method=method, stem=stem, backend=backend)

                images_dir = output_dir / stem / "images"
                images_count = len(list(images_dir.glob("*"))) if images_dir.exists() else 0

                if md_path and md_path.exists():
                    md_content = md_path.read_text(encoding="utf-8")
                else:
                    md_content = ""
                    logger.warning(f"未找到Markdown输出 (method={method} backend={backend})")

                logger.info(
                    f"[MinerU] paper_name={pid} runner={effective_runner} backend={backend} "
                    f"method={method} effort={effort} elapsed={elapsed:.1f}s "
                    f"gpu_required={runtime_config_from_env().require_gpu}"
                )

                self._write_timing_log(
                    paper_name=pid,
                    input_path=input_path,
                    file_size=file_size,
                    start_time=t0,
                    end_time=time.time(),
                    gpu_snapshot_before=gpu_before,
                    gpu_snapshot_after=gpu_after,
                    cmd=cmd,
                    return_code=result.returncode,
                    stdout_tail=result.stdout,
                    stderr_tail=result.stderr,
                    output_dir=str(output_dir / stem),
                    md_path=str(md_path) if md_path and md_path.exists() else "",
                    images_count=images_count,
                    success=True,
                    error="",
                    backend=backend, method=method,
                    effort=effort, lang=lang, runner=effective_runner,
                )

                update_mineru_lock_stage("done")
                elapsed_sec = round(time.time() - t0, 2)
                return {
                    "success": True,
                    "markdown": md_content,
                    "md_path": str(md_path) if md_path and md_path.exists() else "",
                    "output_dir": str(output_dir / stem),
                    "source_file": input_path.name,
                    "backend": backend, "method": method,
                    "effort": effort, "runner": effective_runner,
                    "elapsed_seconds": elapsed_sec,
                }

            except Exception as e:
                return _fail_with_log(str(e), gpu_before=gpu_before, cmd_log=cmd, stderr_log=str(e))
        finally:
            mineru_lock.release()

    def convert_via_api(
        self,
        input_path: str | Path,
        output_dir: str | Path,
        backend: str = "hybrid-engine",
        method: str = "auto",
        lang: str = "ch",
        effort: str = "medium",
        api_url: str = "",
    ) -> dict:
        """通过 mineru-api 服务转换。

        API 上传协议仍需按当前 MinerU API 文档校验；这里先做健康检查并返回
        结构化失败，避免 ``api_url`` 参数存在但静默走 CLI 或抛 NotImplementedError。
        """
        health = preflight_mineru_api(api_url)
        if not health.api_available:
            return {
                "success": False,
                "error": f"mineru-api unavailable: {health.message}",
                "backend": backend,
                "method": method,
                "effort": effort,
                "runner": "api",
            }
        return {
            "success": False,
            "error": (
                "mineru-api is reachable, but HTTP upload adapter is not implemented "
                "for this MinerU API version. Use CLI runner or implement the adapter "
                "after verifying mineru-api docs."
            ),
            "backend": backend,
            "method": method,
            "effort": effort,
            "runner": "api",
        }

    def convert_batch(
        self,
        input_paths: list[str | Path],
        output_dir: str | Path,
        backend: str = "hybrid-engine",
        method: str = "auto",
        lang: str = "ch",
        effort: str = "medium",
    ) -> list[dict]:
        """批量转换，每篇打印耗时，末尾输出汇总统计。

        Returns:
            list[dict]: 每篇的 convert() 结果（含 elapsed_seconds）。
        """
        import time as _time
        results = []
        records = []  # (filename, result)
        total = len(input_paths)
        for i, path in enumerate(input_paths, 1):
            pid = Path(path).stem
            result = self.convert(path, output_dir, backend, method, lang, effort, paper_name=pid)
            results.append(result)
            records.append((Path(path).name, result))
            elapsed = result.get("elapsed_seconds", "?")
            status = "OK" if result["success"] else "FAIL"
            logger.info(f"[{i}/{total}] {Path(path).name} MinerU elapsed={elapsed}s [{status}]")

        # ── 汇总统计 ──
        success_records = [(name, r) for name, r in records if r.get("success")]
        if success_records:
            elapsed_pairs = sorted(
                [(name, r.get("elapsed_seconds", 0.0)) for name, r in success_records],
                key=lambda x: x[1],
            )
            n = len(elapsed_pairs)
            total_elapsed = sum(e for _, e in elapsed_pairs)
            avg_elapsed = total_elapsed / n
            median_elapsed = elapsed_pairs[n // 2][1]
            max_elapsed = elapsed_pairs[-1][1]
            slowest_file = elapsed_pairs[-1][0]
            logger.info(
                f"[batch summary] total={total} success={n} failed={total - n} "
                f"total_elapsed={total_elapsed:.1f}s avg={avg_elapsed:.1f}s "
                f"median={median_elapsed:.1f}s max={max_elapsed:.1f}s ({slowest_file})"
            )
            if n < total:
                failed_names = [name for name, r in records if not r.get("success")]
                logger.info(f"[batch summary] failed_files: {', '.join(failed_names)}")
        elif results:
            failed_names = [name for name, r in records if not r.get("success")]
            logger.info(
                f"[batch summary] total={total} success=0 failed={total} "
                f"failed_files: {', '.join(failed_names)}"
            )
        return results

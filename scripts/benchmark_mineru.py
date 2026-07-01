"""MinerU 单 PDF 性能基准测试（不修改文献库）。

用途：
    python scripts/benchmark_mineru.py "E:\\papers_to_import\\test.pdf"

行为：
    - 不写正式 v2 catalog（不会污染文献库）
    - 只调用 MinerUConverter 转到临时目录
    - 输出 timing summary + GPU snapshot
    - 转换前检查 GPU 是否 busy（需要 --force 跳过）
    - 可选 --repeat N 重复转换，计算统计量

用法:
    conda activate mineru
    python scripts/benchmark_mineru.py <pdf_path>
    python scripts/benchmark_mineru.py <pdf_path> --repeat 3
    python scripts/benchmark_mineru.py <pdf_path> --method ocr --effort high
    python scripts/benchmark_mineru.py <pdf_path> --force     # 跳过 GPU busy 检查
    python scripts/benchmark_mineru.py <pdf_path> --keep-output
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

from config.settings import MINERU_BACKEND, MINERU_EFFORT, MINERU_METHOD, MINERU_LANG
from src.converter import MinerUConverter
from src.mineru_lock import read_mineru_lock_status, clear_stale_mineru_lock
from src.mineru_runtime import (
    describe_runtime,
    preflight_gpu,
    preflight_torch_cuda,
    runtime_config_from_env,
    snapshot_nvidia_smi,
)

# GPU busy 阈值（超过则警告）
GPU_BUSY_MEM_MB = 2000
GPU_BUSY_UTIL_PCT = 20


def _format_gpu_snapshot(snap: dict) -> str:
    if not snap or not snap.get("available"):
        return "  GPU snapshot: unavailable"
    lines = ["  GPU snapshot:"]
    for g in snap.get("gpus", []):
        lines.append(
            f"    {g['name']}: "
            f"mem={g['memory_used_mb']}/{g['memory_total_mb']}MB "
            f"gpu_util={g['gpu_util_pct']}% "
            f"mem_util={g['memory_util_pct']}%"
        )
    return "\n".join(lines)


def _check_gpu_busy(snap: dict) -> tuple[bool, str]:
    """检查 GPU 是否 busy。返回 (is_busy, reason)。"""
    if not snap or not snap.get("available"):
        return False, "GPU snapshot unavailable"
    reasons = []
    for g in snap.get("gpus", []):
        mem_used = g.get("memory_used_mb", 0)
        gpu_util = g.get("gpu_util_pct", 0)
        if mem_used > GPU_BUSY_MEM_MB:
            reasons.append(f"GPU memory {mem_used}MB > {GPU_BUSY_MEM_MB}MB")
        if gpu_util > GPU_BUSY_UTIL_PCT:
            reasons.append(f"GPU util {gpu_util}% > {GPU_BUSY_UTIL_PCT}%")
    if reasons:
        return True, "; ".join(reasons)
    return False, ""


def _print_env_warnings(config):
    """如果关键环境变量未设置，打印 warning。"""
    if not config.require_gpu:
        print("  ** WARNING: MinerU formal ingest conversion requires GPU.")
        print("    CPU/no-GPU mode is debug-only. If this is unintended, run:")
        print("    set MINERU_REQUIRE_GPU=true        (cmd)")
        print("    $env:MINERU_REQUIRE_GPU='true'     (PowerShell)")
        if config.allow_cpu:
            print("    MINERU_ALLOW_CPU=true is active; unset it for formal ingestion.")
    if config.runner.value != "cli":
        print(f"  ** NOTE: MINERU_RUNNER={config.runner.value} (not cli)")
    if not config.cuda_path:
        print("  ** WARNING: CUDA_PATH is not set. MinerU may not find CUDA libraries.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MinerU 单 PDF 性能基准测试（不修改文献库）"
    )
    parser.add_argument("pdf_path", type=Path, help="待测试 PDF 路径")
    parser.add_argument("--backend", default=MINERU_BACKEND,
                        choices=["hybrid-engine", "pipeline", "vlm-engine"])
    parser.add_argument("--method", default=MINERU_METHOD,
                        choices=["auto", "ocr", "txt"])
    parser.add_argument("--effort", default=MINERU_EFFORT,
                        choices=["medium", "high"])
    parser.add_argument("--lang", default=MINERU_LANG)
    parser.add_argument("--repeat", type=int, default=1,
                        help="重复转换次数（默认 1）")
    parser.add_argument("--keep-output", action="store_true",
                        help="保留临时输出目录")
    parser.add_argument("--force", action="store_true",
                        help="即使 GPU busy 也继续 benchmark")
    parser.add_argument("--api-url", default=None,
                        help="mineru-api 地址（启用 cli_api_proxy 模式）")
    args = parser.parse_args()

    pdf_path = args.pdf_path
    if not pdf_path.exists():
        logger.error(f"PDF 不存在: {pdf_path}")
        return 1

    # ── Runtime 信息 ──
    config = runtime_config_from_env()
    gpu_health = preflight_gpu()
    torch_health = preflight_torch_cuda()
    print("=" * 60)
    print("MinerU 性能基准测试")
    print("=" * 60)
    print(f"  PDF:          {pdf_path} ({pdf_path.stat().st_size / 1024:.0f} KB)")
    print(f"  MINERU_RUNNER:        {os.environ.get('MINERU_RUNNER', '(not set)')}")
    print(f"  MINERU_REQUIRE_GPU:   {os.environ.get('MINERU_REQUIRE_GPU', '(not set)')}")
    print(f"  MINERU_ALLOW_CPU:     {os.environ.get('MINERU_ALLOW_CPU', '(not set)')}")
    print(f"  MINERU_API_URL:       {os.environ.get('MINERU_API_URL', '(not set)')}")
    print(f"  CUDA_PATH:            {os.environ.get('CUDA_PATH') or '(not set, default=' + config.cuda_path + ')'}")
    print(f"  CUDA_VISIBLE_DEVICES: {os.environ.get('CUDA_VISIBLE_DEVICES', '(not set)')}")
    print(f"  Runner (effective):   {config.runner.value}")
    print(f"  Require GPU:          {config.require_gpu}")
    print(f"  Allow CPU fallback:   {config.allow_cpu}")
    print(f"  Backend:      {args.backend}")
    print(f"  Method:       {args.method}")
    print(f"  Effort:       {args.effort} (仅 hybrid-engine)")
    print(f"  Lang:         {args.lang}")
    print(f"  Repeat:       {args.repeat}")
    print(f"  Preflight:    ok={gpu_health.ok} msg={gpu_health.message}")
    print(f"  nvidia-smi:   {gpu_health.nvidia_smi}")
    print(f"  torch_cuda:   ok={torch_health.ok} msg={torch_health.message}")
    print(f"  torch.version.cuda: {torch_health.torch_cuda_version or '(empty)'}")
    print(f"  torch.cuda.is_available(): {torch_health.cuda_available}")
    print(f"  torch.cuda.device_count(): {torch_health.device_count}")
    print(f"  torch.cuda.get_device_name(0): {torch_health.device_name or '(empty)'}")
    _print_env_warnings(config)
    print()

    # ── Lock 状态 ──
    lock_status = read_mineru_lock_status()
    if lock_status["locked"]:
        print(f"** MinerU Lock: HELD by PID {lock_status['owner_pid']} "
              f"(age={lock_status.get('age_seconds','?')}s)")
        if lock_status.get("stale"):
            print("  Lock is stale — will be auto-cleared.")
            clear_stale_mineru_lock()
        else:
            print(f"  command: {lock_status.get('command','?')}")
            if not args.force:
                print("  Use --force to continue anyway, or stop the other conversion first.")
                print(f"  Run: python scripts/check_mineru_processes.py")
                return 1
    else:
        print("[OK] MinerU Lock: free")
    print()

    # ── GPU busy 检查 ──
    gpu_idle = snapshot_nvidia_smi()
    print(_format_gpu_snapshot(gpu_idle))
    is_busy, busy_reason = _check_gpu_busy(gpu_idle)
    if is_busy:
        print(f"\n** GPU IS BUSY before benchmark: {busy_reason}")
        print("  There may be an existing mineru/python/watcher process using GPU.")
        print("  Run: python scripts/check_mineru_processes.py")
        print("  Stop watcher/import/batch/mineru-api before benchmarking.")
        if not args.force:
            print("  Use --force to continue anyway.")
            return 1
        print("  --force: continuing despite GPU busy state.")
    else:
        print("[OK] GPU appears idle (memory < 2000MB, util < 20%)")
    print()

    # ── 转换（可重复） ──
    converter = MinerUConverter(log_dir="")  # 禁用重复日志写入（benchmark 自己输出）
    all_elapsed = []
    temp_dirs = []

    for run_i in range(1, args.repeat + 1):
        temp_dir = Path(tempfile.mkdtemp(prefix=f"bench_mineru_{run_i}_"))
        temp_dirs.append(temp_dir)

        print(f"[{run_i}/{args.repeat}] 转换中...", end=" ", flush=True)
        t_start = time.time()
        result = converter.convert(
            pdf_path, temp_dir,
            backend=args.backend, method=args.method,
            lang=args.lang, effort=args.effort,
            paper_id=f"bench_{pdf_path.stem}",
            api_url=args.api_url,
        )
        elapsed = time.time() - t_start
        all_elapsed.append(elapsed)

        if result["success"]:
            md_chars = len(result.get("markdown", ""))
            images_dir = Path(result.get("output_dir", "")) / "images"
            img_count = len(list(images_dir.glob("*"))) if images_dir.exists() else 0
            print(f"OK  elapsed={elapsed:.1f}s  md_chars={md_chars}  images={img_count}")
        else:
            print(f"FAIL  elapsed={elapsed:.1f}s  error={result.get('error', 'unknown')[:120]}")

    # ── 最终 GPU 状态 ──
    print()
    gpu_final = snapshot_nvidia_smi()
    print(_format_gpu_snapshot(gpu_final))
    print()

    # ── 统计 ──
    if all_elapsed:
        sorted_times = sorted(all_elapsed)
        n = len(sorted_times)
        total = sum(sorted_times)
        avg = total / n
        median = sorted_times[n // 2]
        min_t = sorted_times[0]
        max_t = sorted_times[-1]

        print("=" * 60)
        print("Timing Summary")
        print("=" * 60)
        print(f"  Runs:       {n}")
        print(f"  Total:      {total:.1f}s")
        print(f"  Mean:       {avg:.1f}s")
        print(f"  Median:     {median:.1f}s")
        print(f"  Min:        {min_t:.1f}s")
        print(f"  Max:        {max_t:.1f}s")
        if n > 1:
            print(f"  Range:      {max_t - min_t:.1f}s")
        if pdf_path.stat().st_size > 0:
            print(f"  Per-MB:     {avg / (pdf_path.stat().st_size / 1e6):.1f}s/MB")
        print()

        # GPU delta 分析
        if gpu_idle.get("available") and gpu_final.get("available"):
            idle_gpus = {g["name"]: g for g in gpu_idle.get("gpus", [])}
            final_gpus = {g["name"]: g for g in gpu_final.get("gpus", [])}
            deltas = []
            for name, ig in idle_gpus.items():
                fg = final_gpus.get(name, {})
                d_mem = fg.get("memory_used_mb", 0) - ig.get("memory_used_mb", 0)
                d_gpu = fg.get("gpu_util_pct", 0) - ig.get("gpu_util_pct", 0)
                deltas.append({
                    "name": name,
                    "delta_mem_mb": d_mem,
                    "delta_gpu_util_pct": d_gpu,
                })
            if deltas:
                print("GPU Delta (final - idle):")
                for d in deltas:
                    print(f"  {d['name']}: mem Δ={d['delta_mem_mb']:+d}MB  "
                          f"gpu_util Δ={d['delta_gpu_util_pct']:+d}%")
                print()

    # ── 清理 ──
    if args.keep_output:
        print(f"输出保留在: {[str(d) for d in temp_dirs]}")
    else:
        for d in temp_dirs:
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass

    # ── 写出 JSON 摘要 ──
    from config.settings import MINERU_LOG_DIR
    summary_path = MINERU_LOG_DIR / "benchmark_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "pdf_path": str(pdf_path),
        "file_size": pdf_path.stat().st_size,
        "config": describe_runtime(config),
        "torch_cuda": {
            "ok": torch_health.ok,
            "message": torch_health.message,
            "torch_version": torch_health.torch_version,
            "torch_cuda_version": torch_health.torch_cuda_version,
            "cuda_available": torch_health.cuda_available,
            "device_count": torch_health.device_count,
            "device_name": torch_health.device_name,
        },
        "backend": args.backend,
        "method": args.method,
        "effort": args.effort,
        "lang": args.lang,
        "repeat": args.repeat,
        "gpu_idle": gpu_idle,
        "gpu_final": gpu_final,
        "elapsed_all": all_elapsed,
        "elapsed_mean": sum(all_elapsed) / len(all_elapsed) if all_elapsed else 0,
    }
    tmp_path = summary_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(summary_path)
    print(f"Benchmark summary written to: {summary_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

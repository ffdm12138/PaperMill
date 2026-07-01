"""Batch convert v2 data/paper_raw sources with guarded MinerU input paths."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

from config.settings import PAPER_RAW_DIR
from config.settings import MINERU_BACKEND, MINERU_EFFORT, MINERU_METHOD
from src.services.v2_library import PaperRawConverter


_WRAPPER_ENV = "MINERU_GPU_WRAPPER_ACTIVE"


def _source_ids(root: Path, args) -> list[str]:
    if args.source_id:
        return [args.source_id]
    if args.source_ids:
        return args.source_ids
    if args.all:
        return sorted(p.name for p in root.iterdir() if p.is_dir() and p.name.isdigit() and len(p.name) == 6)
    raise ValueError("--source-id, --source-ids, or --all is required")


def _preflight_status(root: Path, source_id: str) -> str:
    path = root / source_id / ".import_status.json"
    if not path.exists():
        return ""
    try:
        return str((json.loads(path.read_text(encoding="utf-8")) or {}).get("status") or "")
    except Exception:
        return ""


def _health_to_dict(health) -> dict:
    if hasattr(health, "__dataclass_fields__"):
        return asdict(health)
    return dict(getattr(health, "__dict__", {}) or {})


def _runtime_snapshot(cfg) -> dict:
    from src.mineru_runtime import MinerURunner, preflight_gpu, preflight_mineru_api, preflight_torch_cuda

    gpu_health = preflight_gpu()
    torch_health = preflight_torch_cuda()
    api_health = None
    if cfg.runner == MinerURunner.CLI_API_PROXY:
        api_health = preflight_mineru_api(cfg.api_url)
    return {
        "runner": cfg.runner.value,
        "api_url": cfg.api_url,
        "backend": MINERU_BACKEND,
        "method": MINERU_METHOD,
        "effort": MINERU_EFFORT,
        "require_gpu": cfg.require_gpu,
        "allow_cpu": cfg.allow_cpu,
        "cuda_visible_devices": cfg.cuda_visible_devices,
        "nvidia_smi": {
            "ok": bool(getattr(gpu_health, "nvidia_smi", False)),
            "message": getattr(gpu_health, "message", ""),
        },
        "torch_cuda": _health_to_dict(torch_health),
        "mineru_api": _health_to_dict(api_health) if api_health else None,
    }


def _runtime_failure(runtime: dict) -> str:
    failures = []
    if runtime.get("require_gpu"):
        nvidia = runtime.get("nvidia_smi") or {}
        torch_cuda = runtime.get("torch_cuda") or {}
        if not nvidia.get("ok"):
            failures.append(f"GPU preflight failed: {nvidia.get('message') or 'nvidia-smi unavailable'}")
        if not torch_cuda.get("ok"):
            failures.append(f"Torch CUDA preflight failed: {torch_cuda.get('message') or 'torch CUDA unavailable'}")
    if runtime.get("runner") == "cli_api_proxy":
        api = runtime.get("mineru_api") or {}
        if not api.get("api_available"):
            failures.append(
                f"mineru-api unavailable at {runtime.get('api_url')}: "
                f"{api.get('message') or 'health check failed'}"
            )
    return "; ".join(failures)


def _print_direct_call_warning() -> None:
    if os.environ.get(_WRAPPER_ENV) == "1":
        return
    print(
        "WARNING: Direct convert_paper_raw_batch.py call detected.\n"
        "Formal ingest SOP is scripts/convert_paper_raw_gpu.py, which enforces "
        "CUDA_VISIBLE_DEVICES=0 and torch.cuda preflight.\n"
        "Continuing because batch script also enforces formal GPU defaults unless "
        "--allow-cpu is explicit.",
        file=sys.stderr,
    )


def _print_runtime_summary(runtime: dict) -> None:
    torch_cuda = runtime.get("torch_cuda") or {}
    cuda_devices = runtime.get("cuda_visible_devices") or "unset"
    print(
        "MinerU runtime:\n"
        f"  runner: {runtime['runner']}\n"
        f"  backend: {runtime['backend']}\n"
        f"  method: {runtime['method']}\n"
        f"  effort: {runtime['effort']}\n"
        f"  require_gpu: {str(runtime['require_gpu']).lower()}\n"
        f"  allow_cpu: {str(runtime['allow_cpu']).lower()}\n"
        f"  cuda_visible_devices: {cuda_devices}\n"
        f"  nvidia_smi: {'ok' if (runtime.get('nvidia_smi') or {}).get('ok') else 'failed'}\n"
        f"  torch_cuda: {'ok' if torch_cuda.get('ok') else 'failed'}\n"
        f"  torch_version: {torch_cuda.get('torch_version') or ''}\n"
        f"  torch_cuda_version: {torch_cuda.get('torch_cuda_version') or ''}\n"
        f"  torch_device_count: {torch_cuda.get('device_count')}\n"
        f"  torch_device_name: {torch_cuda.get('device_name') or ''}",
        file=sys.stderr,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert v2 paper_raw PDFs into md/images.")
    parser.add_argument("--source-id", default=None)
    parser.add_argument("--source-ids", nargs="+", default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-cpu", action="store_true",
                        help="debug only: set MINERU_ALLOW_CPU=true for this process")
    parser.add_argument("--allow-cold-cli-batch", action="store_true",
                        help="debug/benchmark only: allow MINERU_RUNNER=cli to cold-start MinerU once per source")
    parser.add_argument("--force-reconvert", action="store_true",
                        help="explicitly delete existing converted md/images/output and rerun MinerU")
    parser.add_argument("--no-skip-existing", action="store_true",
                        help="debug only: do not skip existing converted paper_raw folders")
    parser.add_argument("--only-preflight-ready", action="store_true",
                        help="only convert paper_raw folders whose .import_status.json status is ready_for_convert")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    _print_direct_call_warning()

    if args.allow_cpu:
        os.environ["MINERU_ALLOW_CPU"] = "true"
        os.environ["MINERU_REQUIRE_GPU"] = "false"
        print(
            "WARNING: --allow-cpu enables debug-only CPU/no-GPU fallback. "
            "This is not formal MinerU ingest SOP.",
            file=sys.stderr,
        )

    write = args.apply and not args.dry_run
    source_ids = _source_ids(args.paper_raw_dir, args)

    from src.mineru_runtime import runtime_config_from_env, MinerURunner
    cfg = runtime_config_from_env()
    converter = PaperRawConverter(args.paper_raw_dir)
    skip_existing = not args.no_skip_existing
    inspections = {
        source_id: converter.inspect_conversion(
            source_id,
            backend=MINERU_BACKEND,
            method=MINERU_METHOD,
            effort=MINERU_EFFORT,
        )
        for source_id in source_ids
    }
    source_ids_needing_runtime = [
        source_id for source_id in source_ids
        if args.force_reconvert
        or not (
            skip_existing
            and inspections[source_id]["state"] in {"converted_current", "converted_legacy"}
        )
    ]
    runtime = None
    runtime_failure = ""
    if source_ids_needing_runtime:
        runtime = _runtime_snapshot(cfg)
        _print_runtime_summary(runtime)
        runtime_failure = _runtime_failure(runtime)
    if write and source_ids_needing_runtime and not cfg.require_gpu:
        if not args.allow_cpu:
            print(
                "ERROR: formal conversion requires GPU. CPU/no-GPU fallback must be explicit via "
                "--allow-cpu; --allow-cpu is debug-only and not formal ingest SOP.",
                file=sys.stderr,
            )
            return 2

    if write and len(source_ids_needing_runtime) > 1 and cfg.runner == MinerURunner.CLI and not args.allow_cold_cli_batch:
        print(
            "ERROR: formal batch conversion cannot use MINERU_RUNNER=cli for multiple sources, "
            "because it may cold-start MinerU once per PDF. Start persistent mineru-api with "
            "start_fast_api_mode.bat, then use MINERU_RUNNER=cli_api_proxy and "
            "MINERU_API_URL=http://127.0.0.1:8000. "
            "For explicit debugging only, pass --allow-cold-cli-batch.",
            file=sys.stderr,
        )
        return 2

    # warn when dry-running >1 PDF with cold-start CLI runner
    if len(source_ids_needing_runtime) > 1:
        try:
            if cfg.runner == MinerURunner.CLI:
                print(
                    f"  ** WARNING: Batch conversion is using MINERU_RUNNER=cli on"
                    f" {len(source_ids)} sources; this may cold-start MinerU per PDF."
                    f" Prefer MINERU_RUNNER=cli_api_proxy with a persistent mineru-api"
                    f" service for large batches.",
                    file=sys.stderr,
                )
        except Exception:
            pass  # never let a warning break conversion

    report = []
    for source_id in source_ids:
        inspection = inspections[source_id]
        item = {
            "source_id": source_id,
            "status": "planned",
            "runtime": runtime,
            "conversion_state": inspection["state"],
        }
        if args.only_preflight_ready:
            preflight_status = _preflight_status(args.paper_raw_dir, source_id)
            item["preflight_status"] = preflight_status
            if preflight_status != "ready_for_convert":
                item["status"] = "skipped"
                item["reason"] = "preflight status is not ready_for_convert"
                report.append(item)
                continue
        if skip_existing and not args.force_reconvert and inspection["state"] in {"converted_current", "converted_legacy"}:
            item["status"] = "skipped"
            item["reason"] = inspection["reason"]
            item["markdown"] = inspection["markdown"]
            item["images_dir"] = inspection["images_dir"]
            report.append(item)
            continue
        if write and runtime_failure:
            item["status"] = "failed"
            item["error"] = runtime_failure
            report.append(item)
            continue
        logger.info("{} convert paper_raw/{}", "CONVERT" if write else "DRY-RUN", source_id)
        if write:
            try:
                result = converter.convert(
                    source_id,
                    force_reconvert=args.force_reconvert,
                    skip_existing=skip_existing,
                )
                item.update(result)
                if result.get("skipped"):
                    item["status"] = "skipped"
                elif result.get("success"):
                    item["status"] = "converted"
                else:
                    item["status"] = "failed"
            except Exception as exc:
                item.update({"status": "failed", "error": str(exc)})
                logger.error("convert failed for {}: {}", source_id, exc)
        report.append(item)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"applied": write, "runtime": runtime, "items": report}, ensure_ascii=False, indent=2))
    return 1 if any(i["status"] == "failed" for i in report) else 0


if __name__ == "__main__":
    raise SystemExit(main())

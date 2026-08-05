"""Batch convert v2 data/paper_raw sources with guarded MinerU input paths."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts import _bootstrap  # noqa: F401  (runtime init: dirs/validate/logging)

from loguru import logger

from config.settings import PAPER_RAW_DIR
from config.settings import MINERU_BACKEND, MINERU_EFFORT, MINERU_METHOD
from src.utils.timestamps import utc_now_iso
from src.utils.identifiers import validate_paper_raw_id
from src.ingest import conversion_gates as gates
from src.ingest.paper_raw import PaperRawConverter


_WRAPPER_ENV = "MINERU_GPU_WRAPPER_ACTIVE"


def _source_ids(root: Path, args) -> list[str]:
    if args.paper_number:
        return [validate_paper_raw_id(args.paper_number)]
    if args.paper_numbers:
        return [validate_paper_raw_id(x) for x in args.paper_numbers]
    if args.all:
        return sorted(p.name for p in root.iterdir() if p.is_dir() and p.name.isdigit() and len(p.name) == 16)
    raise ValueError("--paper-number, --paper-numbers, or --all is required")


def _health_to_dict(health) -> dict:
    if hasattr(health, "__dataclass_fields__"):
        return asdict(health)
    return dict(getattr(health, "__dict__", {}) or {})


def _now_iso() -> str:
    return utc_now_iso()


def _api_task_delta(before: dict | None, after: dict | None, key: str) -> int:
    try:
        return int((after or {}).get(key) or 0) - int((before or {}).get(key) or 0)
    except Exception:
        return 0


def _finish_item(item: dict, started: float, api_before: dict | None, api_after: dict | None) -> None:
    item["finished_at"] = _now_iso()
    item["duration_seconds"] = round(max(0.0, time.time() - started), 3)
    item["api_health_before_item"] = api_before
    item["api_health_after_item"] = api_after
    item["api_completed_delta"] = _api_task_delta(api_before, api_after, "completed_tasks")
    item["api_failed_delta"] = _api_task_delta(api_before, api_after, "failed_tasks")


def _default_lock_fields() -> dict:
    return {
        "lock_wait_seconds": 0,
        "lock_owner_pid": None,
        "lock_owner_paper_number": "",
        "lock_age_seconds": None,
        "lock_owner_live": None,
        "lock_wait_warning": "",
    }


def _lock_fields_after_wait(started: float, stuck_warn_seconds: int | None) -> dict:
    try:
        from src.mineru.lock import read_mineru_lock_status

        status = read_mineru_lock_status(stuck_warn_seconds=stuck_warn_seconds)
    except Exception as exc:
        fields = _default_lock_fields()
        fields["lock_wait_warning"] = f"lock status unavailable: {exc}"
        return fields
    fields = _default_lock_fields()
    fields.update({
        "lock_wait_seconds": round(max(0.0, time.time() - started), 3),
        "lock_owner_pid": status.get("owner_pid"),
        "lock_owner_paper_number": status.get("paper_number") or "",
        "lock_age_seconds": status.get("age_seconds"),
        "lock_owner_live": status.get("owner_live"),
    })
    verdict = status.get("verdict") or ""
    if verdict:
        fields["lock_wait_warning"] = verdict
    return fields


def _summarize(items: list[dict]) -> dict:
    return {
        "planned": len(items),
        "converted": sum(1 for i in items if i.get("status") == "converted"),
        "restored_from_output_cache": sum(1 for i in items if i.get("status") == "restored_from_output_cache"),
        "skipped": sum(1 for i in items if i.get("status") == "skipped"),
        "failed": sum(1 for i in items if i.get("status") == "failed"),
        "api_completed_delta": sum(int(i.get("api_completed_delta") or 0) for i in items),
        "api_failed_delta": sum(int(i.get("api_failed_delta") or 0) for i in items),
    }


def _runtime_snapshot(cfg) -> dict:
    from src.mineru.runtime import MinerURunner, preflight_gpu, preflight_mineru_api, preflight_torch_cuda

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


def _parse_args() -> argparse.Namespace:
    """Build the batch-converter CLI and validate cross-flag constraints."""
    parser = argparse.ArgumentParser(description="Convert v2 paper_raw PDFs into md/images.")
    parser.add_argument("--paper-number", default=None)
    parser.add_argument("--paper-numbers", nargs="+", default=None)
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
    parser.add_argument("--ignore-output-cache", action="store_true",
                        help="do not reuse verified MinerU output cache; run MinerU if conversion is needed")
    parser.add_argument("--output-cache-dir", type=Path, default=None,
                        help="override MinerU output cache directory for this run")
    parser.add_argument("--cache-only", action="store_true",
                        help="restore from output cache only; never run MinerU")
    parser.add_argument("--only-convertible", action="store_true",
                        help="only convert workspaces with a PDF, metadata.json shell, and a status that is safe for conversion; "
                             "this includes unmatched/incomplete metadata bootstrap statuses (doi_invalid, "
                             "metadata_resolve_failed, etc.) but still requires the metadata shell. "
                             "Formalize/commit-stage and duplicate-parked workspaces are skipped.")
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--smoke-report", type=Path, default=None,
                        help="lower-level debug only: warn if this smoke report is missing/invalid during --all --apply")
    parser.add_argument("--lock-wait-timeout-seconds", type=int, default=None,
                        help="maximum time to wait for mineru_convert.lock before failing the current item")
    parser.add_argument("--lock-stuck-warn-seconds", type=int, default=None,
                        help="age threshold for reporting a live MinerU conversion lock as stuck-suspected")
    args = parser.parse_args()
    if args.cache_only and args.ignore_output_cache:
        parser.error("--cache-only cannot be combined with --ignore-output-cache")
    if args.cache_only and args.force_reconvert:
        parser.error("--cache-only cannot be combined with --force-reconvert")
    return args


def _apply_env_flags(args: argparse.Namespace) -> None:
    """Emit the direct-call warning and apply debug env overrides."""
    _print_direct_call_warning()
    if args.lock_wait_timeout_seconds is not None:
        os.environ["MINERU_LOCK_WAIT_TIMEOUT_SECONDS"] = str(args.lock_wait_timeout_seconds)
    if args.lock_stuck_warn_seconds is not None:
        os.environ["MINERU_LOCK_STUCK_WARN_SECONDS"] = str(args.lock_stuck_warn_seconds)

    if args.allow_cpu:
        os.environ["MINERU_ALLOW_CPU"] = "true"
        os.environ["MINERU_REQUIRE_GPU"] = "false"
        print(
            "WARNING: --allow-cpu enables debug-only CPU/no-GPU fallback. "
            "This is not formal MinerU ingest SOP.",
            file=sys.stderr,
        )


def _warn_missing_smoke(args: argparse.Namespace, write: bool) -> None:
    """Warn when a low-level --all --apply run has no valid smoke report."""
    if not (write and args.all):
        return
    try:
        from src.mineru.smoke import validate_smoke_report

        smoke = validate_smoke_report(args.smoke_report)
        if not smoke.get("ok"):
            print(
                "WARNING: lower-level convert_paper_raw_batch.py is running --all --apply "
                "without a valid smoke report; formal entrypoints hard-fail this case.",
                file=sys.stderr,
            )
            for error in smoke.get("errors") or []:
                print(f"WARNING: smoke: {error}", file=sys.stderr)
    except Exception as exc:
        print(f"WARNING: smoke report check failed: {exc}", file=sys.stderr)


def _build_converter(args: argparse.Namespace) -> PaperRawConverter:
    """Construct the converter with the run's output-cache configuration."""
    converter = PaperRawConverter(args.paper_raw_dir)
    if args.output_cache_dir:
        from src.ingest.mineru_output_cache import MinerUOutputCache

        converter.output_cache = MinerUOutputCache(args.output_cache_dir, cleaner=converter.cleaner)
    if args.ignore_output_cache:
        converter.reuse_output_cache = False
    return converter


def _plan_batch(
    args: argparse.Namespace,
    converter: PaperRawConverter,
    source_ids: list[str],
    skip_existing: bool,
) -> tuple[dict, dict, dict, list[str]]:
    """Inspect every source and decide which items need a live MinerU runtime."""
    inspections = {
        source_id: converter.inspect_conversion(
            source_id,
            backend=MINERU_BACKEND,
            method=MINERU_METHOD,
            effort=MINERU_EFFORT,
        )
        for source_id in source_ids
    }
    asset_gates = {
        source_id: gates.conversion_asset_gate(args.paper_raw_dir, source_id)
        for source_id in source_ids
    }
    cache_inspections = {}
    for source_id in source_ids:
        if not asset_gates[source_id][0]:
            cache_inspections[source_id] = {
                "hit": False,
                "output_cache_enabled": not args.ignore_output_cache,
                "output_cache_state": "miss",
                "output_cache_reason": asset_gates[source_id][1],
                "output_cache_dir": "",
                "output_cache_manifest": "",
            }
            continue
        if skip_existing and not args.force_reconvert and inspections[source_id]["state"] == "converted_current":
            cache_inspections[source_id] = {
                "hit": False,
                "output_cache_enabled": not args.ignore_output_cache,
                "output_cache_state": "bypassed",
                "output_cache_reason": "already converted_current",
                "output_cache_dir": "",
                "output_cache_manifest": "",
            }
            continue
        inspect_cache = getattr(converter, "inspect_output_cache", None)
        if inspect_cache is None:
            cache_inspections[source_id] = {
                "hit": False,
                "output_cache_enabled": False,
                "output_cache_state": "disabled",
                "output_cache_reason": "converter has no output cache inspector",
                "output_cache_dir": "",
                "output_cache_manifest": "",
            }
        else:
            cache_inspections[source_id] = inspect_cache(
                source_id,
                backend=MINERU_BACKEND,
                method=MINERU_METHOD,
                effort=MINERU_EFFORT,
                force_reconvert=args.force_reconvert,
                reuse_output_cache=not args.ignore_output_cache,
            )
    source_ids_needing_runtime = [
        source_id for source_id in source_ids
        if asset_gates[source_id][0]
        and not args.cache_only
        and not cache_inspections[source_id].get("hit")
        and (
            args.force_reconvert
            or not (
                skip_existing
                and inspections[source_id]["state"] == "converted_current"
            )
        )
    ]
    return inspections, asset_gates, cache_inspections, source_ids_needing_runtime


def _gate_runtime(
    args: argparse.Namespace,
    cfg,
    write: bool,
    source_ids: list[str],
    source_ids_needing_runtime: list[str],
) -> tuple[dict | None, str, dict | None, int | None]:
    """Snapshot/print the runtime and enforce the formal-GPU/cold-CLI gates."""
    from src.mineru.runtime import MinerURunner

    runtime = None
    runtime_failure = ""
    api_health_before = None
    if source_ids_needing_runtime:
        runtime = _runtime_snapshot(cfg)
        _print_runtime_summary(runtime)
        runtime_failure = _runtime_failure(runtime)
        # Structured API health (task counts) for failure diagnostics. Only the
        # cli_api_proxy runner talks to a persistent mineru-api worth snapshotting.
        if cfg.runner == MinerURunner.CLI_API_PROXY:
            try:
                from src.mineru.runtime import snapshot_mineru_api
                api_health_before = snapshot_mineru_api(cfg.api_url)
            except Exception as exc:
                api_health_before = {"api_available": False, "error": str(exc)}
    if write and source_ids_needing_runtime and not cfg.require_gpu:
        if not args.allow_cpu:
            print(
                "ERROR: formal conversion requires GPU. CPU/no-GPU fallback must be explicit via "
                "--allow-cpu; --allow-cpu is debug-only and not formal ingest SOP.",
                file=sys.stderr,
            )
            return runtime, runtime_failure, api_health_before, 2

    if write and len(source_ids_needing_runtime) > 1 and cfg.runner == MinerURunner.CLI and not args.allow_cold_cli_batch:
        print(
            "ERROR: formal batch conversion cannot use MINERU_RUNNER=cli for multiple sources, "
            "because it may cold-start MinerU once per PDF. Start persistent mineru-api with "
            "start_fast_api_mode.bat, then use MINERU_RUNNER=cli_api_proxy and "
            "MINERU_API_URL=http://127.0.0.1:8000. "
            "For explicit debugging only, pass --allow-cold-cli-batch.",
            file=sys.stderr,
        )
        return runtime, runtime_failure, api_health_before, 2

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
    return runtime, runtime_failure, api_health_before, None


def _convert_one(
    source_id: str,
    *,
    args: argparse.Namespace,
    cfg,
    write: bool,
    skip_existing: bool,
    converter: PaperRawConverter,
    inspections: dict,
    asset_gates: dict,
    cache_inspections: dict,
    source_ids_needing_runtime: list[str],
    runtime: dict | None,
    runtime_failure: str,
    api_health_before: dict | None,
) -> dict:
    """Run (or plan) the conversion of one source and return its report item."""
    from src.mineru.runtime import MinerURunner

    item_started = time.time()
    api_before_item = None
    api_after_item = None
    inspection = inspections[source_id]
    item = {
        "paper_number": source_id,
        "paper_raw_id": source_id,
        "status": "planned",
        "stage": "inspect",
        "started_at": _now_iso(),
        "runtime": runtime,
        "conversion_state": inspection["state"],
    }
    cache_item = cache_inspections.get(source_id, {})
    item.update({
        "output_cache_enabled": cache_item.get("output_cache_enabled", not args.ignore_output_cache),
        "output_cache_state": cache_item.get("output_cache_state", ""),
        "output_cache_reason": cache_item.get("output_cache_reason", ""),
        "output_cache_dir": cache_item.get("output_cache_dir", ""),
        "output_cache_manifest": cache_item.get("output_cache_manifest", ""),
        "restored_from_output_cache": False,
    })
    item.update(_default_lock_fields())
    item.update(gates.metadata_fields_for_report(args.paper_raw_dir, source_id))
    asset_ok, asset_reason, has_pdf, has_metadata_shell = asset_gates[source_id]
    item["has_pdf"] = has_pdf
    item["has_metadata_shell"] = has_metadata_shell
    if args.only_convertible:
        preflight_status = gates.preflight_status(args.paper_raw_dir, source_id)
        item["preflight_status"] = preflight_status
        if not asset_ok:
            item["status"] = "skipped"
            item["stage"] = "skip"
            item["reason"] = asset_reason
            _finish_item(item, item_started, api_before_item, api_before_item)
            return item
        if preflight_status in gates.CONVERSION_BLOCKED_STATUSES:
            item["status"] = "skipped"
            item["stage"] = "skip"
            item["reason"] = "formalization/commit-stage workspace is not convertible"
            _finish_item(item, item_started, api_before_item, api_before_item)
            return item
    if not asset_ok:
        item["status"] = "failed" if write else "skipped"
        item["stage"] = "failed" if write else "skip"
        item["reason"] = asset_reason
        if write:
            item["error"] = asset_reason
        _finish_item(item, item_started, api_before_item, api_before_item)
        return item
    if skip_existing and not args.force_reconvert and inspection["state"] == "converted_current":
        item["status"] = "skipped"
        item["stage"] = "skip"
        item["reason"] = inspection["reason"]
        item["markdown"] = inspection["markdown"]
        item["images_dir"] = inspection["images_dir"]
        _finish_item(item, item_started, api_before_item, api_before_item)
        return item
    if cfg.runner == MinerURunner.CLI_API_PROXY and source_id in source_ids_needing_runtime:
        try:
            from src.mineru.runtime import snapshot_mineru_api
            api_before_item = snapshot_mineru_api(cfg.api_url)
        except Exception as exc:
            api_before_item = {"api_available": False, "error": str(exc)}
    if write and runtime_failure:
        item["status"] = "failed"
        item["stage"] = "failed"
        item["error"] = runtime_failure
        item["api_health_before"] = api_health_before
        _finish_item(item, item_started, api_before_item, api_before_item)
        return item
    logger.info("{} convert paper_raw/{}", "CONVERT" if write else "DRY-RUN", source_id)
    if write:
        item["stage"] = "submit"
        try:
            result = converter.convert(
                source_id,
                force_reconvert=args.force_reconvert,
                skip_existing=skip_existing,
                cache_only=args.cache_only,
            )
            item.update(result)
            if result.get("skipped"):
                item["status"] = "skipped"
                item["stage"] = "skip"
            elif result.get("restored_from_output_cache"):
                item["status"] = "restored_from_output_cache"
                item["stage"] = "done"
            elif result.get("success"):
                item["status"] = "converted"
                item["stage"] = "done"
                # Conversion is metadata-independent, but when a citation
                # record already exists this is the first point at which
                # independent Markdown identity evidence is available.
                # Conversion writes the identity receipt only; the freeze
                # is a separate phase.
                try:
                    from src.metadata.pdf_identity import extract_pdf_identity_evidence
                    from src.metadata.pdf_match import (
                        RECEIPT_STATUS_TO_METADATA_STATE,
                        build_match_receipt,
                        write_match_receipt,
                    )
                    from src.metadata.freeze import assert_metadata_frozen
                    from src.ingest.status import update_status
                    from src.ingest.workspace import PaperRawWorkspace
                    from src.ingest.locking import (
                        assert_no_active_identity_migration,
                        paper_raw_write_lock,
                    )
                    folder = args.paper_raw_dir / source_id
                    metadata_path = folder / f"{source_id}.metadata.json"
                    pdf_path = folder / f"{source_id}.pdf"
                    if metadata_path.exists() and pdf_path.exists():
                        assert_no_active_identity_migration(args.paper_raw_dir)
                        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                        freeze_path=folder/f"{source_id}.metadata_freeze.json"
                        if freeze_path.exists():
                            assert_metadata_frozen(folder,source_id)
                            item["metadata_frozen"]=True
                        else:
                            evidence = extract_pdf_identity_evidence(
                                pdf_path=pdf_path,
                                markdown_path=folder / f"{source_id}.md",
                                conversion_manifest_path=folder / f"{source_id}.conversion.json",
                            )
                            provider_record=str((metadata.get("source") or {}).get("raw_record_path") or "")
                            receipt = build_match_receipt(folder, source_id, metadata, evidence,
                                                          requested_doi=((metadata.get("identifiers") or {}).get("doi") or ""),provider_records=[provider_record] if provider_record else [])
                            with paper_raw_write_lock(args.paper_raw_dir):
                                assert_no_active_identity_migration(args.paper_raw_dir)
                                write_match_receipt(folder, receipt)
                                state = RECEIPT_STATUS_TO_METADATA_STATE.get(
                                    receipt["match_status"], "resolved"
                                )
                                update_status(
                                    PaperRawWorkspace.from_path(folder),
                                    "metadata",
                                    state,
                                    match_method=receipt["match_method"],
                                    match_status=receipt["match_status"],
                                )
                            item["metadata_match_method"] = receipt["match_method"]
                            item["metadata_match_status"] = receipt["match_status"]
                except Exception as exc:
                    item.setdefault("warnings", []).append(f"metadata match deferred: {exc}")
            else:
                item["status"] = "failed"
                item["stage"] = "failed"
        except Exception as exc:
            item.update({"status": "failed", "stage": "failed", "error": str(exc)})
            logger.error("convert failed for {}: {}", source_id, exc)
        if item["status"] == "failed":
            item.update(_lock_fields_after_wait(item_started, args.lock_stuck_warn_seconds))
        # On failure, capture API health so the report explains WHY (e.g.
        # API healthy but task failed) rather than leaving it in stderr.
        if item["status"] == "failed" and api_health_before is not None:
            item["api_health_before"] = api_health_before
            try:
                from src.mineru.runtime import snapshot_mineru_api
                item["api_health_after"] = snapshot_mineru_api(cfg.api_url)
            except Exception as exc:
                item["api_health_after"] = {"api_available": False, "error": str(exc)}
        if cfg.runner == MinerURunner.CLI_API_PROXY and source_id in source_ids_needing_runtime:
            try:
                from src.mineru.runtime import snapshot_mineru_api
                api_after_item = snapshot_mineru_api(cfg.api_url)
            except Exception as exc:
                api_after_item = {"api_available": False, "error": str(exc)}
    else:
        api_after_item = api_before_item
    _finish_item(item, item_started, api_before_item, api_after_item)
    return item


def _emit_report(
    args: argparse.Namespace,
    cfg,
    write: bool,
    report: list[dict],
    source_ids_needing_runtime: list[str],
    runtime: dict | None,
    api_health_before: dict | None,
) -> int:
    """Collect post-batch API health, persist/print the payload, return exit code."""
    from src.mineru.runtime import MinerURunner

    # Post-batch API health + warning when the API is healthy but tasks failed.
    api_health_after = None
    api_warning = ""
    if cfg.runner == MinerURunner.CLI_API_PROXY and source_ids_needing_runtime:
        try:
            from src.mineru.runtime import snapshot_mineru_api, mineru_api_failed_task_warning
            api_health_after = snapshot_mineru_api(cfg.api_url)
            api_warning = mineru_api_failed_task_warning(api_health_after) or mineru_api_failed_task_warning(api_health_before)
        except Exception as exc:
            api_health_after = {"api_available": False, "error": str(exc)}
        if api_warning:
            print(f"WARNING: {api_warning}", file=sys.stderr)

    payload = {
        "applied": write,
        "summary": _summarize(report),
        "runtime": runtime,
        "api_health_before": api_health_before,
        "api_health_after": api_health_after,
        "api_warning": api_warning or None,
        "items": report,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if any(i["status"] == "failed" for i in report) else 0


def main() -> int:
    args = _parse_args()
    _apply_env_flags(args)

    from src.ingest.locking import assert_no_active_identity_migration

    assert_no_active_identity_migration(args.paper_raw_dir)
    write = args.apply and not args.dry_run
    source_ids = _source_ids(args.paper_raw_dir, args)

    from src.mineru.runtime import runtime_config_from_env
    cfg = runtime_config_from_env()
    _warn_missing_smoke(args, write)
    converter = _build_converter(args)
    skip_existing = not args.no_skip_existing
    inspections, asset_gates, cache_inspections, source_ids_needing_runtime = _plan_batch(
        args, converter, source_ids, skip_existing
    )
    runtime, runtime_failure, api_health_before, exit_code = _gate_runtime(
        args, cfg, write, source_ids, source_ids_needing_runtime
    )
    if exit_code is not None:
        return exit_code

    report = []
    for source_id in source_ids:
        report.append(_convert_one(
            source_id,
            args=args,
            cfg=cfg,
            write=write,
            skip_existing=skip_existing,
            converter=converter,
            inspections=inspections,
            asset_gates=asset_gates,
            cache_inspections=cache_inspections,
            source_ids_needing_runtime=source_ids_needing_runtime,
            runtime=runtime,
            runtime_failure=runtime_failure,
            api_health_before=api_health_before,
        ))

    return _emit_report(
        args, cfg, write, report, source_ids_needing_runtime, runtime, api_health_before
    )


if __name__ == "__main__":
    raise SystemExit(main())

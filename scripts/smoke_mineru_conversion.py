"""Run a single-paper MinerU conversion smoke test for formal batch unlock."""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import PAPER_RAW_DIR, PROJECT_ROOT
from src.mineru_runtime import (
    activate_formal_gpu_env,
    runtime_config_from_env,
    snapshot_mineru_api,
    verify_gpu_runtime,
)
from src.services.ingest_ids import validate_paper_raw_id
from src.services.v2_library import PaperRawConverter
from src.utils.atomic_io import atomic_write_json


def _parse_last_json(text: str) -> dict | None:
    try:
        return json.loads(text)
    except Exception:
        pass
    last = None
    for start in range(len(text)):
        if text[start] != "{":
            continue
        depth = 0
        for end in range(start, len(text)):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        last = json.loads(text[start:end + 1])
                    except Exception:
                        pass
                    break
    return last


def _run_convert(argv: list[str]) -> tuple[int, dict | None]:
    import scripts.convert_paper_raw_gpu as gpu

    saved = sys.argv
    sys.argv = argv
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = gpu.main()
    finally:
        sys.argv = saved
    return (rc if isinstance(rc, int) else 0), _parse_last_json(buf.getvalue())


def _item_status(convert_report: dict | None, paper_number: str) -> dict:
    for item in (convert_report or {}).get("items") or []:
        if item.get("paper_number") == paper_number or item.get("paper_raw_id") == paper_number:
            return item
    return {}


def _manifest(folder: Path, paper_number: str) -> dict:
    path = folder / f"{paper_number}.conversion.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _api_delta(before: dict | None, after: dict | None, key: str) -> int:
    try:
        return int((after or {}).get(key) or 0) - int((before or {}).get(key) or 0)
    except Exception:
        return 0


def _is_text_only_success(runtime: dict, manifest: dict, md_path: Path) -> bool:
    service = runtime.get("service_identity") or {}
    torch_cuda = runtime.get("torch_cuda") or {}
    runtime_cfg = runtime.get("runtime") or {}
    return (
        runtime.get("verdict") == "READY_FOR_CONVERSION"
        and runtime_cfg.get("require_gpu") is True
        and torch_cuda.get("cuda_available") is True
        and service.get("verdict") == "managed_ready"
        and manifest.get("status") == "converted"
        and manifest.get("runner") in {"cli_api_proxy", "api"}
        and md_path.exists()
        and md_path.stat().st_size > 0
    )


def main() -> int:
    activate_formal_gpu_env()

    parser = argparse.ArgumentParser(description="Run a single-paper MinerU conversion smoke test.")
    parser.add_argument("--paper-number", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "perform a real one-paper MinerU conversion smoke test; "
            "without this, only readiness/inspection is checked and the "
            "report cannot unlock formal batch conversion"
        ),
    )
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--report", type=Path, default=PROJECT_ROOT / "reports" / "smoke_mineru_conversion.json")
    parser.add_argument("--convert-report", type=Path, default=None)
    parser.add_argument("--api-url", default=None)
    args = parser.parse_args()

    paper_number = validate_paper_raw_id(args.paper_number)
    config = runtime_config_from_env()
    api_url = (args.api_url or config.api_url).rstrip("/")
    convert_report_path = args.convert_report or (PROJECT_ROOT / "reports" / f"convert_smoke_{paper_number}.json")
    runtime = verify_gpu_runtime(api_url)
    api_before = snapshot_mineru_api(api_url)

    # Readiness-only mode (default, no --apply): never invoke real conversion.
    # Live apply mode (--apply): may perform a real one-paper conversion.
    live_apply = bool(args.apply)
    rc = 2
    convert_report = None
    if live_apply and runtime.get("verdict") == "READY_FOR_CONVERSION":
        rc, convert_report = _run_convert([
            "convert_paper_raw_gpu.py",
            "--paper-raw-dir", str(args.paper_raw_dir),
            "--paper-number", paper_number,
            "--only-convertible",
            "--apply",
            "--skip-smoke-check",
            "--report", str(convert_report_path),
        ])
    api_after = snapshot_mineru_api(api_url)

    folder = args.paper_raw_dir / paper_number
    md_path = folder / f"{paper_number}.md"
    manifest = _manifest(folder, paper_number)
    try:
        inspection = PaperRawConverter(args.paper_raw_dir).inspect_conversion(paper_number)
    except Exception as exc:
        inspection = {"state": "inspect_failed", "reason": str(exc)}
    item = _item_status(convert_report, paper_number)

    completed_delta = _api_delta(api_before, api_after, "completed_tasks")
    failed_delta = _api_delta(api_before, api_after, "failed_tasks")
    if runtime.get("verdict") != "READY_FOR_CONVERSION":
        verdict = "SMOKE_FAILED"
    elif not live_apply:
        # Readiness-only mode: runtime is ready, no real conversion attempted.
        verdict = "SMOKE_READINESS_READY"
    elif rc != 0:
        verdict = "SMOKE_FAILED"
    elif item.get("status") == "skipped" and inspection.get("state") == "converted_current":
        verdict = "SMOKE_SKIPPED_ALREADY_CURRENT"
    elif inspection.get("state") == "converted_current" and completed_delta > 0:
        verdict = "SMOKE_CONVERTED"
    elif inspection.get("state") == "converted_current" and _is_text_only_success(runtime, manifest, md_path):
        verdict = "SMOKE_NO_GPU_ACTIVITY_BUT_CONVERTED_TEXT_ONLY"
    else:
        verdict = "SMOKE_FAILED"

    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "live_apply" if live_apply else "readiness_only",
        "applied": live_apply,
        "side_effects": live_apply,
        "paper_number": paper_number,
        "api_url": api_url,
        "runner": config.runner.value,
        "runtime_verdict": runtime.get("verdict"),
        "service_identity": runtime.get("service_identity"),
        "api_health_before": api_before,
        "api_health_after": api_after,
        "api_completed_delta": completed_delta,
        "api_failed_delta": failed_delta,
        "convert_report_path": str(convert_report_path),
        "convert_return_code": rc,
        "convert_item": item,
        "conversion_state": inspection.get("state"),
        "markdown": str(md_path),
        "manifest": str(folder / f"{paper_number}.conversion.json"),
        "verdict": verdict,
    }
    atomic_write_json(args.report, payload, indent=2)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if verdict != "SMOKE_FAILED" else 1


if __name__ == "__main__":
    raise SystemExit(main())

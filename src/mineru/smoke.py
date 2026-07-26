"""Smoke-test report helpers for formal MinerU batch conversion."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from config.settings import PROJECT_ROOT
from src.mineru.runtime import runtime_config_from_env


DEFAULT_SMOKE_REPORT = PROJECT_ROOT / "reports" / "smoke_mineru_conversion.json"
PASSING_SMOKE_VERDICTS = {
    "SMOKE_CONVERTED",
    "SMOKE_SKIPPED_ALREADY_CURRENT",
    "SMOKE_NO_GPU_ACTIVITY_BUT_CONVERTED_TEXT_ONLY",
}


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_smoke_report(
    path: str | Path | None = None,
    *,
    max_age_hours: float = 24.0,
) -> dict:
    """Validate whether a smoke report can unlock formal batch conversion."""
    report_path = Path(path) if path else DEFAULT_SMOKE_REPORT
    config = runtime_config_from_env()
    errors: list[str] = []
    data: dict = {}
    if not report_path.exists():
        return {
            "ok": False,
            "path": str(report_path),
            "errors": [f"smoke report does not exist: {report_path}"],
            "report": data,
        }
    try:
        data = json.loads(report_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return {
            "ok": False,
            "path": str(report_path),
            "errors": [f"smoke report is unreadable: {exc}"],
            "report": {},
        }

    # A report that can unlock formal batch conversion must be a live-apply
    # report (produced by ``smoke_mineru_conversion.py --apply``). Readiness-only
    # reports and legacy reports lacking mode/applied/side_effects are rejected
    # so agents cannot mistake a readiness check for a real conversion gate.
    report_mode = str(data.get("mode") or "")
    applied_flag = data.get("applied")
    side_effects_flag = data.get("side_effects")
    if report_mode != "live_apply":
        errors.append(
            "smoke report is not a live apply report "
            f"(mode={report_mode or '(missing)'}); rerun smoke_mineru_conversion.py with --apply"
        )
    if applied_flag is not True:
        errors.append("smoke report applied flag must be true for batch unlock")
    if side_effects_flag is not True:
        errors.append("smoke report side_effects flag must be true for batch unlock")

    verdict = str(data.get("verdict") or "")
    if verdict not in PASSING_SMOKE_VERDICTS:
        errors.append(f"smoke verdict is not passing: {verdict or '(empty)'}")

    created_at = _parse_time(str(data.get("created_at") or ""))
    if created_at is None:
        errors.append("smoke report missing/invalid created_at")
    else:
        age = datetime.now(timezone.utc) - created_at
        if age < timedelta(seconds=-60) or age > timedelta(hours=max_age_hours):
            errors.append(f"smoke report is outside {max_age_hours:g}h window")

    api_url = str(data.get("api_url") or "")
    if api_url.rstrip("/") != config.api_url.rstrip("/"):
        errors.append(f"smoke api_url {api_url or '(empty)'} does not match current {config.api_url}")
    runner = str(data.get("runner") or "")
    if runner != config.runner.value:
        errors.append(f"smoke runner {runner or '(empty)'} does not match current {config.runner.value}")

    runtime_verdict = str(data.get("runtime_verdict") or "")
    if runtime_verdict and runtime_verdict != "READY_FOR_CONVERSION":
        errors.append(f"smoke runtime_verdict is not READY_FOR_CONVERSION: {runtime_verdict}")
    service = data.get("service_identity") or {}
    service_verdict = str(service.get("verdict") or "")
    if service_verdict and service_verdict != "managed_ready":
        errors.append(f"smoke service identity is not managed_ready: {service_verdict}")

    return {
        "ok": not errors,
        "path": str(report_path),
        "errors": errors,
        "report": data,
    }


def smoke_required_message(path: str | Path | None = None) -> str:
    report_path = Path(path) if path else DEFAULT_SMOKE_REPORT
    return (
        "Batch conversion requires a recent successful single-paper live smoke test.\n"
        f"Expected default smoke report:\n{report_path}\n"
        "Run:\n"
        "python scripts/smoke_mineru_conversion.py --paper-number <id> "
        f"--apply --report {report_path}\n"
        "Without --apply, smoke_mineru_conversion.py only performs readiness-only "
        "diagnostics and cannot unlock formal batch conversion.\n"
        "Or pass:\n--smoke-report <path>"
    )

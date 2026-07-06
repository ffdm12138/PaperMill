"""Staged SOP: MinerU GPU conversion -> post-conversion metadata re-resolution.

This script wires the two layered steps together so operators do not have to
remember the exact flag order, while keeping the layers separate:

  1. Convert paper_raw PDFs to Markdown/images. Conversion does NOT require
     metadata — missing DOI / unmatched metadata is NOT a conversion blocker.
  2. Re-resolve metadata for still-unmatched workspaces, preferring the freshly
     converted Markdown (`--prefer-markdown`).

It NEVER formalizes or commits on its own — formalize/commit is a separate,
strict-metadata step the operator must run explicitly.

Default is dry-run (no writes to paper_raw). Pass --apply to actually convert
and resolve. Phases can be run independently via --convert-only / --resolve-only.

The script invokes the existing script mains in-process (sharing sys.argv) and
aggregates their JSON reports into a single summary report. It does not
re-implement conversion or resolution logic.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import PAPER_RAW_DIR
from src.mineru_runtime import activate_formal_gpu_env


def _run_phase(module, argv: list[str]) -> tuple[int, dict | None]:
    """Run a phase script's main() with a constructed argv, capturing its JSON stdout.

    Returns (rc, parsed_json_or_None). The convert/resolve mains print a single
    JSON object to stdout; we parse the last JSON object on stdout.
    """
    saved = sys.argv
    sys.argv = argv
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = module.main()
    finally:
        sys.argv = saved
    parsed = _parse_last_json(buf.getvalue())
    return (rc if isinstance(rc, int) else 0), parsed


def _parse_last_json(text: str) -> dict | None:
    """Parse the last JSON object in `text` (scripts may print warnings first)."""
    if not text.strip():
        return None
    # Try direct parse first (common case: stdout is a single JSON blob).
    try:
        return json.loads(text)
    except Exception:
        pass
    # Fall back to scanning for the last top-level JSON object.
    last: dict | None = None
    for start in range(len(text)):
        if text[start] != "{":
            continue
        depth = 0
        for end in range(start, len(text)):
            ch = text[end]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        last = json.loads(text[start:end + 1])
                    except Exception:
                        pass
                    break
    return last


def _ensure_api(start: bool = True) -> dict:
    """Ensure the persistent mineru-api is healthy.

    With ``start=True`` (apply mode), actually start it via ``start_services``.
    With ``start=False`` (dry-run), only probe the current API health without
    starting anything — no pid/log writes, no side effects.
    """
    if start:
        try:
            from src.mineru_service_manager import start_services
            return start_services(wait=True, port=8000, restart_if_stale=True)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    return _check_api_only()


def _check_api_only() -> dict:
    """Probe the current mineru-api health without starting it (dry-run safe)."""
    try:
        from src.mineru_runtime import snapshot_mineru_api
        snap = snapshot_mineru_api("http://127.0.0.1:8000")
        return {"ok": bool(snap.get("api_available")), "started": False, "api_health": snap}
    except Exception as exc:
        return {"ok": False, "started": False, "error": str(exc)}


def _convert_phase(args, paper_raw_dir: Path) -> tuple[int, dict | None]:
    import scripts.convert_paper_raw_gpu as gpu
    argv = ["convert_paper_raw_gpu.py", "--paper-raw-dir", str(paper_raw_dir)]
    if args.all:
        argv.append("--all")
        argv.append("--only-convertible")
    elif args.paper_number:
        argv += ["--paper-number", args.paper_number]
    if args.apply and not args.dry_run:
        argv.append("--apply")
    else:
        argv.append("--dry-run")
    if getattr(args, "smoke_report", None):
        argv += ["--smoke-report", str(args.smoke_report)]
    if getattr(args, "skip_smoke_check", False):
        argv.append("--skip-smoke-check")
    if args.report:
        argv += ["--report", str(args.report.parent / "convert_phase.json") if args.report else ""]
    return _run_phase(gpu, [a for a in argv if a])


def _resolve_phase(args, paper_raw_dir: Path) -> tuple[int, dict | None]:
    import scripts.resolve_paper_raw_metadata as resolve_cli
    argv = ["resolve_paper_raw_metadata.py", "--paper-raw-dir", str(paper_raw_dir)]
    if args.paper_number:
        argv += ["--paper-number", args.paper_number]
    else:
        argv.append("--all-unmatched")
    argv.append("--prefer-markdown")
    if args.apply and not args.dry_run:
        argv.append("--apply")
    else:
        argv.append("--dry-run")
    return _run_phase(resolve_cli, argv)


def _aggregate(convert_report: dict | None, resolve_report: dict | None) -> dict:
    summary: dict = {
        "conversion": {"planned": 0, "converted": 0, "skipped_current": 0, "failed": 0},
        "metadata_resolution_after_convert": {"matched": 0, "manual_review_required": 0},
        "still_blocked_for_formalize": [],
    }
    if convert_report and isinstance(convert_report.get("items"), list):
        for item in convert_report["items"]:
            status = item.get("status")
            if status == "planned":
                summary["conversion"]["planned"] += 1
            elif status == "converted":
                summary["conversion"]["converted"] += 1
            elif status == "skipped":
                # Distinguish "already converted" skips from gate skips.
                reason = (item.get("reason") or "").lower()
                if "converted" in reason or item.get("conversion_state") == "converted_current":
                    summary["conversion"]["skipped_current"] += 1
                # else: gate skip (e.g. commit-stage) — not counted as converted.
            elif status == "failed":
                summary["conversion"]["failed"] += 1
            if item.get("metadata_ready_for_commit") is False:
                summary["still_blocked_for_formalize"].append(item.get("paper_number"))
    resolved_success_numbers: set[str] = set()
    if resolve_report and isinstance(resolve_report.get("items"), list):
        for item in resolve_report["items"]:
            decision = item.get("decision") or item.get("status")
            if item.get("applied_status") == "matched" or decision == "auto_matched" or decision == "matched":
                summary["metadata_resolution_after_convert"]["matched"] += 1
                if item.get("applied_status") == "matched":
                    number = item.get("paper_number") or item.get("paper_raw_id") or item.get("source_id")
                    if number:
                        resolved_success_numbers.add(str(number))
            elif decision in {"manual_review", "manual_review_required"}:
                summary["metadata_resolution_after_convert"]["manual_review_required"] += 1
    summary["still_blocked_for_formalize"] = sorted(set(summary["still_blocked_for_formalize"]) - resolved_success_numbers)
    return summary


def main() -> int:
    activate_formal_gpu_env()

    parser = argparse.ArgumentParser(description="Staged SOP: GPU conversion then post-conversion metadata re-resolution.")
    parser.add_argument("--all", action="store_true", help="operate on all 16-digit paper_raw workspaces")
    parser.add_argument("--paper-number", default=None, help="operate on a single paper_raw workspace")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--convert-only", action="store_true", help="only run the conversion phase")
    parser.add_argument("--resolve-only", action="store_true", help="only run the metadata-resolution phase")
    parser.add_argument("--apply", action="store_true", help="actually convert/resolve (default is dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="plan only, write nothing (default)")
    parser.add_argument("--report", type=Path, default=None, help="write summary JSON to this path")
    parser.add_argument("--smoke-report", type=Path, default=None)
    parser.add_argument("--skip-smoke-check", action="store_true")
    args = parser.parse_args()

    if not args.all and not args.paper_number:
        parser.error("--all or --paper-number is required")

    result: dict = {"applied": args.apply and not args.dry_run, "phases": {}, "summary": {}}

    if not args.resolve_only:
        applying = args.apply and not args.dry_run
        if applying and args.all and not args.skip_smoke_check:
            from src.mineru_smoke import smoke_required_message, validate_smoke_report

            smoke = validate_smoke_report(args.smoke_report)
            result["phases"]["smoke_check"] = smoke
            if not smoke.get("ok"):
                print(json.dumps({
                    "ok": False,
                    "error": smoke_required_message(args.smoke_report),
                    "smoke_check": smoke,
                }, ensure_ascii=False, indent=2))
                return 2
        api = _ensure_api(start=applying)
        result["phases"]["api"] = api
        if not api.get("ok"):
            # In dry-run we can still plan conversion without a live API.
            # In apply mode the API must be available to actually convert.
            if applying:
                print(json.dumps({"ok": False, "error": "mineru-api not healthy; aborting conversion phase", "api": api},
                                 ensure_ascii=False, indent=2))
                return 2
        rc, conv = _convert_phase(args, args.paper_raw_dir)
        result["phases"]["conversion"] = {"rc": rc, "report": conv}

    if not args.convert_only:
        rc, reso = _resolve_phase(args, args.paper_raw_dir)
        result["phases"]["metadata_resolution"] = {"rc": rc, "report": reso}

    conv_report = (result["phases"].get("conversion") or {}).get("report")
    reso_report = (result["phases"].get("metadata_resolution") or {}).get("report")
    result["summary"] = _aggregate(conv_report, reso_report)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    failed = (result["summary"].get("conversion") or {}).get("failed", 0)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

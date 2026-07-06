"""Repair stale paper_raw ``<paper_number>.asset_manifest.json`` in ``data/papers``.

One-shot admin tool. The validator (``validate_v2_library.py``) requires each
formal paper directory to contain exactly one ``<paper_id>.asset_manifest.json``.
Papers installed before the commit-side stale-manifest cleanup landed may carry
an extra ``<paper_number>.asset_manifest.json`` left over from the paper_raw
readiness step. This repair scans ``data/papers/<paper_id>/`` for any
``*.asset_manifest.json`` whose prefix is not ``<paper_id>`` and removes it.

The unique ``<paper_id>.asset_manifest.json`` is always preserved; if it is
missing the offending extra is left in place and the folder is reported as
``failed`` (run formalize/commit repair for that case instead).

Usage:
    python scripts/repair_stale_formal_asset_manifests.py --dry-run
    python scripts/repair_stale_formal_asset_manifests.py --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import PAPERS_DIR


def _scan(papers_dir: Path, *, apply: bool) -> list[dict]:
    items: list[dict] = []
    if not papers_dir.exists():
        return items
    for folder in sorted(p for p in papers_dir.iterdir() if p.is_dir()):
        pid = folder.name
        expected = folder / f"{pid}.asset_manifest.json"
        manifests = sorted(folder.glob("*.asset_manifest.json"))
        extras = [m for m in manifests if m != expected]
        if not extras:
            items.append({"folder": str(folder), "status": "skipped", "removed": []})
            continue
        status = "failed"
        removed: list[str] = []
        if expected.exists():
            for extra in extras:
                if apply:
                    extra.unlink()
                removed.append(extra.name)
            status = "repaired" if apply else "would_repair"
        else:
            # 缺正式 manifest —— 不可删除唯一存在的 manifest，交给 commit/formalize 修复。
            items.append({
                "folder": str(folder),
                "status": "failed",
                "error": f"missing expected {expected.name}; refusing to remove extras without a formal manifest",
                "extras": [m.name for m in extras],
            })
            continue
        items.append({"folder": str(folder), "status": status, "removed": removed})
    return items


def _summary(items: list[dict]) -> dict:
    return {
        "scanned": len(items),
        "needs_repair": sum(1 for it in items if it.get("status") in {"would_repair", "repaired"}),
        "repaired": sum(1 for it in items if it.get("status") == "repaired"),
        "skipped": sum(1 for it in items if it.get("status") == "skipped"),
        "failed": sum(1 for it in items if it.get("status") == "failed"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove stale <paper_number>.asset_manifest.json from data/papers.")
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--apply", action="store_true", help="delete the stale manifests; default is dry-run")
    parser.add_argument("--dry-run", action="store_true", help="report without deleting")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    apply = args.apply and not args.dry_run
    items = _scan(args.papers_dir, apply=apply)
    payload = {"applied": apply, "summary": _summary(items), "items": items}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if payload["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
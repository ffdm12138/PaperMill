"""Generate/apply v2 paper_raw curation prompts and validate curated catalog.

curate only validates metadata/catalog and writes ``status=catalog_ready``;
it does NOT rename the folder. paper_number reservation is done at staging,
and the paper_id rename is done by ``formalize_paper_raw.py``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import PAPER_RAW_DIR
from src.services.ingest_ids import PAPER_NUMBER_RE, validate_paper_raw_id
from src.services.v2_library import PaperCurationService


def _metadata_match_ready(folder: Path, name: str) -> bool:
    path = folder / f"{name}.metadata.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    status = str(((data.get("metadata_match") or {}).get("status")) or "")
    return status in {"matched", "manual_confirmed"}


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _candidates(root: Path, args) -> list[Path]:
    if args.paper_dir:
        return [args.paper_dir]
    if args.paper_number:
        return [root / validate_paper_raw_id(args.paper_number)]
    if args.all_ready or args.all_matched:
        out = []
        for folder in sorted(p for p in root.iterdir() if p.is_dir()):
            name = folder.name
            if not PAPER_NUMBER_RE.match(name):
                continue  # numbered-workspace gate only by design; legacy/untitled
                # dedup is handled by ingest_duplicate_guard.
            has_meta = (folder / f"{name}.metadata.json").exists()
            has_md = (folder / f"{name}.md").exists()
            has_images = (folder / "images").is_dir()
            if not (has_meta and has_md and has_images):
                continue
            if args.all_matched and not _metadata_match_ready(folder, name):
                continue
            # --all-ready --apply only processes folders that already have a
            # curated catalog output (the LLM/skill has run). dry-run still
            # generates prompts for every ready folder.
            if args.apply:
                if not (folder / f"{name}.catalog.json").exists():
                    continue
            out.append(folder)
        return out
    raise ValueError("--paper-dir, --paper-number, --all-ready, or --all-matched is required")


def main() -> int:
    parser = argparse.ArgumentParser(description="Curate v2 paper_raw metadata/catalog and rename folder.")
    parser.add_argument("--paper-dir", type=Path, default=None)
    parser.add_argument("--paper-number", default=None)
    parser.add_argument("--all-ready", action="store_true")
    parser.add_argument("--all-matched", action="store_true")
    parser.add_argument("--paper-raw-dir", type=Path, default=PAPER_RAW_DIR)
    parser.add_argument("--metadata", type=Path, default=None, help="curated metadata JSON for single target")
    parser.add_argument("--catalog", type=Path, default=None, help="curated catalog JSON for single target")
    parser.add_argument("--paper-id", default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    write = args.apply and not args.dry_run
    service = PaperCurationService()
    report = []
    for folder in _candidates(args.paper_raw_dir, args):
        item = {"folder": str(folder), "status": "planned"}
        name = folder.name
        try:
            if write:
                # Auto-detect metadata resolver patch if not provided explicitly.
                # NOTE: catalog curator must NOT generate a metadata patch; this
                # patch is produced by the metadata resolver / enrichment flow.
                curated_meta = args.metadata
                if curated_meta is None:
                    for auto_name in (f"{name}.metadata.patch.json", f"{name}.curated_metadata.json"):
                        auto_path = folder / auto_name
                        if auto_path.exists():
                            curated_meta = auto_path
                            break
                # Auto-detect curated catalog if not provided explicitly.
                curated_cat = args.catalog
                if curated_cat is None:
                    auto_cat = folder / f"{name}.catalog.json"
                    if auto_cat.exists():
                        curated_cat = auto_cat
                result = service.apply_curated_files(
                    folder,
                    paper_id=args.paper_id,
                    curated_metadata_path=curated_meta,
                    curated_catalog_path=curated_cat,
                )
                item.update(result)
                item["status"] = "curated" if result.get("success") else "failed"
            else:
                prompt = service.build_prompt(folder)
                prompt_path = folder / "curation_prompt.md"
                _write_text_atomic(prompt_path, prompt)
                item["prompt_path"] = str(prompt_path)
                item["prompt_preview"] = prompt[:1000]
                item["status"] = "prompt_generated"
        except Exception as exc:
            item.update({"status": "failed", "error": str(exc)})
        report.append(item)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"applied": write, "items": report}, ensure_ascii=False, indent=2))
    return 1 if any(i["status"] == "failed" for i in report) else 0


if __name__ == "__main__":
    raise SystemExit(main())

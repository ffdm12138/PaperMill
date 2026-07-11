"""Conversion manifest helpers for existing synthetic or restored assets."""
from __future__ import annotations

from pathlib import Path

from config.settings import MINERU_BACKEND, MINERU_EFFORT, MINERU_LANG, MINERU_METHOD, MINERU_OUTPUT_CACHE_ENABLED
from src.file_fingerprint import compute_file_hashes, compute_sha256
from src.ingest.models import now_iso
from src.services.asset_manifest import write_asset_manifest
from src.services.ingest_ids import PAPER_NUMBER_RE
from src.utils.atomic_io import atomic_write_json


def write_conversion_manifest_for_existing_assets(folder: str | Path, file_prefix: str) -> dict:
    root = Path(folder)
    pdf = root / f"{file_prefix}.pdf"
    markdown = root / f"{file_prefix}.md"
    images = root / "images"
    if not pdf.is_file() or not markdown.is_file() or not images.is_dir():
        raise FileNotFoundError(f"conversion manifest requires {file_prefix}.pdf, {file_prefix}.md and images/")
    pdf_hashes = compute_file_hashes(pdf)
    number = file_prefix if PAPER_NUMBER_RE.fullmatch(file_prefix) else ""
    manifest = {
        "schema_version": "1.0", "status": "converted", "paper_number": number,
        "paper_raw_id": number, "pdf_md5": pdf_hashes["md5"],
        "pdf_sha256": pdf_hashes["sha256"], "pdf_file_size": pdf_hashes["file_size"],
        "markdown_path": markdown.name, "markdown_sha256": compute_sha256(markdown),
        "images_dir": "images", "images_count": sum(1 for path in images.rglob("*") if path.is_file()),
        "backend": MINERU_BACKEND, "method": MINERU_METHOD, "lang": MINERU_LANG,
        "effort": MINERU_EFFORT, "runner": "", "api_url": "", "output_dir": "",
        "conversion_source": "existing_assets", "restored_from_output_cache": False,
        "output_cache_enabled": MINERU_OUTPUT_CACHE_ENABLED, "output_cache_hit": False,
        "output_cache_dir": "", "output_cache_manifest": "", "converted_at": now_iso(),
    }
    atomic_write_json(root / f"{file_prefix}.conversion.json", manifest, indent=2)
    write_asset_manifest(root, prefix=file_prefix, paper_number=number, paper_id="" if number else file_prefix, stage="paper_raw")
    return manifest

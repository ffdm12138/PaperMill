"""Conversion manifest factory for integration tests.

This module replaces the retired ``src/ingest/conversion.py``, providing
test-focused helpers that produce conversion manifests in the same format
that production code expects.
"""
from __future__ import annotations

from pathlib import Path

from src.utils.file_fingerprint import compute_sha256
from src.utils.atomic_io import atomic_write_json


def write_conversion_manifest_for_existing_assets(folder: str | Path, file_prefix: str) -> dict:
    """Write a conversion manifest for a folder that already has processed assets.

    The manifest is written to ``<file_prefix>.conversion.json`` and matches the
    schema produced by the production ``PaperRawConverter.convert`` path.  This
    is a **test helper** — no active production code calls this function.
    """
    root = Path(folder)
    pdf = root / f"{file_prefix}.pdf"
    md = root / f"{file_prefix}.md"
    images = root / "images"
    pdf_sha = compute_sha256(pdf) if pdf.exists() else ""
    markdown_sha = compute_sha256(md) if md.exists() else ""
    images_count = sum(1 for p in images.rglob("*") if p.is_file()) if images.exists() else 0

    manifest = {
        "schema_version": "1.0",
        "status": "converted",
        "paper_number": file_prefix,
        "paper_raw_id": file_prefix,
        "pdf_md5": "",
        "pdf_sha256": pdf_sha,
        "pdf_file_size": pdf.stat().st_size if pdf.exists() else 0,
        "markdown_path": f"{file_prefix}.md",
        "markdown_sha256": markdown_sha,
        "images_dir": "images",
        "images_count": images_count,
        "backend": "hybrid-engine",
        "method": "auto",
        "lang": "ch",
        "effort": "medium",
        "runner": "test",
        "api_url": "",
        "output_dir": str(root),
        "conversion_source": "mineru",
        "restored_from_output_cache": False,
        "output_cache_enabled": False,
        "output_cache_hit": False,
        "output_cache_dir": "",
        "output_cache_manifest": "",
        "converted_at": "2026-01-01T00:00:00+00:00",
    }
    atomic_write_json(root / f"{file_prefix}.conversion.json", manifest, indent=2)
    return manifest

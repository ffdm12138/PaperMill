"""Filesystem primitives shared across ingest and cache layers."""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def replace_images_dir(images_source: Path | None, images_target: Path) -> int:
    """Atomically swap an images directory via ``.images.tmp`` + ``os.replace``.

    A missing/``None`` source yields an empty images directory.  Returns the
    file count under the installed target.  Single source for the swap idiom
    (previously duplicated byte-for-byte in ``ingest.paper_raw`` and the
    MinerU output cache).
    """
    tmp_images_target = images_target.parent / ".images.tmp"
    shutil.rmtree(tmp_images_target, ignore_errors=True)
    try:
        if images_source and images_source.exists():
            shutil.copytree(images_source, tmp_images_target)
        else:
            tmp_images_target.mkdir(parents=True)
        shutil.rmtree(images_target, ignore_errors=True)
        os.replace(tmp_images_target, images_target)
        return sum(1 for p in images_target.rglob("*") if p.is_file())
    finally:
        shutil.rmtree(tmp_images_target, ignore_errors=True)

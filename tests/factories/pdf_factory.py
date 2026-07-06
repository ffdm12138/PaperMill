from __future__ import annotations

from pathlib import Path


def write_fake_pdf(path: Path, content: bytes = b"%PDF-1.4\n% test\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path

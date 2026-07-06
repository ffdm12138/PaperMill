"""Safe file target allocation helpers."""
from __future__ import annotations

from pathlib import Path

from src.file_fingerprint import compute_sha256


def allocate_unique_path(target: Path, sha256: str) -> tuple[Path, bool]:
    """Return a non-conflicting target path for content with *sha256*.

    If an existing file has the same sha256, that path is reused and the second
    return value is True. If it has different content, suffixes are tried until
    an empty path or same-content path is found.
    """
    target = Path(target)
    sha8 = (sha256 or "unknown")[:8]
    candidates = [target]
    suffix = 1
    while True:
        if suffix == 1:
            candidates.append(target.with_name(f"{target.stem}_{sha8}{target.suffix}"))
        else:
            candidates.append(target.with_name(f"{target.stem}_{sha8}_{suffix}{target.suffix}"))
        suffix += 1
        candidate = candidates.pop(0)
        if not candidate.exists():
            return candidate, False
        if compute_sha256(candidate) == sha256:
            return candidate, True

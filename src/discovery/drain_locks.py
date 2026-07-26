"""DOI / title-resolution drain lock paths for the candidate journal drain."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from filelock import FileLock

from src.discovery.contracts.page_journal import stable_hash, title_resolution_key
from src.utils.identifiers import normalize_doi

DISCOVERY_LOCK_TIMEOUT = 30


def doi_lock_path(locks_dir: Path, doi: str) -> Path:
    return Path(locks_dir) / "doi" / f"{stable_hash(normalize_doi(doi), length=40)}.lock"


def resolution_lock_path(locks_dir: Path, candidate_record: dict[str, Any]) -> Path:
    return Path(locks_dir) / "resolution" / f"{title_resolution_key(candidate_record)}.lock"


def drain_lock(path: Path) -> FileLock:
    path.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(path), timeout=DISCOVERY_LOCK_TIMEOUT)



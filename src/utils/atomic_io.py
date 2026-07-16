"""Atomic JSON write helpers.

Provides a locked public entry point (:func:`atomic_write_json`) and an
unlocked variant (:func:`atomic_write_json_unlocked`) for callers that
already hold the canonical sidecar ``FileLock``.

Durable write order (when ``fsync=True``)::

    write tmp  ->  flush  ->  fsync(tmp file)  ->  validate JSON
    ->  os.replace(tmp, target)  ->  fsync(parent directory, POSIX only)

Windows does not support directory ``fsync``; ``_fsync_dir`` is a no-op
there.  The file-level ``fsync`` is the critical durability guarantee and
works on every platform.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from filelock import FileLock


def _fsync_dir(directory: Path) -> None:
    """Best-effort fsync of the parent directory (POSIX only).

    Windows does not support directory fsync — the call is a no-op.
    Some network/overlay filesystems raise ``OSError`` even on POSIX;
    those are silently ignored so a best-effort durability guarantee
    never breaks a normal write.
    """
    if os.name == "nt":
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def lock_path_for(path: str | Path) -> Path:
    """Return the canonical lock file path for a data file.

    Convention: ``foo.json`` -> ``foo.json.lock`` (suffix + ``.lock``).
    """
    path = Path(path)
    return path.with_suffix(path.suffix + ".lock")


def atomic_write_json_unlocked(
    path: str | Path,
    data: dict,
    *,
    indent: int = 2,
    sort_keys: bool = False,
    fsync: bool = True,
) -> None:
    """Write JSON atomically **without** acquiring a lock.

    The caller MUST already hold the canonical ``FileLock`` for *path*
    (see :func:`lock_path_for`) to avoid concurrent-write corruption.

    Steps: tmp write -> flush -> fsync(tmp) -> JSON round-trip validate
    -> ``os.replace`` -> fsync(parent dir, POSIX only).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(
                json.dumps(data, ensure_ascii=False, indent=indent, sort_keys=sort_keys)
            )
            if fsync:
                fh.flush()
                os.fsync(fh.fileno())
        # Round-trip validation before replace (existing behaviour).
        json.loads(tmp.read_text(encoding="utf-8"))
        os.replace(tmp, path)
        if fsync:
            _fsync_dir(path.parent)
    except Exception:
        # Clean up tmp on any failure so it does not linger.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_json(
    path: str | Path,
    data: dict,
    *,
    indent: int = 2,
    sort_keys: bool = False,
    fsync: bool = True,
) -> None:
    """Write JSON with filelock + tmp + os.replace + fsync.

    Acquires the canonical sidecar ``FileLock`` (``<path>.lock``) and
    delegates to :func:`atomic_write_json_unlocked`.
    """
    path = Path(path)
    lock = FileLock(str(lock_path_for(path)))
    with lock:
        atomic_write_json_unlocked(
            path, data, indent=indent, sort_keys=sort_keys, fsync=fsync
        )


def atomic_replace_bytes_unlocked(
    path: str | Path,
    payload: bytes,
    *,
    fsync: bool = True,
) -> None:
    """Atomically publish exact bytes while the caller holds the file lock.

    This is intentionally separate from the text JSON writer: transaction
    plans bind the byte-for-byte result, so platform newline translation must
    never change the planned payload during apply or resume.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("wb") as fh:
            fh.write(payload)
            if fsync:
                fh.flush()
                os.fsync(fh.fileno())
        os.replace(tmp, path)
        if fsync:
            _fsync_dir(path.parent)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_replace_bytes(
    path: str | Path,
    payload: bytes,
    *,
    fsync: bool = True,
) -> None:
    """Lock and atomically publish exact bytes."""
    path = Path(path)
    with FileLock(str(lock_path_for(path))):
        atomic_replace_bytes_unlocked(path, payload, fsync=fsync)

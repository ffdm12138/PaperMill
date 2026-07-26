"""Entry-point runtime initialization for operational scripts.

Importing this module (the deliberate side effect of the ENTRY layer, and
only this layer) performs the startup work that ``config.settings`` no
longer does at import time:

- ``validate_settings()`` — fail fast on unsafe/invalid configuration
- ``ensure_runtime_dirs()`` — create the runtime data directories
- ``configure_logging()`` — single loguru sink, level from MINERU_LOG_LEVEL

Usage (after the standard ``sys.path`` bootstrap lines)::

    from scripts import _bootstrap  # noqa: F401

Test-infra scripts (agent_acceptance, pack_repo, test_runtime_workspace,
cleanup_test_caches) intentionally do NOT import this: acceptance running
inside an unpacked snapshot must never create runtime data directories.
"""
from __future__ import annotations

from config.settings import ensure_runtime_dirs, validate_settings
from src.logging_setup import configure_logging


def init_runtime() -> None:
    """Validate settings, create runtime dirs, configure logging."""
    validate_settings()
    ensure_runtime_dirs()
    configure_logging()


init_runtime()

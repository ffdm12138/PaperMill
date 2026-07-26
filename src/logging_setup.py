"""Central logging configuration (loguru).

Library modules use ``from loguru import logger`` and never configure sinks;
entry points (server startup, ``scripts/_bootstrap``) call
:func:`configure_logging` exactly once.  Level comes from ``MINERU_LOG_LEVEL``
(default ``INFO``).  Standard-library ``logging`` records from third-party
packages are forwarded into loguru so one stream carries everything.
"""
from __future__ import annotations

import logging
import os
import sys

from loguru import logger

_CONFIGURED = False


class _InterceptHandler(logging.Handler):
    """Forward stdlib logging records into loguru."""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - thin glue
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        logger.opt(depth=6, exception=record.exc_info).log(level, record.getMessage())


def configure_logging(level: str | None = None) -> None:
    """Configure the single loguru sink; idempotent.

    ``level`` overrides ``MINERU_LOG_LEVEL`` (default ``INFO``).
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    effective = (level or os.environ.get("MINERU_LOG_LEVEL", "") or "INFO").upper()
    logger.remove()
    logger.add(sys.stderr, level=effective, enqueue=False, backtrace=False, diagnose=False)
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    _CONFIGURED = True

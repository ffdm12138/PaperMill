"""Typed discovery contract errors — leaf vocabulary shared across contracts.

Lives below every other contracts module so value-type modules (notebook,
lane_history, page_journal) can raise these without importing each other.
"""
from __future__ import annotations


class NotebookContractError(ValueError):
    """Raised when a notebook dict does not match the v4 contract."""


class NotebookCorruptError(RuntimeError):
    """Raised when a notebook file cannot be parsed as valid JSON dict."""


class UnsupportedNotebookSchemaError(RuntimeError):
    """Raised when a notebook has an unsupported schema_version (including v1/v2)."""


class DiscoveryNotReadyError(RuntimeError):
    """Raised when a notebook lacks required bilingual queries for discovery."""


class CursorConflictError(RuntimeError):
    """Raised when expected-cursor CAS detects a stale writer."""

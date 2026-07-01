"""LEGACY-ONLY MIGRATION SCRIPT HELPERS.

This module is not part of ingest-v2.3 normal workflow.
Do not import from normal src/services code.
"""
from __future__ import annotations

import re


LEGACY_TEMP_SOURCE_ID_RE = re.compile(r"^\d{6}$")

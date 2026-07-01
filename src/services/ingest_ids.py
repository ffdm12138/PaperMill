"""Shared normal-flow ingest identifier rules for paper_raw workspaces."""
from __future__ import annotations

import re


PAPER_NUMBER_RE = re.compile(r"^\d{16}$")


def is_paper_number(value: object) -> bool:
    return bool(PAPER_NUMBER_RE.match(str(value or "")))


def validate_paper_raw_id(value: object) -> str:
    text = str(value or "")
    if is_paper_number(text):
        return text
    raise ValueError("paper_number must be 16 digits")

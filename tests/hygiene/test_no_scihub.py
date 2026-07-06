"""No-SciHub hygiene — hard guard that Sci-Hub code is permanently removed."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.fetch.access_policy import AccessPolicy
from src.fetch.resolver_registry import RESOLVER_REGISTRY


pytestmark = [pytest.mark.hygiene, pytest.mark.security]

ROOT = Path(__file__).resolve().parents[2]


def test_no_scihub_in_resolver_registry():
    assert "scihub" not in RESOLVER_REGISTRY


def test_no_allow_scihub_field_in_access_policy():
    assert "allow_scihub" not in AccessPolicy.__dataclass_fields__


def test_no_fetch_scihub_file():
    assert not (ROOT / "src" / "fetch" / "fetch_scihub.py").exists()


# Scan src/scripts only — tests/ may contain these strings in assertions.
@pytest.mark.parametrize("term", [
    "fetch_scihub",
    "allow_scihub",
    "SciHubResolver",
    '"scihub"',
    "'scihub'",
])
def test_no_scihub_code_path_in_source_or_scripts(term):
    offenders: list[str] = []
    for base in ("src", "scripts"):
        for path in (ROOT / base).rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if term in text:
                offenders.append(f"{path}: {term}")
    assert not offenders, f"Sci-Hub code paths found: {offenders}"

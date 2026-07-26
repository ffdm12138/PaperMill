"""Read-only Catalog legacy-field inspector.

The Catalog v3.2 contract forbids ``library_locator``, old ``asset_refs``,
``provenance.markdown_path``, and ``provenance.original_markdown_path``.
This module only detects them — it never writes or canonicalizes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

# Fields that the v3.2 contract explicitly forbids in a catalog.
FORBIDDEN_CATALOG_KEYS = frozenset({
    "library_locator",
    "asset_refs",
})
FORBIDDEN_PROVENANCE_KEYS = frozenset({
    "markdown_path",
    "original_markdown_path",
})


@dataclass(frozen=True)
class LegacyCatalogIssue:
    code: str
    json_path: str
    message: str


def inspect_legacy_catalog_fields(
    catalog: Mapping[str, object],
) -> tuple[LegacyCatalogIssue, ...]:
    """Return any legacy fields found in *catalog*.

    Only reports the presence of forbidden keys.  Does NOT modify the
    input and does NOT attempt to repair — the caller decides how to
    handle each issue (rejection, migration, audit log, …).
    """
    issues: list[LegacyCatalogIssue] = []

    if not isinstance(catalog, dict):
        return ()

    for key in FORBIDDEN_CATALOG_KEYS:
        if key in catalog:
            issues.append(LegacyCatalogIssue(
                code="forbidden_root_key",
                json_path=f"$.{key}",
                message=f"catalog root contains forbidden key: {key!r}",
            ))

    provenance = catalog.get("provenance")
    if isinstance(provenance, dict):
        for key in FORBIDDEN_PROVENANCE_KEYS:
            if key in provenance:
                issues.append(LegacyCatalogIssue(
                    code="forbidden_provenance_key",
                    json_path=f"$.provenance.{key}",
                    message=f"catalog.provenance contains forbidden key: {key!r}",
                ))

    return tuple(issues)

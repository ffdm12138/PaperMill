"""Read-only direct dependency license audit."""
from __future__ import annotations

import argparse
import importlib.metadata as metadata
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements.txt"

EXPECTED_DIRECT_LICENSES: dict[str, set[str]] = {
    "mineru": {"MinerU Open Source License", "LicenseRef-MinerU-Open-Source-License"},
    "fastapi": {"MIT"},
    "uvicorn": {"BSD-3-Clause"},
    "python-multipart": {"Apache-2.0"},
    "requests": {"Apache-2.0"},
    "loguru": {"MIT"},
    "pydantic": {"MIT"},
    "pymupdf": {"AGPL-3.0-or-commercial", "GNU AFFERO GPL"},
    "filelock": {"MIT"},
    "orjson": {"Apache-2.0", "MIT"},
    "pytest": {"MIT"},
    "pytest-xdist": {"MIT"},
    "jsonschema": {"MIT"},
    "psutil": {"BSD-3-Clause"},
}

PACKAGE_OVERRIDES = {
    "mineru[all]": "mineru",
    "pymupdf": "pymupdf",
}


def _normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_requirement_name(line: str) -> str:
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith(("-", "git+", "http://", "https://")):
        return ""
    match = re.match(r"([A-Za-z0-9_.-]+(?:\[[^\]]+\])?)", line)
    return match.group(1).lower() if match else ""


def direct_requirements(path: Path = REQUIREMENTS) -> list[str]:
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        name = _parse_requirement_name(line)
        if not name:
            continue
        canonical = PACKAGE_OVERRIDES.get(name, name.split("[", 1)[0])
        if canonical not in names:
            names.append(canonical)
    return names


def _declared_license(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    expression = meta.get("License-Expression")
    if expression:
        return expression.strip()
    license_field = meta.get("License")
    if license_field:
        return " ".join(license_field.split())
    classifiers = meta.get_all("Classifier") or []
    license_classifiers = [c.rsplit("::", 1)[-1].strip() for c in classifiers if "License ::" in c]
    return "; ".join(license_classifiers)


def _source_url(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    for key in ("Project-URL", "Home-page"):
        values = meta.get_all(key) or []
        if values:
            return values[0]
    return ""


def audit() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    installed = {_normalize_name(dist.metadata["Name"]): dist for dist in metadata.distributions() if dist.metadata.get("Name")}
    for name in direct_requirements():
        expected = EXPECTED_DIRECT_LICENSES.get(name, set())
        dist = installed.get(_normalize_name(name))
        if dist is None:
            rows.append({
                "package": name,
                "version": "",
                "declared_license": "",
                "expected_license": sorted(expected),
                "source_url": "",
                "status": "missing",
            })
            continue
        declared = _declared_license(dist)
        status = "ok"
        if not expected:
            status = "unknown_expected"
        elif not declared:
            status = "unknown_declared"
        elif not _license_matches(declared, expected):
            status = "mismatch"
        rows.append({
            "package": name,
            "version": dist.version,
            "declared_license": declared,
            "expected_license": sorted(expected),
            "source_url": _source_url(dist),
            "status": status,
        })
    return rows


def _license_matches(declared: str, expected: set[str]) -> bool:
    declared_lower = declared.lower()
    return any(exp.lower() in declared_lower for exp in expected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="fail on missing, unknown, or mismatched licenses")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a text table")
    args = parser.parse_args(argv)

    rows = audit()
    if args.json:
        print(json.dumps({"items": rows}, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print(
                f"{row['status']:18} {row['package']:24} {row['version']:12} "
                f"declared={row['declared_license']!r} expected={row['expected_license']}"
            )
    bad = [row for row in rows if row["status"] != "ok"]
    return 1 if args.strict and bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

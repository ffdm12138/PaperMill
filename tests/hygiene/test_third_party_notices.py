from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_third_party_notices_key_license_boundaries():
    text = _read("THIRD_PARTY_NOTICES.md")
    assert "original source code" in text
    assert "not relicensed by this repository" in text
    assert "Do not describe the entire stack as MIT licensed" in text
    assert "MinerU Open Source License" in text
    assert "based on Apache License 2.0 with additional terms" in text
    assert "AGPL-or-commercial" in text
    assert "| FastAPI |" in text and "| MIT." in text
    assert "| uvicorn |" in text and "BSD-3-Clause" in text
    assert "| python-multipart |" in text and "Apache-2.0" in text
    assert "| Gradio |" in text and "Apache-2.0" in text
    assert "| filelock |" in text and "| MIT." in text
    assert "ref-downloader" in text


def test_agents_and_claude_are_byte_identical():
    assert (ROOT / "AGENTS.md").read_bytes() == (ROOT / "CLAUDE.md").read_bytes()


def test_jsonschema_is_declared():
    requirements = _read("requirements.txt")
    assert "jsonschema" in requirements


def _normalize_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _direct_requirements() -> list[str]:
    names: list[str] = []
    for raw in _read("requirements.txt").splitlines():
        line = raw.split("#", 1)[0].split(";", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http://", "https://")):
            continue
        match = re.match(r"([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?", line)
        if match:
            names.append(_normalize_package_name(match.group(1)))
    return names


def test_third_party_notices_cover_direct_requirements():
    text = _read("THIRD_PARTY_NOTICES.md").lower()
    missing = [name for name in _direct_requirements() if name not in text]
    assert not missing, f"direct dependencies missing from THIRD_PARTY_NOTICES.md: {missing}"


def test_audit_scripts_are_documented_and_read_only():
    script_usage = _read("docs/SCRIPT_USAGE.md")
    for script in ("audit_third_party_licenses.py", "audit_source_provenance.py"):
        assert script in script_usage
        text = _read(f"scripts/{script}")
        assert "argparse" in text
        assert "atomic_write" not in text
        assert "write_text(" not in text


def test_pdf_resolver_docs_describe_direct_first_transport():
    text = _read("docs/PDF_RESOLVER_DESIGN.md")
    assert "direct-first" in text
    assert "MINERU_PDF_PROXY_URL" in text
    assert "FETCH_PROXY" in text
    assert "Metadata/discovery API queries keep" in text
    assert "Metadata JSON must not contain transport attempts" in text

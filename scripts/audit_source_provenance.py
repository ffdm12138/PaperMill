"""Read-only source provenance scan for copied/adapted code evidence."""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parent.parent
SCAN_ROOTS = ("src", "scripts", "skills", "config", "docs", "web")
SCAN_FILES = ("THIRD_PARTY_NOTICES.md", "README.md", "SECURITY.md")
SCAN_SUFFIXES = {".py", ".md", ".sh", ".bat", ".ps1", ".txt"}

HIGH_CONFIDENCE_PATTERNS = {
    "copied_or_adapted": re.compile(
        r"\b(copied|adapted|vendored)\s+from\b.*\b(source|code|repo|repository|github|gitlab)\b"
        r"|\bbased on\b.*\b(source|code|repo|repository|github|gitlab)\b",
        re.I,
    ),
    "copyright": re.compile(r"^\s*(?:#|//|/\*|\*)?\s*copyright\s+(?:\(c\)|©|\d{4})", re.I),
    "spdx": re.compile(r"\bSPDX-License-Identifier\b"),
}
REFERENCE_PATTERNS = {
    "repo_url": re.compile(r"https?://(?:www\.)?(?:github|gitlab|bitbucket)\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", re.I),
    "external_integration": re.compile(r"\b(ref-downloader|MinerU|MuPDF|PyMuPDF)\b", re.I),
}

ALLOWLIST = {
    ("src/fetch/resolvers/ref_downloader_bridge.py", "repo_url"),
    ("src/fetch/resolvers/ref_downloader_bridge.py", "external_integration"),
    ("THIRD_PARTY_NOTICES.md", "repo_url"),
    ("THIRD_PARTY_NOTICES.md", "external_integration"),
}


@dataclass
class Finding:
    path: str
    line: int
    report_layer: str
    match_type: str
    text: str
    confidence: str
    review_status: str


def _iter_files() -> Iterable[Path]:
    for root_name in SCAN_ROOTS:
        root = ROOT / root_name
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in SCAN_SUFFIXES:
                yield path
    for name in SCAN_FILES:
        path = ROOT / name
        if path.exists():
            yield path


def scan() -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(set(_iter_files())):
        rel = path.relative_to(ROOT).as_posix()
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines, start=1):
            for match_type, pattern in HIGH_CONFIDENCE_PATTERNS.items():
                if pattern.search(line):
                    findings.append(_finding(rel, index, match_type, line, "high"))
            for match_type, pattern in REFERENCE_PATTERNS.items():
                if pattern.search(line):
                    findings.append(_finding(rel, index, match_type, line, "medium"))
    return findings


def _finding(rel: str, line: int, match_type: str, text: str, confidence: str) -> Finding:
    review_status = "registered" if (rel, match_type) in ALLOWLIST else "needs_review"
    if confidence != "high" and review_status != "registered":
        review_status = "informational"
    report_layer = _report_layer(match_type, confidence)
    return Finding(
        path=rel,
        line=line,
        report_layer=report_layer,
        match_type=match_type,
        text=text.strip()[:240],
        confidence=confidence,
        review_status=review_status,
    )


def _report_layer(match_type: str, confidence: str) -> str:
    if confidence == "high":
        return "copied_or_adapted_source"
    if match_type == "repo_url":
        return "dependency_reference"
    return "integration_reference"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="fail on unregistered high-confidence findings")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    findings = scan()
    if args.json:
        print(json.dumps({"items": [asdict(item) for item in findings]}, ensure_ascii=False, indent=2))
    else:
        for item in findings:
            print(
                f"{item.review_status:14} {item.confidence:6} {item.report_layer:26} "
                f"{item.path}:{item.line} {item.match_type}: {item.text}"
            )
    bad = [
        item for item in findings
        if item.confidence == "high" and item.review_status == "needs_review"
    ]
    return 1 if args.strict and bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

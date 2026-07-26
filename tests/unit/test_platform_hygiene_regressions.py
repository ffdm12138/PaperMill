from __future__ import annotations

import sys
from pathlib import Path

from scripts import fetch_pdf_for_paper_raw as fetch_cli
from scripts import pack_repo
from scripts import resolve_paper_raw_metadata as resolve_cli
from src.fetch import fetch_pipeline
from src.utils.rate_limit import default_config


PAPER_NUMBER = "0000000000000001"


def test_snapshot_secret_scan_covers_test_members(tmp_path, monkeypatch):
    test_file = tmp_path / "tests" / "test_accidental_secret.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        'OPENALEX_API_KEY = "accidental_realistic_key_123456"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(pack_repo, "PROJECT_ROOT", tmp_path)

    findings = pack_repo.scan_files_for_secrets(
        ["tests/test_accidental_secret.py"], include_tests=True,
    )

    assert [(finding.rule, finding.path) for finding in findings] == [
        ("openalex_api_key_assignment", "tests/test_accidental_secret.py")
    ]


def test_snapshot_secret_scan_keeps_explicit_test_placeholders_allowed(tmp_path, monkeypatch):
    fixture = tmp_path / "tests" / "fixtures" / "synthetic_library" / "credentials.py"
    fixture.parent.mkdir(parents=True)
    fixture.write_text(
        'OPENALEX_EMAIL = "test@example.com"\n'
        'OPENALEX_API_KEY = "test-openalex-key"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(pack_repo, "PROJECT_ROOT", tmp_path)

    assert pack_repo.scan_files_for_secrets(
        ["tests/fixtures/synthetic_library/credentials.py"], include_tests=True,
    ) == []


def test_download_pdf_propagates_caller_timeout(tmp_path, monkeypatch):
    captured: dict[str, object] = {}

    class Response:
        status_code = 200
        url = "https://example.test/paper.pdf"
        headers = {"content-type": "application/pdf"}

        @staticmethod
        def iter_content(*, chunk_size):
            assert chunk_size > 0
            yield b"%PDF-1.7\n"

    class Transport:
        response = Response()
        error = ""
        safe_attempts: list[dict] = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    def fake_transport(url, **kwargs):
        captured.update(kwargs)
        return Transport()

    monkeypatch.setattr(fetch_pipeline, "fetch_url_direct_then_proxy", fake_transport)

    target, _ = fetch_pipeline._download_pdf(
        "https://example.test/paper.pdf",
        tmp_path / "paper.pdf",
        timeout=7,
    )

    assert target.is_file()
    assert captured["direct_timeout"] == 7
    assert captured["proxy_timeout"] == 7


def test_fetch_candidate_fails_closed_on_malformed_metadata(tmp_path):
    folder = tmp_path / PAPER_NUMBER
    folder.mkdir()
    (folder / f"{PAPER_NUMBER}.metadata.json").write_text("{", encoding="utf-8")

    candidate = fetch_cli.classify_pdf_fetch_candidate(folder, PAPER_NUMBER)

    assert candidate.status == "failed"
    assert "metadata" in candidate.reason.lower()


def test_all_unmatched_exits_nonzero_for_malformed_metadata(tmp_path, monkeypatch):
    folder = tmp_path / PAPER_NUMBER
    folder.mkdir()
    (folder / f"{PAPER_NUMBER}.metadata.json").write_text("{", encoding="utf-8")
    papers = tmp_path / "papers"
    papers.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "resolve_paper_raw_metadata.py",
            "--all-unmatched",
            "--paper-raw-dir",
            str(tmp_path),
            "--papers-dir",
            str(papers),
        ],
    )

    assert resolve_cli.main() == 1


def test_rate_config_default_is_repo_bound_outside_repo_cwd(tmp_path, monkeypatch):
    paper_raw = tmp_path / "paper_raw"
    paper_raw.mkdir()
    captured: dict[str, Path] = {}

    def load_rate_config(path):
        captured["path"] = Path(path)
        return default_config()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(resolve_cli, "load_rate_config", load_rate_config)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "resolve_paper_raw_metadata.py",
            "--all",
            "--paper-raw-dir",
            str(paper_raw),
            "--provider-min-interval",
            "crossref=0",
        ],
    )

    assert resolve_cli.main() == 0
    assert captured["path"] == (
        Path(resolve_cli.__file__).resolve().parents[1]
        / "config"
        / "metadata_rate_limits.json"
    )
    assert captured["path"].is_file()

from __future__ import annotations

import json

import pytest

from src.fetch.access_policy import AccessMode, AccessPolicy
from src.fetch.resolvers.base import ResolveContext
from src.fetch.resolvers.custom_resolvers import ExternalCommandResolver


def _context(tmp_path, doi: str) -> ResolveContext:
    return ResolveContext(
        doi=doi,
        metadata={"allowed_output_dir": str(tmp_path)},
        access_policy=AccessPolicy(mode=AccessMode.CUSTOM, extra={"allowed_output_dir": str(tmp_path)}),
    )


def test_valid_doi_reaches_command_with_shell_false(monkeypatch, tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-ok")
    captured = {}

    class Result:
        returncode = 0
        stdout = json.dumps({"pdf_path": str(pdf)})
        stderr = ""

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["shell"] = kwargs.get("shell")
        return Result()

    monkeypatch.setattr("src.fetch.resolvers.custom_resolvers.subprocess.run", fake_run)
    resolver = ExternalCommandResolver(["resolver.exe", "--doi", "{doi}", "--out", "{output_dir}"])

    result = resolver.resolve(_context(tmp_path, "10.1038/S41586-023-06185-3"))

    assert result.success is True
    assert captured["shell"] is False
    assert captured["args"][0] == "resolver.exe"
    assert captured["args"][2] == "10.1038/S41586-023-06185-3"


@pytest.mark.parametrize("doi", [
    "10.1000/abc def",
    "10.1000/abc\"def",
    "10.1000/abc\\def",
    "10.1000/<abc>",
    "10.1000/abc\n",
    "not-a-doi",
])
def test_invalid_doi_rejected_before_subprocess(monkeypatch, tmp_path, doi):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("subprocess should not run for invalid DOI")

    monkeypatch.setattr("src.fetch.resolvers.custom_resolvers.subprocess.run", fail_if_called)
    resolver = ExternalCommandResolver(["resolver.exe", "{doi}"])

    result = resolver.resolve(_context(tmp_path, doi))

    assert result.success is False
    assert result.error == "invalid DOI format for custom resolver"


def test_doi_placeholder_for_executable_is_rejected(monkeypatch, tmp_path):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("subprocess should not run when executable uses DOI")

    monkeypatch.setattr("src.fetch.resolvers.custom_resolvers.subprocess.run", fail_if_called)
    resolver = ExternalCommandResolver(["{doi}", "--out", "{output_dir}"])

    result = resolver.resolve(_context(tmp_path, "10.1000/ok"))

    assert result.success is False
    assert "executable cannot contain {doi}" in result.error

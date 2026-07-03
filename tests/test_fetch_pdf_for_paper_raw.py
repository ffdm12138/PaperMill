from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

from src.fetch.models import FetchResult
from src.services.v2_library import empty_metadata
from scripts.fetch_pdf_for_paper_raw import classify_pdf_fetch_candidate


REPO = Path(__file__).resolve().parent.parent
PN = "0000000000000001"


def _metadata(paper_number: str, doi: str = "10.1000/fetch") -> dict:
    meta = empty_metadata(paper_number, source_type="network_search")
    meta["identifiers"]["doi"] = doi
    meta["title"]["original"] = "Fetch Candidate"
    meta["year"] = 2024
    return meta


def _workspace(root: Path, paper_number: str = PN, *, doi: str = "10.1000/fetch", pdf: bool = False, status: str = "") -> Path:
    folder = root / paper_number
    folder.mkdir(parents=True)
    (folder / f"{paper_number}.metadata.json").write_text(json.dumps(_metadata(paper_number, doi)), encoding="utf-8")
    if pdf:
        (folder / f"{paper_number}.pdf").write_bytes(b"%PDF existing")
    if status:
        (folder / ".import_status.json").write_text(json.dumps({"status": status}), encoding="utf-8")
    return folder


def _run(argv: list[str]) -> int:
    saved = sys.argv
    sys.argv = argv
    try:
        runpy.run_path(str(REPO / "scripts" / "fetch_pdf_for_paper_raw.py"), run_name="__main__")
        return 0
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = saved


def test_candidate_valid_metadata_only_is_eligible(tmp_path):
    folder = _workspace(tmp_path / "paper_raw")
    item = classify_pdf_fetch_candidate(folder, PN)
    assert item.status == "planned"
    assert item.doi == "10.1000/fetch"


def test_candidate_skips_existing_pdf_missing_and_invalid_doi(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    existing = _workspace(paper_raw, PN, pdf=True)
    missing = paper_raw / "0000000000000002"
    missing.mkdir(parents=True)
    invalid = _workspace(paper_raw, "0000000000000003", doi="not-a-doi")

    assert classify_pdf_fetch_candidate(existing, PN).reason == "PDF already exists"
    assert classify_pdf_fetch_candidate(missing, "0000000000000002").reason == "metadata file missing"
    assert classify_pdf_fetch_candidate(invalid, "0000000000000003").reason == "invalid DOI in metadata"


def test_candidate_skips_blocked_status_but_allows_valid_doi_invalid_status(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    blocked = _workspace(paper_raw, PN, status="catalog_ready")
    doi_invalid = _workspace(paper_raw, "0000000000000002", status="doi_invalid")

    assert "blocked import status" in classify_pdf_fetch_candidate(blocked, PN).reason
    assert classify_pdf_fetch_candidate(doi_invalid, "0000000000000002").status == "planned"


def test_dry_run_does_not_call_network_or_write_pdf(tmp_path, monkeypatch):
    paper_raw = tmp_path / "paper_raw"
    folder = _workspace(paper_raw)
    report = tmp_path / "report.json"

    def fail_fetch(*args, **kwargs):
        raise AssertionError("dry-run must not fetch")

    monkeypatch.setattr("src.fetch.fetch_pipeline.fetch_pdf", fail_fetch)
    rc = _run([
        "fetch_pdf_for_paper_raw.py",
        "--all",
        "--only-missing-pdf",
        "--paper-raw-dir", str(paper_raw),
        "--papers-dir", str(tmp_path / "papers"),
        "--dry-run",
        "--report", str(report),
    ])

    assert rc == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["summary"]["planned"] == 1
    assert payload["items"][0]["status"] == "planned"
    assert not (folder / f"{PN}.pdf").exists()


def test_apply_attaches_pdf_and_writes_sanitized_provenance(tmp_path, monkeypatch):
    paper_raw = tmp_path / "paper_raw"
    folder = _workspace(paper_raw)

    def fake_fetch(doi, output_root=None, **kwargs):
        output = Path(output_root) / "download.pdf"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"%PDF fetched")
        return FetchResult(
            doi=doi,
            success=True,
            output_path=str(output),
            pdf_url="https://example.test/p.pdf",
            landing_url="https://example.test/landing",
            resolver="header_based",
            resolver_chain=["header_based"],
            access_mode="custom",
        )

    monkeypatch.setattr("src.fetch.fetch_pipeline.fetch_pdf", fake_fetch)
    rc = _run([
        "fetch_pdf_for_paper_raw.py",
        "--paper-number", PN,
        "--paper-raw-dir", str(paper_raw),
        "--papers-dir", str(tmp_path / "papers"),
        "--resolver", "header-based",
        "--url-template", "https://example.test/fetch?doi={doi}",
        "--header", "Cookie: secret",
        "--apply",
    ])

    assert rc == 0
    assert (folder / f"{PN}.pdf").read_bytes() == b"%PDF fetched"
    metadata = json.loads((folder / f"{PN}.metadata.json").read_text(encoding="utf-8"))
    # fetch_result.json is a SEPARATE file from the metadata source record.
    # metadata.source.raw_record_path must NOT point at fetch_result.json.
    assert metadata["source"]["raw_record_path"] != "source_records/fetch_result.json"
    fetch_path = folder / "source_records" / "fetch_result.json"
    assert fetch_path.exists()
    fetch_doc = json.loads(fetch_path.read_text(encoding="utf-8"))
    fetch_result = fetch_doc["fetch_result"]
    assert fetch_result["resolver"] == "header_based"
    assert fetch_result["headers_masked"] is True
    assert fetch_result["header_keys"] == ["User-Agent", "Cookie"]
    assert "secret" not in json.dumps(fetch_result)
    manifest = json.loads((folder / f"{PN}.asset_manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"]["pdf"]["path"] == f"{PN}.pdf"
    # stage_manifest must record the doi_fetch pdf_source pointing at fetch_result.json
    stage = json.loads((folder / "stage_manifest.json").read_text(encoding="utf-8"))
    assert stage["workflow_path"] == "network_metadata_pdf_fetch"
    assert stage["pdf_source"]["kind"] == "doi_fetch"
    assert stage["pdf_source"]["fetch_record_path"] == "source_records/fetch_result.json"
    assert stage["staged_pdf"]["sha256"]


def test_existing_pdf_skips_resolver_even_when_custom_or_unsafe(tmp_path, monkeypatch):
    """Regression: an existing <paper_number>.pdf must short-circuit before any
    resolver (header-based / unsafe/custom) is invoked. Guarantees we never
    re-fetch or overwrite an already-attached PDF, and that no network/Sci-Hub
    path is reached."""
    paper_raw = tmp_path / "paper_raw"
    folder = _workspace(paper_raw, PN, pdf=True, status="staged_metadata")
    original_bytes = (folder / f"{PN}.pdf").read_bytes()

    def fail_fetch(*args, **kwargs):
        raise AssertionError("resolver must not be called when a PDF already exists")

    monkeypatch.setattr("src.fetch.fetch_pipeline.fetch_pdf", fail_fetch)
    monkeypatch.setattr(
        "scripts.fetch_pdf_for_paper_raw.PaperRawAllocator.attach_pdf",
        lambda self, *a, **k: (_ for _ in ()).throw(
            AssertionError("attach_pdf must not run when PDF already exists")
        ),
    )

    rc = _run([
        "fetch_pdf_for_paper_raw.py",
        "--all",
        "--paper-raw-dir", str(paper_raw),
        "--papers-dir", str(tmp_path / "papers"),
        "--resolver", "header-based",
        "--url-template", "https://example.test/fetch?doi={doi}",
        "--header", "Cookie: secret",
        "--allow-unsafe-sources",
        "--apply",
    ])

    assert rc == 0  # skipped is not a failure
    # PDF untouched
    assert (folder / f"{PN}.pdf").read_bytes() == original_bytes
    # metadata not mutated with a fetch_result
    metadata = json.loads((folder / f"{PN}.metadata.json").read_text(encoding="utf-8"))
    assert "source" not in metadata or "fetch_result" not in metadata.get("source", {}).get("raw_record", {})


def test_user_agent_header_ignored_with_warning_by_default(tmp_path, monkeypatch):
    """Default UA-friendliness: a User-Agent copied from browser DevTools must
    be ignored (not error), while Cookie is still accepted and masked."""
    paper_raw = tmp_path / "paper_raw"
    _workspace(paper_raw, PN, pdf=True)  # existing PDF -> short-circuits, no fetch

    warnings = []
    monkeypatch.setattr("scripts.fetch_pdf_for_paper_raw.logger.warning", lambda *a, **k: warnings.append(a[0] if a else ""))

    rc = _run([
        "fetch_pdf_for_paper_raw.py",
        "--paper-number", PN,
        "--paper-raw-dir", str(paper_raw),
        "--papers-dir", str(tmp_path / "papers"),
        "--resolver", "header-based",
        "--url-template", "https://example.test/fetch?doi={doi}",
        "--header", "Cookie: secret",
        "--header", "User-Agent: Mozilla/5.0 ...",
    ])

    assert rc == 0
    assert any("User-Agent is ignored" in str(w) for w in warnings)


def test_strict_headers_rejects_user_agent(tmp_path):
    """--strict-headers restores the legacy hard-error on a supplied User-Agent."""
    paper_raw = tmp_path / "paper_raw"
    _workspace(paper_raw, PN)

    rc = _run([
        "fetch_pdf_for_paper_raw.py",
        "--paper-number", PN,
        "--paper-raw-dir", str(paper_raw),
        "--papers-dir", str(tmp_path / "papers"),
        "--resolver", "header-based",
        "--url-template", "https://example.test/fetch?doi={doi}",
        "--header", "User-Agent: x",
        "--strict-headers",
        "--dry-run",
    ])

    assert rc != 0  # parser.error -> SystemExit(2)

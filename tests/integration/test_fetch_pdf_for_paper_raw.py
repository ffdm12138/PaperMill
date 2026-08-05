from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

import pytest

from src.fetch.access_policy import classify_pdf_fetch_candidate
from src.fetch.models import FetchResult
from src.library.paper_number_ledger import PaperNumberLedger
from src.metadata.schema import empty_metadata


REPO = Path(__file__).resolve().parent.parent.parent
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
    PaperNumberLedger.write_marker(folder, paper_number, state="reserved")
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
            pdf_url="https://example.test/p.pdf?X-Amz-Signature=SECRET&ok=1",
            landing_url="https://example.test/landing?sig=SECRET",
            resolver="header_based",
            resolver_chain=["header_based"],
            access_mode="custom",
            attempts=[{
                "resolver": "header_based",
                "status": "success",
                "pdf_url": "https://example.test/p.pdf?X-Amz-Signature=SECRET",
            }],
            transport_attempts=[{
                "mode": "direct",
                "request_url": "https://example.test/p.pdf?X-Amz-Signature=SECRET",
                "final_url": "https://example.test/p.pdf?sig=SECRET",
            }],
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
    assert metadata["links"]["pdf_url"] == ""
    assert "landing_url" not in metadata["links"]
    fetch_path = folder / "source_records" / "fetch_result.json"
    assert fetch_path.exists()
    fetch_doc = json.loads(fetch_path.read_text(encoding="utf-8"))
    fetch_result = fetch_doc["fetch_result"]
    assert fetch_result["resolver"] == "header_based"
    assert fetch_result["headers_masked"] is True
    assert fetch_result["header_keys"] == ["User-Agent", "Cookie"]
    assert "secret" not in json.dumps(fetch_result)
    assert "SECRET" not in json.dumps(fetch_result)
    assert "X-Amz-Signature" not in json.dumps(fetch_result)
    manifest = json.loads((folder / f"{PN}.asset_manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"]["pdf"]["path"] == f"{PN}.pdf"
    # stage_manifest must record the doi_fetch pdf_source pointing at fetch_result.json
    stage = json.loads((folder / "stage_manifest.json").read_text(encoding="utf-8"))
    assert stage["workflow_path"] == "network_metadata_pdf_fetch"
    assert stage["pdf_source"]["kind"] == "doi_fetch"
    assert stage["pdf_source"]["fetch_record_path"] == "source_records/fetch_result.json"
    assert stage["pdf_source"]["pdf_url"] == "https://example.test/p.pdf"
    assert stage["staged_pdf"]["sha256"]
    assert (folder/f"{PN}.metadata_match.json").exists()
    # Fetch decoupling: the match receipt is written but the freeze is a
    # separate phase — no metadata_freeze.json may appear here, and the
    # workspace metadata state reflects the receipt decision.
    assert not (folder / f"{PN}.metadata_freeze.json").exists()
    import_status = json.loads((folder / ".import_status.json").read_text(encoding="utf-8"))
    assert import_status["metadata"]["state"] in {
        "matched", "related_version", "ambiguous", "unverifiable", "extraction_failed",
    }
    assert import_status["metadata"].get("match_status")


def test_existing_pdf_skips_resolver_even_when_custom_or_unsafe(tmp_path, monkeypatch):
    """Regression: an existing <paper_number>.pdf must short-circuit before any
    resolver (header-based / custom) is invoked. Guarantees we never
    re-fetch or overwrite an already-attached PDF, and that no network
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


def test_access_mode_rejected_by_argparse(tmp_path):
    """--access-mode has been removed; argparse must reject it."""
    paper_raw = tmp_path / "paper_raw"
    _workspace(paper_raw, PN, pdf=True)  # existing PDF -> short-circuits

    rc = _run([
        "fetch_pdf_for_paper_raw.py",
        "--paper-number", PN,
        "--paper-raw-dir", str(paper_raw),
        "--papers-dir", str(tmp_path / "papers"),
        "--access-mode", "oa_only",
    ])

    assert rc != 0  # argparse rejects unknown arguments


# ── batch selection and interruption safety ────────────────────────────
#
# The eligible backlog is far larger than one run, so a run must reach
# never-attempted work without replaying known-hard failures, and must not
# lose completed results when it is interrupted.

def _attempted(folder: Path, *, fetched_at: str) -> None:
    records = folder / "source_records"
    records.mkdir(exist_ok=True)
    (records / "fetch_result.json").write_text(
        json.dumps({"fetch_result": {"success": False, "fetched_at": fetched_at}}),
        encoding="utf-8")


def test_skip_attempted_leaves_previously_tried_workspaces_alone(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    fresh = _workspace(paper_raw, "0000000000000001", doi="10.5194/acp-1-1-2020")
    tried = _workspace(paper_raw, "0000000000000002", doi="10.5194/acp-2-2-2020")
    _attempted(tried, fetched_at="2026-07-01T00:00:00+00:00")
    report = tmp_path / "report.json"

    _run(["fetch_pdf_for_paper_raw.py", "--all", "--paper-raw-dir", str(paper_raw),
          "--skip-attempted", "--dry-run", "--report", str(report)])

    payload = json.loads(report.read_text(encoding="utf-8"))
    by_number = {item["paper_number"]: item for item in payload["items"]}
    assert by_number[fresh.name]["status"] == "planned"
    assert by_number[tried.name]["status"] == "skipped"
    assert by_number[tried.name]["reason"] == "already attempted"
    assert payload["selection"]["skip_attempted"] is True


def test_doi_prefix_and_limit_bound_the_batch(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    _workspace(paper_raw, "0000000000000001", doi="10.5194/acp-1-1-2020")
    _workspace(paper_raw, "0000000000000002", doi="10.5194/acp-2-2-2020")
    _workspace(paper_raw, "0000000000000003", doi="10.3390/su18031645")
    report = tmp_path / "report.json"

    _run(["fetch_pdf_for_paper_raw.py", "--all", "--paper-raw-dir", str(paper_raw),
          "--doi-prefix", "10.5194", "--limit", "1", "--dry-run", "--report", str(report)])

    payload = json.loads(report.read_text(encoding="utf-8"))
    planned = [i["paper_number"] for i in payload["items"] if i["status"] == "planned"]
    assert planned == ["0000000000000001"]
    assert payload["selection"]["doi_prefixes"] == ["10.5194"]
    assert payload["selection"]["limit"] == 1


def test_interrupted_run_keeps_already_completed_results(tmp_path, monkeypatch):
    """A backlog run of thousands of workspaces will be interrupted; an
    all-or-nothing report would throw away every completed item when it is.

    With a single worker the pool runs the two items strictly in order, so
    the first result is flushed before the second is even requested — no
    sleeps or timing assumptions needed.
    """
    paper_raw = tmp_path / "paper_raw"
    _workspace(paper_raw, "0000000000000001", doi="10.5194/acp-1-1-2020")
    _workspace(paper_raw, "0000000000000002", doi="10.5194/acp-2-2-2020")
    report = tmp_path / "report.json"
    seen: list[str] = []

    def fake_fetch(doi, output_root=None, **kwargs):
        seen.append(doi)
        if len(seen) > 1:
            raise KeyboardInterrupt("operator stopped the run")
        output = Path(output_root) / "download.pdf"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"%PDF fetched")
        return FetchResult(doi=doi, success=True, output_path=str(output),
                           resolver="static", resolver_chain=["static"])

    monkeypatch.setattr("src.fetch.fetch_pipeline.fetch_pdf", fake_fetch)
    with pytest.raises(KeyboardInterrupt):
        _run(["fetch_pdf_for_paper_raw.py", "--all", "--paper-raw-dir", str(paper_raw),
              "--max-workers", "1", "--apply", "--report", str(report)])

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["summary"]["attached"] == 1, "the completed item survived the interrupt"


def test_full_run_reports_every_item(tmp_path, monkeypatch):
    paper_raw = tmp_path / "paper_raw"
    _workspace(paper_raw, "0000000000000001", doi="10.5194/acp-1-1-2020")
    _workspace(paper_raw, "0000000000000002", doi="10.5194/acp-2-2-2020")
    report = tmp_path / "report.json"

    serial = iter(range(100))

    def fake_fetch(doi, output_root=None, **kwargs):
        output = Path(output_root) / "download.pdf"
        output.parent.mkdir(parents=True, exist_ok=True)
        # Distinct bytes per call, but WITHOUT the DOI: identical PDFs would
        # (correctly) trip the duplicate guard, and fetch writes the match
        # receipt WITHOUT freezing (freeze is a separate phase). Neither is
        # what this test is about.
        output.write_bytes(b"%PDF fetched body " + str(next(serial)).encode())
        return FetchResult(doi=doi, success=True, output_path=str(output),
                           resolver="static", resolver_chain=["static"])

    monkeypatch.setattr("src.fetch.fetch_pipeline.fetch_pdf", fake_fetch)
    _run(["fetch_pdf_for_paper_raw.py", "--all", "--paper-raw-dir", str(paper_raw),
          "--max-workers", "1", "--apply", "--report", str(report)])

    assert json.loads(report.read_text(encoding="utf-8"))["summary"]["attached"] == 2


def test_blocked_publisher_worklist_lists_only_unreachable_failures(tmp_path, monkeypatch):
    paper_raw = tmp_path / "paper_raw"
    _workspace(paper_raw, "0000000000000001", doi="10.3390/su18031645")
    _workspace(paper_raw, "0000000000000002", doi="10.5194/acp-2-2-2020")
    worklist = tmp_path / "blocked.csv"

    def fake_fetch(doi, output_root=None, **kwargs):
        host = ("https://www.mdpi.com/x/pdf" if doi.startswith("10.3390")
                else "https://acp.copernicus.org/x.pdf")
        return FetchResult(doi=doi, success=False, error="HTTP 403",
                           resolver_chain=["original_link"],
                           transport_attempts=[{"mode": "direct", "request_url": host,
                                                "final_url": host, "status_code": 403}])

    monkeypatch.setattr("src.fetch.fetch_pipeline.fetch_pdf", fake_fetch)
    _run(["fetch_pdf_for_paper_raw.py", "--all", "--paper-raw-dir", str(paper_raw),
          "--max-workers", "1", "--apply", "--report-blocked", str(worklist)])

    rows = worklist.read_text(encoding="utf-8").strip().splitlines()
    assert rows[0] == "paper_number,doi,doi_url,reason"
    assert len(rows) == 2, "only the ASN-blocked publisher belongs on the worklist"
    assert "10.3390/su18031645" in rows[1]

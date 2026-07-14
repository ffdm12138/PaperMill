import json
from pathlib import Path

from src.file_fingerprint import compute_file_hashes
from src.services.ingest_duplicate_guard import (
    build_doi_duplicate_index,
    build_ingest_duplicate_index,
    check_doi_duplicate,
    check_pdf_duplicate,
)
from src.metadata.schema import empty_metadata


PN1 = "0000000000000001"
PN2 = "0000000000000002"


def _write_metadata(folder: Path, name: str, *, doi: str = "", md5: str = "", sha256: str = "", paper_number: str | None = None) -> None:
    meta = empty_metadata(name)
    if paper_number is not None:
        # legacy/untitled folders carry their marker paper_number in metadata
        # even though the folder name isn't 16-digit.
        meta["paper_number"] = paper_number
        meta["paper_raw_id"] = paper_number
    meta["identifiers"]["doi"] = doi
    if md5 or sha256:
        (folder / f"{name}.asset_manifest.json").write_text(json.dumps({
            "schema_version": "1.0",
            "paper_number": paper_number or name,
            "paper_name": name,
            "stage": "paper_raw",
            "files": {"pdf": {"path": f"{name}.pdf", "md5": md5, "sha256": sha256, "file_size": 4}},
        }, ensure_ascii=False), encoding="utf-8")
    (folder / f"{name}.metadata.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def test_pdf_duplicate_checks_actual_paper_raw_and_papers(tmp_path):
    pdf_bytes = b"%PDF duplicate bytes"
    paper_raw = tmp_path / "paper_raw"
    raw_folder = paper_raw / PN1
    raw_folder.mkdir(parents=True)
    (raw_folder / f"{PN1}.pdf").write_bytes(pdf_bytes)
    _write_metadata(raw_folder, PN1, doi="10.1000/raw")

    papers = tmp_path / "papers"
    formal = papers / "2024_wang_existing"
    formal.mkdir(parents=True)
    (formal / "2024_wang_existing.pdf").write_bytes(pdf_bytes)
    _write_metadata(formal, "2024_wang_existing", doi="10.1000/formal")

    incoming = tmp_path / "incoming.pdf"
    incoming.write_bytes(pdf_bytes)

    result = check_pdf_duplicate(incoming, paper_raw_dir=paper_raw, papers_dir=papers)

    assert result.blocking is True
    assert "pdf_sha256_duplicate" in result.reasons
    assert "pdf_md5_duplicate" in result.reasons
    assert {ref.scope for ref in result.refs} == {"paper_raw", "papers"}


def test_doi_duplicate_skips_current_paper_raw_only(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    current = paper_raw / PN1
    current.mkdir(parents=True)
    _write_metadata(current, PN1, doi="10.1000/self")
    other = paper_raw / PN2
    other.mkdir(parents=True)
    _write_metadata(other, PN2, doi="10.1000/other")

    self_result = check_doi_duplicate("10.1000/self", paper_raw_dir=paper_raw, papers_dir=tmp_path / "papers", skip_paper_number=PN1)
    other_result = check_doi_duplicate("10.1000/other", paper_raw_dir=paper_raw, papers_dir=tmp_path / "papers", skip_paper_number=PN1)

    assert self_result.blocking is False
    assert other_result.blocking is True
    assert other_result.refs[0].paper_number == PN2


def test_md5_collision_or_inconsistent_hash_blocks(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    folder = paper_raw / PN1
    folder.mkdir(parents=True)
    incoming = tmp_path / "incoming.pdf"
    incoming.write_bytes(b"%PDF incoming")
    hashes = compute_file_hashes(incoming)
    _write_metadata(folder, PN1, md5=hashes["md5"], sha256="different-sha")

    result = check_pdf_duplicate(incoming, paper_raw_dir=paper_raw, papers_dir=tmp_path / "papers")

    assert result.blocking is True
    assert "pdf_md5_collision_or_inconsistent_hash" in result.reasons


def test_duplicate_guard_reads_nested_stage_manifest_hashes_without_pdf(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    folder = paper_raw / PN1
    folder.mkdir(parents=True)
    incoming = tmp_path / "incoming.pdf"
    incoming.write_bytes(b"%PDF staged manifest only")
    hashes = compute_file_hashes(incoming)
    _write_metadata(folder, PN1)
    (folder / "stage_manifest.json").write_text(json.dumps({
        "schema_version": "1.0",
        "paper_number": PN1,
        "paper_raw_id": PN1,
        "workflow_path": "manual_pdf",
        "source_type": "manual_pdf",
        "pdf_source": {
            "kind": "local_raw_queue",
            "original_md5": hashes["md5"],
            "original_sha256": hashes["sha256"],
        },
        "staged_pdf": {
            "path": f"{PN1}.pdf",
            "md5": hashes["md5"],
            "sha256": hashes["sha256"],
            "file_size": hashes["file_size"],
        },
    }, ensure_ascii=False), encoding="utf-8")

    result = check_pdf_duplicate(incoming, paper_raw_dir=paper_raw, papers_dir=tmp_path / "papers")

    assert result.blocking is True
    assert "pdf_sha256_duplicate" in result.reasons
    assert result.refs[0].paper_number == PN1


def test_quarantine_excluded_by_default_indexed_with_include_quarantine(tmp_path):
    pdf_bytes = b"%PDF quarantine dup bytes"
    paper_raw = tmp_path / "paper_raw"
    quarantined = paper_raw / "quarantine" / "2024_quarantined_paper"
    quarantined.mkdir(parents=True)
    (quarantined / "2024_quarantined_paper.pdf").write_bytes(pdf_bytes)
    _write_metadata(quarantined, "2024_quarantined_paper", doi="10.1000/q")

    # default: quarantine excluded
    index_default = build_ingest_duplicate_index(paper_raw_dir=paper_raw, papers_dir=tmp_path / "papers")
    q_default = [r for g in index_default.pdf_sha256_to_refs.values() for r in g if r.folder.endswith("2024_quarantined_paper")]
    assert q_default == []

    # include_quarantine=True: indexed
    index_with_q = build_ingest_duplicate_index(paper_raw_dir=paper_raw, papers_dir=tmp_path / "papers", include_quarantine=True)
    q_refs = [r for g in index_with_q.pdf_sha256_to_refs.values() for r in g if r.folder.endswith("2024_quarantined_paper")]
    assert q_refs, "quarantined workspace should be indexed when include_quarantine=True"


def test_corrupt_metadata_json_does_not_crash_index(tmp_path):
    """A malformed .metadata.json must not break index build; PDF still indexes via actual_pdf."""
    paper_raw = tmp_path / "paper_raw"
    folder = paper_raw / PN1
    folder.mkdir(parents=True)
    (folder / f"{PN1}.metadata.json").write_text("{not valid json", encoding="utf-8")
    (folder / f"{PN1}.pdf").write_bytes(b"%PDF bytes here")

    index = build_ingest_duplicate_index(paper_raw_dir=paper_raw, papers_dir=tmp_path / "papers")

    actual_refs = [r for group in index.pdf_sha256_to_refs.values() for r in group if r.source == "actual_pdf"]
    assert actual_refs, "actual PDF should still be indexed despite corrupt metadata"
    assert actual_refs[0].paper_number == PN1


def test_empty_numbered_folder_not_indexed(tmp_path):
    """An empty 16-digit folder (ledger-reserved but never staged) contributes no refs."""
    paper_raw = tmp_path / "paper_raw"
    (paper_raw / PN1).mkdir(parents=True)

    index = build_ingest_duplicate_index(paper_raw_dir=paper_raw, papers_dir=tmp_path / "papers")

    all_refs = [r for group in index.pdf_sha256_to_refs.values() for r in group]
    all_refs += [r for group in index.pdf_md5_to_refs.values() for r in group]
    all_refs += [r for group in index.doi_to_refs.values() for r in group]
    assert not all_refs, "empty folder should not be indexed"


def test_candidate_sidecar_doi_not_indexed_for_duplicate_guard(tmp_path):
    paper_raw = tmp_path / "paper_raw"
    folder = paper_raw / PN1
    folder.mkdir(parents=True)
    _write_metadata(folder, PN1, doi="")
    (folder / f"{PN1}.metadata.candidates.json").write_text(json.dumps({
        "candidates": [{"doi": "10.1000/sidecar"}],
    }), encoding="utf-8")

    result = check_doi_duplicate("10.1000/sidecar", paper_raw_dir=paper_raw, papers_dir=tmp_path / "papers")

    assert result.blocking is False
    assert result.refs == []


def test_doi_only_index_does_not_hash_pdfs(tmp_path, monkeypatch):
    paper_raw = tmp_path / "paper_raw"
    folder = paper_raw / PN1
    folder.mkdir(parents=True)
    _write_metadata(folder, PN1, doi="10.1000/doi-only")
    (folder / f"{PN1}.pdf").write_bytes(b"pdf bytes")

    def boom(*args, **kwargs):
        raise AssertionError("DOI-only index must not hash PDFs")

    monkeypatch.setattr("src.services.ingest_duplicate_guard.compute_file_hashes", boom)
    index = build_doi_duplicate_index(paper_raw_dir=paper_raw, papers_dir=tmp_path / "papers")
    result = check_doi_duplicate("10.1000/doi-only", paper_raw_dir=paper_raw, papers_dir=tmp_path / "papers", index=index)

    assert result.duplicate
    assert result.refs[0].doi == "10.1000/doi-only"

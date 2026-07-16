"""Canonical workspace factories built through production writers."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from src.ingest.paper_raw import PaperRawAllocator
from src.discovery.formal_publication import publish_formal_publication_state
from src.library.paper_number_ledger import PaperNumberLedger
from src.services.network_metadata_staging import stage_network_metadata_records
from src.services.network_metadata_staging import _metadata_from_record, _source_record_payload
from src.discovery.discovery_receipt import build_receipt_payload, write_or_validate_discovery_receipt
from src.services.source_records import write_metadata_source_record
from src.services.stage_manifest import write_stage_manifest
from src.services.ingest_state import write_import_status
from src.utils.atomic_io import atomic_write_json


def create_manual_pdf_workspace(tmp_path: Path, *, pdf_bytes: bytes = b"%PDF-1.4\nsynthetic") -> Path:
    source = tmp_path / "input.pdf"
    source.write_bytes(pdf_bytes)
    result = PaperRawAllocator(
        tmp_path / "paper_raw", ledger_path=tmp_path / "ledger.json",
        papers_dir=tmp_path / "papers",
    ).allocate_from_pdf(source)
    return Path(result["folder"])


def create_network_metadata_workspace(tmp_path: Path, *, doi: str = "10.1000/factory",
                                      candidate_id: str = "candidate-1") -> Path:
    result = stage_network_metadata_records([{
        "title": "Synthetic network paper", "year": 2026, "doi": doi,
        "discovery_context": {
            "candidate_id": candidate_id, "page_id": "page-1", "keyword_id": "keyword-1",
            "provider": "crossref", "normalized_doi": doi,
        },
    }], paper_raw_dir=tmp_path / "paper_raw", papers_dir=tmp_path / "papers",
       ledger_path=tmp_path / "ledger.json", apply=True)
    item = result["items"][0]
    if item["status"] != "staged":
        raise AssertionError(item)
    return Path(item["folder"])


def create_metadata_staged_network_workspace(
    tmp_path: Path, *, doi: str = "10.1000/factory", candidate_id: str = "candidate-1",
) -> Path:
    return create_network_metadata_workspace(tmp_path, doi=doi, candidate_id=candidate_id)


def create_reserved_network_workspace(tmp_path: Path) -> Path:
    _, folder = PaperNumberLedger(tmp_path / "ledger.json").reserve_next_for_paper_raw_workspace(
        tmp_path / "paper_raw")
    return folder


def create_multi_identity_workspace(
    tmp_path: Path, *, doi: str = "10.1000/multi", receipt_provider: str = "openalex",
) -> Path:
    """Create a reserved workspace with two provider identities via production writers."""
    folder = create_reserved_network_workspace(tmp_path)
    identities = {
        "openalex": {"candidate_id": "candidate-a", "page_id": "page-a"},
        "crossref": {"candidate_id": "candidate-b", "page_id": "page-b"},
    }
    for provider, values in identities.items():
        context = {
            **values, "keyword_id": "keyword-multi", "provider": provider,
            "normalized_doi": doi,
        }
        write_metadata_source_record(folder, provider, {
            "provider": provider, "record": {"title": f"{provider} paper", "doi": doi},
            "discovery_context": context,
        })
    selected = identities[receipt_provider]
    write_or_validate_discovery_receipt(
        folder / f"{folder.name}.discovery_receipt.json",
        build_receipt_payload(
            paper_number=folder.name, keyword_id="keyword-multi",
            provider=receipt_provider, normalized_doi=doi, **selected,
        ),
        workspace_root=tmp_path / "paper_raw",
    )
    return folder


def create_network_metadata_pdf_workspace(tmp_path: Path, *, doi: str = "10.1000/factory") -> Path:
    folder = create_network_metadata_workspace(tmp_path, doi=doi)
    pdf = tmp_path / "network.pdf"
    pdf.write_bytes(b"%PDF-1.4\nnetwork")
    PaperRawAllocator(tmp_path / "paper_raw", ledger_path=tmp_path / "ledger.json",
                      papers_dir=tmp_path / "papers").attach_pdf(folder.name, pdf)
    return folder


def create_marker_only_workspace(tmp_path: Path) -> Path:
    _, folder = PaperNumberLedger(tmp_path / "ledger.json").reserve_next_for_paper_raw_workspace(
        tmp_path / "paper_raw")
    return folder


def create_reserved_partial_workspace(tmp_path: Path) -> Path:
    return create_marker_only_workspace(tmp_path)


def create_network_metadata_workspaces_bulk(tmp_path: Path, *, count: int,
                                            unsettled: int = 0) -> None:
    """Fast canonical benchmark setup using only production artifact writers."""
    raw = tmp_path / "paper_raw"
    ledger = PaperNumberLedger(tmp_path / "ledger.json")
    data = ledger.empty_data()
    total = count + unsettled
    data["max_number"] = f"{total:016d}"
    for n in range(1, total + 1):
        number = f"{n:016d}"
        folder = raw / number
        folder.mkdir(parents=True, exist_ok=True)
        state = "metadata_staged" if n <= count else "reserved"
        PaperNumberLedger.write_marker(folder, number, state=state)
        data["items"][number] = {
            "folder_name": number, "folder_path": str(folder), "state": state,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        if n > count:
            continue
        doi = f"10.7000/bench.{n}"
        record = {"title": f"Benchmark {n}", "year": 2026, "doi": doi,
                  "provider": "crossref"}
        metadata = _metadata_from_record(record, number)
        context = {"candidate_id": f"candidate-{n}", "page_id": f"page-{n}",
                   "keyword_id": "benchmark", "provider": "crossref", "normalized_doi": doi}
        source = {**_source_record_payload(metadata, record), "discovery_context": context}
        write_metadata_source_record(folder, "crossref", source)
        atomic_write_json(folder / f"{number}.metadata.json", metadata, indent=2)
        receipt = build_receipt_payload(paper_number=number, **context)
        write_or_validate_discovery_receipt(folder / f"{number}.discovery_receipt.json",
                                            receipt, workspace_root=raw)
        write_stage_manifest(folder, paper_number=number, paper_raw_id=number,
                             workflow_path="network_metadata", source_type="network_search",
                             pdf_source=None, staged_pdf=None)
        write_import_status(folder, "staged_metadata", extra={
            "paper_number": number, "paper_raw_id": number, "source_type": "network_search",
            "source_provider": "crossref", "doi": doi,
        })
    ledger.save(data)


def write_minimal_formal_publication_identity(
    formal: Path, *, paper_number: str, paper_name: str | None = None,
) -> None:
    """Write the lightweight publication identity emitted by formal commit."""
    paper_name = paper_name or formal.name
    numeric_metadata = formal / f"{paper_number}.metadata.json"
    canonical_metadata = formal / f"{paper_name}.metadata.json"
    if numeric_metadata != canonical_metadata and numeric_metadata.is_file():
        numeric_metadata.rename(canonical_metadata)
    PaperNumberLedger.write_marker(
        formal, paper_number, state="active", planned_paper_name=paper_name)
    atomic_write_json(formal / f"{paper_name}.catalog.json", {
        "schema_version": "3.2", "paper_number": paper_number,
        "paper_name": paper_name,
    }, indent=2)
    from src.file_fingerprint import compute_sha256
    atomic_write_json(formal / f"{paper_name}.asset_manifest.json", {
        "schema_version": "2.0", "stage": "papers",
        "paper_number": paper_number, "paper_name": paper_name,
        "files": {"metadata": f"{paper_name}.metadata.json"},
        "asset_hashes": {"metadata": compute_sha256(canonical_metadata)},
        "image_hashes": {},
        "source_record_hashes": {},
    }, indent=2)


def activate_minimal_formal_publication(
    ledger: PaperNumberLedger, raw: Path, formal: Path,
) -> Path:
    paper_number = raw.name
    formal.parent.mkdir(parents=True, exist_ok=True)
    raw.rename(formal)
    ledger.activate_metadata_staged(paper_number, formal, paper_name=formal.name)
    write_minimal_formal_publication_identity(
        formal, paper_number=paper_number, paper_name=formal.name)
    publish_formal_publication_state(
        papers_dir=formal.parent,
        ledger_items=ledger.load().get("items") or {},
        allow_initialize=True,
    )
    return formal


def create_active_formal_workspace(tmp_path: Path, *, doi: str = "10.1000/formal") -> Path:
    raw = create_network_metadata_workspace(tmp_path, doi=doi)
    formal = tmp_path / "papers" / "synthetic_formal"
    return activate_minimal_formal_publication(
        PaperNumberLedger(tmp_path / "ledger.json"), raw, formal)


def make_staged_source(tmp_path: Path, paper_number: str = "0000000000000001") -> Path:
    folder = create_network_metadata_workspace(tmp_path, doi=f"10.1000/{paper_number}")
    pdf = tmp_path / "conversion.pdf"
    pdf.write_bytes(b"%PDF-1.4\nconversion fixture")
    PaperRawAllocator(tmp_path / "paper_raw", ledger_path=tmp_path / "ledger.json",
                      papers_dir=tmp_path / "papers").attach_pdf(folder.name, pdf)
    (folder / f"{folder.name}.md").write_text("# Synthetic conversion", encoding="utf-8")
    (folder / "images").mkdir(exist_ok=True)
    from tests.factories import write_conversion_manifest_for_existing_assets
    write_conversion_manifest_for_existing_assets(folder, folder.name)
    return folder

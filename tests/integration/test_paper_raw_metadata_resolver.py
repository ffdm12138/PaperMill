"""Tests for the paper_raw metadata resolver service + CLI.

Network is always mocked:
- DOI-enrichment branch: monkeypatch metadata_enrichment_service.query_crossref_by_doi
  (it imports requests INSIDE the function, so requests.get cannot be patched).
- title-search branch: monkeypatch resolve_crossref.requests.get,
  search_openalex.requests.get with FakeResponse.
"""
from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

import pytest

from src.services import metadata_enrichment_service as mes
from src.services import metadata_resolver as mr
from src.services.asset_manifest import write_asset_manifest
from src.metadata.schema import empty_metadata
from src.services.rate_limit import ProviderRateLimiter, default_config


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _zero_rate_limiter() -> ProviderRateLimiter:
    """A rate limiter with all intervals zeroed — no real waits during tests.

    Tests that exercise network-enabled code paths must pass this (or their
    own limiter) together with ``allow_network=True`` so that the resolver
    has a limiter to call while mocked HTTP replaces real requests.
    """
    cfg = default_config()
    cfg["global"]["paper_interval_seconds"] = 0.0
    cfg["global"]["jitter_seconds"] = 0.0
    for p in cfg.get("providers", ()):
        cfg["providers"][p]["min_interval_seconds"] = 0.0
    return ProviderRateLimiter(cfg)


# 鈹€鈹€ Fixtures 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def _make_folder(tmp_path: Path, source_id: str = "0000000000000001", *, doi: str = "",
                 title: str = "", year=None, authors: list[dict] | None = None,
                 journal: str = "", status: str = "unmatched",
                 pdf_bytes: bytes = b"%PDF-1.4 fake", md_text: str | None = None) -> Path:
    folder = tmp_path / "paper_raw" / source_id
    folder.mkdir(parents=True)
    meta = empty_metadata(source_id, source_type="manual_pdf")
    if title:
        meta["title"]["original"] = title
    if year is not None:
        meta["year"] = year
    if authors:
        meta["authors"] = authors
    if journal:
        meta["container"]["journal"] = journal
    if doi:
        meta["identifiers"]["doi"] = doi
    (folder / f"{source_id}.metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (folder / f"{source_id}.pdf").write_bytes(pdf_bytes)
    if md_text is not None:
        (folder / f"{source_id}.md").write_text(md_text, encoding="utf-8")
    return folder


def _crossref_message(*, doi: str, title: str, year: int, authors: list[tuple[str, str]],
                      venue: str, volume: str = "8", pages: str = "395-414") -> dict:
    return {
        "DOI": doi,
        "title": [title],
        "author": [{"given": g, "family": f} for g, f in authors],
        "container-title": [venue],
        "published-print": {"date-parts": [[year]]},
        "issued": {"date-parts": [[year]]},
        "volume": volume,
        "page": pages,
        "URL": f"https://doi.org/{doi}",
        "publisher": "Test Publisher",
    }


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload

    @property
    def text(self):
        return json.dumps(self._payload)


def _patch_crossref_doi(monkeypatch, message: dict | None):
    """Patch mes.query_crossref_by_doi to return `message` (or None)."""
    monkeypatch.setattr(mes, "query_crossref_by_doi", lambda doi, timeout=15: message)
    monkeypatch.setattr(mes, "enrich_from_doi",
                        lambda doi, chinese_title="", query_crossref=True: _enrich_from_message(doi, message))


def _enrich_from_message(doi, message):
    if message is None:
        return mes.EnrichmentResult(doi=doi, warnings=["crossref unresolved"])
    norm = mes.normalize_crossref_metadata(message)
    return mes.EnrichmentResult(
        doi=norm["doi"], title=norm["title"], year=norm["year"],
        authors=norm["authors"], first_author=norm["first_author"],
        venue=norm["venue"], publisher=norm["publisher"],
        volume=norm["volume"], number=norm["number"], issue=norm["issue"],
        pages=norm["pages"], article_number=norm["article_number"],
        url=norm["url"], issn=norm["issn"], published=norm["published"],
        source="crossref", confidence=0.95, raw=message, warnings=[],
    )


def _empty_catalog(tmp_path: Path) -> Path:
    cat = tmp_path / "catalog" / "all.catalog.json"
    cat.parent.mkdir(parents=True)
    cat.write_text(json.dumps({"schema_version": "1.0", "papers": []}), encoding="utf-8")
    return cat


def _seed_formal_paper(tmp_path: Path, pid: str, *, doi: str = "", pdf_sha: str = "") -> Path:
    papers = tmp_path / "papers"
    folder = papers / pid
    folder.mkdir(parents=True)
    meta = empty_metadata(pid)
    if doi:
        meta["identifiers"]["doi"] = doi
    (folder / f"{pid}.metadata.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    if pdf_sha:
        (folder / f"{pid}.pdf").write_bytes(b"%PDF unique-bytes")
        write_asset_manifest(
            folder,
            prefix=pid,
            paper_number="",
            paper_id=pid,
            stage="papers",
            extra_files={"pdf": {"path": f"{pid}.pdf", "sha256": pdf_sha, "file_size": 4}},
        )
    return folder


# 鈹€鈹€ 1. existing-DOI enriches, no overwrite 鈫?matched 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_existing_doi_enriches_via_crossref_no_overwrite(tmp_path, monkeypatch):
    folder = _make_folder(tmp_path, doi="10.5194/tc-8-395-2014",
                          title="Local kept title", year=2014,
                          authors=[{"full_name": "Vionnet V", "family": "Vionnet", "given": "V", "orcid": "", "affiliation": ""}],
                          journal="The Cryosphere")
    msg = _crossref_message(doi="10.5194/tc-8-395-2014",
                            title="Simulation of wind-induced snow transport",
                            year=2014, authors=[("V.", "Vionnet")], venue="The Cryosphere")
    _patch_crossref_doi(monkeypatch, msg)
    cat = _empty_catalog(tmp_path)

    report = mr.resolve_metadata_candidates(folder, allow_network=True,
                                           rate_limiter=_zero_rate_limiter(), papers_dir=tmp_path / "papers")
    assert report.existing_doi == "10.5194/tc-8-395-2014"
    res = mr.apply_resolution(folder, report, papers_dir=tmp_path / "papers")
    assert res["applied"] is True
    assert res["status"] == "resolved"
    meta = json.loads((folder / "0000000000000001.metadata.json").read_text(encoding="utf-8"))
    # existing non-empty title preserved (not overwritten by Crossref title)
    assert meta["title"]["original"] == "Local kept title"
    assert "metadata_match" not in meta
    assert meta["identifiers"]["doi"] == "10.5194/tc-8-395-2014"


# 鈹€鈹€ 2. existing-DOI conflict 鈫?stops, unmatched 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_existing_doi_conflict_stops(tmp_path, monkeypatch):
    folder = _make_folder(tmp_path, doi="10.5194/tc-8-395-2014", title="T", year=2014)
    # Crossref returns a different DOI
    msg = _crossref_message(doi="10.9999/different-doi", title="T", year=2014,
                            authors=[("V.", "Vionnet")], venue="V")
    _patch_crossref_doi(monkeypatch, msg)
    cat = _empty_catalog(tmp_path)

    report = mr.resolve_metadata_candidates(folder, allow_network=True,
                                           rate_limiter=_zero_rate_limiter(), papers_dir=tmp_path / "papers")
    assert report.decision == "conflict"
    res = mr.apply_resolution(folder, report, papers_dir=tmp_path / "papers")
    assert res["applied"] is False
    meta = json.loads((folder / "0000000000000001.metadata.json").read_text(encoding="utf-8"))
    assert "metadata_match" not in meta


# 鈹€鈹€ 3. DOI from filename 鈫?auto matched 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_doi_from_filename_auto_matches(tmp_path, monkeypatch):
    folder = _make_folder(tmp_path)  # no metadata doi; pdf named 0000000000000001.pdf so filename won't have doi
    # give the PDF a DOI-bearing name via a separate file is not possible (must be <source_id>.pdf).
    # Instead mock extract_doi_from_filename.
    monkeypatch.setattr(mr, "extract_doi_from_filename", lambda name: "10.5194/tc-8-395-2014")
    monkeypatch.setattr(mr, "extract_doi_from_pdf_file", lambda pdf: None)
    msg = _crossref_message(doi="10.5194/tc-8-395-2014",
                            title="Simulation of wind-induced snow transport",
                            year=2014, authors=[("V.", "Vionnet")], venue="The Cryosphere")
    _patch_crossref_doi(monkeypatch, msg)
    cat = _empty_catalog(tmp_path)

    report = mr.resolve_metadata_candidates(folder, allow_network=True,
                                           rate_limiter=_zero_rate_limiter(), papers_dir=tmp_path / "papers")
    assert report.decision == "auto_matched"
    res = mr.apply_resolution(folder, report, papers_dir=tmp_path / "papers")
    assert res["applied"] is True and res["status"] == "resolved"
    meta = json.loads((folder / "0000000000000001.metadata.json").read_text(encoding="utf-8"))
    assert meta["identifiers"]["doi"] == "10.5194/tc-8-395-2014"


# 鈹€鈹€ 4. DOI from PDF text 鈫?matched 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_doi_from_pdf_text_auto_matches(tmp_path, monkeypatch):
    folder = _make_folder(tmp_path)
    monkeypatch.setattr(mr, "extract_doi_from_filename", lambda name: None)
    monkeypatch.setattr(mr, "extract_doi_from_pdf_file", lambda pdf: "10.5194/tc-8-395-2014")
    msg = _crossref_message(doi="10.5194/tc-8-395-2014",
                            title="Simulation of wind-induced snow transport",
                            year=2014, authors=[("V.", "Vionnet")], venue="The Cryosphere")
    _patch_crossref_doi(monkeypatch, msg)
    cat = _empty_catalog(tmp_path)

    report = mr.resolve_metadata_candidates(folder, allow_network=True,
                                           rate_limiter=_zero_rate_limiter(), papers_dir=tmp_path / "papers")
    assert report.decision == "auto_matched"
    res = mr.apply_resolution(folder, report, papers_dir=tmp_path / "papers")
    assert res["applied"] is True and res["status"] == "resolved"


# 鈹€鈹€ 5. DOI from markdown header 鈫?matched 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_doi_from_markdown_header_auto_matches(tmp_path, monkeypatch):
    md = ("# Simulation of wind-induced snow transport\n\n"
          "Vionnet, V.\n\nThe Cryosphere 2014\n\n"
          "https://doi.org/10.5194/tc-8-395-2014\n\n## Abstract\ntext\n")
    folder = _make_folder(tmp_path, md_text=md)
    monkeypatch.setattr(mr, "extract_doi_from_filename", lambda name: None)
    monkeypatch.setattr(mr, "extract_doi_from_pdf_file", lambda pdf: None)
    msg = _crossref_message(doi="10.5194/tc-8-395-2014",
                            title="Simulation of wind-induced snow transport",
                            year=2014, authors=[("V.", "Vionnet")], venue="The Cryosphere")
    _patch_crossref_doi(monkeypatch, msg)
    cat = _empty_catalog(tmp_path)

    report = mr.resolve_metadata_candidates(folder, allow_network=True,
                                           rate_limiter=_zero_rate_limiter(), papers_dir=tmp_path / "papers")
    assert report.doi_source == "markdown"
    assert report.decision == "auto_matched"
    res = mr.apply_resolution(folder, report, papers_dir=tmp_path / "papers")
    assert res["applied"] is True and res["status"] == "resolved"


# ── 5b. --prefer-markdown: Markdown front-matter preferred over metadata title ──

def test_prefer_markdown_prefers_markdown_title_over_metadata(tmp_path, monkeypatch):
    """With --prefer-markdown (post-conversion), Markdown front-matter title
    wins over an existing metadata title+DOI, and the report surfaces
    used_markdown + metadata_sources. Without it, the metadata title is kept."""
    md = ("# Markdown Front Title From Converted MD\n\n"
          "Vionnet, V.\n\nThe Cryosphere 2014\n\n"
          "https://doi.org/10.5194/tc-8-395-2014\n\n## Abstract\ntext\n")
    # metadata already has BOTH a DOI and a title: by default the markdown
    # front-matter title would NOT override it (prefer_front_title is False).
    folder = _make_folder(tmp_path, doi="10.5194/tc-8-395-2014",
                          title="Wrong Metadata Title", md_text=md)
    monkeypatch.setattr(mr, "extract_doi_from_filename", lambda name: None)
    monkeypatch.setattr(mr, "extract_doi_from_pdf_file", lambda pdf: None)
    msg = _crossref_message(doi="10.5194/tc-8-395-2014",
                            title="Markdown Front Title From Converted MD",
                            year=2014, authors=[("V.", "Vionnet")], venue="The Cryosphere")
    _patch_crossref_doi(monkeypatch, msg)
    cat = _empty_catalog(tmp_path)

    report = mr.resolve_metadata_candidates(folder, allow_network=False, papers_dir=tmp_path / "papers",
                                           prefer_markdown=True)
    assert report.title_source == "markdown_front_matter"
    assert report.local_title == "Markdown Front Title From Converted MD"
    assert report.used_markdown() is True
    assert "markdown" in report.metadata_sources()
    assert report.post_conversion is True

    # Without prefer_markdown, the metadata title is kept (markdown not preferred).
    report2 = mr.resolve_metadata_candidates(folder, allow_network=False, papers_dir=tmp_path / "papers",
                                            prefer_markdown=False)
    assert report2.title_source == "metadata"
    assert report2.local_title == "Wrong Metadata Title"


# 鈹€鈹€ 6. title-search candidate never auto-matched; manual-confirm ok 鈹€鈹€鈹€

def test_title_search_candidate_never_auto_matched(tmp_path, monkeypatch):
    md = ("# Simulation of wind-induced snow transport\n\n"
          "Vionnet, V.\n\nPublished: 2014\n\nThe Cryosphere\n\n")
    folder = _make_folder(tmp_path, md_text=md)
    monkeypatch.setattr(mr, "extract_doi_from_filename", lambda name: None)
    monkeypatch.setattr(mr, "extract_doi_from_pdf_file", lambda pdf: None)
    # network title search returns a DOI-bearing crossref item
    item = {
        "DOI": "10.5194/tc-8-395-2014",
        "title": ["Simulation of wind-induced snow transport"],
        "author": [{"given": "V.", "family": "Vionnet"}],
        "container-title": ["The Cryosphere"],
        "issued": {"date-parts": [[2014]]},
        "URL": "https://doi.org/10.5194/tc-8-395-2014",
    }
    from src.discovery import resolve_crossref as rc
    monkeypatch.setattr(rc.requests, "get",
                        lambda *a, **k: _FakeResp({"message": {"items": [item]}}))
    cat = _empty_catalog(tmp_path)

    report = mr.resolve_metadata_candidates(folder, allow_network=True,
                                           rate_limiter=_zero_rate_limiter(), papers_dir=tmp_path / "papers")
    assert report.doi_source == "network_title"
    # NOT auto_matched even though title/year match well
    assert report.decision != "auto_matched"
    # apply without manual-confirm 鈫?not applied
    res = mr.apply_resolution(folder, report, papers_dir=tmp_path / "papers")
    assert res["applied"] is False
    # apply WITH manual-confirm 鈫?manual_confirmed (passes full validation gate)
    res2 = mr.apply_resolution(folder, report, manual_confirm=True, papers_dir=tmp_path / "papers")
    assert res2["applied"] is True and res2["status"] == "resolved"


def test_title_search_uses_markdown_front_matter_line_50_over_metadata_title(tmp_path, monkeypatch):
    md = "\n".join([""] * 49 + ["# Correct Markdown Front Title", "", "Vionnet, V.", "", "Published: 2014"])
    folder = _make_folder(tmp_path, title="Wrong PDF Extracted Title", md_text=md)
    monkeypatch.setattr(mr, "extract_doi_from_filename", lambda name: None)
    monkeypatch.setattr(mr, "extract_doi_from_pdf_file", lambda pdf: None)
    captured = {}

    def fake_title_search(title, year=None, limit=5):
        captured["title"] = title
        return [mr.PaperCandidate(
            title="Correct Markdown Front Title",
            authors=["V. Vionnet"],
            year=2014,
            venue="The Cryosphere",
            doi="10.5194/tc-8-395-2014",
            source="crossref",
            confidence=0.9,
        )]

    monkeypatch.setattr(mr, "resolve_crossref_by_title", fake_title_search)
    monkeypatch.setattr(mr, "get_crossref_work_by_doi", lambda doi: {"DOI": doi})
    monkeypatch.setattr(mr, "search_openalex", lambda *a, **k: [])
    cat = _empty_catalog(tmp_path)

    report = mr.resolve_metadata_candidates(folder, allow_network=True,
                                           rate_limiter=_zero_rate_limiter(), papers_dir=tmp_path / "papers")

    assert captured["title"] == "Correct Markdown Front Title"
    assert report.local_title == "Correct Markdown Front Title"
    assert report.title_source == "markdown_front_matter"
    assert report.markdown_front_matter_max_lines == 100
    assert report.markdown_front_matter_lines[49] == "# Correct Markdown Front Title"


def test_title_search_falls_back_to_pdf_first_pages_title(tmp_path, monkeypatch):
    folder = _make_folder(tmp_path, md_text="DOI: not-a-real-doi\nAbstract\nKeywords: snow\n")
    monkeypatch.setattr(mr, "extract_doi_from_filename", lambda name: None)
    monkeypatch.setattr(mr, "extract_doi_from_pdf_file", lambda pdf: None)
    monkeypatch.setattr(mr, "_extract_pdf_title_candidate", lambda pdf: "PDF Fallback Title for Search")
    captured = {}

    def fake_title_search(title, year=None, limit=5):
        captured["title"] = title
        return [mr.PaperCandidate(
            title="PDF Fallback Title for Search",
            authors=["Alice Smith"],
            year=2024,
            venue="Fallback Journal",
            doi="10.1000/fallback-title",
            source="crossref",
            confidence=0.9,
        )]

    monkeypatch.setattr(mr, "resolve_crossref_by_title", fake_title_search)
    monkeypatch.setattr(mr, "get_crossref_work_by_doi", lambda doi: {"DOI": doi})
    monkeypatch.setattr(mr, "search_openalex", lambda *a, **k: [])
    cat = _empty_catalog(tmp_path)

    report = mr.resolve_metadata_candidates(folder, allow_network=True,
                                           rate_limiter=_zero_rate_limiter(), papers_dir=tmp_path / "papers")

    assert captured["title"] == "PDF Fallback Title for Search"
    assert report.title_source == "pdf_fallback"
    assert report.decision != "auto_matched"


def test_resolve_report_has_structured_local_evidence_and_decision(tmp_path, monkeypatch):
    folder = _make_folder(tmp_path, doi="10.5194/tc-8-395-2014", title="T", year=2014,
                          authors=[{"full_name": "Vionnet V", "family": "Vionnet", "given": "V", "orcid": "", "affiliation": ""}],
                          journal="The Cryosphere")
    msg = _crossref_message(doi="10.5194/tc-8-395-2014", title="T", year=2014,
                            authors=[("V.", "Vionnet")], venue="The Cryosphere")
    _patch_crossref_doi(monkeypatch, msg)
    cat = _empty_catalog(tmp_path)

    report = mr.resolve_metadata_candidates(folder, allow_network=True,
                                           rate_limiter=_zero_rate_limiter(), papers_dir=tmp_path / "papers")
    payload = report.to_dict()

    assert payload["decision"] == "auto_matched"
    assert payload["decision_detail"]["status"] == "auto_matched"
    assert payload["local_evidence"]["title_source"] == "metadata"
    assert payload["local_evidence"]["doi_candidates"] == ["10.5194/tc-8-395-2014"]
    assert payload["local_evidence"]["local_title_evidence_missing"] is False


# 鈹€鈹€ 7. title search manual-band 鈫?unmatched, candidates written 鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_title_search_manual_band_stays_unmatched(tmp_path, monkeypatch):
    md = "# Some obscure paper title\n\nUnknown Author\n\n2010\n\n"
    folder = _make_folder(tmp_path, md_text=md)
    monkeypatch.setattr(mr, "extract_doi_from_filename", lambda name: None)
    monkeypatch.setattr(mr, "extract_doi_from_pdf_file", lambda pdf: None)
    item = {
        "DOI": "10.9999/obscure",
        "title": ["A completely different title that does not match"],
        "author": [{"given": "X.", "family": "Nobody"}],
        "container-title": ["Other Journal"],
        "issued": {"date-parts": [[2005]]},
        "URL": "https://doi.org/10.9999/obscure",
    }
    from src.discovery import resolve_crossref as rc
    monkeypatch.setattr(rc.requests, "get",
                        lambda *a, **k: _FakeResp({"message": {"items": [item]}}))
    cat = _empty_catalog(tmp_path)

    report = mr.resolve_metadata_candidates(folder, allow_network=True,
                                           rate_limiter=_zero_rate_limiter(), papers_dir=tmp_path / "papers")
    # low score 鈫?rejected or manual_review; never auto_matched
    assert report.decision != "auto_matched"
    res = mr.apply_resolution(folder, report, papers_dir=tmp_path / "papers")
    assert res["applied"] is False
    meta = json.loads((folder / "0000000000000001.metadata.json").read_text(encoding="utf-8"))
    assert "metadata_match" not in meta


# 鈹€鈹€ 8. no-DOI candidates 鈫?resolve_failed 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_no_doi_candidates_resolve_failed(tmp_path, monkeypatch):
    md = "# Some title\n\nAuthor\n\n2020\n\n"
    folder = _make_folder(tmp_path, md_text=md)
    monkeypatch.setattr(mr, "extract_doi_from_filename", lambda name: None)
    monkeypatch.setattr(mr, "extract_doi_from_pdf_file", lambda pdf: None)
    # crossref returns an item WITHOUT a DOI
    item = {"title": ["Some title"], "author": [{"family": "Author"}],
            "container-title": ["J"], "issued": {"date-parts": [[2020]]}}
    from src.discovery import resolve_crossref as rc
    monkeypatch.setattr(rc.requests, "get",
                        lambda *a, **k: _FakeResp({"message": {"items": [item]}}))
    cat = _empty_catalog(tmp_path)

    report = mr.resolve_metadata_candidates(folder, allow_network=True,
                                           rate_limiter=_zero_rate_limiter(), papers_dir=tmp_path / "papers")
    # no DOI-bearing candidates
    assert all(not c.doi for c in report.candidates) or not report.candidates
    assert report.decision in ("no_candidates", "rejected")


# 鈹€鈹€ 9. non-empty field never overwritten 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_non_empty_field_never_overwritten(tmp_path, monkeypatch):
    folder = _make_folder(tmp_path, doi="10.5194/tc-8-395-2014",
                          title="Pre-existing Title", year=2014,
                          authors=[{"full_name": "Vionnet V", "family": "Vionnet", "given": "V", "orcid": "", "affiliation": ""}],
                          journal="Pre-existing Journal")
    msg = _crossref_message(doi="10.5194/tc-8-395-2014",
                            title="Different Crossref Title", year=2014,
                            authors=[("V.", "Vionnet")], venue="Different Venue")
    _patch_crossref_doi(monkeypatch, msg)
    cat = _empty_catalog(tmp_path)

    report = mr.resolve_metadata_candidates(folder, allow_network=True,
                                           rate_limiter=_zero_rate_limiter(), papers_dir=tmp_path / "papers")
    res = mr.apply_resolution(folder, report, papers_dir=tmp_path / "papers")
    assert res["applied"] is True
    meta = json.loads((folder / "0000000000000001.metadata.json").read_text(encoding="utf-8"))
    assert meta["title"]["original"] == "Pre-existing Title"
    assert meta["container"]["journal"] == "Pre-existing Journal"
    assert any("preserved" in w for w in res["warnings"])


# 鈹€鈹€ 10. duplicate formal DOI blocks both auto and manual 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_duplicate_formal_doi_blocks(tmp_path, monkeypatch):
    folder = _make_folder(tmp_path, doi="10.5194/tc-8-395-2014", title="T", year=2014,
                          authors=[{"full_name": "Vionnet V", "family": "Vionnet", "given": "V", "orcid": "", "affiliation": ""}],
                          journal="The Cryosphere")
    msg = _crossref_message(doi="10.5194/tc-8-395-2014", title="T", year=2014,
                            authors=[("V.", "Vionnet")], venue="The Cryosphere")
    _patch_crossref_doi(monkeypatch, msg)
    cat = _empty_catalog(tmp_path)
    # seed the same DOI in data/papers metadata (not in catalog)
    _seed_formal_paper(tmp_path, "2014_Vionnet_dup", doi="10.5194/tc-8-395-2014")

    report = mr.resolve_metadata_candidates(folder, allow_network=False, papers_dir=tmp_path / "papers")
    assert report.decision != "auto_matched"
    res = mr.apply_resolution(folder, report, papers_dir=tmp_path / "papers")
    assert res["applied"] is False
    assert any("duplicate_formal_doi" in w for w in res["warnings"])
    # manual-confirm still blocked
    res2 = mr.apply_resolution(folder, report, manual_confirm=True, papers_dir=tmp_path / "papers")
    assert res2["applied"] is False


def test_duplicate_paper_raw_doi_blocks_manual_confirm(tmp_path, monkeypatch):
    folder = _make_folder(tmp_path, doi="10.5194/tc-8-395-2014", title="T", year=2014,
                          authors=[{"full_name": "Vionnet V", "family": "Vionnet", "given": "V", "orcid": "", "affiliation": ""}],
                          journal="The Cryosphere", pdf_bytes=b"%PDF current")
    _make_folder(tmp_path, source_id="0000000000000002", doi="10.5194/tc-8-395-2014",
                 title="Other", year=2014, pdf_bytes=b"%PDF other")
    msg = _crossref_message(doi="10.5194/tc-8-395-2014", title="T", year=2014,
                            authors=[("V.", "Vionnet")], venue="The Cryosphere")
    _patch_crossref_doi(monkeypatch, msg)
    cat = _empty_catalog(tmp_path)

    report = mr.resolve_metadata_candidates(folder, allow_network=False, papers_dir=tmp_path / "papers")
    assert report.decision != "auto_matched"
    assert any("duplicate_paper_raw_doi" in r for r in report.candidates[0].gate_reasons)
    res = mr.apply_resolution(folder, report, manual_confirm=True, papers_dir=tmp_path / "papers")
    assert res["applied"] is False
    assert any("duplicate_paper_raw_doi" in w for w in res["warnings"])


# 鈹€鈹€ 10b. duplicate PDF sha256 blocks 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_duplicate_pdf_sha256_blocks(tmp_path, monkeypatch):
    folder = _make_folder(tmp_path, doi="10.5194/tc-8-395-2014", title="T", year=2014,
                          authors=[{"full_name": "Vionnet V", "family": "Vionnet", "given": "V", "orcid": "", "affiliation": ""}],
                          journal="The Cryosphere", pdf_bytes=b"%PDF unique-bytes")
    msg = _crossref_message(doi="10.5194/tc-8-395-2014", title="T", year=2014,
                            authors=[("V.", "Vionnet")], venue="The Cryosphere")
    _patch_crossref_doi(monkeypatch, msg)
    cat = _empty_catalog(tmp_path)
    # compute the paper_raw pdf sha and seed it in a formal paper with a DIFFERENT doi
    sha = mr.compute_sha256(folder / "0000000000000001.pdf")
    _seed_formal_paper(tmp_path, "2014_other", doi="10.9999/other", pdf_sha=sha)

    report = mr.resolve_metadata_candidates(folder, allow_network=False, papers_dir=tmp_path / "papers")
    assert report.decision != "auto_matched"
    assert any("duplicate_pdf_sha256" in r for r in (report.candidates[0].gate_reasons if report.candidates else [])) \
        or any("duplicate_pdf_sha256" in r for r in report.warnings)
    res = mr.apply_resolution(folder, report, papers_dir=tmp_path / "papers")
    assert res["applied"] is False
    assert any("duplicate_pdf_sha256" in w for w in res["warnings"])


# 鈹€鈹€ 11. manual-confirm rejects DOI conflict 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_manual_confirm_rejects_doi_conflict(tmp_path, monkeypatch):
    # Resolve with NO existing metadata DOI; candidate found via markdown DOI = A.
    md = ("# Simulation of wind-induced snow transport\n\n"
          "Vionnet, V.\n\nPublished: 2014\n\nThe Cryosphere\n\n"
          "https://doi.org/10.5194/tc-8-395-2014\n\n")
    folder = _make_folder(tmp_path, md_text=md)
    monkeypatch.setattr(mr, "extract_doi_from_filename", lambda name: None)
    monkeypatch.setattr(mr, "extract_doi_from_pdf_file", lambda pdf: None)
    msg = _crossref_message(doi="10.5194/tc-8-395-2014",
                            title="Simulation of wind-induced snow transport",
                            year=2014, authors=[("V.", "Vionnet")], venue="The Cryosphere")
    _patch_crossref_doi(monkeypatch, msg)
    cat = _empty_catalog(tmp_path)

    report = mr.resolve_metadata_candidates(folder, allow_network=True,
                                           rate_limiter=_zero_rate_limiter(), papers_dir=tmp_path / "papers")
    assert report.candidates, "expected at least one candidate"
    candidate_doi = report.candidates[0].doi
    # Now inject a DIFFERENT existing DOI into metadata on disk, then apply with --manual-confirm.
    meta_path = folder / "0000000000000001.metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    conflicting_doi = "10.9999/conflict-with-existing"
    assert conflicting_doi != candidate_doi
    meta["identifiers"]["doi"] = conflicting_doi
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    res = mr.apply_resolution(folder, report, manual_confirm=True, papers_dir=tmp_path / "papers")
    assert res["applied"] is False
    assert any("conflict" in w.lower() for w in res["warnings"])
    # metadata unchanged (still the conflicting DOI, status still unmatched)
    after = json.loads(meta_path.read_text(encoding="utf-8"))
    assert "metadata_match" not in after


# 鈹€鈹€ 12. manual-confirm rejects incomplete candidate 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_manual_confirm_rejects_incomplete(tmp_path, monkeypatch):
    folder = _make_folder(tmp_path)  # no local metadata
    monkeypatch.setattr(mr, "extract_doi_from_filename", lambda name: None)
    monkeypatch.setattr(mr, "extract_doi_from_pdf_file", lambda pdf: None)
    md = "# Some title\n\nAuthor\n\n2020\n\n"
    (folder / "0000000000000001.md").write_text(md, encoding="utf-8")
    # candidate with DOI but no venue
    item = {"DOI": "10.9999/x", "title": ["Some title"], "author": [{"family": "Author"}],
            "container-title": [], "issued": {"date-parts": [[2020]]}}
    from src.discovery import resolve_crossref as rc
    monkeypatch.setattr(rc.requests, "get",
                        lambda *a, **k: _FakeResp({"message": {"items": [item]}}))
    cat = _empty_catalog(tmp_path)

    report = mr.resolve_metadata_candidates(folder, allow_network=True,
                                           rate_limiter=_zero_rate_limiter(), papers_dir=tmp_path / "papers")
    res = mr.apply_resolution(folder, report, manual_confirm=True, papers_dir=tmp_path / "papers")
    assert res["applied"] is False


# 鈹€鈹€ 12b. --candidate-id chooses specified candidate 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_candidate_id_chooses_specified(tmp_path, monkeypatch):
    md = "# Simulation of wind-induced snow transport\n\nVionnet, V.\n\nThe Cryosphere 2014\n\n"
    folder = _make_folder(tmp_path, md_text=md)
    monkeypatch.setattr(mr, "extract_doi_from_filename", lambda name: None)
    monkeypatch.setattr(mr, "extract_doi_from_pdf_file", lambda pdf: None)
    item1 = {"DOI": "10.5194/tc-8-395-2014", "title": ["Simulation of wind-induced snow transport"],
             "author": [{"given": "V.", "family": "Vionnet"}], "container-title": ["The Cryosphere"],
             "issued": {"date-parts": [[2014]]}}
    item2 = {"DOI": "10.9999/second", "title": ["Simulation of wind-induced snow transport"],
             "author": [{"given": "V.", "family": "Vionnet"}], "container-title": ["The Cryosphere"],
             "issued": {"date-parts": [[2014]]}}
    from src.discovery import resolve_crossref as rc
    monkeypatch.setattr(rc.requests, "get",
                        lambda *a, **k: _FakeResp({"message": {"items": [item1, item2]}}))
    cat = _empty_catalog(tmp_path)

    report = mr.resolve_metadata_candidates(folder, allow_network=True,
                                           rate_limiter=_zero_rate_limiter(), papers_dir=tmp_path / "papers")
    # find cand_002's doi
    cand2 = next(c for c in report.candidates if c.doi == "10.9999/second")
    res = mr.apply_resolution(folder, report, manual_confirm=True, candidate_id=cand2.candidate_id, papers_dir=tmp_path / "papers")
    assert res["applied"] is True
    assert res["chosen_candidate_id"] == cand2.candidate_id
    meta = json.loads((folder / "0000000000000001.metadata.json").read_text(encoding="utf-8"))
    assert meta["identifiers"]["doi"] == "10.9999/second"


def test_candidate_id_invalid_errors(tmp_path, monkeypatch):
    folder = _make_folder(tmp_path, doi="10.5194/tc-8-395-2014", title="T", year=2014,
                          authors=[{"full_name": "Vionnet V", "family": "Vionnet", "given": "V", "orcid": "", "affiliation": ""}],
                          journal="The Cryosphere")
    msg = _crossref_message(doi="10.5194/tc-8-395-2014", title="T", year=2014,
                            authors=[("V.", "Vionnet")], venue="The Cryosphere")
    _patch_crossref_doi(monkeypatch, msg)
    cat = _empty_catalog(tmp_path)
    report = mr.resolve_metadata_candidates(folder, allow_network=False, papers_dir=tmp_path / "papers")
    with pytest.raises(ValueError):
        mr.apply_resolution(folder, report, manual_confirm=True, candidate_id="cand_999", papers_dir=tmp_path / "papers")


# 鈹€鈹€ 13. multi-DOI conflict 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_multi_doi_conflict(tmp_path, monkeypatch):
    md = ("# Title\n\nhttps://doi.org/10.5194/tc-8-395-2014\n\n"
          "also https://doi.org/10.9999/another\n\n## Abstract\n")
    folder = _make_folder(tmp_path, md_text=md)
    monkeypatch.setattr(mr, "extract_doi_from_filename", lambda name: None)
    monkeypatch.setattr(mr, "extract_doi_from_pdf_file", lambda pdf: None)
    cat = _empty_catalog(tmp_path)
    report = mr.resolve_metadata_candidates(folder, allow_network=False, papers_dir=tmp_path / "papers")
    assert report.decision == "conflict"
    assert "multiple distinct DOIs" in report.reason or "multiple" in report.reason.lower()
    res = mr.apply_resolution(folder, report, papers_dir=tmp_path / "papers")
    assert res["applied"] is False


# 鈹€鈹€ 14. references-region DOI not auto-matched 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_references_region_doi_not_used(tmp_path, monkeypatch):
    md = ("# Title\n\nAuthor\n\n2020\n\n## Abstract\ntext\n\n"
          "## References\n\n1. Smith 2020 https://doi.org/10.9999/ref-only\n")
    folder = _make_folder(tmp_path, md_text=md)
    monkeypatch.setattr(mr, "extract_doi_from_filename", lambda name: None)
    monkeypatch.setattr(mr, "extract_doi_from_pdf_file", lambda pdf: None)
    cat = _empty_catalog(tmp_path)
    report = mr.resolve_metadata_candidates(folder, allow_network=False, papers_dir=tmp_path / "papers")
    # no header-region DOI 鈫?no_candidates (network disabled)
    assert report.decision in ("no_candidates", "rejected")
    # the references DOI must not appear as a chosen candidate doi
    assert not any(c.doi == "10.9999/ref-only" and c.decision == "auto_matched" for c in report.candidates)


# 鈹€鈹€ 14b. local-evidence fallback 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_local_evidence_fallback_auto_matches(tmp_path, monkeypatch):
    # DOI from filename, Crossref returns full metadata, but local md has NO author/year
    md = "# Some title with no author or year line\n\ntext body\n"
    folder = _make_folder(tmp_path, md_text=md)
    monkeypatch.setattr(mr, "extract_doi_from_filename", lambda name: "10.5194/tc-8-395-2014")
    monkeypatch.setattr(mr, "extract_doi_from_pdf_file", lambda pdf: None)
    msg = _crossref_message(doi="10.5194/tc-8-395-2014",
                            title="Some title with no author or year line",
                            year=2014, authors=[("V.", "Vionnet")], venue="The Cryosphere")
    _patch_crossref_doi(monkeypatch, msg)
    cat = _empty_catalog(tmp_path)
    report = mr.resolve_metadata_candidates(folder, allow_network=True,
                                           rate_limiter=_zero_rate_limiter(), papers_dir=tmp_path / "papers")
    # local year/author absent 鈫?fallback to authoritative completeness 鈫?auto_matched
    assert report.decision == "auto_matched", report.reason
    res = mr.apply_resolution(folder, report, papers_dir=tmp_path / "papers")
    assert res["applied"] is True and res["status"] == "resolved"


# 鈹€鈹€ 15/16/17. CLI three-tier write semantics 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def _run_cli(argv: list[str]) -> int:
    saved = sys.argv
    sys.argv = argv
    try:
        runpy.run_path(str(_REPO_ROOT / "scripts" / "resolve_paper_raw_metadata.py"), run_name="__main__")
        return 0
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = saved


def test_cli_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    folder = _make_folder(tmp_path, doi="10.5194/tc-8-395-2014", title="T", year=2014,
                          authors=[{"full_name": "Vionnet V", "family": "Vionnet", "given": "V", "orcid": "", "affiliation": ""}],
                          journal="The Cryosphere")
    msg = _crossref_message(doi="10.5194/tc-8-395-2014", title="T", year=2014,
                            authors=[("V.", "Vionnet")], venue="The Cryosphere")
    _patch_crossref_doi(monkeypatch, msg)
    cat = _empty_catalog(tmp_path)
    monkeypatch.syspath_prepend(str(_REPO_ROOT))
    before = (folder / "0000000000000001.metadata.json").read_text(encoding="utf-8")

    rc = _run_cli([
        "resolve_paper_raw_metadata.py", "--paper-number", "0000000000000001",
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--papers-dir", str(tmp_path / "papers"),
        "--allow-network",
    ])
    assert rc == 0
    # nothing written
    assert not (folder / "0000000000000001.metadata.candidates.json").exists()
    assert not (folder / "0000000000000001.metadata.resolve_report.json").exists()
    assert not (folder / ".import_status.json").exists()
    assert (folder / "0000000000000001.metadata.json").read_text(encoding="utf-8") == before
    out = capsys.readouterr().out
    assert "auto_matched" in out


def test_cli_write_candidates_no_apply(tmp_path, monkeypatch):
    folder = _make_folder(tmp_path, doi="10.5194/tc-8-395-2014", title="T", year=2014,
                          authors=[{"full_name": "Vionnet V", "family": "Vionnet", "given": "V", "orcid": "", "affiliation": ""}],
                          journal="The Cryosphere")
    msg = _crossref_message(doi="10.5194/tc-8-395-2014", title="T", year=2014,
                            authors=[("V.", "Vionnet")], venue="The Cryosphere")
    _patch_crossref_doi(monkeypatch, msg)
    cat = _empty_catalog(tmp_path)
    monkeypatch.syspath_prepend(str(_REPO_ROOT))
    before = (folder / "0000000000000001.metadata.json").read_text(encoding="utf-8")

    rc = _run_cli([
        "resolve_paper_raw_metadata.py", "--paper-number", "0000000000000001",
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--papers-dir", str(tmp_path / "papers"),
        "--write-candidates",
        "--allow-network",
    ])
    assert rc == 0
    assert (folder / "0000000000000001.metadata.candidates.json").exists()
    assert (folder / "0000000000000001.metadata.resolve_report.json").exists()
    patch_path = folder / "0000000000000001.metadata.patch.json"
    assert patch_path.exists()
    patch = json.loads(patch_path.read_text(encoding="utf-8"))
    assert patch["identifiers"]["doi"] == "10.5194/tc-8-395-2014"
    assert "metadata_match" not in patch
    assert (folder / ".import_status.json").exists()
    # metadata.json unchanged
    assert (folder / "0000000000000001.metadata.json").read_text(encoding="utf-8") == before


def test_cli_default_no_network_no_call(tmp_path, monkeypatch):
    md = "# Title\n\nAuthor\n\n2020\n\n"
    folder = _make_folder(tmp_path, md_text=md)
    monkeypatch.setattr(mr, "extract_doi_from_filename", lambda name: None)
    monkeypatch.setattr(mr, "extract_doi_from_pdf_file", lambda pdf: None)
    cat = _empty_catalog(tmp_path)
    monkeypatch.syspath_prepend(str(_REPO_ROOT))
    # make any network call explode if attempted
    from src.discovery import resolve_crossref as rc
    def _boom(*a, **k):
        raise AssertionError("network call attempted with default no-network")
    monkeypatch.setattr(rc.requests, "get", _boom)
    rc2 = _run_cli([
        "resolve_paper_raw_metadata.py", "--paper-number", "0000000000000001",
        "--paper-raw-dir", str(tmp_path / "paper_raw"),
        "--papers-dir", str(tmp_path / "papers"),
        "--write-candidates",
    ])
    assert rc2 == 0
    assert not (folder / "0000000000000001.metadata.patch.json").exists()


# 鈹€鈹€ 18. conservative author split 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_conservative_author_split_cjk():
    fam, giv = mr._split_name("\u738b\u6b63\u5e05")
    assert fam == "" and giv == ""  # CJK 鈫?not split


def test_conservative_author_split_single_token():
    fam, giv = mr._split_name("NASA")
    assert fam == "" and giv == ""  # single token / institution-like 鈫?not split


def test_conservative_author_split_normal():
    fam, giv = mr._split_name("Vincent Vionnet")
    assert fam == "Vionnet" and giv == "Vincent"


def test_conservative_author_split_initial_last():
    # "Vionnet V" is ambiguous (Family Initial citation form) 鈫?refuse to split
    fam, giv = mr._split_name("Vionnet V")
    assert fam == "" and giv == ""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

import src.server as server_module
from src.catalog import Catalog
from src.library import PaperLibrary
from src.prompt_builder import PromptBuilder
from src.services.v2_library import empty_catalog, empty_metadata


_ROOT = Path(__file__).resolve().parent.parent


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _fake_library(tmp_path: Path) -> tuple[Path, Path, str, str]:
    paper_number = "0000000000000001"
    paper_id = "2024_author_content_only"
    catalog_dir = tmp_path / "data" / "catalog"
    papers_dir = tmp_path / "data" / "papers"
    folder = papers_dir / paper_id
    images = folder / "images"
    images.mkdir(parents=True)

    metadata = empty_metadata(paper_id)
    metadata["title"]["original"] = "Content Only Consumer Test"
    metadata["year"] = 2024
    metadata["authors"] = [{"full_name": "Author A", "family": "Author", "given": "A", "orcid": "", "affiliation": ""}]
    metadata["first_author"]["family"] = "Author"
    metadata["first_author"]["display"] = "Author A"
    metadata["container"]["journal"] = "Consumer Journal"
    metadata["publication"]["volume"] = "1"
    metadata["publication"]["number"] = "2"
    metadata["publication"]["issue"] = "2"
    metadata["publication"]["pages"] = "3-9"
    metadata["identifiers"]["doi"] = "10.7777/content-only"
    metadata["pdf"]["sha256"] = "abc"
    metadata["pdf"]["file_size"] = 4
    metadata["metadata_match"]["status"] = "matched"

    catalog = empty_catalog()
    catalog["paper_number"] = paper_number
    catalog["paper_id"] = paper_id
    catalog["content_identity"]["content_title"] = "Content Only Consumer Test"
    catalog["classification"]["primary_domain"] = "consumer_test"
    catalog["screening"]["read_decision"] = "must_read"
    catalog["screening"]["relevance_score"] = 5
    catalog["research_card"]["research_problem"] = "Consumer path compatibility"

    md_path = folder / f"{paper_id}.md"
    pdf_path = folder / f"{paper_id}.pdf"
    metadata_path = folder / f"{paper_id}.metadata.json"
    catalog_path = folder / f"{paper_id}.catalog.json"
    _write_json(metadata_path, metadata)
    _write_json(catalog_path, catalog)
    md_path.write_text("# Content Only\n\nfull text from markdown", encoding="utf-8")
    pdf_path.write_bytes(b"%PDF")
    (images / "fig1.png").write_bytes(b"png")

    all_entry = {
        "paper_number": paper_number,
        "paper_id": paper_id,
        "asset_refs": {"markdown": "", "pdf": "", "images_dir": "", "figures": []},
        "content_identity": catalog["content_identity"],
        "classification": catalog["classification"],
        "screening": catalog["screening"],
        "research_card": catalog["research_card"],
        "evidence_profile": catalog["evidence_profile"],
        "content_notes": catalog["content_notes"],
        "provenance": catalog["provenance"],
    }
    for forbidden in ("main_md", "folder_path", "metadata_file", "catalog_file", "metadata"):
        assert forbidden not in all_entry

    all_catalog = catalog_dir / "all.catalog.json"
    _write_json(all_catalog, {"schema_version": "2.0", "updated_at": "", "papers": [all_entry]})
    _write_json(catalog_dir / "paper_index.json", {
        "schema_version": "1.1",
        "updated_at": "",
        "papers": [{
            "paper_number": paper_number,
            "paper_id": paper_id,
            "metadata_path": str(metadata_path),
            "catalog_path": str(catalog_path),
            "markdown_path": str(md_path),
            "pdf_path": str(pdf_path),
            "images_dir": str(images),
        }],
    })
    return all_catalog, papers_dir, paper_number, paper_id


def _install_fake_server(monkeypatch, all_catalog: Path, papers_dir: Path) -> TestClient:
    catalog = Catalog(all_catalog, papers_dir=papers_dir)
    library = PaperLibrary(catalog=catalog)
    monkeypatch.setattr(server_module, "ALL_CATALOG_PATH", all_catalog)
    monkeypatch.setattr(server_module, "catalog", catalog)
    monkeypatch.setattr(server_module, "library", library)
    monkeypatch.setattr(server_module, "prompt_builder", PromptBuilder(catalog=catalog, library=library))
    return TestClient(server_module.app)


def test_server_markdown_endpoint_uses_paper_index(tmp_path, monkeypatch):
    all_catalog, papers_dir, paper_number, _ = _fake_library(tmp_path)
    client = _install_fake_server(monkeypatch, all_catalog, papers_dir)

    response = client.get(f"/papers/by-number/{paper_number}/markdown")

    assert response.status_code == 200
    assert "full text from markdown" in response.text


def test_server_bibtex_endpoint_uses_metadata_not_all_catalog(tmp_path, monkeypatch):
    all_catalog, papers_dir, paper_number, _ = _fake_library(tmp_path)
    client = _install_fake_server(monkeypatch, all_catalog, papers_dir)

    response = client.post("/bibtex", json={"paper_numbers": [paper_number]})

    assert response.status_code == 200
    assert "10.7777/content-only" in response.json()["bibtex"]
    all_data = json.loads(all_catalog.read_text(encoding="utf-8"))
    assert "doi" not in json.dumps(all_data["papers"][0])


def test_server_image_endpoint_uses_paper_index_images_dir(tmp_path, monkeypatch):
    all_catalog, papers_dir, paper_number, _ = _fake_library(tmp_path)
    client = _install_fake_server(monkeypatch, all_catalog, papers_dir)

    response = client.get(f"/papers/by-number/{paper_number}/images/fig1.png")

    assert response.status_code == 200
    assert response.content == b"png"


def test_prompt_builder_fulltext_uses_paper_number(tmp_path):
    all_catalog, papers_dir, paper_number, paper_id = _fake_library(tmp_path)
    catalog = Catalog(all_catalog, papers_dir=papers_dir)
    builder = PromptBuilder(catalog=catalog, library=PaperLibrary(catalog=catalog))

    result = builder.build_fulltext_prompt("How does this work?", [paper_number])

    assert result["success"] is True
    assert paper_id in result["included_papers"]
    assert "full text from markdown" in result["prompt"]


def test_prompt_builder_catalog_entry_prompt_no_folder_path(tmp_path):
    all_catalog, papers_dir, paper_number, paper_id = _fake_library(tmp_path)
    catalog = Catalog(all_catalog, papers_dir=papers_dir)
    builder = PromptBuilder(catalog=catalog, library=PaperLibrary(catalog=catalog))

    result = builder.build_catalog_entry_prompt(paper_number)

    assert result["success"] is True
    assert paper_id in result["prompt"]


def test_src_library_wrapper_uses_paper_index(tmp_path):
    all_catalog, papers_dir, paper_number, _ = _fake_library(tmp_path)
    library = PaperLibrary(catalog=Catalog(all_catalog, papers_dir=papers_dir))

    assert library.markdown_path(paper_number).name.endswith(".md")
    assert library.read_markdown(paper_number).startswith("# Content Only")
    assert library.image_path(paper_number, "fig1.png").exists()


def test_no_v1_1_claims_in_active_skills():
    docs = [
        _ROOT / "skills" / "paper_raw_catalog_curator" / "SKILL.md",
        _ROOT / "skills" / "paper_raw_catalog_curator" / "CLAUDE.md",
        _ROOT / "skills" / "paper_raw_catalog_curator" / "README.md",
        _ROOT / "skills" / "literature_library_manager" / "SKILL.md",
        _ROOT / "skills" / "literature_library_manager" / "CLAUDE.md",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in docs).lower()

    assert "v1.1 catalog" not in text
    assert "catalog v1.1" not in text
    assert "catalog（v1.1" not in text
    assert "catalog + metadata patch" not in text
    assert "生成 catalog 与 metadata patch" not in text


def test_active_consumers_do_not_read_legacy_all_catalog_fields():
    for rel in ("src/server.py", "src/library.py", "src/prompt_builder.py"):
        text = (_ROOT / rel).read_text(encoding="utf-8")
        assert '["main_md"]' not in text
        assert '["metadata_file"]' not in text
        assert '["folder_path"]' not in text
        assert '["catalog_file"]' not in text

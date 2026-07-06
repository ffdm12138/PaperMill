"""E2E structural smoke — the one unique behavior not covered elsewhere.

The former big regression suite (commit / dedup / numbering / rebuild / bibtex
/ destructive / paper.md / marker-conflict / PaperLibrary.resolve) was removed
because every one of those behaviors is covered by the layered tests:
  - commit / dedup / numbering / rebuild -> tests/integration/test_v2_library.py,
    tests/integration/test_paper_raw_commit_atomic.py,
    tests/integration/test_paper_raw_to_formal_papers.py
  - bibtex_from_metadata               -> tests/unit/test_reference_generation.py
  - paper.md / screening rejection      -> tests/integration/test_v2_library.py,
    tests/contract/test_catalog_repository_state.py
  - ledger marker conflict              -> tests/integration/test_v2_library.py
  - PaperLibrary.resolve                -> tests/integration/test_v2_library.py

The single retained test is the only place that exercises
``build_compact_catalog_text`` and the content-only compact-catalog contract
(screening fields present, bibliography not leaked). It also serves as a
minimal happy-path smoke: paper_raw -> commit -> rebuild -> compact text.
"""
import json
from pathlib import Path

from src.services.v2_library import (
    AllCatalogBuilder,
    PaperNumberLedger,
    V2PaperCommitService,
)
from src.catalog import build_compact_catalog_text
from src.services.paper_library import PaperLibrary


def _curated_raw(root: Path, pid: str, *, doi: str = "10.1/x", year: int = 2024,
                 family: str | None = None, tz: str | None = None,
                 pdf_content: bytes = b"%PDF-X") -> Path:
    """Build a complete 16-digit paper_raw source folder ready for formalize."""
    from tests.helpers.paper_raw_factory import make_staged_source

    parts = pid.split("_")
    family = parts[1] if len(parts) > 1 else (family or "wang")
    tz = tz if tz is not None else ("_".join(parts[2:]) or "测试论文")
    source_id = PaperNumberLedger(root / "catalog" / "paper_number_ledger.json").peek_next_numbers(1)[0]
    return make_staged_source(
        root,
        source_id,
        title_zh=tz,
        title_original=f"Paper {pid}",
        doi=doi,
        family=family,
        year=year,
        pdf_bytes=pdf_content,
        catalog_domain="test",
    )


def _reserve_staged_marker_on_ledger(ledger: Path, folder: Path) -> None:
    number = PaperNumberLedger(ledger).paper_number_from_marker(folder) or folder.name
    PaperNumberLedger(ledger).reserve_specific_for_paper_raw(number, folder)


def _commit_svc(svc: V2PaperCommitService, folder: Path) -> dict:
    """formalize (using svc's ledger) then commit — for tests that hold a svc handle."""
    from src.services.paper_raw_formalizer import PaperRawFormalizationService

    _reserve_staged_marker_on_ledger(svc.ledger.path, folder)
    formalized = PaperRawFormalizationService(
        paper_raw_dir=folder.parent, papers_dir=svc.papers_dir,
        ledger_path=svc.ledger.path, all_catalog_path=svc.all_catalog_path,
    ).formalize(folder)
    if not formalized.get("success"):
        return formalized
    return svc.commit_paper_raw(formalized["folder"])


def test_compact_catalog_smoke_includes_screening_fields_and_hides_bibliography(tmp_path):
    """Minimal happy-path smoke + the unique compact-catalog contract.

    Drives paper_raw -> commit -> rebuild -> compact text, and verifies:
    - content-only compact text contains screening fields + paper_number
    - content-only compact text does NOT leak bibliographic bits (venue/doi)
    - include_metadata=True joins bibliography from metadata (display layer)
    """
    f = _curated_raw(tmp_path, "2024_wang_测试论文")
    svc = V2PaperCommitService(papers_dir=tmp_path / "papers",
                                all_catalog_path=tmp_path / "c" / "all.json",
                                ledger_path=tmp_path / "c" / "l.json")
    _commit_svc(svc, f)
    AllCatalogBuilder(tmp_path / "papers", tmp_path / "c" / "all.json",
                      PaperNumberLedger(tmp_path / "c" / "l.json")).build(write=True)
    data = json.loads((tmp_path / "c" / "all.json").read_text(encoding="utf-8"))
    lib = PaperLibrary(all_catalog_path=tmp_path / "c" / "all.json", papers_dir=tmp_path / "papers")
    # default include_metadata=False: content-only — paper_number + content, no bibliography
    txt_content = build_compact_catalog_text(data["papers"], library=lib)
    for kw in ("0000000000000001", "pending", "method:", "usefulness:"):
        assert kw in txt_content, f"compact catalog (content-only) missing: {kw}"
    # bibliographic bits must NOT leak in the default content-only view.
    # (year/author surname also appear in paper_id, so only check venue+doi,
    #  which are purely bibliographic and never part of paper_id.)
    for kw in ("Test Journal", "10.1/x"):
        assert kw not in txt_content, f"content-only view leaked bibliography: {kw}"
    # include_metadata=True: bibliographic bits joined from metadata (display-layer only)
    txt_meta = build_compact_catalog_text(data["papers"], library=lib, include_metadata=True)
    for kw in ("0000000000000001", "2024", "wang", "Test Journal", "10.1/x",
               "pending", "method:", "usefulness:"):
        assert kw in txt_meta, f"compact catalog (include_metadata) missing: {kw}"

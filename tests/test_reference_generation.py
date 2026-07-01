import warnings
from types import SimpleNamespace

from src.services.metadata_enrichment_service import normalize_crossref_metadata
from src.services.metadata_resolver import patch_from_enrichment
from src.services.v2_library import bibtex_from_metadata, format_reference_from_metadata


def _bi_metadata(doi: str = "10.1038/s41586-023-06185-3") -> dict:
    return {
        "title": {"original": "Accurate medium-range global weather forecasting with 3D neural networks"},
        "authors": [
            {"full_name": "Kaifeng Bi", "family": "Bi", "given": "Kaifeng"},
            {"full_name": "Lingxi Xie", "family": "Xie", "given": "Lingxi"},
            {"full_name": "Hengheng Zhang", "family": "Zhang", "given": "Hengheng"},
            {"full_name": "Xin Chen", "family": "Chen", "given": "Xin"},
            {"full_name": "Xiaotao Gu", "family": "Gu", "given": "Xiaotao"},
            {"full_name": "Qi Tian", "family": "Tian", "given": "Qi"},
        ],
        "year": 2023,
        "container": {"journal": "Nature", "publisher": "Springer Science and Business Media LLC"},
        "publication": {"volume": "619", "number": "7970", "issue": "7970", "pages": "533-538", "article_number": ""},
        "identifiers": {"doi": doi},
        "links": {"url": "https://doi.org/10.1038/s41586-023-06185-3"},
    }


def test_normalize_crossref_metadata_extracts_publication_fields():
    raw = {
        "DOI": "10.1038/S41586-023-06185-3",
        "title": ["Accurate medium-range global weather forecasting with 3D neural networks"],
        "author": [{"family": "Bi", "given": "Kaifeng"}, {"family": "Xie", "given": "Lingxi"}],
        "container-title": ["Nature"],
        "publisher": "Springer Science and Business Media LLC",
        "volume": "619",
        "issue": "7970",
        "page": "533-538",
        "article-number": "06185",
        "ISSN": ["0028-0836", "1476-4687"],
        "URL": "https://doi.org/10.1038/s41586-023-06185-3",
        "issued": {"date-parts": [[2023, 7, 20]]},
    }

    meta = normalize_crossref_metadata(raw)

    assert meta["doi"] == "10.1038/s41586-023-06185-3"
    assert meta["journal"] == "Nature"
    assert meta["volume"] == "619"
    assert meta["issue"] == "7970"
    assert meta["number"] == "7970"
    assert meta["pages"] == "533-538"
    assert meta["article_number"] == "06185"
    assert meta["issn"] == "0028-0836"
    assert meta["published"] == "2023-07-20"


def test_patch_from_enrichment_maps_publication_fields_to_metadata():
    result = SimpleNamespace(
        title="T",
        year=2023,
        doi="10.1/x",
        venue="Nature",
        publisher="Publisher",
        volume="619",
        number="7970",
        issue="7970",
        pages="533-538",
        article_number="06185",
        issn="0028-0836",
        url="https://doi.org/10.1/x",
        published="2023-07-20",
        authors=[],
    )

    patch = patch_from_enrichment("000001", result)

    assert patch["identifiers"]["doi"] == "10.1/x"
    assert patch["identifiers"]["issn"] == "0028-0836"
    assert patch["container"]["journal"] == "Nature"
    assert patch["container"]["publisher"] == "Publisher"
    assert patch["publication"]["volume"] == "619"
    assert patch["publication"]["number"] == "7970"
    assert patch["publication"]["issue"] == "7970"
    assert patch["publication"]["pages"] == "533-538"
    assert patch["publication"]["article_number"] == "06185"
    assert patch["links"]["url"] == "https://doi.org/10.1/x"
    assert patch["date"]["published"] == "2023-07-20"


def test_bibtex_from_metadata_uses_publication_fields_and_existing_doi_only():
    bib = bibtex_from_metadata(_bi_metadata(), key="bi2023")

    assert "volume = {619}" in bib
    assert "number = {7970}" in bib
    assert "pages = {533-538}" in bib
    assert "doi = {10.1038/s41586-023-06185-3}" in bib
    assert "url = {https://doi.org/10.1038/s41586-023-06185-3}" in bib

    no_doi = bibtex_from_metadata(_bi_metadata(doi=""), key="bi2023")
    assert "doi =" not in no_doi


def test_format_reference_from_metadata_apa_and_missing_doi_warning():
    ref = format_reference_from_metadata(_bi_metadata())

    assert ref == (
        "Bi, K., Xie, L., Zhang, H., Chen, X., Gu, X., & Tian, Q. (2023). "
        "Accurate medium-range global weather forecasting with 3D neural networks. "
        "Nature, 619(7970), 533-538. doi: 10.1038/s41586-023-06185-3"
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ref_without_doi = format_reference_from_metadata(_bi_metadata(doi=""))

    assert "doi:" not in ref_without_doi
    assert any("metadata.identifiers.doi is empty" in str(item.message) for item in caught)

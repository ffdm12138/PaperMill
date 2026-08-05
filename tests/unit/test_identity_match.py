"""Decision-policy tests for identity_match.decide_identity.

Invariants under test: weak/medium DOIs never trigger identifier_conflict;
strong exact requested hit wins over all weak evidence; related_version
requires a DOI family AND strong bibliographic consistency; empty requested
DOI routes to the stable-identifier branch; contradictory is
high-precision (split thresholds, author completeness).
"""
from __future__ import annotations

from difflib import SequenceMatcher

import pytest

from src.metadata.identity_match import (
    bibliographic_strength,
    decide_identity,
    known_version_family,
)
from src.metadata.pdf_identity import (
    DoiEvidence,
    ExtractedIdentifier,
    PdfIdentityEvidence,
)

REQUESTED = "10.5194/egusphere-2025-5135"
PUBLISHED = "10.5194/acp-26-9643-2026"
FOREIGN = "10.1007/s10546-021-00629"
TITLE = "A Study of Snow"


def metadata(
    *,
    doi: str = REQUESTED,
    title: str = TITLE,
    year: int | None = 2025,
    authors: tuple[str, ...] = ("Smith", "Jones"),
    identifiers: dict | None = None,
    entry_type: str = "article",
    booktitle: str | None = None,
) -> dict:
    value: dict = {
        "identifiers": {"doi": doi} if doi else {},
        "title": {"original": title},
        "year": year,
        "authors": [{"family": family} for family in authors],
        "first_author": {"family": authors[0]} if authors else {},
        "entry_type": entry_type,
    }
    if identifiers:
        value["identifiers"].update(identifiers)
    if booktitle:
        value["container"] = {"booktitle": booktitle}
    return value


def evidence(
    dois: list[tuple],
    *,
    title: str | None = TITLE,
    year: int | None = 2025,
    families: tuple[str, ...] = ("Smith", "Jones"),
    identifiers: tuple[ExtractedIdentifier, ...] = (),
    confidence: str = "explicit_identifier",
) -> PdfIdentityEvidence:
    items = []
    for entry in dois:
        doi, source, conf = entry[0], entry[1], entry[2]
        labeled = bool(entry[3]) if len(entry) > 3 else True
        items.append(
            DoiEvidence(
                doi=doi,
                source=source,
                page_number=1 if source == "first_page" else None,
                labeled=labeled,
                context="",
                confidence=conf,
            )
        )
    return PdfIdentityEvidence(
        pdf_sha256="x",
        doi_evidence=tuple(items),
        canonical_title=title,
        publication_year=year,
        first_author_family=families[0] if families else None,
        author_families=tuple(families),
        extracted_identifiers=identifiers,
        extraction_sources=("test",),
        confidence=confidence,
        parser_failures=(),
        warnings=(),
    )


def decide(meta: dict, ev: PdfIdentityEvidence) -> dict:
    decision = decide_identity(meta, ev, requested_doi=meta["identifiers"].get("doi") or "")
    return decision.to_dict()


# ── Rule 1: strong exact wins over everything ───────────────────────────

class TestStrongExactWins:
    def test_strong_requested_beats_dozens_of_reference_dois(self) -> None:
        reference_dois = [(f"10.9999/ref-{i:04d}26", "reference_list", "weak") for i in range(30)]
        ev = evidence([(REQUESTED, "first_page", "strong"), *reference_dois])
        result = decide(metadata(), ev)
        assert result["match_status"] == "matched"
        assert result["match_method"] == "doi_exact"
        assert result["pdf_primary_doi"] == REQUESTED

    def test_strong_requested_beats_weak_foreign(self) -> None:
        ev = evidence(
            [(REQUESTED, "first_page", "strong"), (FOREIGN, "body_text", "weak")]
        )
        result = decide(metadata(), ev)
        assert result["match_status"] == "matched"

    def test_xmp_strong_exact(self) -> None:
        ev = evidence([(REQUESTED, "xmp_metadata", "strong")], title=None, families=())
        result = decide(metadata(), ev)
        assert result["match_status"] == "matched"
        assert result["match_method"] == "doi_exact"

    def test_unlabeled_first_page_needs_bibliographic_strength(self) -> None:
        # Same DOI, same title/author/year -> strong via biblio.
        ev = evidence([(REQUESTED, "first_page", "medium", False)])
        result = decide(metadata(), ev)
        assert result["match_status"] == "matched"
        assert result["match_method"] == "doi_medium_bibliographic"

    def test_medium_requested_without_bib_is_ambiguous(self) -> None:
        ev = evidence(
            [(REQUESTED, "first_page", "medium", False)],
            title="A Completely Different Paper",
            families=("Zhang",),
        )
        result = decide(metadata(), ev)
        assert result["match_status"] == "ambiguous"

    def test_first_page_strong_downgraded_without_bib(self) -> None:
        # Labeled first-page DOI with NO bibliographic corroboration
        # (title differs, authors differ, year differs) -> medium tier.
        ev = evidence(
            [(REQUESTED, "first_page", "strong")],
            title="A Completely Different Paper",
            families=("Zhang",),
            year=1999,
        )
        result = decide(metadata(), ev)
        assert result["match_status"] == "ambiguous"

    def test_year_only_never_strong(self) -> None:
        # Final contract: a labeled first-page DOI corroborated ONLY by a
        # compatible year stays medium -> ambiguous, never matched.
        ev = evidence(
            [(REQUESTED, "first_page", "strong")],
            title="A Completely Different Paper",
            families=("Zhang",),
        )
        result = decide(metadata(), ev)
        assert result["match_status"] == "ambiguous"
        assert result["details"]["evidence_tiers"]["strong"] == []

    def test_title_match_is_strong(self) -> None:
        ev = evidence([(REQUESTED, "first_page", "strong")])
        result = decide(metadata(), ev)
        assert result["match_status"] == "matched"

    def test_author_and_year_is_strong(self) -> None:
        # Title extraction failed (None) but reliable author overlap plus a
        # compatible year corroborates -> strong.
        ev = evidence(
            [(REQUESTED, "first_page", "strong")],
            title=None,
        )
        result = decide(metadata(), ev)
        assert result["match_status"] == "matched"

    def test_single_common_surname_and_year_not_strong(self) -> None:
        # A single common-surname overlap is NOT reliable: stays medium.
        ev = evidence(
            [(REQUESTED, "first_page", "strong")],
            title=None,
            families=("Smith",),
        )
        result = decide(metadata(), ev)
        assert result["match_status"] == "ambiguous"

    def test_single_uncommon_surname_and_year_is_strong(self) -> None:
        meta = metadata(authors=("Xylophone", "Qwerty"))
        ev = evidence(
            [(REQUESTED, "first_page", "strong")],
            title=None,
            families=("Xylophone",),
        )
        result = decide(meta, ev)
        assert result["match_status"] == "matched"

    def test_two_common_surnames_and_year_is_strong(self) -> None:
        ev = evidence(
            [(REQUESTED, "first_page", "strong")],
            title=None,
            families=("Smith", "Jones"),
        )
        result = decide(metadata(), ev)
        assert result["match_status"] == "matched"

    def test_comments_on_doi_never_strong(self) -> None:
        # "This article comments on doi:…" with no corroboration stays
        # medium and can never drive a match.
        ev = evidence(
            [(FOREIGN, "first_page", "strong")],
            title="A Completely Different Paper",
            families=("Zhang",),
            year=1999,
        )
        result = decide(metadata(), ev)
        assert result["match_status"] == "ambiguous"

    def test_data_availability_doi_never_strong(self) -> None:
        # A dataset/supplement DOI line ("Data availability: …") is a
        # labeled first-page DOI with no corroboration -> medium; it can
        # never match the article itself.
        data_doi = "10.5281/zenodo.123456"
        ev = evidence(
            [(data_doi, "first_page", "strong")],
            title="A Completely Different Paper",
            families=("Zhang",),
            year=1999,
        )
        result = decide(metadata(), ev)
        assert result["match_status"] == "ambiguous"
        assert result["details"]["evidence_tiers"]["strong"] == []


# ── Medium/weak never conflict ─────────────────────────────────────────

class TestMediumNeverConflicts:
    def test_medium_foreign_only_ambiguous(self) -> None:
        ev = evidence([(FOREIGN, "document_info", "medium")])
        result = decide(metadata(), ev)
        assert result["match_status"] == "ambiguous"
        assert result["match_method"] != "identifier_conflict"

    def test_subject_keywords_medium_never_conflict(self) -> None:
        ev = evidence([(FOREIGN, "document_info", "medium")])
        result = decide(metadata(), ev)
        assert result["match_status"] == "ambiguous"

    def test_reference_doi_never_conflict(self) -> None:
        ev = evidence([(FOREIGN, "reference_list", "weak")])
        result = decide(metadata(), ev)
        assert result["match_status"] in {"unverifiable", "ambiguous"}

    def test_raw_bytes_never_conflict(self) -> None:
        ev = evidence([(FOREIGN, "raw_bytes", "weak")], title=None, families=())
        result = decide(metadata(), ev)
        assert result["match_status"] in {"unverifiable", "ambiguous"}


# ── Candidate arbitration ──────────────────────────────────────────────

class TestCandidateArbitration:
    def test_multiple_primary_candidates_ambiguous_even_with_family(self) -> None:
        # Two strong foreign DOIs, one of which forms a family with the
        # requested DOI: ambiguity wins (Rule 7 before version relations).
        ev = evidence(
            [(PUBLISHED, "first_page", "strong"), (FOREIGN, "first_page", "strong")]
        )
        result = decide(metadata(), ev)
        assert result["match_status"] == "ambiguous"

    def test_unique_structured_primary_wins_over_first_page(self) -> None:
        # The XMP structured DOI (FOREIGN) beats the first-page candidate
        # (PUBLISHED, which would otherwise form a family with the
        # requested DOI).  It has no family relation and the bibliographic
        # fields contradict -> the only conflict path: unique structured
        # primary + full negative bibliographic evidence.
        ev = evidence(
            [
                (FOREIGN, "xmp_metadata", "strong"),
                (PUBLISHED, "first_page", "strong"),
            ],
            title="Completely Different Paper",
            families=("Zhang",),
        )
        result = decide(metadata(), ev)
        assert result["pdf_primary_doi"] == FOREIGN
        assert result["match_status"] == "identifier_conflict"

    def test_two_structured_dois_ambiguous(self) -> None:
        ev = evidence(
            [
                (PUBLISHED, "xmp_metadata", "strong"),
                (FOREIGN, "xmp_metadata", "strong"),
            ]
        )
        result = decide(metadata(), ev)
        assert result["match_status"] == "ambiguous"

    def test_no_trusted_evidence_unverifiable(self) -> None:
        ev = evidence([(FOREIGN, "reference_list", "weak")], title=None, families=())
        result = decide(metadata(), ev)
        assert result["match_status"] == "unverifiable"


# ── Version relations ──────────────────────────────────────────────────

class TestVersionRelations:
    def test_family_with_bibliographic_strength_related_version(self) -> None:
        ev = evidence([(PUBLISHED, "first_page", "strong")], year=2026)
        result = decide(metadata(year=2025), ev)
        assert result["match_status"] == "related_version"
        assert result["match_method"] == "version_relation"
        assert result["pdf_primary_doi"] == PUBLISHED
        relation = result["relation"]
        assert relation["kind"] == "discussion_to_journal"
        assert relation["direction"] == "preprint_to_published"
        assert relation["year_evidence"] == "close"

    def test_year_diff_one_ok(self) -> None:
        # The real case: egusphere-2025 -> acp-26-9643-2026 (1 year apart).
        ev = evidence([(PUBLISHED, "first_page", "strong")], year=2026)
        result = decide(metadata(year=2025), ev)
        assert result["match_status"] == "related_version"

    def test_published_to_preprint_direction(self) -> None:
        ev = evidence([(REQUESTED, "first_page", "strong")], year=2025)
        result = decide(metadata(doi=PUBLISHED, year=2026), ev)
        assert result["match_status"] == "related_version"
        assert result["relation"]["direction"] == "published_to_preprint"

    def test_year_missing_still_related_version(self) -> None:
        ev = evidence([(PUBLISHED, "first_page", "strong")], year=None)
        result = decide(metadata(year=2025), ev)
        assert result["match_status"] == "related_version"
        assert result["relation"]["year_evidence"] == "missing"

    def test_family_without_bibliographic_strength_ambiguous(self) -> None:
        ev = evidence(
            [(PUBLISHED, "first_page", "strong")],
            title="A Completely Different Paper",
            families=("Zhang",),
        )
        result = decide(metadata(), ev)
        assert result["match_status"] == "ambiguous"

    def test_arxiv_to_journal_needs_bibliographic_strength(self) -> None:
        arxiv = "10.48550/arxiv.2201.12345"
        meta = metadata(doi=arxiv, title=TITLE, year=2022)
        ev = evidence([(PUBLISHED, "first_page", "strong")], year=2022)
        result = decide(meta, ev)
        assert result["match_status"] == "related_version"
        assert result["relation"]["kind"] == "preprint_to_journal"
        # Without bibliographic consistency the family alone is ambiguous.
        ev_bad = evidence(
            [(PUBLISHED, "first_page", "strong")],
            title="Unrelated Work",
            families=("Zhang",),
        )
        result_bad = decide(meta, ev_bad)
        assert result_bad["match_status"] == "ambiguous"

    def test_prefix_migration_requires_bib(self) -> None:
        old = "10.3189/2020jog66.103"
        new = "10.1017/jog.2020.66.103"
        meta = metadata(doi=old, title=TITLE, year=2020)
        ev = evidence([(new, "first_page", "strong")], year=2020)
        result = decide(meta, ev)
        assert result["match_status"] == "related_version"
        assert result["relation"]["kind"] == "journal_prefix_migration"
        ev_bad = evidence(
            [(new, "first_page", "strong")], title="Unrelated", families=("Zhang",)
        )
        assert decide(meta, ev_bad)["match_status"] == "ambiguous"

    def test_known_version_family_both_directions(self) -> None:
        assert known_version_family(REQUESTED, PUBLISHED) == (
            "discussion_to_journal",
            "preprint_to_published",
        )
        assert known_version_family(PUBLISHED, REQUESTED) == (
            "discussion_to_journal",
            "published_to_preprint",
        )
        assert known_version_family(FOREIGN, PUBLISHED) is None

    def test_modern_egu_discussion_format_family(self) -> None:
        # tc-2023-46 (modern discussion format) <-> tc-17-3041-2023 (journal).
        assert known_version_family(
            "10.5194/tc-2023-46", "10.5194/tc-17-3041-2023"
        ) == ("discussion_to_journal", "preprint_to_published")
        assert known_version_family(
            "10.5194/tc-17-3041-2023", "10.5194/tc-2023-46"
        ) == ("discussion_to_journal", "published_to_preprint")
        # Different journals are NOT a family.
        assert known_version_family(
            "10.5194/tc-2023-46", "10.5194/acp-17-3041-2023"
        ) is None

    def test_prefix_migration_identical_suffix_family(self) -> None:
        # Annals of Glaciology moved DOIs verbatim from 10.3189 to 10.1017.
        assert known_version_family(
            "10.1017/s0260305500008028", "10.3189/s0260305500008028"
        ) == ("journal_prefix_migration", "published_to_preprint")
        # Different suffixes are not a migration family.
        assert known_version_family(
            "10.1017/s0260305500008028", "10.3189/s0260305500009999"
        ) is None

    def test_angeocom_family(self) -> None:
        assert known_version_family(
            "10.5194/angeocom-32-669-2014", "10.5194/angeo-32-669-2014"
        ) == ("discussion_to_journal", "preprint_to_published")

    def test_prefix_migration_conflict_now_ambiguous_without_bib(self) -> None:
        # The previously-false-conflict case: 10.1017/s0260305500008028 vs
        # 10.3189/s0260305500008028 is a family; without bibliographic
        # strength it is ambiguous, NEVER identifier_conflict.
        meta = metadata(
            doi="10.1017/s0260305500008028",
            title="The Annals of Glaciology Paper",
            year=1983,
        )
        ev = evidence(
            [("10.3189/s0260305500008028", "xmp_metadata", "strong")],
            title="The Creative Commons Attribution License",
            families=("Zhang",),
            year=1983,
        )
        result = decide(meta, ev)
        assert result["match_status"] == "ambiguous"


# ── identifier_conflict precision ──────────────────────────────────────

class TestIdentifierConflictPrecision:
    def test_first_page_foreign_no_corroboration_is_ambiguous(self) -> None:
        # A labeled foreign first-page DOI with NO bibliographic
        # corroboration (title, authors, and year all differ) stays
        # medium -> ambiguous (human review via confirm script).
        ev = evidence(
            [(FOREIGN, "first_page", "strong")],
            title="Completely Different Topic Altogether",
            families=("Zhang", "Li"),
            year=1999,
        )
        result = decide(metadata(), ev)
        assert result["match_status"] == "ambiguous"

    def test_first_page_foreign_year_only_contradiction_is_ambiguous(
        self,
    ) -> None:
        # A labeled foreign first-page DOI with a compatible year but no
        # title/author corroboration is medium under the final contract ->
        # ambiguous, never conflict (year-only cannot upgrade evidence).
        ev = evidence(
            [(FOREIGN, "first_page", "strong")],
            title="Completely Different Topic Altogether",
            families=("Zhang", "Li"),
        )
        result = decide(metadata(), ev)
        assert result["match_status"] == "ambiguous"

    def test_structured_foreign_contradiction_is_conflict(self) -> None:
        # The unique structured (XMP / Document-Info) primary DOI plus full
        # negative bibliographic evidence is the classic conflict path.
        ev = evidence(
            [(FOREIGN, "xmp_metadata", "strong")],
            title="Completely Different Topic Altogether",
            families=("Zhang", "Li"),
        )
        result = decide(metadata(), ev)
        assert result["match_status"] == "identifier_conflict"
        assert result["pdf_primary_doi"] == FOREIGN

    def test_middle_band_title_not_conflict(self) -> None:
        # Title similarity ~0.69 (between 0.55 and 0.85): the match and
        # conflict thresholds are split, so a mid-band title is not
        # contradictory and a structured foreign DOI is ambiguous, never
        # conflict.
        meta_title = "snow and ice in the high mountains"
        pdf_title = "snow and ice in the lower valleys"
        ratio = SequenceMatcher(None, meta_title, pdf_title).ratio()
        assert 0.55 < ratio < 0.85
        ev = evidence(
            [(FOREIGN, "xmp_metadata", "strong")],
            title=pdf_title,
            families=("Smith", "Jones"),
        )
        result = decide(metadata(title=meta_title), ev)
        assert result["match_status"] == "ambiguous"

    def test_incomplete_authors_not_author_conflict(self) -> None:
        # Metadata has no authors; the PDF title clearly differs, but a
        # conflict requires BOTH title and authors to be decisive.
        meta = metadata(authors=())
        ev = evidence(
            [(FOREIGN, "first_page", "strong")],
            title="Totally Different Topic",
            families=("Zhang",),
        )
        result = decide(meta, ev)
        assert result["match_status"] == "ambiguous"

    def test_positive_bibliographic_ambiguous_not_conflict(self) -> None:
        # Same paper, different DOI, no known family -> ambiguous.
        ev = evidence([(FOREIGN, "first_page", "strong")])
        result = decide(metadata(), ev)
        assert result["match_status"] == "ambiguous"


# ── Stable identifier branch ───────────────────────────────────────────

class TestStableIdentifierBranch:
    def test_no_doi_metadata_uses_stable_branch_not_foreign_flow(self) -> None:
        # PDF has a strong foreign DOI but metadata has no DOI: the stable
        # branch runs and no conflict is ever produced.
        meta = metadata(doi="", identifiers={"handle": "20.500.12345/abc"},
                        title=TITLE, year=2025)
        ev = evidence(
            [(FOREIGN, "first_page", "strong")],
            identifiers=(
                ExtractedIdentifier(type="handle", kind="work", value="20.500.12345/abc"),
            ),
        )
        result = decide(meta, ev)
        assert result["match_status"] == "matched"
        assert result["match_method"] == "stable_identifier_exact"

    def test_work_identifier_exact_match(self) -> None:
        meta = metadata(doi="", identifiers={"arxiv": "2201.12345"},
                        title=TITLE, year=2025)
        ev = evidence(
            [],
            identifiers=(
                ExtractedIdentifier(type="arxiv", kind="work", value="2201.12345"),
            ),
        )
        result = decide(meta, ev)
        assert result["match_status"] == "matched"

    def test_no_match_unverifiable_even_with_foreign_doi(self) -> None:
        meta = metadata(doi="", title=TITLE, year=2025)
        ev = evidence([(FOREIGN, "first_page", "strong")])
        result = decide(meta, ev)
        assert result["match_status"] == "unverifiable"

    def test_isbn_same_but_chapter_title_different_not_matched(self) -> None:
        meta = metadata(
            doi="",
            identifiers={"isbn": "978-1-2345-6789-0"},
            title=TITLE,
            year=2021,
            entry_type="incollection",
            booktitle="The Big Snow Book",
        )
        ev = evidence(
            [],
            title="A Completely Different Chapter",
            identifiers=(
                ExtractedIdentifier(
                    type="isbn", kind="container", value="978-1-2345-6789-0"
                ),
            ),
        )
        result = decide(meta, ev)
        assert result["match_status"] == "unverifiable"

    def test_isbn_container_consistent_chapter_matched(self) -> None:
        meta = metadata(
            doi="",
            identifiers={"isbn": "978-1-2345-6789-0"},
            title=TITLE,
            year=2021,
            entry_type="incollection",
            booktitle="The Big Snow Book",
        )
        ev = evidence(
            [],
            title=TITLE,
            identifiers=(
                ExtractedIdentifier(
                    type="isbn", kind="container", value="978-1-2345-6789-0"
                ),
            ),
        )
        result = decide(meta, ev)
        assert result["match_status"] == "matched"


# ── Extraction failure and unverifiable ────────────────────────────────

class TestExtractionFailure:
    def test_unreadable_evidence_extraction_failed(self) -> None:
        ev = evidence([], confidence="unreadable")
        result = decide(metadata(), ev)
        assert result["match_status"] == "extraction_failed"

    def test_scanned_pdf_no_text_unverifiable(self) -> None:
        # A scanned PDF yields no usable identity evidence: unverifiable,
        # never mismatch.
        ev = evidence([], title=None, year=None, families=())
        result = decide(metadata(), ev)
        assert result["match_status"] == "unverifiable"


# ── Details traceability ───────────────────────────────────────────────

class TestDecisionTrace:
    def test_rules_and_tiers_recorded(self) -> None:
        ev = evidence([(REQUESTED, "first_page", "strong")])
        result = decide(metadata(), ev)
        assert result["details"]["rules_applied"] == ["rule1_strong_doi_exact"]
        assert REQUESTED in result["details"]["evidence_tiers"]["strong"]
        assert result["details"]["bibliographic"]["title_match"] is True

    def test_relation_recorded_in_details(self) -> None:
        ev = evidence([(PUBLISHED, "first_page", "strong")], year=2026)
        result = decide(metadata(year=2025), ev)
        assert result["details"]["relation"]["kind"] == "discussion_to_journal"


# ── bibliographic_strength unit checks ─────────────────────────────────

class TestBibliographicStrength:
    def test_year_missing_is_insufficient_not_negative(self) -> None:
        ev = evidence([], year=None)
        strength = bibliographic_strength(metadata(), ev)
        assert strength.year_evidence == "missing"
        assert strength.year_compatible is False
        assert strength.verdict != "contradictory"

    def test_year_diff_one_without_family_close(self) -> None:
        # Without a family hint the tolerance is +/-1 (online/print).
        ev = evidence([], year=2026)
        strength = bibliographic_strength(metadata(year=2025), ev)
        assert strength.year_evidence == "close"
        # +/-2 without a family hint is out of range.
        strength_two = bibliographic_strength(metadata(year=2024), ev)
        assert strength_two.year_evidence == "conflict"
        # A family hint widens the tolerance to +/-2.
        strength_family = bibliographic_strength(
            metadata(year=2024), ev, family_hint="discussion_to_journal"
        )
        assert strength_family.year_evidence == "close"

    def test_empty_titles_not_a_match_or_conflict(self) -> None:
        ev = evidence([], title=None)
        strength = bibliographic_strength(metadata(), ev)
        assert strength.title_similarity is None
        assert strength.title_match is False
        assert strength.title_conflict is False
        assert strength.verdict != "contradictory"

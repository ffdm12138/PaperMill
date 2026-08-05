"""Noisy-candidate DOI cleaning layer (pdf_identity v2 prerequisite).

Covers: decoration prefixes, URL-decode, NFC, bracket balance, trailing
punctuation, line-break joins, fragment/placeholder rejection, and the
invariant that ``normalize_doi`` semantics are unchanged.
"""
from __future__ import annotations

import pytest

from src.utils.identifiers import (
    clean_extracted_doi_candidate,
    collect_dois_from_text,
    extract_doi_candidates,
    extract_doi_from_text,
    is_valid_doi,
    join_line_broken_doi_lines,
    normalize_doi,
)

# Test DOIs must survive the length gate (>= 10 chars) and carry a digit
# in the suffix (pure-alphabetic short suffixes are fragments by design).
VALID_A = "10.1234/abc123"
VALID_B = "10.1234/efgh456"


# ── is_valid_doi ───────────────────────────────────────────────────────

class TestIsValidDoi:
    @pytest.mark.parametrize(
        "doi",
        [
            "10.5194/egusphere-2026-2129",
            "10.1007/s10546-021-00629",
            "10.1029/2020gl088919",
            "10.1017/s0022112004009073",
            "10.1016/j.aeolia.2021.100730",
            "10.3189/2020jog66.103",
            "10.1103/physrevlett.128.123456",
        ],
    )
    def test_accepts_well_formed(self, doi: str) -> None:
        assert is_valid_doi(doi)

    @pytest.mark.parametrize(
        "doi",
        [
            "",                          # empty
            "10.1103/P",                 # fragment: 9 chars, no suffix body
            "10.1073/pnas.",             # truncated journal-code fragment
            "10.1073/pnas.xxxxxxxxxx",   # placeholder run
            "10.1007/",                  # empty suffix
            "10.12/x",                   # too-short prefix
            "not a doi",
            "10.1007/some doi with spaces",
            "10.1234/abc123.",           # trailing dot (direct call)
            "10.1234/abc123..",
            "10.1234/alphaonly",         # no digit, short -> fragment
        ],
    )
    def test_rejects_fragments_and_placeholders(self, doi: str) -> None:
        assert not is_valid_doi(doi)


# ── clean_extracted_doi_candidate ──────────────────────────────────────

class TestCleanExtractedDoiCandidate:
    def test_trailing_period_stripped(self) -> None:
        assert (
            clean_extracted_doi_candidate("10.1029/2020gl088919.")
            == "10.1029/2020gl088919"
        )

    def test_trailing_paren_stripped(self) -> None:
        # Unbalanced close bracket from sentence punctuation.
        assert (
            clean_extracted_doi_candidate("10.64898/2026.01.16.699976)")
            == "10.64898/2026.01.16.699976"
        )
        # Balanced parens inside the suffix are preserved.
        assert (
            clean_extracted_doi_candidate("10.1234/(abc123)def")
            == "10.1234/(abc123)def"
        )
        # Punct then close bracket: "…abc.)" -> "…abc".
        assert (
            clean_extracted_doi_candidate("10.1234/abc123.)")
            == "10.1234/abc123"
        )

    def test_trailing_comma_semicolon_colon_stripped(self) -> None:
        for noise in [",", ";", ":", "'", '"', "}"]:
            assert (
                clean_extracted_doi_candidate(f"10.1234/abc123{noise}")
                == "10.1234/abc123"
            )

    def test_line_break_trailing_hyphen_stripped(self) -> None:
        assert (
            clean_extracted_doi_candidate("10.1007/s10546-021-00629-")
            == "10.1007/s10546-021-00629"
        )

    @pytest.mark.parametrize(
        "decorated",
        [
            "doi:10.1234/abc123",
            "DOI: 10.1234/abc123",
            "DOI 10.1234/abc123",
            "https://doi.org/10.1234/abc123",
            "http://dx.doi.org/10.1234/abc123",
            "  https://doi.org/10.1234/abc123  ",
        ],
    )
    def test_decoration_prefixes_stripped(self, decorated: str) -> None:
        assert clean_extracted_doi_candidate(decorated) == VALID_A

    def test_url_decode(self) -> None:
        # URL-encoded slash must decode back into the suffix.
        assert (
            clean_extracted_doi_candidate("https://doi.org/10.1234/x%2Fy1")
            == "10.1234/x/y1"
        )

    def test_non_ascii_suffix_rejected(self) -> None:
        # The syntactic gate is ASCII-only; non-ASCII suffixes (even after
        # NFC normalization) are not candidate DOIs.
        assert clean_extracted_doi_candidate("10.1234/é") is None

    def test_lowercasing(self) -> None:
        assert (
            clean_extracted_doi_candidate("10.1029/2020GL088919")
            == "10.1029/2020gl088919"
        )

    def test_rejects_fragments(self) -> None:
        assert clean_extracted_doi_candidate("10.1103/P") is None
        assert clean_extracted_doi_candidate("10.1073/pnas.") is None
        assert clean_extracted_doi_candidate("10.1073/pnas.xxxxxxxxxx") is None
        assert clean_extracted_doi_candidate("10.12/x") is None
        assert clean_extracted_doi_candidate("garbage") is None
        assert clean_extracted_doi_candidate("") is None
        assert clean_extracted_doi_candidate(None) is None


# ── extract_doi_candidates ─────────────────────────────────────────────

class TestExtractDoiCandidates:
    def test_multiple_and_dedup(self) -> None:
        text = (
            f"See {VALID_A} and https://doi.org/{VALID_B} "
            f"and {VALID_A} again."
        )
        assert extract_doi_candidates(text) == [VALID_A, VALID_B]

    def test_noisy_surroundings(self) -> None:
        text = "…(10.1029/2020gl088919.)\n10.1103/P fragment"
        assert extract_doi_candidates(text) == ["10.1029/2020gl088919"]

    def test_empty(self) -> None:
        assert extract_doi_candidates("") == []
        assert extract_doi_candidates(None) == []


# ── join_line_broken_doi_lines ─────────────────────────────────────────

class TestJoinLineBrokenDoiLines:
    def test_doi_split_across_lines_joined(self) -> None:
        lines = [
            "The identifier is 10.1007/s10546-021-0062",
            "9-5 and continues here",
            "another line",
        ]
        assert "10.1007/s10546-021-00629-5" in join_line_broken_doi_lines(lines)

    def test_intact_dois_kept(self) -> None:
        lines = [VALID_A, VALID_B, "no doi here"]
        assert join_line_broken_doi_lines(lines) == [VALID_A, VALID_B]

    def test_empty(self) -> None:
        assert join_line_broken_doi_lines([]) == []


# ── normalize_doi unchanged semantics ──────────────────────────────────

class TestNormalizeDoiInvariant:
    def test_semantics_unchanged(self) -> None:
        # These are the pre-existing behaviors; the cleaning layer must
        # not have altered them (discovery/duplicate-guard depend on it).
        assert normalize_doi("") == ""
        assert normalize_doi(None) == ""
        assert normalize_doi("  10.1234/AbC123  ") == "10.1234/abc123"
        assert (
            normalize_doi("https://doi.org/10.1234/AbC123")
            == "10.1234/abc123"
        )
        # Idempotent.
        normalized = normalize_doi("10.1234/AbC123")
        assert normalize_doi(normalized) == normalized

    def test_legacy_extractors_unchanged(self) -> None:
        assert extract_doi_from_text(f"see {VALID_A} here") == VALID_A
        assert extract_doi_from_text("nothing") is None
        assert collect_dois_from_text(f"{VALID_A} {VALID_A} {VALID_B}") == [
            VALID_A,
            VALID_B,
        ]

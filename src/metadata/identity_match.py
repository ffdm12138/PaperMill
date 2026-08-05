"""Evidence-tiered PDF identity decision policy (policy v2.0).

Consumes structured ``PdfIdentityEvidence`` (extractor v2) plus metadata and
produces the automatic identity decision recorded in match receipts.

Invariants (asserted by tests):
- weak evidence (body_text / reference_list / raw_bytes) never triggers
  ``identifier_conflict``;
- medium evidence never triggers ``identifier_conflict`` either (ambiguous
  at most);
- an exact requested-DOI hit on strong evidence wins over all weak evidence;
- ``related_version`` requires a known DOI family AND strong bibliographic
  consistency — a family relation alone is never decisive;
- an empty requested DOI routes to the stable-identifier branch and never
  enters the foreign-DOI conflict path;
- ``contradictory`` is a high-precision, low-recall conclusion: the title
  conflict threshold (0.60) is separate from the match threshold (0.85) and
  author conflict only counts when both sides are complete;
- a labeled first-page DOI is strong only on title match OR (reliable
  author overlap AND year compatibility) — year-only or common-surname-only
  corroboration stays medium (final contract, audited 2026-07-31).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
import re

from src.utils.identifiers import normalize_doi
from src.metadata.normalization import canonical_title
from src.metadata.pdf_identity import DoiEvidence, PdfIdentityEvidence

DECISION_POLICY_VERSION = "2.0"

PRIMARY_SOURCES = {"xmp_metadata", "document_info", "first_page"}

TITLE_MATCH_THRESHOLD = 0.85
TITLE_CONFLICT_THRESHOLD = 0.60
MIN_AUTHOR_OVERLAP = 1

# A single shared surname is not reliable author overlap when the surname
# is extremely common (a "Wang"/"Smith" coincidence proves nothing).  The
# contract requires author corroboration to be reliable before it can
# upgrade a labeled first-page DOI to strong.
COMMON_SURNAMES = {
    "wang", "li", "zhang", "liu", "chen", "yang", "huang", "zhao", "wu", "zhou",
    "smith", "johnson", "williams", "brown", "jones", "miller", "davis", "garcia",
    "rodriguez", "wilson", "martinez", "anderson", "taylor", "thomas", "moore",
    "jackson", "martin", "lee", "white", "harris", "clark", "lewis", "robinson",
    "walker", "young", "allen", "king", "wright", "scott", "torres", "nguyen",
    "hill", "flores", "green", "adams", "nelson", "baker", "hall", "rivera",
    "campbell", "mitchell", "carter", "roberts", "gomez", "phillips", "evans",
    "turner", "diaz", "parker", "cruz", "edwards", "collins", "reyes", "stewart",
    "morris", "morales", "murphy", "cook", "rogers", "gutierrez", "ortiz",
    "morgan", "cooper", "peterson", "bailey", "reed", "kelly", "howard",
    "ramos", "kim", "cox", "ward", "richardson", "watson", "brooks", "chavez",
    "wood", "james", "bennett", "gray", "mendoza", "ruiz", "hughes", "price",
    "alvarez", "castillo", "sanders", "patel", "myers", "long", "ross", "foster",
}

MATCH_METHODS = {
    "doi_exact",
    "doi_medium_bibliographic",
    "stable_identifier_exact",
    "manual_confirmed",
    "version_relation",
    "identifier_conflict",
    "ambiguous",
    "unverifiable",
    "extraction_failed",
}
MATCHED_METHODS = {
    "doi_exact",
    "doi_medium_bibliographic",
    "stable_identifier_exact",
    "manual_confirmed",
}

# Workspace metadata-layer state per final receipt decision status.
RECEIPT_STATUS_TO_METADATA_STATE = {
    "matched": "matched",
    "related_version": "related_version",
    "identifier_conflict": "identifier_conflict",
    "ambiguous": "ambiguous",
    "unverifiable": "unverifiable",
    "extraction_failed": "extraction_failed",
}

# Preprint DOI families that may relate to ANY journal DOI (the verdict still
# requires strong bibliographic consistency, so "any" is bounded by the bib).
PREPRINT_FAMILIES = ("10.48550/arxiv.", "10.2139/ssrn.")

# EGU journals with the modern discussion format
# ("10.5194/tc-2023-46" = discussion, "10.5194/tc-17-3041-2023" = journal).
_EGU_JOURNALS = (
    "acp", "amt", "tc", "gmd", "hess", "nhess", "essd", "os", "se", "wcd",
    "sd", "angeo",
)
_EGU_DISCUSSION_NEW_RE = re.compile(
    r"^10\.5194/(" + "|".join(_EGU_JOURNALS) + r")-(\d{4})-(\d{1,5})$"
)
_EGU_JOURNAL_RE = re.compile(
    r"^10\.5194/(" + "|".join(_EGU_JOURNALS) + r")-(\d+)-(\d+)-(\d{4})$"
)


@dataclass(frozen=True)
class VersionRelation:
    """A known DOI-family version relation (precise prefixes, no wildcards)."""

    kind: str
    left_prefixes: tuple[str, ...]
    right_prefixes: tuple[str, ...]

    def matches(self, left: str, right: str) -> bool:
        return any(left.startswith(prefix) for prefix in self.left_prefixes) and any(
            right.startswith(prefix) for prefix in self.right_prefixes
        )


# Curated family table: EGU discussion/preprint versions vs journal versions,
# plus the Journal of Glaciology DOI-prefix migration (10.3189 -> 10.1017/jog,
# not mechanically convertible, so it needs bibliographic strength too).
VERSION_RELATIONS: tuple[VersionRelation, ...] = (
    VersionRelation("discussion_to_journal", ("10.5194/egusphere-",), ("10.5194/acp-",)),
    VersionRelation("discussion_to_journal", ("10.5194/tc-discussion-",), ("10.5194/tc-",)),
    VersionRelation("discussion_to_journal", ("10.5194/acpd-",), ("10.5194/acp-",)),
    VersionRelation("discussion_to_journal", ("10.5194/angeocom-",), ("10.5194/angeo-",)),
    VersionRelation("discussion_to_journal", ("10.5194/amtd-",), ("10.5194/amt-",)),
    VersionRelation("discussion_to_journal", ("10.5194/gmd-discussion-",), ("10.5194/gmd-",)),
    VersionRelation("discussion_to_journal", ("10.5194/hess-discussion-",), ("10.5194/hess-",)),
    VersionRelation("discussion_to_journal", ("10.5194/nhess-discussion-",), ("10.5194/nhess-",)),
    VersionRelation("discussion_to_journal", ("10.5194/essd-discussion-",), ("10.5194/essd-",)),
    VersionRelation("discussion_to_journal", ("10.5194/os-discussion-",), ("10.5194/os-",)),
    VersionRelation("discussion_to_journal", ("10.5194/se-discussion-",), ("10.5194/se-",)),
    VersionRelation("discussion_to_journal", ("10.5194/wcd-discussion-",), ("10.5194/wcd-",)),
    VersionRelation("discussion_to_journal", ("10.5194/sd-discussion-",), ("10.5194/sd-",)),
    VersionRelation("journal_prefix_migration", ("10.3189/",), ("10.1017/jog",)),
)


def known_version_family(requested_doi: str, foreign_doi: str) -> tuple[str, str] | None:
    """Known DOI-family relation between two DOIs, either order.

    Returns ``(kind, direction)`` where direction describes
    ``requested_doi -> foreign_doi`` (``preprint_to_published`` /
    ``published_to_preprint``), or ``None``.  A hit is only a candidate:
    the decision layer still requires strong bibliographic consistency.

    Beyond the curated prefix pairs this recognizes:
    - the 10.3189 -> 10.1017 prefix migration with an IDENTICAL suffix
      (Annals of Glaciology moved DOIs verbatim);
    - the modern EGU discussion format ("tc-2023-46" vs "tc-17-3041-2023").
    """
    for relation in VERSION_RELATIONS:
        if relation.matches(requested_doi, foreign_doi):
            return relation.kind, "preprint_to_published"
        if relation.matches(foreign_doi, requested_doi):
            return relation.kind, "published_to_preprint"
    # Prefix migration with identical suffix: 10.3189/x <-> 10.1017/x.
    for left, right in ((requested_doi, foreign_doi), (foreign_doi, requested_doi)):
        if left.startswith("10.3189/") and right.startswith("10.1017/") and (
            left.split("/", 1)[1] == right.split("/", 1)[1]
        ):
            return "journal_prefix_migration", (
                "preprint_to_published" if left is requested_doi else "published_to_preprint"
            )
    # Modern EGU discussion format: tc-2023-46 <-> tc-17-3041-2023.
    for left, right in ((requested_doi, foreign_doi), (foreign_doi, requested_doi)):
        left_disc = _EGU_DISCUSSION_NEW_RE.match(left)
        right_journal = _EGU_JOURNAL_RE.match(right)
        if left_disc and right_journal and left_disc.group(1) == right_journal.group(1):
            return "discussion_to_journal", (
                "preprint_to_published" if left is requested_doi else "published_to_preprint"
            )
    requested_preprint = requested_doi.startswith(PREPRINT_FAMILIES)
    foreign_preprint = foreign_doi.startswith(PREPRINT_FAMILIES)
    if requested_preprint and not foreign_preprint:
        return "preprint_to_journal", "preprint_to_published"
    if foreign_preprint and not requested_preprint:
        return "preprint_to_journal", "published_to_preprint"
    return None


def _metadata_identity(metadata: dict) -> dict:
    identifiers = (
        metadata.get("identifiers")
        if isinstance(metadata.get("identifiers"), dict)
        else {}
    )
    doi = normalize_doi(str(identifiers.get("doi") or ""))
    stable = {
        str(key).casefold(): str(value).strip().casefold()
        for key, value in identifiers.items()
        if str(key).casefold() != "doi" and str(value).strip()
    }
    authors = [a for a in metadata.get("authors") or [] if isinstance(a, dict)]
    families = [
        str(a.get("family") or a.get("full_name") or "").strip() for a in authors
    ]
    families = [value for value in families if value]
    first = str((metadata.get("first_author") or {}).get("family") or "").strip()
    if not first and families:
        first = families[0]
    return {
        "doi": doi,
        "stable_identifiers": stable,
        "title": canonical_title(str((metadata.get("title") or {}).get("original") or "")),
        "year": (
            int(metadata.get("year"))
            if str(metadata.get("year") or "").isdigit()
            else None
        ),
        "first_author": first,
        "author_families": families,
        "entry_type": str(metadata.get("entry_type") or "").lower(),
        "booktitle": (
            str((metadata.get("container") or {}).get("booktitle") or "").strip()
            or None
        ),
    }


@dataclass(frozen=True)
class BibStrength:
    """Bibliographic consistency between metadata and PDF identity fields.

    ``verdict`` is one of ``strong`` / ``compatible`` / ``insufficient`` /
    ``contradictory``.  ``contradictory`` is high-precision: it requires
    either (clear title conflict AND incompatible authors, both complete) or
    (a unique structured primary DOI AND an explicit title/author conflict
    that the other dimension does not support).

    ``author_reliable`` is the contract's "reliable overlap": at least two
    shared families, or a single shared family that is not a common
    surname.  A single common-surname overlap (a "Wang"/"Smith"
    coincidence) never counts as corroboration.
    """

    title_similarity: float | None
    title_match: bool
    title_conflict: bool
    year_compatible: bool
    year_evidence: str  # equal | close | missing | conflict
    author_overlap: bool
    author_reliable: bool
    author_complete: bool
    verdict: str  # strong | compatible | insufficient | contradictory

    def to_dict(self) -> dict:
        return asdict(self)


def bibliographic_strength(
    metadata: dict,
    evidence: PdfIdentityEvidence,
    *,
    family_hint: str | None = None,
    structured_primary: bool = False,
) -> BibStrength:
    """Four-state bibliographic strength (match/conflict thresholds split)."""
    identity = _metadata_identity(metadata)
    meta_title = canonical_title(identity["title"])
    pdf_title = canonical_title(evidence.canonical_title or "")
    if meta_title and pdf_title:
        similarity = SequenceMatcher(None, meta_title, pdf_title).ratio()
        title_match = similarity >= TITLE_MATCH_THRESHOLD
        title_conflict = similarity < TITLE_CONFLICT_THRESHOLD
    else:
        similarity, title_match, title_conflict = None, False, False

    meta_year = identity["year"]
    pdf_year = evidence.publication_year
    if meta_year is not None and pdf_year is not None:
        delta = abs(meta_year - pdf_year)
        limit = 2 if family_hint else 1
        year_compatible = delta <= limit
        year_evidence = (
            "equal" if delta == 0 else "close" if year_compatible else "conflict"
        )
    else:
        year_compatible, year_evidence = False, "missing"

    meta_authors = {f.casefold() for f in identity["author_families"] if f}
    pdf_authors = {f.casefold() for f in evidence.author_families if f}
    author_complete = bool(meta_authors) and bool(pdf_authors)
    shared = (meta_authors & pdf_authors) if author_complete else set()
    author_overlap = len(shared) >= MIN_AUTHOR_OVERLAP
    author_reliable = (
        (len(shared) >= 2)
        or (len(shared) == 1 and next(iter(shared)) not in COMMON_SURNAMES)
        if author_complete
        else False
    )
    author_conflict = author_complete and not author_overlap

    if title_conflict and author_conflict:
        verdict = "contradictory"
    elif (
        structured_primary
        and (title_conflict or author_conflict)
        and not (title_match and author_overlap)
    ):
        verdict = "contradictory"
    elif title_match and year_compatible and author_overlap:
        verdict = "strong"
    elif title_match and not (title_conflict or author_conflict):
        verdict = "compatible"
    else:
        verdict = "insufficient"
    return BibStrength(
        title_similarity=similarity,
        title_match=title_match,
        title_conflict=title_conflict,
        year_compatible=year_compatible,
        year_evidence=year_evidence,
        author_overlap=author_overlap,
        author_reliable=author_reliable,
        author_complete=author_complete,
        verdict=verdict,
    )


def effective_confidence(item: DoiEvidence, biblio: BibStrength) -> str:
    """Decision-time confidence for first-page evidence (contract rule).

    A labeled first-page DOI (``doi:`` / ``https://doi.org/`` form) is
    strong ONLY when:
      A. the title matches strongly; OR
      B. the author overlap is reliable AND the year is compatible.
    Year-only, author-only, or single-common-surname corroboration stays
    medium — a page-1 labeled DOI whose title, authors, and year cannot be
    reconciled is identity-ambiguous even though the label is present
    ("This article comments on doi:…" noise fails all dimensions).

    XMP/Document-Info explicit keys are never weakened; body/reference/raw
    evidence stays weak.  Real-corpus audit (2026-07-31): the previous
    title-OR-author-OR-year rule left 405/585 matched papers resting on
    year- or author-only corroboration; this rule requires explainable
    evidence for every strong upgrade."""
    if item.source in {"xmp_metadata", "document_info"}:
        return item.confidence
    if item.source == "first_page":
        if item.labeled and (
            biblio.title_match
            or (biblio.author_reliable and biblio.year_compatible)
        ):
            return "strong"
        return "medium"
    return "weak"


def _unique_structured_primary(evidence: PdfIdentityEvidence) -> str | None:
    """The unique explicit structured-primary DOI, if any.

    Only a single DOI-valued XMP/Document-Info primary field counts; two
    different structured DOIs mean ambiguity, never a field-order pick.
    """
    structured = {
        e.doi
        for e in evidence.doi_evidence
        if e.source in {"xmp_metadata", "document_info"} and e.confidence == "strong"
    }
    return next(iter(structured)) if len(structured) == 1 else None


@dataclass(frozen=True)
class IdentityDecision:
    match_status: str
    match_method: str
    pdf_primary_doi: str | None
    relation: dict | None
    details: dict

    def to_dict(self) -> dict:
        return asdict(self)


def _decision(
    *,
    status: str,
    method: str,
    requested_doi: str,
    pdf_primary_doi: str | None,
    relation: dict | None,
    evidence: PdfIdentityEvidence,
    identity: dict,
    biblio: BibStrength,
    strong: set[str],
    medium: set[str],
    weak: set[str],
    rules: list[str],
) -> IdentityDecision:
    details = {
        "metadata_doi": identity["doi"],
        "requested_doi": requested_doi,
        "pdf_primary_doi": pdf_primary_doi,
        "evidence_tiers": {
            "strong": sorted(strong),
            "medium": sorted(medium),
            "weak": sorted(weak),
        },
        "bibliographic": biblio.to_dict(),
        "relation": relation,
        "rules_applied": rules,
    }
    return IdentityDecision(
        match_status=status,
        match_method=method,
        pdf_primary_doi=pdf_primary_doi,
        relation=relation,
        details=details,
    )


def decide_identity(
    metadata: dict,
    evidence: PdfIdentityEvidence,
    *,
    requested_doi: str,
) -> IdentityDecision:
    """The automatic identity decision (ordered rules, single source)."""
    identity = _metadata_identity(metadata)
    requested = normalize_doi(requested_doi)

    if evidence.confidence == "unreadable":
        return _decision(
            status="extraction_failed",
            method="extraction_failed",
            requested_doi=requested,
            pdf_primary_doi=None,
            relation=None,
            evidence=evidence,
            identity=identity,
            biblio=BibStrength(
                title_similarity=None,
                title_match=False,
                title_conflict=False,
                year_compatible=False,
                year_evidence="missing",
                author_overlap=False,
                author_reliable=False,
                author_complete=False,
                verdict="insufficient",
            ),
            strong=set(),
            medium=set(),
            weak=set(),
            rules=["rule_extraction_failed"],
        )

    structured_primary = _unique_structured_primary(evidence)
    biblio = bibliographic_strength(
        metadata, evidence, structured_primary=bool(structured_primary)
    )
    strong = {
        e.doi
        for e in evidence.doi_evidence
        if effective_confidence(e, biblio) == "strong" and e.source in PRIMARY_SOURCES
    }
    medium = {
        e.doi for e in evidence.doi_evidence if effective_confidence(e, biblio) == "medium"
    }
    weak = {
        e.doi for e in evidence.doi_evidence if effective_confidence(e, biblio) == "weak"
    }

    def emit(
        status: str,
        method: str,
        pdf_primary_doi: str | None,
        relation: dict | None,
        rules: list[str],
        biblio_value: BibStrength = biblio,
    ) -> IdentityDecision:
        return _decision(
            status=status,
            method=method,
            requested_doi=requested,
            pdf_primary_doi=pdf_primary_doi,
            relation=relation,
            evidence=evidence,
            identity=identity,
            biblio=biblio_value,
            strong=strong,
            medium=medium,
            weak=weak,
            rules=rules,
        )

    # Rule 1 (hard): exact requested DOI on strong evidence wins over all
    # weak evidence, even dozens of reference-list DOIs.
    if requested and requested in strong:
        return emit("matched", "doi_exact", requested, None, ["rule1_strong_doi_exact"])

    # Rules 2-3: requested DOI on medium evidence.
    if requested and requested in medium:
        if biblio.verdict == "strong":
            return emit(
                "matched",
                "doi_medium_bibliographic",
                requested,
                None,
                ["rule2_medium_doi_strong_bibliographic"],
            )
        return emit(
            "ambiguous",
            "ambiguous",
            requested,
            None,
            ["rule3_medium_doi_cannot_confirm"],
        )

    # Empty requested DOI routes to the stable-identifier branch; it never
    # enters the foreign-DOI conflict path.
    if not requested:
        return _stable_identifier_decision(identity, evidence, biblio, emit)

    # Candidate arbitration: trusted = strong | medium, minus the requested.
    trusted_foreign = (strong | medium) - {requested}
    if structured_primary and structured_primary in trusted_foreign:
        candidates = {structured_primary}
    else:
        candidates = trusted_foreign
    if len(candidates) >= 2:
        return emit(
            "ambiguous",
            "ambiguous",
            None,
            None,
            ["rule7_multiple_primary_candidates"],
        )
    if not candidates:
        return emit(
            "unverifiable",
            "unverifiable",
            None,
            None,
            ["rule9_no_trusted_identity_evidence"],
        )
    candidate = next(iter(candidates))
    if candidate not in strong:
        return emit(
            "ambiguous",
            "ambiguous",
            candidate,
            None,
            ["rule9b_single_medium_foreign_ambiguous"],
        )

    # Exactly one strong foreign DOI.
    family = known_version_family(requested, candidate)
    if family:
        family_kind, direction = family
        family_biblio = bibliographic_strength(
            metadata,
            evidence,
            family_hint=family_kind,
            structured_primary=bool(structured_primary),
        )
        if (
            family_biblio.title_match
            and family_biblio.author_overlap
            and (family_biblio.year_compatible or family_biblio.year_evidence == "missing")
        ):
            relation = {
                "kind": family_kind,
                "requested_doi": requested,
                "pdf_primary_doi": candidate,
                "direction": direction,
                "year_evidence": family_biblio.year_evidence,
            }
            return emit(
                "related_version",
                "version_relation",
                candidate,
                relation,
                ["rule10a_family_with_bibliographic_strength"],
                biblio_value=family_biblio,
            )
        return emit(
            "ambiguous",
            "ambiguous",
            candidate,
            None,
            ["rule10c_family_without_bibliographic_strength"],
        )
    if biblio.verdict == "contradictory":
        return emit(
            "identifier_conflict",
            "identifier_conflict",
            candidate,
            None,
            ["rule10b_strong_foreign_contradictory_bibliographic"],
        )
    return emit(
        "ambiguous",
        "ambiguous",
        candidate,
        None,
        ["rule10d_strong_foreign_positive_bibliographic_ambiguous"],
    )


def _stable_identifier_decision(
    identity: dict,
    evidence: PdfIdentityEvidence,
    biblio: BibStrength,
    emit,
) -> IdentityDecision:
    """Stable-identifier branch for metadata without a DOI.

    work_identifier exact match + non-contradictory bibliographic fields ->
    matched.  ISBN is a container identifier: for a book chapter it also
    requires a strong chapter-title match, author overlap, and a metadata
    booktitle before it can confirm the chapter identity.
    """
    extracted = {item.type.casefold(): item for item in evidence.extracted_identifiers}
    contradictory = biblio.verdict == "contradictory"
    for kind, value in identity["stable_identifiers"].items():
        item = extracted.get(kind)
        if item is None:
            continue
        if item.kind == "work" and item.value.casefold() == value.casefold():
            if not contradictory:
                return emit(
                    "matched",
                    "stable_identifier_exact",
                    None,
                    None,
                    ["ruleS_work_identifier_exact"],
                )
            return emit(
                "unverifiable",
                "unverifiable",
                None,
                None,
                ["ruleS_work_identifier_contradictory_bibliographic"],
            )
        if item.kind == "container" and item.type.casefold() == "isbn":
            entry_type = str((identity.get("entry_type") or "")).lower()
            booktitle = identity.get("booktitle")
            if (
                item.value.casefold() == value.casefold()
                and biblio.title_match
                and biblio.author_overlap
                and booktitle
                and not contradictory
            ):
                return emit(
                    "matched",
                    "stable_identifier_exact",
                    None,
                    None,
                    ["ruleS_isbn_container_chapter_consistent"],
                )
    return emit(
        "unverifiable",
        "unverifiable",
        None,
        None,
        ["ruleS_no_stable_identifier_match"],
    )

"""Strict legacy pending-page journal contract used only by the v4 migration.

The on-disk truth (verified against all 1875 production journals under
``data/discovery/pending_pages/``):

- Every journal carries ``schema_version == "2.0"``.  No ``"3.0"`` page
  journal exists on disk; the schema-3.0 contract applies to keyword
  notebooks only (see ``notebook_v3.py``).  ``"2.0"`` is therefore the
  complete accepted version set — anything else fails closed.
- Every journal has exactly the 22 top-level keys in
  ``_JOURNAL_TOP_LEVEL_FIELDS``; ``state`` is ``"drained"`` or
  ``"draining"``.
- Every candidate wrapper has exactly the 14 core keys in
  ``_CANDIDATE_CORE_FIELDS`` plus an optional subset of
  ``_CANDIDATE_OPTIONAL_FIELDS``; ``status`` is one of
  :data:`LEGACY_CANDIDATE_STATUS_VALUES``.
- Every inner ``candidate`` record has exactly the 19 keys in
  ``_INNER_CANDIDATE_FIELDS``.

The migration matrix (:data:`LEGACY_STATUS_MIGRATION_MATRIX`) maps each real
legacy status to its v4 disposition.  Unknown statuses, corrupt or partial
journals, and schema violations raise :class:`LegacyPageJournalContractError`
with the offending file path — the migration never guesses.

These types must never be imported by production discovery runtime code.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

# Real on-disk schema versions: all 1875 production journals are "2.0".
LEGACY_PAGE_JOURNAL_SCHEMA_VERSIONS: frozenset[str] = frozenset({"2.0"})

# Real top-level page states (drained=1874, draining=1 in production).
LEGACY_PAGE_STATE_VALUES: frozenset[str] = frozenset({"drained", "draining"})

# Real candidate statuses observed in production journals.
LEGACY_CANDIDATE_STATUS_VALUES: frozenset[str] = frozenset({
    "pending",
    "processing",
    "staged",
    "existing_duplicate",
    "unresolved",
    "duplicate_observation",
    "emitted",
})

# Relevance verdict states that may accompany a candidate.  "passed" and
# "rejected" are observed in production; "verification_deferred" and
# "profile_unbound" are the remaining nonterminal verdicts of the legacy
# relevance vocabulary and are treated as retryable.
LEGACY_RELEVANCE_STATE_VALUES: frozenset[str] = frozenset({
    "passed",
    "rejected",
    "verification_deferred",
    "profile_unbound",
})

# ── v4 dispositions (migration matrix output vocabulary) ─────────────────

DISPOSITION_QUEUE = "queue"
DISPOSITION_RECONCILE = "reconcile"
DISPOSITION_ALREADY_EXISTING = "already_existing"
DISPOSITION_DUPLICATE = "duplicate"
DISPOSITION_INVALID = "invalid"
DISPOSITION_TERMINAL = "terminal"

# Migration matrix, one row per real legacy status.  The reconcile rows are
# resolved against the known-DOI index during extraction: a known DOI
# becomes ``already_existing``, an unknown DOI is re-queued as a
# PendingCandidateV4.
#
#   pending (relevance rejected)      -> terminal   (never re-ingested)
#   pending / processing              -> queue      (retryable work)
#   staged with durable receipt       -> already_existing
#   staged without durable receipt    -> reconcile  (known-DOI lookup)
#   emitted, reconciled               -> already_existing
#   emitted, not reconciled           -> reconcile  (known-DOI lookup)
#   existing_duplicate                -> already_existing (library hit)
#   duplicate_observation             -> duplicate  (in-batch evidence)
#   unresolved (doi_unresolved)       -> invalid    (no DOI evidence)
#   <anything else>                   -> fail closed (raise)
LEGACY_STATUS_MIGRATION_MATRIX: dict[str, str] = {
    "pending": DISPOSITION_QUEUE,
    "processing": DISPOSITION_QUEUE,
    "staged": DISPOSITION_ALREADY_EXISTING,
    "existing_duplicate": DISPOSITION_ALREADY_EXISTING,
    "unresolved": DISPOSITION_INVALID,
    "duplicate_observation": DISPOSITION_DUPLICATE,
    "emitted": DISPOSITION_RECONCILE,
}


class LegacyPageJournalContractError(ValueError):
    """A legacy page journal violates the strict v3 contract.

    Always carries the journal file path so operators can locate the
    corrupt input.
    """


def _fail(source: str, path: str, message: str) -> None:
    raise LegacyPageJournalContractError(f"{source}: {path}: {message}")


_JOURNAL_TOP_LEVEL_FIELDS = frozenset({
    "schema_version", "page_id", "keyword_id", "keyword_zh", "query_id",
    "query", "query_language", "provider", "lane", "generation",
    "request_cursor", "next_cursor", "request_signature", "page_sequence",
    "refresh_run_id", "fetched_at", "cursor_committed_at", "drained_at",
    "state", "provider_exhausted", "statistics", "candidates",
})
_CANDIDATE_CORE_FIELDS = frozenset({
    "candidate_id", "status", "attempts", "last_error", "terminal_reason",
    "staged_paper_number", "claimed_by", "claimed_at", "lease_expires_at",
    "export_id", "export_path", "emitted_at", "reconciled", "candidate",
})
_CANDIDATE_OPTIONAL_FIELDS = frozenset({
    "relevance", "stage_item", "duplicate_refs", "reused_paper_number",
    "primary_candidate_id", "deferred_generation", "last_deferred_reason",
    "next_attempt_at", "manifest_path",
})
_INNER_CANDIDATE_FIELDS = frozenset({
    "title", "year", "authors", "doi", "venue", "abstract", "source",
    "source_id", "url", "pdf_url", "open_access", "citation_count",
    "confidence", "query", "domain_id", "doi_resolution",
    "existing_duplicate_refs", "duplicate_indexed", "raw",
})
_RELEVANCE_FIELDS = frozenset({
    "state", "profile_hash", "matched_groups", "negative_matches", "reason",
    "verification", "attempt_count", "next_retry_at", "last_attempt_at",
    "last_error_class", "last_http_status",
})
_STATISTICS_CORE_FIELDS = frozenset({
    "returned", "pending", "terminal", "staged", "emitted",
    "existing_duplicate", "duplicate_observation", "invalid", "unresolved",
    "failed_retryable", "failed_terminal",
})
_STATISTICS_RELEVANCE_FIELDS = frozenset({
    "relevance_profile_unbound", "relevance_passed", "relevance_rejected",
    "relevance_verification_deferred", "relevance_candidate_invalid",
})

_PROVIDERS = frozenset({"openalex", "crossref"})
_LANES = frozenset({"refresh", "backfill"})
_QUERY_LANGUAGES = frozenset({"zh", "en"})


def _require_exact_keys(
    value: dict[str, Any], expected: frozenset, source: str, path: str,
) -> None:
    missing = sorted(expected - set(value))
    if missing:
        _fail(source, path, f"missing keys: {missing}")
    extra = sorted(set(value) - expected)
    if extra:
        _fail(source, path, f"unknown keys: {extra}")


def _require_dict(value: Any, source: str, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(source, path, f"must be an object, got {type(value).__name__}")
    return value


def _require_str(value: Any, source: str, path: str) -> str:
    if not isinstance(value, str):
        _fail(source, path, f"must be a string, got {type(value).__name__}")
    return value


def _require_optional_str(value: Any, source: str, path: str) -> str | None:
    if value is not None and not isinstance(value, str):
        _fail(source, path, "must be a string or null")
    return value


def _require_int(value: Any, source: str, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(source, path, f"must be an integer, got {type(value).__name__}")
    return value


def _require_optional_int(value: Any, source: str, path: str) -> int | None:
    if value is None:
        return None
    return _require_int(value, source, path)


def _require_bool(value: Any, source: str, path: str) -> bool:
    if not isinstance(value, bool):
        _fail(source, path, f"must be boolean, got {type(value).__name__}")
    return value


def _validate_statistics(value: Any, source: str, path: str) -> None:
    stats = _require_dict(value, source, path)
    keys = set(stats)
    if keys == set(_STATISTICS_CORE_FIELDS | _STATISTICS_RELEVANCE_FIELDS):
        expected = _STATISTICS_CORE_FIELDS | _STATISTICS_RELEVANCE_FIELDS
    elif keys == set(_STATISTICS_CORE_FIELDS):
        expected = _STATISTICS_CORE_FIELDS
    else:
        _fail(
            source, path,
            "statistics keys must be the core set, optionally extended "
            f"with the relevance counters; got {sorted(keys)}",
        )
    _require_exact_keys(stats, expected, source, path)
    for key in expected:
        counter = stats[key]
        if isinstance(counter, bool) or not isinstance(counter, int) or counter < 0:
            _fail(source, f"{path}.{key}", "must be a non-negative integer")


def _validate_relevance(value: Any, source: str, path: str) -> str:
    relevance = _require_dict(value, source, path)
    _require_exact_keys(relevance, _RELEVANCE_FIELDS, source, path)
    state = relevance["state"]
    if state not in LEGACY_RELEVANCE_STATE_VALUES:
        _fail(
            source, f"{path}.state",
            f"must be one of {sorted(LEGACY_RELEVANCE_STATE_VALUES)}, "
            f"got {state!r}",
        )
    return str(state)


def _validate_optional_wrapper_fields(
    wrapper: dict[str, Any], source: str, path: str,
) -> str | None:
    relevance_state: str | None = None
    if "relevance" in wrapper:
        relevance_state = _validate_relevance(
            wrapper["relevance"], source, f"{path}.relevance"
        )
    if "stage_item" in wrapper:
        _require_dict(wrapper["stage_item"], source, f"{path}.stage_item")
    if "duplicate_refs" in wrapper:
        refs = wrapper["duplicate_refs"]
        if not isinstance(refs, list) or any(not isinstance(r, dict) for r in refs):
            _fail(source, f"{path}.duplicate_refs", "must be a list of objects")
    for key in ("reused_paper_number", "primary_candidate_id", "last_deferred_reason",
                "deferred_generation", "manifest_path"):
        if key in wrapper:
            # deferred_generation is a drain-worker lease id (string), not a
            # numeric generation, in the real journals.
            _require_str(wrapper[key], source, f"{path}.{key}")
    if "next_attempt_at" in wrapper:
        _require_optional_str(
            wrapper["next_attempt_at"], source, f"{path}.next_attempt_at"
        )
    return relevance_state


def _validate_inner_candidate(value: Any, source: str, path: str) -> dict[str, Any]:
    inner = _require_dict(value, source, path)
    _require_exact_keys(inner, _INNER_CANDIDATE_FIELDS, source, path)
    for key in ("title", "doi", "venue", "abstract", "source", "source_id",
                "url", "pdf_url", "query"):
        _require_str(inner[key], source, f"{path}.{key}")
    _require_optional_int(inner["year"], source, f"{path}.year")
    _require_optional_int(inner["citation_count"], source, f"{path}.citation_count")
    confidence = inner["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        _fail(source, f"{path}.confidence", "must be a number")
    _require_optional_str(inner["domain_id"], source, f"{path}.domain_id")
    authors = inner["authors"]
    if not isinstance(authors, list) or any(not isinstance(a, str) for a in authors):
        _fail(source, f"{path}.authors", "must be a list of strings")
    _require_dict(inner["doi_resolution"], source, f"{path}.doi_resolution")
    refs = inner["existing_duplicate_refs"]
    if not isinstance(refs, list):
        _fail(source, f"{path}.existing_duplicate_refs", "must be a list")
    _require_bool(inner["open_access"], source, f"{path}.open_access")
    _require_bool(inner["duplicate_indexed"], source, f"{path}.duplicate_indexed")
    _require_dict(inner["raw"], source, f"{path}.raw")
    return inner


@dataclass(frozen=True)
class LegacyCandidateV3:
    """One strictly validated candidate from a legacy page journal."""

    candidate_id: str
    status: str
    relevance_state: str | None
    doi: str
    title: str | None
    year: int | None
    authors: tuple[str, ...] | None
    venue: str | None
    staged_paper_number: str | None
    reconciled: bool
    # Journal context (provenance).
    keyword_id: str
    keyword_zh: str
    query_id: str
    query: str
    query_language: str
    provider: str
    lane: str
    page_id: str
    source_schema_version: str
    journal_sha256: str

    @property
    def has_durable_receipt(self) -> bool:
        """True when the candidate carries proof of library ingestion."""
        return bool(self.staged_paper_number) or self.reconciled


def classify_legacy_candidate(candidate: LegacyCandidateV3) -> str:
    """Map one validated legacy candidate to its v4 disposition.

    Implements :data:`LEGACY_STATUS_MIGRATION_MATRIX`.  A relevance-rejected
    pending candidate is terminal (never re-ingested); a staged candidate
    without a durable receipt falls back to the reconcile lane, exactly like
    an unreconciled emitted candidate.  Unknown statuses already failed
    closed inside the strict reader.
    """
    if (
        candidate.status in ("pending", "processing")
        and candidate.relevance_state == "rejected"
    ):
        return DISPOSITION_TERMINAL
    disposition = LEGACY_STATUS_MIGRATION_MATRIX.get(candidate.status)
    if disposition is None:
        raise LegacyPageJournalContractError(
            f"candidate {candidate.candidate_id!r} has unmapped legacy "
            f"status {candidate.status!r}"
        )
    if disposition == DISPOSITION_RECONCILE and candidate.has_durable_receipt:
        # emitted and reconciled into the library: proven already existing.
        return DISPOSITION_ALREADY_EXISTING
    if disposition == DISPOSITION_ALREADY_EXISTING and not (
        candidate.has_durable_receipt
        or candidate.status == "existing_duplicate"
    ):
        # staged without receipt: reconcile against the known-DOI index.
        return DISPOSITION_RECONCILE
    return disposition


@dataclass(frozen=True)
class LegacyPageJournalV3:
    """One strictly validated legacy pending-page journal."""

    page_id: str
    schema_version: str
    keyword_id: str
    keyword_zh: str
    query_id: str
    query: str
    query_language: str
    provider: str
    lane: str
    state: str
    journal_sha256: str
    candidates: tuple[LegacyCandidateV3, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict_strict(
        cls,
        data: Any,
        *,
        source_path: str = "<memory>",
        journal_sha256: str = "",
    ) -> "LegacyPageJournalV3":
        """Parse and strictly validate one legacy page journal dict.

        Raises:
            LegacyPageJournalContractError: on any schema, field-set, type,
                or status-vocabulary violation.  The message always carries
                ``source_path``.
        """
        source = str(source_path)
        root = _require_dict(data, source, "journal")
        _require_exact_keys(root, _JOURNAL_TOP_LEVEL_FIELDS, source, "journal")

        version = root["schema_version"]
        if version not in LEGACY_PAGE_JOURNAL_SCHEMA_VERSIONS:
            _fail(
                source, "journal.schema_version",
                f"must be one of {sorted(LEGACY_PAGE_JOURNAL_SCHEMA_VERSIONS)}, "
                f"got {version!r}",
            )
        state = root["state"]
        if state not in LEGACY_PAGE_STATE_VALUES:
            _fail(
                source, "journal.state",
                f"must be one of {sorted(LEGACY_PAGE_STATE_VALUES)}, got {state!r}",
            )
        provider = root["provider"]
        if provider not in _PROVIDERS:
            _fail(source, "journal.provider", f"must be one of {sorted(_PROVIDERS)}")
        lane = root["lane"]
        if lane not in _LANES:
            _fail(source, "journal.lane", f"must be one of {sorted(_LANES)}")
        query_language = root["query_language"]
        if query_language not in _QUERY_LANGUAGES:
            _fail(
                source, "journal.query_language",
                f"must be one of {sorted(_QUERY_LANGUAGES)}",
            )
        for key in ("page_id", "keyword_id", "keyword_zh", "query_id", "query",
                    "request_cursor", "fetched_at", "cursor_committed_at"):
            _require_str(root[key], source, f"journal.{key}")
        for key in ("next_cursor", "refresh_run_id", "drained_at"):
            _require_optional_str(root[key], source, f"journal.{key}")
        generation = _require_int(root["generation"], source, "journal.generation")
        if generation < 1:
            _fail(source, "journal.generation", "must be at least 1")
        _require_optional_int(root["page_sequence"], source, "journal.page_sequence")
        _require_dict(root["request_signature"], source, "journal.request_signature")
        _require_bool(root["provider_exhausted"], source, "journal.provider_exhausted")
        _validate_statistics(root["statistics"], source, "journal.statistics")

        raw_candidates = root["candidates"]
        if not isinstance(raw_candidates, list):
            _fail(source, "journal.candidates", "must be a list")

        candidates: list[LegacyCandidateV3] = []
        for index, item in enumerate(raw_candidates):
            path = f"journal.candidates[{index}]"
            wrapper = _require_dict(item, source, path)
            unknown = sorted(
                set(wrapper) - _CANDIDATE_CORE_FIELDS - _CANDIDATE_OPTIONAL_FIELDS
            )
            if unknown:
                _fail(source, path, f"unknown keys: {unknown}")
            missing = sorted(_CANDIDATE_CORE_FIELDS - set(wrapper))
            if missing:
                _fail(source, path, f"missing keys: {missing}")

            status = wrapper["status"]
            if status not in LEGACY_CANDIDATE_STATUS_VALUES:
                _fail(
                    source, f"{path}.status",
                    f"must be one of {sorted(LEGACY_CANDIDATE_STATUS_VALUES)}, "
                    f"got {status!r}",
                )
            candidate_id = _require_str(
                wrapper["candidate_id"], source, f"{path}.candidate_id"
            )
            if not candidate_id.strip():
                _fail(source, f"{path}.candidate_id", "must be non-blank")
            attempts = _require_int(wrapper["attempts"], source, f"{path}.attempts")
            if attempts < 0:
                _fail(source, f"{path}.attempts", "must be non-negative")
            _require_bool(wrapper["reconciled"], source, f"{path}.reconciled")
            for key in ("last_error", "terminal_reason", "staged_paper_number",
                        "claimed_by", "claimed_at", "lease_expires_at",
                        "export_id", "export_path", "emitted_at"):
                _require_optional_str(wrapper[key], source, f"{path}.{key}")
            relevance_state = _validate_optional_wrapper_fields(wrapper, source, path)
            inner = _validate_inner_candidate(
                wrapper["candidate"], source, f"{path}.candidate"
            )

            candidates.append(LegacyCandidateV3(
                candidate_id=candidate_id,
                status=str(status),
                relevance_state=relevance_state,
                doi=inner["doi"].strip(),
                title=inner["title"].strip() or None,
                year=inner["year"],
                authors=tuple(inner["authors"]) if inner["authors"] else None,
                venue=inner["venue"].strip() or None,
                staged_paper_number=wrapper["staged_paper_number"],
                reconciled=wrapper["reconciled"],
                keyword_id=root["keyword_id"],
                keyword_zh=root["keyword_zh"],
                query_id=root["query_id"],
                query=root["query"],
                query_language=query_language,
                provider=provider,
                lane=lane,
                page_id=root["page_id"],
                source_schema_version=str(version),
                journal_sha256=journal_sha256,
            ))

        return cls(
            page_id=root["page_id"],
            schema_version=str(version),
            keyword_id=root["keyword_id"],
            keyword_zh=root["keyword_zh"],
            query_id=root["query_id"],
            query=root["query"],
            query_language=query_language,
            provider=provider,
            lane=lane,
            state=str(state),
            journal_sha256=journal_sha256,
            candidates=tuple(candidates),
        )

    @classmethod
    def from_file(cls, path: Path) -> "LegacyPageJournalV3":
        """Read, hash, and strictly validate one journal file."""
        source = str(path)
        try:
            raw_bytes = path.read_bytes()
        except OSError as exc:
            raise LegacyPageJournalContractError(
                f"{source}: journal: unreadable: {exc}"
            ) from exc
        digest = hashlib.sha256(raw_bytes).hexdigest()
        try:
            data = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LegacyPageJournalContractError(
                f"{source}: journal: corrupt JSON: {exc}"
            ) from exc
        return cls.from_dict_strict(
            data, source_path=source, journal_sha256=digest
        )


def iter_legacy_page_journals(journal_dir: Path) -> Iterator[LegacyPageJournalV3]:
    """Yield strictly validated journals one file at a time.

    Never loads more than one journal file into memory.  Any corrupt,
    partial, or schema-violating journal raises
    :class:`LegacyPageJournalContractError` with the file path — there is no
    silent skip.
    """
    for path in sorted(journal_dir.rglob("*.json")):
        if path.name == "archive_manifest.json":
            # Legacy archive snapshots co-locate their archive manifest with
            # the journals; it is not a page journal.
            continue
        if path.is_file():
            yield LegacyPageJournalV3.from_file(path)

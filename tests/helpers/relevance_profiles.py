from __future__ import annotations

from src.discovery.relevance import validate_relevance_profile


def relevance_profile(*, object_term: str = "wind-blown sand") -> dict:
    return validate_relevance_profile({
        "schema_version": "1.0",
        "matcher_schema_version": "1.0",
        "openalex": {
            "filter_level": "subfield",
            "resolved": True,
            "filter_ids": ["S1"],
            "filter_labels": ["Test Subfield"],
            "refresh_sort": "publication_date:desc",
            "backfill_sort": "cited_by_count:desc",
        },
        "crossref": {
            "scope_policy": "require_openalex_subfield",
            "refresh_sort": "published",
            "refresh_order": "desc",
            "backfill_sort": "relevance",
            "backfill_order": "desc",
        },
        "anchors": {
            "required_groups": [
                {"name": "object", "terms": [object_term]},
                {"name": "process", "terms": ["transport"]},
            ],
            "negative_any": ["semiconductor"],
            "missing_abstract_policy": "require_all_groups_in_title",
        },
    })


def bind_test_relevance_profile(store, keyword_zh: str) -> dict:
    """Install an explicit, strict profile for synthetic discovery fixtures."""
    profile = relevance_profile(object_term="test")
    profile = dict(profile)
    profile.pop("profile_hash", None)
    profile["anchors"] = {
        "required_groups": [
            {"name": "object", "terms": ["test"]},
            {"name": "process", "terms": ["candidate"]},
        ],
        "negative_any": ["semiconductor"],
        "missing_abstract_policy": "require_all_groups_in_title",
    }
    normalized = validate_relevance_profile(profile)
    current = store.require_v3(keyword_zh)
    store.set_relevance_profile(
        keyword_zh, normalized,
        generation=int(current.get("relevance_generation") or 1) + 1,
    )
    return normalized


def relevance_candidate(title: str = "Test candidate", **kwargs):
    from src.discovery.models import PaperCandidate

    raw = dict(kwargs.pop("raw", {}) or {})
    raw.setdefault("topics", [{
        "subfield": {"id": "https://openalex.org/subfields/S1"}
    }])
    return PaperCandidate(title=title, raw=raw, **kwargs)


class AlwaysVerifiedScopeVerifier:
    def verify_doi(self, doi: str, subfield_ids: list[str]):
        from src.discovery.relevance import ScopeVerification
        return ScopeVerification(status="verified")


def finalize_all_passed(journal_store, page_path, profile_hash="test-hash"):
    """Finalize every candidate on a page as ``passed`` for the given profile.

    Returns the decisions dict so it can be used as the ``finalize_page``
    callback of :func:`run_backfill_page_transaction`.
    """
    page = journal_store.read(page_path)
    decisions = {}
    for item in page["candidates"]:
        cid = str(item.get("candidate_id") or "")
        if not cid:
            continue
        decisions[cid] = {
            "state": "passed",
            "profile_hash": profile_hash,
            "reason": "profile_match",
            "matched_groups": {},
            "negative_matches": [],
            "verification": {"status": "test_fixture"},
        }
    journal_store.finalize_relevance(page_path, decisions)
    return decisions

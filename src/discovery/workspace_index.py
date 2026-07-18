"""Pure in-memory discovery identity index."""
from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from src.discovery.models import normalize_doi


IdentityKey = tuple[str, str, str, str, str]
ObservationKey = tuple[str, str, str, str]


@dataclass(frozen=True)
class DiscoveryIdentityRef:
    paper_number: str
    scope: str
    workspace_path: Any
    provider: str
    keyword_id: str
    page_id: str
    candidate_id: str
    normalized_doi: str
    source_record_path: Any | None = None
    receipt_path: Any | None = None

    @property
    def workspace_kind(self) -> str:
        return "formal" if self.scope == "papers" else self.scope


WorkspaceIdentityRef = DiscoveryIdentityRef


class IdentityRefConflict(ValueError):
    """Raised when one identity/paper entity contains conflicting facts."""


def identity_key(*, provider: str, keyword_id: str, page_id: str,
                 candidate_id: str, normalized_doi: str) -> IdentityKey | None:
    doi = normalize_doi(normalized_doi)
    candidate_id = str(candidate_id or "").strip()
    page_id = str(page_id or "").strip()
    if not candidate_id or not page_id or not doi:
        return None
    return (str(provider or "").strip().lower(), str(keyword_id or "").strip(),
            page_id, candidate_id, doi)


def observation_key(*, provider: str, keyword_id: str, page_id: str,
                    candidate_id: str) -> ObservationKey | None:
    candidate_id = str(candidate_id or "").strip()
    page_id = str(page_id or "").strip()
    if not candidate_id or not page_id:
        return None
    return (str(provider or "").strip().lower(), str(keyword_id or "").strip(),
            page_id, candidate_id)


def _merge_optional(old: Any, new: Any, field: str) -> Any:
    if old and new and old != new:
        raise IdentityRefConflict(f"conflicting {field}")
    return old or new


def _merge_ref(old: DiscoveryIdentityRef, new: DiscoveryIdentityRef) -> DiscoveryIdentityRef:
    if old.paper_number != new.paper_number:
        raise IdentityRefConflict("conflicting paper_number")
    return replace(
        old,
        scope=_merge_optional(old.scope, new.scope, "scope"),
        workspace_path=_merge_optional(old.workspace_path, new.workspace_path, "workspace_path"),
        source_record_path=_merge_optional(
            old.source_record_path, new.source_record_path, "source_record_path"),
        receipt_path=_merge_optional(old.receipt_path, new.receipt_path, "receipt_path"),
    )


class DiscoveryWorkspaceIndex:
    """One mutable fact map keyed by ``(identity_key, paper_number)``."""

    def __init__(self, refs: Iterable[DiscoveryIdentityRef] = ()) -> None:
        self._by_identity: dict[IdentityKey, dict[str, DiscoveryIdentityRef]] = {}
        self._by_observation: dict[ObservationKey, dict[str, DiscoveryIdentityRef]] = {}
        for ref in refs:
            self.add_or_merge(ref)

    def add_or_merge(self, ref: DiscoveryIdentityRef) -> None:
        key = identity_key(
            provider=ref.provider, keyword_id=ref.keyword_id, page_id=ref.page_id,
            candidate_id=ref.candidate_id, normalized_doi=ref.normalized_doi)
        if key is None:
            return
        bucket = self._by_identity.setdefault(key, {})
        old = bucket.get(ref.paper_number)
        bucket[ref.paper_number] = _merge_ref(old, ref) if old else ref
        observation = observation_key(
            provider=ref.provider, keyword_id=ref.keyword_id,
            page_id=ref.page_id, candidate_id=ref.candidate_id)
        if observation is not None:
            observation_bucket = self._by_observation.setdefault(observation, {})
            observation_old = observation_bucket.get(ref.paper_number)
            observation_bucket[ref.paper_number] = (
                _merge_ref(observation_old, ref) if observation_old else ref)

    def remove_workspace(self, paper_number: str) -> None:
        for key in tuple(self._by_identity):
            self._by_identity[key].pop(paper_number, None)
            if not self._by_identity[key]:
                del self._by_identity[key]
        for key in tuple(self._by_observation):
            self._by_observation[key].pop(paper_number, None)
            if not self._by_observation[key]:
                del self._by_observation[key]

    def lookup(self, *, candidate_id: str, page_id: str, keyword_id: str,
               normalized_doi: str, provider: str = "") -> list[DiscoveryIdentityRef]:
        key = identity_key(provider=provider, keyword_id=keyword_id, page_id=page_id,
                           candidate_id=candidate_id, normalized_doi=normalized_doi)
        if key:
            return list(self._by_identity.get(key, {}).values())
        observation = observation_key(
            provider=provider, keyword_id=keyword_id, page_id=page_id,
            candidate_id=candidate_id)
        return list(self._by_observation.get(observation, {}).values()) if observation else []

    def lookup_by_doi(self, normalized_doi: str) -> list[DiscoveryIdentityRef]:
        doi = normalize_doi(normalized_doi)
        return [ref for ref in self.refs if ref.normalized_doi == doi]

    @property
    def workspace_count(self) -> int:
        return len({ref.paper_number for ref in self.refs})

    @property
    def refs(self) -> tuple[DiscoveryIdentityRef, ...]:
        return tuple(
            ref
            for key in sorted(self._by_identity)
            for _, ref in sorted(self._by_identity[key].items())
        )

    def copy(self) -> "DiscoveryWorkspaceIndex":
        return DiscoveryWorkspaceIndex(self.refs)

    def freeze(self) -> "FrozenDiscoveryWorkspaceIndex":
        return FrozenDiscoveryWorkspaceIndex(self._by_identity, self._by_observation)

    def with_added_refs(self, refs: Iterable[DiscoveryIdentityRef]) -> "FrozenDiscoveryWorkspaceIndex":
        mutable = self.copy()
        for ref in refs:
            mutable.add_or_merge(ref)
        return mutable.freeze()


class FrozenDiscoveryWorkspaceIndex(DiscoveryWorkspaceIndex):
    def __init__(self, facts: Mapping[IdentityKey, Mapping[str, DiscoveryIdentityRef]],
                 observations: Mapping[ObservationKey, Mapping[str, DiscoveryIdentityRef]] | None = None) -> None:
        self._by_identity = MappingProxyType({
            key: MappingProxyType(dict(bucket)) for key, bucket in facts.items()
        })
        self._by_observation = MappingProxyType({
            key: MappingProxyType(dict(bucket)) for key, bucket in (observations or {}).items()
        })

    def add_or_merge(self, ref: DiscoveryIdentityRef) -> None:
        raise TypeError("frozen discovery workspace index")

    def remove_workspace(self, paper_number: str) -> None:
        raise TypeError("frozen discovery workspace index")

    def copy(self) -> DiscoveryWorkspaceIndex:
        return DiscoveryWorkspaceIndex(self.refs)

    def freeze(self) -> "FrozenDiscoveryWorkspaceIndex":
        return self

    def with_added_refs(self, refs: Iterable[DiscoveryIdentityRef]) -> "FrozenDiscoveryWorkspaceIndex":
        """Copy only the outer fact map and identity buckets being appended."""
        facts: dict[IdentityKey, Mapping[str, DiscoveryIdentityRef]] = dict(self._by_identity)
        for ref in refs:
            key = identity_key(
                provider=ref.provider, keyword_id=ref.keyword_id, page_id=ref.page_id,
                candidate_id=ref.candidate_id, normalized_doi=ref.normalized_doi)
            if key is None:
                continue
            bucket = dict(facts.get(key, {}))
            old = bucket.get(ref.paper_number)
            bucket[ref.paper_number] = _merge_ref(old, ref) if old else ref
            facts[key] = MappingProxyType(bucket)
        result = object.__new__(FrozenDiscoveryWorkspaceIndex)
        result._by_identity = MappingProxyType(facts)
        rebuilt = DiscoveryWorkspaceIndex(result.refs)
        result._by_observation = MappingProxyType({
            key: MappingProxyType(dict(bucket))
            for key, bucket in rebuilt._by_observation.items()})
        return result

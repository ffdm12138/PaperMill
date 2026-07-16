"""Pure in-memory duplicate index used by registry snapshots."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from src.discovery.models import normalize_doi


@dataclass(frozen=True)
class DuplicateRef:
    scope: str
    paper_number: str
    paper_name: str
    folder: str
    source: str
    workspace_kind: str = ""
    doi: str = ""
    pdf_md5: str = ""
    pdf_sha256: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _ref_matches_workspace(ref: DuplicateRef, paper_number: str) -> bool:
    target = str(paper_number or "")
    if not target:
        return False
    folder_name = str(ref.folder or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return target in {str(ref.paper_number or ""), str(ref.paper_name or ""), folder_name}


def _unique_refs(refs: list[DuplicateRef]) -> list[DuplicateRef]:
    unique: dict[tuple[str, str, str, str], DuplicateRef] = {}
    for ref in refs:
        unique[(ref.scope, ref.paper_number, ref.paper_name, ref.folder)] = ref
    return list(unique.values())


@dataclass
class DuplicateIndex:
    doi_to_refs: dict[str, list[DuplicateRef]] = field(default_factory=dict)
    pdf_md5_to_refs: dict[str, list[DuplicateRef]] = field(default_factory=dict)
    pdf_sha256_to_refs: dict[str, list[DuplicateRef]] = field(default_factory=dict)
    _frozen: bool = False

    def add_doi_ref(self, ref: DuplicateRef) -> None:
        if self._frozen:
            raise TypeError("frozen duplicate index")
        doi = normalize_doi(ref.doi)
        if not doi:
            return
        bucket = self.doi_to_refs.setdefault(doi, [])
        if ref.paper_number and any(existing.paper_number == ref.paper_number for existing in bucket):
            return
        bucket.append(ref)

    def lookup_doi(self, normalized_doi: str, *, exclude_paper_number: str = "") -> list[DuplicateRef]:
        doi = normalize_doi(normalized_doi)
        if not doi:
            return []
        refs = self.doi_to_refs.get(doi, [])
        if exclude_paper_number:
            refs = [ref for ref in refs if not _ref_matches_workspace(ref, exclude_paper_number)]
        return _unique_refs(refs)

    def contains_workspace(self, paper_number: str) -> bool:
        return any(
            _ref_matches_workspace(ref, paper_number)
            for refs in self.doi_to_refs.values()
            for ref in refs
        )

    def remove_workspace(self, paper_number: str) -> None:
        if self._frozen:
            raise TypeError("frozen duplicate index")
        for bucket in self.doi_to_refs.values():
            bucket[:] = [ref for ref in bucket if not _ref_matches_workspace(ref, paper_number)]

    def view_excluding(self, paper_number: str) -> "DuplicateIndexView":
        return DuplicateIndexView(self, excluded=paper_number)

    def copy(self) -> "DuplicateIndex":
        return DuplicateIndex(
            doi_to_refs={key: list(value) for key, value in self.doi_to_refs.items()},
            pdf_md5_to_refs={key: list(value) for key, value in self.pdf_md5_to_refs.items()},
            pdf_sha256_to_refs={key: list(value) for key, value in self.pdf_sha256_to_refs.items()},
        )

    def freeze(self) -> "DuplicateIndex":
        frozen = self.copy()
        frozen._frozen = True
        return frozen

    def with_added_doi_refs(self, refs: list[DuplicateRef]) -> "DuplicateIndex":
        """Copy-on-write append for refs belonging to a new workspace."""
        result = DuplicateIndex(
            doi_to_refs=dict(self.doi_to_refs),
            pdf_md5_to_refs=dict(self.pdf_md5_to_refs),
            pdf_sha256_to_refs=dict(self.pdf_sha256_to_refs),
        )
        for ref in refs:
            doi = normalize_doi(ref.doi)
            if not doi:
                continue
            bucket = list(result.doi_to_refs.get(doi, ()))
            if ref.paper_number and any(item.paper_number == ref.paper_number for item in bucket):
                continue
            bucket.append(ref)
            result.doi_to_refs[doi] = bucket
        result._frozen = True
        return result


class DuplicateIndexView:
    __slots__ = ("_underlying", "excluded")

    def __init__(self, underlying: DuplicateIndex, *, excluded: str = ""):
        self._underlying = underlying
        self.excluded = str(excluded or "")

    def lookup_doi(self, normalized_doi: str, *, exclude_paper_number: str = "") -> list[DuplicateRef]:
        return self._underlying.lookup_doi(
            normalized_doi, exclude_paper_number=exclude_paper_number or self.excluded)

    @property
    def doi_to_refs(self) -> dict[str, list[DuplicateRef]]:
        if not self.excluded:
            return self._underlying.doi_to_refs
        return {
            doi: [ref for ref in refs if not _ref_matches_workspace(ref, self.excluded)]
            for doi, refs in self._underlying.doi_to_refs.items()
        }

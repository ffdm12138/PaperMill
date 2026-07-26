"""Batch-level Crossref title→DOI resolution service.

Contract (frozen):

- All title-resolution HTTP goes through the unified ``ProviderClient``
  (shared Crossref limiter, retry, backoff, circuit breaker) with
  ``purpose="title_resolution"`` — never a raw ``requests`` call.
- The resolution budget is **batch-level**: one
  :class:`~src.discovery.budgets.BatchDoiResolutionBudget` instance is
  shared by every drain in the batch (not re-initialized per drain).
- Identical titles within one batch are resolved once (in-batch dedup).
- A durable cache (JSON files under a cache dir) prevents re-requesting
  the same (normalized title, year) across restarts.
- On 429 the service freezes dispatch for the rest of the batch instead
  of letting every worker keep hammering the API.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Callable

from src.discovery.runtime.budgets import BatchDoiResolutionBudget
from src.utils.identifiers import normalize_title
from src.discovery.providers.provider_client import ProviderClient
from src.discovery.providers.provider_errors import (
    ProviderError,
    ProviderRateLimited,
    ProviderRequestBudgetExhausted,
)
from src.discovery.resolve_crossref import ResolvedDoiMatch, resolve_doi_match_by_title


class DurableTitleCache:
    """Small JSON-file cache keyed by (normalized title, year bucket)."""

    def __init__(self, cache_dir: Path | None) -> None:
        self._dir = cache_dir
        self._lock = threading.Lock()

    @staticmethod
    def _key(title_norm: str, year: int | None) -> str:
        raw = f"{title_norm}|{year if year is not None else ''}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def _path(self, key: str) -> Path:
        assert self._dir is not None
        return self._dir / f"{key}.json"

    def get(self, title_norm: str, year: int | None) -> dict[str, Any] | None:
        if self._dir is None:
            return None
        path = self._path(self._key(title_norm, year))
        with self._lock:
            if not path.is_file():
                return None
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
        if not isinstance(data, dict) or "result" not in data:
            return None
        return data

    def put(self, title_norm: str, year: int | None, result: dict[str, Any] | None) -> None:
        if self._dir is None:
            return
        path = self._path(self._key(title_norm, year))
        payload = json.dumps(
            {"title_norm": title_norm, "year": year, "result": result},
            ensure_ascii=False,
        )
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(path)


class TitleResolutionService:
    """Batch-scoped title→DOI resolver with budget, dedup and cache."""

    def __init__(
        self,
        *,
        client: ProviderClient,
        budget: BatchDoiResolutionBudget,
        cache: DurableTitleCache | None = None,
        runtime_guard: Any | None = None,
    ) -> None:
        self._client = client
        self._budget = budget
        self._cache = cache or DurableTitleCache(None)
        self._in_batch: dict[tuple[str, int | None], dict[str, Any] | None] = {}
        self._in_batch_lock = threading.Lock()
        self._runtime_guard = runtime_guard

    def bind_guard(self, guard: Any) -> None:
        self._runtime_guard = guard

    def resolve(
        self,
        title: str,
        *,
        year: int | None = None,
        domain_id: str | None = None,
    ) -> ResolvedDoiMatch | None:
        """Resolve one title to a DOI match, or ``None``.

        Never raises for provider failures: budget/rate-limit/provider
        failures all yield ``None`` (candidate stays unresolved and will be
        retried by a later drain), and the batch budget records why.
        """
        if self._runtime_guard is not None:
            self._runtime_guard.ensure_open()
        title_norm = normalize_title(title)
        if not title_norm:
            return None
        key = (title_norm, year)

        # 1. In-batch dedup: identical title resolved once per batch.
        with self._in_batch_lock:
            if key in self._in_batch:
                self._budget.note_dedup_hit()
                cached = self._in_batch[key]
                return self._to_match(cached)

        # 2. Durable cache: avoid re-requesting across restarts.
        cached_entry = self._cache.get(title_norm, year)
        if cached_entry is not None:
            self._budget.note_cache_hit()
            with self._in_batch_lock:
                self._in_batch[key] = cached_entry.get("result")
            return self._to_match(cached_entry.get("result"))

        # 3. Batch-level budget gate (shared across all drains).
        if not self._budget.try_acquire():
            return None

        # 4. Real provider request through the unified client.
        try:
            match = resolve_doi_match_by_title(
                title, year=year, domain_id=domain_id, client=self._client,
            )
        except ProviderRequestBudgetExhausted:
            # The request valve belongs to the enclosing batch.  It must not
            # be converted to a missing DOI or a provider failure here.
            raise
        except ProviderRateLimited:
            # 429 after retries: freeze dispatch for the rest of the batch.
            self._budget.stop_for_rate_limit()
            with self._in_batch_lock:
                self._in_batch[key] = None
            return None
        except ProviderError:
            with self._in_batch_lock:
                self._in_batch[key] = None
            return None

        result = match.to_dict() if match is not None else None
        if result is not None:
            self._budget.note_resolved()
        with self._in_batch_lock:
            self._in_batch[key] = result
        self._cache.put(title_norm, year, result)
        return match

    @staticmethod
    def _to_match(result: dict[str, Any] | None) -> ResolvedDoiMatch | None:
        if not result:
            return None
        return ResolvedDoiMatch(
            doi=str(result.get("doi") or ""),
            provider=str(result.get("provider") or "crossref"),
            confidence=float(result.get("confidence") or 0.0),
            matched_title=str(result.get("matched_title") or ""),
            raw_record=result.get("raw_record") if isinstance(result.get("raw_record"), dict) else {},
        )

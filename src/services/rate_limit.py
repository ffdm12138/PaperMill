"""Conservative rate limiter for metadata API providers (Crossref/OpenAlex).

Single-threaded by default. Enforces:
- A global ``paper_interval_seconds`` between papers (not between requests).
- A per-provider ``min_interval_seconds`` between requests to the same provider.
- Jitter so requests don't fall on a fixed cadence.
- ``Retry-After`` header respect on 429.
- Crossref ``x-rate-limit-limit`` / ``x-rate-limit-interval`` adaptive parsing.
- Exponential backoff on 429, long backoff on 403.
- Accumulated statistics for the resolve report.

This module does NOT make HTTP requests itself — it is a pure timing/coordination
helper. Callers call ``wait(provider)`` before a request, ``record_response(...)``
after, and ``backoff(...)`` when they hit 429/403/timeout. The caller is
responsible for retrying.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from loguru import logger


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class ProviderState:
    last_request_at: float = 0.0
    min_interval: float = 0.0
    backoff_level: int = 0  # exponential backoff level for 429
    consecutive_429: int = 0
    consecutive_403: int = 0
    request_count: int = 0
    last_status: int = 0


@dataclass
class RateLimitStats:
    total_requests: int = 0
    requests_by_provider: dict[str, int] = field(default_factory=dict)
    http_429_count: int = 0
    http_403_count: int = 0
    timeout_count: int = 0
    total_sleep_seconds: float = 0.0
    sleep_log: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_requests": self.total_requests,
            "requests_by_provider": dict(self.requests_by_provider),
            "http_429_count": self.http_429_count,
            "http_403_count": self.http_403_count,
            "timeout_count": self.timeout_count,
            "total_sleep_seconds": round(self.total_sleep_seconds, 3),
            "sleep_log": list(self.sleep_log),
        }


def default_config() -> dict[str, Any]:
    """Return the conservative default rate-limit configuration."""
    return {
        "schema_version": "1.0",
        "global": {
            "concurrency": 1,
            "paper_interval_seconds": 8.0,
            "jitter_seconds": 1.5,
            "max_retries": 5,
            "timeout_seconds": 30,
            "retry_after_respected": True,
        },
        "providers": {
            "crossref": {
                "enabled": True,
                "min_interval_seconds": 3.0,
                "mailto": "",
                "user_agent": "MinerU/1.0",
                "adaptive_from_headers": True,
            },
            "openalex": {
                "enabled": True,
                "min_interval_seconds": 2.0,
                "mailto": "",
                "user_agent": "MinerU/1.0",
                "adaptive_from_headers": True,
            },
            "semantic_scholar": {
                "enabled": False,
                "min_interval_seconds": 1.2,
            },
        },
        "backoff": {
            "on_429_initial_sleep_seconds": 60,
            "on_403_initial_sleep_seconds": 300,
            "multiplier": 2.0,
            "max_sleep_seconds": 1800,
        },
    }


class ProviderRateLimiter:
    """Conservative single-threaded rate limiter with adaptive backoff.

    ``wait(provider)`` must be called BEFORE each request. It sleeps so that
    both the per-provider min interval and the global paper interval are
    respected. ``record_response(...)`` must be called AFTER each response so
    adaptive headers can tighten the min interval. ``backoff(...)`` is called
    when the caller detects 429/403/timeout and needs to sleep before retrying.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        config = config or default_config()
        self._global = dict(config.get("global") or {})
        self._providers: dict[str, dict] = {
            k: dict(v) for k, v in (config.get("providers") or {}).items()
        }
        self._backoff = dict(config.get("backoff") or {})
        self.paper_interval = float(self._global.get("paper_interval_seconds", 8.0))
        self.jitter = float(self._global.get("jitter_seconds", 1.5))
        self.max_retries = int(self._global.get("max_retries", 5))
        self.timeout_seconds = int(self._global.get("timeout_seconds", 30))
        self.retry_after_respected = bool(self._global.get("retry_after_respected", True))
        self._state: dict[str, ProviderState] = {}
        self._last_paper_time: float = 0.0
        self._paper_started: bool = False
        self.stats = RateLimitStats()

    # ── Configuration accessors ──────────────────────────────────────────

    def provider_config(self, provider: str) -> dict[str, Any]:
        return self._providers.get(provider, {})

    def is_provider_enabled(self, provider: str) -> bool:
        cfg = self.provider_config(provider)
        return cfg.get("enabled", True)

    def provider_headers(self, provider: str) -> dict[str, str]:
        """Return mailto/User-Agent headers for a provider (for callers to use)."""
        cfg = self.provider_config(provider)
        headers: dict[str, str] = {}
        ua = str(cfg.get("user_agent") or "").strip()
        if ua:
            headers["User-Agent"] = ua
        mailto = str(cfg.get("mailto") or "").strip()
        if mailto:
            if provider == "openalex":
                headers["X-Email"] = mailto  # OpenAlex prefers mailto param, but header is harmless
            else:
                headers["User-Agent"] = f"{ua} (mailto:{mailto})" if ua else f"MinerU/1.0 (mailto:{mailto})"
        return headers

    def provider_mailto(self, provider: str) -> str:
        return str(self.provider_config(provider).get("mailto") or "").strip()

    # ── State accessors ──────────────────────────────────────────────────

    def _state_for(self, provider: str) -> ProviderState:
        if provider not in self._state:
            cfg = self.provider_config(provider)
            self._state[provider] = ProviderState(
                min_interval=float(cfg.get("min_interval_seconds", 1.0)),
            )
        return self._state[provider]

    def set_provider_min_interval(self, provider: str, seconds: float) -> None:
        """Override a provider's min interval (e.g. from CLI --provider-min-interval)."""
        state = self._state_for(provider)
        state.min_interval = max(0.0, float(seconds))
        # Also update the config dict so provider_config reflects it.
        self._providers.setdefault(provider, {})["min_interval_seconds"] = state.min_interval

    def set_paper_interval(self, seconds: float) -> None:
        self.paper_interval = max(0.0, float(seconds))

    # ── Core timing ──────────────────────────────────────────────────────

    def _jitter(self) -> float:
        """Non-negative jitter in [0, jitter_seconds]."""
        if self.jitter <= 0:
            return 0.0
        return random.uniform(0.0, self.jitter)

    def _sleep(self, seconds: float, reason: str, provider: str = "") -> None:
        if seconds <= 0:
            return
        logger.info("rate_limit sleep {:.1f}s: {} (provider={})", seconds, reason, provider or "-")
        time.sleep(seconds)
        self.stats.total_sleep_seconds += seconds
        self.stats.sleep_log.append({
            "provider": provider,
            "seconds": round(seconds, 3),
            "reason": reason,
            "at": _now_iso(),
        })

    def begin_paper(self) -> None:
        """Mark the start of processing a new paper.

        After this is called, the first ``wait(provider)`` will also enforce
        the global ``paper_interval_seconds`` since the last paper's first
        request.
        """
        self._paper_started = True

    def wait(self, provider: str) -> None:
        """Sleep so the provider min interval AND the paper interval are respected.

        Call this BEFORE each request to ``provider``.
        """
        state = self._state_for(provider)
        now = time.monotonic()
        # Per-provider min interval
        if state.last_request_at > 0:
            elapsed = now - state.last_request_at
            need = state.min_interval - elapsed
            if need > 0:
                self._sleep(need, f"provider_min_interval {state.min_interval:.1f}s", provider)
        # Global paper interval: enforced once per paper, before the first
        # request of that paper.
        if self._paper_started and self._last_paper_time > 0:
            elapsed = time.monotonic() - self._last_paper_time
            need = self.paper_interval - elapsed
            if need > 0:
                self._sleep(need, f"paper_interval {self.paper_interval:.1f}s", provider)
        if self._paper_started and self._last_paper_time == 0:
            self._last_paper_time = time.monotonic()
        # Record request time + jitter
        jitter = self._jitter()
        if jitter > 0:
            self._sleep(jitter, "jitter", provider)
        self._state_for(provider).last_request_at = time.monotonic()
        self._paper_started = False  # paper interval consumed
        state.request_count += 1
        self.stats.total_requests += 1
        self.stats.requests_by_provider[provider] = self.stats.requests_by_provider.get(provider, 0) + 1

    # ── Response handling ────────────────────────────────────────────────

    def record_response(self, provider: str, response_headers: dict, status_code: int) -> None:
        """Record a response and adapt provider min interval from headers.

        Parses:
        - ``Retry-After`` (seconds)
        - Crossref ``X-Rate-Limit-Limit`` + ``X-Rate-Limit-Interval``
        """
        state = self._state_for(provider)
        state.last_status = int(status_code)
        headers = {str(k).lower(): str(v) for k, v in (response_headers or {}).items()}
        # Crossref adaptive: X-Rate-Limit-Limit / X-Rate-Limit-Interval
        cfg = self.provider_config(provider)
        if cfg.get("adaptive_from_headers", False):
            limit_str = headers.get("x-rate-limit-limit")
            interval_str = headers.get("x-rate-limit-interval")
            if limit_str and interval_str:
                try:
                    limit = float(limit_str)
                    interval_sec = _parse_interval_seconds(interval_str)
                    if limit > 0 and interval_sec > 0:
                        # min interval = interval / limit (the shared-pool rate)
                        adapted = interval_sec / limit
                        # Only tighten (never loosen) to stay conservative
                        if adapted > state.min_interval:
                            state.min_interval = adapted
                            logger.info("rate_limit {} adapted min_interval to {:.2f}s from headers",
                                        provider, adapted)
                except (ValueError, ZeroDivisionError):
                    pass
        # Reset consecutive counters on success
        if 200 <= status_code < 300:
            state.consecutive_429 = 0
            state.consecutive_403 = 0
            state.backoff_level = 0

    def backoff(self, provider: str, reason: str, retry_after: int | None = None) -> float:
        """Sleep for a backoff duration and return the seconds slept.

        reason: "429" | "403" | "timeout" | other
        retry_after: seconds from a Retry-After header (429/503)
        """
        state = self._state_for(provider)
        initial = float(self._backoff.get("on_429_initial_sleep_seconds", 60))
        on_403 = float(self._backoff.get("on_403_initial_sleep_seconds", 300))
        multiplier = float(self._backoff.get("multiplier", 2.0))
        max_sleep = float(self._backoff.get("max_sleep_seconds", 1800))

        if reason == "429":
            self.stats.http_429_count += 1
            state.consecutive_429 += 1
            state.backoff_level += 1
            if self.retry_after_respected and retry_after is not None and retry_after > 0:
                sleep = float(retry_after)
                self._sleep(sleep, f"429 retry_after={retry_after}s", provider)
                return sleep
            level = max(1, state.backoff_level)
            sleep = min(initial * (multiplier ** (level - 1)), max_sleep)
            self._sleep(sleep, f"429 exponential backoff level={level}", provider)
            return sleep
        if reason == "403":
            self.stats.http_403_count += 1
            state.consecutive_403 += 1
            sleep = min(on_403 * (multiplier ** max(0, state.consecutive_403 - 1)), max_sleep)
            self._sleep(sleep, f"403 long backoff consecutive={state.consecutive_403}", provider)
            return sleep
        if reason == "timeout":
            self.stats.timeout_count += 1
            state.backoff_level += 1
            level = max(1, state.backoff_level)
            sleep = min(initial * (multiplier ** (level - 1)), max_sleep)
            self._sleep(sleep, f"timeout backoff level={level}", provider)
            return sleep
        # generic
        state.backoff_level += 1
        sleep = min(initial * (multiplier ** max(0, state.backoff_level - 1)), max_sleep)
        self._sleep(sleep, f"backoff reason={reason}", provider)
        return sleep

    def should_stop(self, provider: str) -> bool:
        """True when a provider has hit too many consecutive 403s (stop)."""
        state = self._state_for(provider)
        return state.consecutive_403 >= max(2, self.max_retries // 2)

    def stats_dict(self) -> dict[str, Any]:
        return self.stats.to_dict()


def _parse_interval_seconds(text: str) -> float:
    """Parse a Crossref X-Rate-Limit-Interval value like '1s' or '5m'."""
    text = str(text).strip().lower()
    if not text:
        return 0.0
    try:
        if text.endswith("ms"):
            return float(text[:-2]) / 1000.0
        if text.endswith("s"):
            return float(text[:-1])
        if text.endswith("m"):
            return float(text[:-1]) * 60.0
        if text.endswith("h"):
            return float(text[:-1]) * 3600.0
        return float(text)
    except ValueError:
        return 0.0


def load_config(path: str | None = None) -> dict[str, Any]:
    """Load a rate-limit config JSON, falling back to defaults."""
    if not path:
        return default_config()
    import json
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return default_config()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default_config()
    if not isinstance(data, dict):
        return default_config()
    return data

"""Tests for the metadata rate limiter + checkpoint + probe.

Covers:
- provider min interval respected
- paper interval respected (2 papers, 3 papers, same-paper multi-provider)
- jitter non-negative
- 429 reads Retry-After
- 429 without Retry-After exponential backoff
- 403 triggers long backoff / stop
- checkpoint resume
- citation-ready metadata skipped by default
- --rate-probe --probe-size processes only N papers
- concurrency off = no parallel requests (single-threaded)
"""
from __future__ import annotations

import json
import runpy
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from src.services.rate_limit import (
    ProviderRateLimiter,
    default_config,
    _parse_interval_seconds,
)
from src.services.metadata_resolve_checkpoint import (
    load_checkpoint,
    record_item,
    is_done,
    save_checkpoint,
)
from src.services.v2_library import empty_metadata


REPO = Path(__file__).resolve().parent.parent.parent


# ── FakeClock helper ────────────────────────────────────────────────────

class FakeClock:
    """Fake ``time.monotonic()`` / ``time.sleep()`` for deterministic tests.

    Usage::

        clock = FakeClock(now=100.0)
        with patch("src.services.rate_limit.time.monotonic", clock.monotonic), \
             patch("src.services.rate_limit.time.sleep", clock.sleep):
            ...

    ``clock.sleeps`` accumulates every sleep duration; ``clock._now`` advances
    accordingly so monotonic values reflect elapsed time after sleeps.
    """

    def __init__(self, now: float = 0.0):
        self._now = now
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.sleeps.append(seconds)
            self._now += seconds


# ── 1. provider min interval respected ─────────────────────────────────

def test_provider_min_interval_respected():
    """Second call within the min interval must sleep the remaining time.

    Uses FakeClock so the test is deterministic and does not depend on real
    time.sleep (which makes the full suite slow and flaky).
    """
    clock = FakeClock(now=100.0)
    rl = ProviderRateLimiter(default_config())
    rl.set_provider_min_interval("crossref", 0.3)
    rl.set_paper_interval(0.0)  # disable paper interval for this test
    rl.set_provider_min_interval("openalex", 0.0)
    rl.jitter = 0.0
    rl.begin_paper()

    with patch("src.services.rate_limit.time.monotonic", clock.monotonic), \
         patch("src.services.rate_limit.time.sleep", clock.sleep):
        rl.wait("crossref")   # first call: no sleep (last_request_at == 0)
        clock.sleeps.clear()
        rl.wait("crossref")   # second call: should sleep ~0.3s

    assert any(abs(s - 0.3) < 0.01 for s in clock.sleeps), \
        f"expected 0.3s provider-min-interval sleep, got {clock.sleeps}"


# ── 2. paper interval respected (fake clock) ───────────────────────────

def test_paper_interval_respected():
    """Paper 1→2 transition enforces paper_interval (base case)."""
    clock = FakeClock(now=100.0)
    rl = ProviderRateLimiter(default_config())
    rl.set_paper_interval(10.0)
    rl.set_provider_min_interval("crossref", 0.0)
    rl.set_provider_min_interval("openalex", 0.0)
    rl.jitter = 0.0

    with patch("src.services.rate_limit.time.monotonic", clock.monotonic), \
         patch("src.services.rate_limit.time.sleep", clock.sleep):
        # Paper 1: first request (no prior paper → no paper interval)
        rl.begin_paper()
        clock.sleeps.clear()
        rl.wait("crossref")
        assert rl._paper_wait_pending is False
        assert rl._last_paper_time == 100.0
        assert len(clock.sleeps) == 0  # no interval sleep for first paper

        # Paper 2: 5 s later → paper_interval=10, elapsed=5, need=5
        clock._now = 105.0
        rl.begin_paper()
        assert rl._paper_wait_pending is True
        clock.sleeps.clear()
        rl.wait("crossref")
        assert any(abs(s - 5.0) < 0.01 for s in clock.sleeps), \
            f"paper2: expected 5s paper-interval sleep, got {clock.sleeps}"
        assert rl._last_paper_time == 110.0  # 105 + 5


def test_paper_interval_respected_three_papers():
    """Every paper transition (1→2, 2→3) enforces paper_interval.

    Regression: the interval must be measured from the *previous* paper's
    first request time, not accumulate across all papers.
    """
    clock = FakeClock(now=100.0)
    rl = ProviderRateLimiter(default_config())
    rl.set_paper_interval(10.0)
    rl.set_provider_min_interval("crossref", 0.0)
    rl.set_provider_min_interval("openalex", 0.0)
    rl.jitter = 0.0

    with patch("src.services.rate_limit.time.monotonic", clock.monotonic), \
         patch("src.services.rate_limit.time.sleep", clock.sleep):
        # Paper 1: no prior paper → no paper-interval sleep
        rl.begin_paper()
        clock.sleeps.clear()
        rl.wait("crossref")
        assert rl._last_paper_time == 100.0
        t1 = rl._last_paper_time

        # Paper 2: advance 3 s → need=10-3=7
        clock._now = 103.0
        rl.begin_paper()
        clock.sleeps.clear()
        rl.wait("crossref")
        assert any(abs(s - 7.0) < 0.01 for s in clock.sleeps), \
            f"paper2: expected 7s paper-interval sleep, got {clock.sleeps}"
        assert rl._last_paper_time == 110.0  # 103 + 7
        assert rl._last_paper_time > t1
        t2 = rl._last_paper_time

        # Paper 3: advance 2 s from t2 (now=112) → need=10-(112-110)=8
        clock._now = 112.0
        rl.begin_paper()
        clock.sleeps.clear()
        rl.wait("crossref")
        assert any(abs(s - 8.0) < 0.01 for s in clock.sleeps), \
            f"paper3: expected 8s paper-interval sleep, got {clock.sleeps}"
        assert rl._last_paper_time == 120.0  # 112 + 8
        assert rl._last_paper_time > t2


def test_same_paper_multiple_providers_no_double_paper_interval():
    """Inside one paper, the second provider must NOT trigger a second
    paper-interval sleep — only the provider min interval applies."""
    clock = FakeClock(now=100.0)
    rl = ProviderRateLimiter(default_config())
    rl.set_paper_interval(10.0)
    rl.set_provider_min_interval("crossref", 0.0)
    rl.set_provider_min_interval("openalex", 0.0)
    rl.jitter = 0.0

    with patch("src.services.rate_limit.time.monotonic", clock.monotonic), \
         patch("src.services.rate_limit.time.sleep", clock.sleep):
        # Paper 1: crossref (triggers paper interval once)
        rl.begin_paper()
        clock.sleeps.clear()
        rl.wait("crossref")
        assert rl._paper_wait_pending is False
        t_before = rl._last_paper_time

        # Same paper: openalex (should NOT trigger paper interval again)
        clock.sleeps.clear()
        rl.wait("openalex")
        # No paper_interval entry should be in sleep log
        paper_reasons = [s["reason"] for s in rl.stats.sleep_log if "paper_interval" in s["reason"]]
        assert len(paper_reasons) == 0, f"unexpected paper-interval sleeps: {paper_reasons}"
        # _last_paper_time unchanged (only updated once per paper)
        assert rl._last_paper_time == t_before


# ── 3. jitter non-negative ─────────────────────────────────────────────

def test_jitter_non_negative():
    rl = ProviderRateLimiter(default_config())
    rl.jitter = 2.0
    for _ in range(100):
        j = rl._jitter()
        assert j >= 0.0
        assert j <= 2.0


def test_jitter_zero_when_disabled():
    rl = ProviderRateLimiter(default_config())
    rl.jitter = 0.0
    assert rl._jitter() == 0.0


# ── 4. 429 reads Retry-After ───────────────────────────────────────────

def test_429_reads_retry_after():
    rl = ProviderRateLimiter(default_config())
    rl.set_paper_interval(0.0)
    rl.set_provider_min_interval("crossref", 0.0)
    # Patch sleep to capture the duration without actually sleeping
    slept = []
    with patch("src.services.rate_limit.time.sleep", lambda s: slept.append(s)):
        rl.wait("crossref")  # initial request (no sleep since first)
        slept.clear()
        rl.backoff("crossref", "429", retry_after=42)
    assert any(abs(s - 42.0) < 0.01 for s in slept), f"expected 42s retry-after sleep, got {slept}"
    assert rl.stats.http_429_count == 1


# ── 5. 429 without Retry-After exponential backoff ─────────────────────

def test_429_exponential_backoff_without_retry_after():
    cfg = default_config()
    cfg["backoff"]["on_429_initial_sleep_seconds"] = 10
    cfg["backoff"]["multiplier"] = 2.0
    rl = ProviderRateLimiter(cfg)
    rl.set_paper_interval(0.0)
    rl.set_provider_min_interval("crossref", 0.0)
    slept = []
    with patch("src.services.rate_limit.time.sleep", lambda s: slept.append(s)):
        rl.wait("crossref")
        slept.clear()
        rl.backoff("crossref", "429")  # level 1: 10s
        rl.backoff("crossref", "429")  # level 2: 20s
        rl.backoff("crossref", "429")  # level 3: 40s
    assert len(slept) == 3
    assert slept[0] == pytest.approx(10.0)
    assert slept[1] == pytest.approx(20.0)
    assert slept[2] == pytest.approx(40.0)


# ── 6. 403 triggers long backoff / stop ────────────────────────────────

def test_403_long_backoff_and_stop():
    cfg = default_config()
    cfg["backoff"]["on_403_initial_sleep_seconds"] = 100
    cfg["backoff"]["multiplier"] = 2.0
    rl = ProviderRateLimiter(cfg)
    rl.set_paper_interval(0.0)
    rl.set_provider_min_interval("crossref", 0.0)
    slept = []
    with patch("src.services.rate_limit.time.sleep", lambda s: slept.append(s)):
        rl.wait("crossref")
        slept.clear()
        rl.backoff("crossref", "403")  # consecutive_403=1, sleep 100
        rl.backoff("crossref", "403")  # consecutive_403=2, sleep 200
    assert rl.stats.http_403_count == 2
    assert slept[0] == pytest.approx(100.0)
    assert slept[1] == pytest.approx(200.0)
    # After 2 consecutive 403s, should_stop returns True
    assert rl.should_stop("crossref")


# ── 7. checkpoint resume ───────────────────────────────────────────────

def test_checkpoint_resume(tmp_path):
    cp_path = tmp_path / "cp.json"
    data = load_checkpoint(cp_path)
    record_item(data, "0000000000000001", status="matched")
    record_item(data, "0000000000000002", status="failed", last_error="timeout")
    save_checkpoint(cp_path, data)
    reloaded = load_checkpoint(cp_path)
    assert is_done(reloaded, "0000000000000001")  # matched = done
    assert not is_done(reloaded, "0000000000000002")  # failed = retry
    assert reloaded["items"]["0000000000000002"]["last_error"] == "timeout"


# ── 8. citation-ready metadata skipped by default ──────────────────────

def test_citation_ready_metadata_skipped(tmp_path, monkeypatch):
    """The CLI must skip paper_raw workspaces whose metadata is already
    matched + has a valid DOI, unless --force is given."""
    paper_raw = tmp_path / "paper_raw"
    folder = paper_raw / "0000000000000001"
    folder.mkdir(parents=True)
    meta = empty_metadata("0000000000000001", source_type="manual_pdf")
    meta["identifiers"]["doi"] = "10.1000/ready"
    meta["metadata_match"]["status"] = "matched"
    (folder / "0000000000000001.metadata.json").write_text(json.dumps(meta), encoding="utf-8")

    # Any network call should explode if attempted
    from src.services import metadata_resolver as mr
    monkeypatch.setattr(mr, "enrich_from_doi", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must skip")))

    argv = [
        "resolve_paper_raw_metadata.py",
        "--paper-number", "0000000000000001",
        "--paper-raw-dir", str(paper_raw),
        "--all-catalog", str(tmp_path / "catalog" / "all.catalog.json"),
        "--papers-dir", str(tmp_path / "papers"),
        "--allow-network",
    ]
    saved = sys.argv
    sys.argv = argv
    try:
        runpy.run_path(str(REPO / "scripts" / "resolve_paper_raw_metadata.py"), run_name="__main__")
    except SystemExit:
        pass
    finally:
        sys.argv = saved


# ── 9. --rate-probe --probe-size processes only N papers ───────────────

def test_rate_probe_processes_only_n_papers(tmp_path, monkeypatch, capsys):
    paper_raw = tmp_path / "paper_raw"
    paper_raw.mkdir()
    for i in range(1, 6):
        folder = paper_raw / f"{i:016d}"
        folder.mkdir()
        meta = empty_metadata(f"{i:016d}", source_type="manual_pdf")
        meta["metadata_match"]["status"] = "unmatched"
        (folder / f"{i:016d}.metadata.json").write_text(json.dumps(meta), encoding="utf-8")

    # Disable network to avoid real calls
    from src.services import metadata_resolver as mr
    monkeypatch.setattr(mr, "enrich_from_doi", lambda *a, **k: mr.EnrichmentResult(doi="", warnings=["no net"]))

    argv = [
        "resolve_paper_raw_metadata.py",
        "--all-unmatched",
        "--paper-raw-dir", str(paper_raw),
        "--all-catalog", str(tmp_path / "catalog" / "all.catalog.json"),
        "--papers-dir", str(tmp_path / "papers"),
        "--rate-probe",
        "--probe-size", "3",
        "--no-network",
        "--paper-interval-seconds", "0",
    ]
    saved = sys.argv
    sys.argv = argv
    try:
        runpy.run_path(str(REPO / "scripts" / "resolve_paper_raw_metadata.py"), run_name="__main__")
    except SystemExit:
        pass
    finally:
        sys.argv = saved
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["summary"]["total"] == 3
    assert len(payload["items"]) == 3
    assert payload["rate_probe"] is True


# ── 10. concurrency off = no parallel requests ─────────────────────────

def test_single_threaded_no_parallel_requests():
    """With concurrency=1 (default), wait() must serialize requests.

    Uses FakeClock so the test is deterministic and does not depend on real
    time.sleep. If requests were parallel, total sleep would be 0 for 3 calls.
    Serial: call1 (0 wait) → call2 (0.1 wait) → call3 (0.1 wait) = 0.2s.
    """
    clock = FakeClock(now=100.0)
    rl = ProviderRateLimiter(default_config())
    assert int(default_config()["global"]["concurrency"]) == 1
    rl.set_paper_interval(0.0)
    rl.set_provider_min_interval("crossref", 0.1)
    rl.jitter = 0.0

    with patch("src.services.rate_limit.time.monotonic", clock.monotonic), \
         patch("src.services.rate_limit.time.sleep", clock.sleep):
        for i in range(3):
            if i > 0:
                rl.begin_paper()
            rl.wait("crossref")

    # Serial: 2 provider-min-interval sleeps of 0.1s each
    interval_sleeps = [s for s in clock.sleeps if abs(s - 0.1) < 0.01]
    assert len(interval_sleeps) == 2, \
        f"expected 2 provider-min-interval sleeps of 0.1s, got {clock.sleeps}"


# ── 11. record_response resets counters on success ─────────────────────

def test_record_response_resets_on_success():
    rl = ProviderRateLimiter(default_config())
    state = rl._state_for("crossref")
    state.consecutive_429 = 3
    state.consecutive_403 = 1
    state.backoff_level = 2
    rl.record_response("crossref", {}, 200)
    assert state.consecutive_429 == 0
    assert state.consecutive_403 == 0
    assert state.backoff_level == 0


# ── 12. Crossref X-Rate-Limit adaptive parsing ─────────────────────────

def test_crossref_adaptive_header_parsing():
    cfg = default_config()
    cfg["providers"]["crossref"]["min_interval_seconds"] = 1.0
    rl = ProviderRateLimiter(cfg)
    # X-Rate-Limit-Limit=50, X-Rate-Limit-Interval=1s → min interval = 1/50 = 0.02s
    # This is less than current 1.0s, so min_interval should NOT decrease (only tightens)
    rl.record_response("crossref", {"x-rate-limit-limit": "50", "x-rate-limit-interval": "1s"}, 200)
    assert rl._state_for("crossref").min_interval == 1.0  # unchanged (only tightens)
    # X-Rate-Limit-Limit=2, X-Rate-Limit-Interval=10s → min interval = 10/2 = 5s
    # This is more than current 1.0s, so min_interval should increase to 5s
    rl.record_response("crossref", {"x-rate-limit-limit": "2", "x-rate-limit-interval": "10s"}, 200)
    assert rl._state_for("crossref").min_interval == pytest.approx(5.0)


def test_parse_interval_seconds():
    assert _parse_interval_seconds("1s") == 1.0
    assert _parse_interval_seconds("5m") == 300.0
    assert _parse_interval_seconds("1h") == 3600.0
    assert _parse_interval_seconds("500ms") == 0.5
    assert _parse_interval_seconds("") == 0.0


# ── 13. provider enabled/disabled ──────────────────────────────────────

def test_provider_enabled_disabled():
    rl = ProviderRateLimiter(default_config())
    assert rl.is_provider_enabled("crossref") is True
    assert rl.is_provider_enabled("openalex") is True
    assert rl.is_provider_enabled("semantic_scholar") is False  # disabled in default config


# ── 14. stats accumulate ───────────────────────────────────────────────

def test_stats_accumulate():
    rl = ProviderRateLimiter(default_config())
    rl.set_paper_interval(0.0)
    rl.set_provider_min_interval("crossref", 0.0)
    with patch("src.services.rate_limit.time.sleep", lambda s: None):
        rl.wait("crossref")
        rl.wait("openalex")
        rl.backoff("crossref", "429", retry_after=1)
    stats = rl.stats_dict()
    assert stats["total_requests"] == 2
    assert stats["requests_by_provider"]["crossref"] == 1
    assert stats["requests_by_provider"]["openalex"] == 1
    assert stats["http_429_count"] == 1


# ── 15. MINERU_METADATA_CONTACT_EMAIL env var override ────────────────

def test_provider_headers_mailto_from_env(monkeypatch):
    """When MINERU_METADATA_CONTACT_EMAIL is set, headers must use it."""
    monkeypatch.setenv("MINERU_METADATA_CONTACT_EMAIL", "researcher@uni.edu")
    rl = ProviderRateLimiter(default_config())
    # crossref
    h = rl.provider_headers("crossref")
    assert "researcher@uni.edu" in h.get("User-Agent", "")
    assert "mailto:researcher@uni.edu" in h["User-Agent"]
    # openalex
    h2 = rl.provider_headers("openalex")
    assert h2.get("X-Email") == "researcher@uni.edu"
    assert "researcher@uni.edu" in h2.get("User-Agent", "")


def test_provider_headers_fallback(monkeypatch):
    """Without MINERU_METADATA_CONTACT_EMAIL, use config defaults."""
    monkeypatch.delenv("MINERU_METADATA_CONTACT_EMAIL", raising=False)
    rl = ProviderRateLimiter(default_config())
    h = rl.provider_headers("crossref")
    # default config has empty mailto → no mailto in headers
    assert "mailto:" not in h.get("User-Agent", "")
    assert h["User-Agent"] == "MinerU/1.0"
    h2 = rl.provider_headers("openalex")
    assert "X-Email" not in h2


def test_apply_env_mailto_override_updates_providers(monkeypatch):
    """_apply_env_mailto_override touches every provider config."""
    monkeypatch.setenv("MINERU_METADATA_CONTACT_EMAIL", "bot@example.org")
    rl = ProviderRateLimiter(default_config())
    for p in ("crossref", "openalex", "semantic_scholar"):
        pcfg = rl.provider_config(p)
        if not pcfg:
            continue
        assert pcfg.get("mailto") == "bot@example.org"
        assert "bot@example.org" in pcfg.get("user_agent", "")

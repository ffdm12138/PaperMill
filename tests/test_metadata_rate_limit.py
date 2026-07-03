"""Tests for the metadata rate limiter + checkpoint + probe.

Covers:
- provider min interval respected
- paper interval respected
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


REPO = Path(__file__).resolve().parent.parent


# ── 1. provider min interval respected ─────────────────────────────────

def test_provider_min_interval_respected():
    rl = ProviderRateLimiter(default_config())
    rl.set_provider_min_interval("crossref", 0.3)
    rl.set_paper_interval(0.0)  # disable paper interval for this test
    rl.set_provider_min_interval("openalex", 0.0)
    rl.begin_paper()
    start = time.monotonic()
    rl.wait("crossref")
    rl.wait("crossref")  # second call should wait ~0.3s
    elapsed = time.monotonic() - start
    assert elapsed >= 0.25  # allow small scheduling slack


# ── 2. paper interval respected ────────────────────────────────────────

def test_paper_interval_respected():
    rl = ProviderRateLimiter(default_config())
    rl.set_paper_interval(0.4)
    rl.set_provider_min_interval("crossref", 0.0)
    rl.set_provider_min_interval("openalex", 0.0)
    # First paper
    rl.begin_paper()
    rl.wait("crossref")
    # Second paper
    rl.begin_paper()
    start = time.monotonic()
    rl.wait("crossref")  # should wait ~0.4s from first paper's request
    elapsed = time.monotonic() - start
    assert elapsed >= 0.35


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
    """With concurrency=1 (default), wait() must serialize requests."""
    rl = ProviderRateLimiter(default_config())
    assert int(default_config()["global"]["concurrency"]) == 1
    rl.set_paper_interval(0.0)
    rl.set_provider_min_interval("crossref", 0.1)
    # If requests were parallel, total time would be < 0.1s for 3 calls.
    # Serial: call1 (0 wait) → call2 (0.1 wait) → call3 (0.1 wait) = ~0.2s
    start = time.monotonic()
    for i in range(3):
        if i > 0:
            rl.begin_paper()
        rl.wait("crossref")
    elapsed = time.monotonic() - start
    assert elapsed >= 0.18  # serial: at least 2 * 0.1s


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

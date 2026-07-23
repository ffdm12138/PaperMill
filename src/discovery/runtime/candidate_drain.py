"""Bounded candidate drain coordinator for DOI discovery.

Extracted from ``coordinator.py`` so the staging queue, consumer thread,
weighted semaphore, and no-progress watchdog each have a single owner.
The coordinator now calls::

    with CandidateDrainCoordinator(runtime, ...) as drain:
        schedule_and_wait_lanes(drain)

All drain operations are bounded by the runtime's cancellation token
and deadline.

Phase 2 (v100): notify() validates guard + cancellation + deadline +
drain closed state.  All drain commands enter a single consumer queue.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from src.discovery.runtime.batch_runtime import DiscoveryBatchRuntime
from src.discovery.pending_queue import (
    DrainOutcome,
    DrainReport,
    drain_pending_candidates,
)
from src.discovery.providers.provider_errors import ProviderRequestBudgetExhausted


STAGING_QUEUE_CAPACITY = 500


@dataclass
class CandidateDrainCoordinator:
    """Bounded, cancellation-aware drain coordinator.

    Owns the staging queue, weighted semaphore, consumer thread, and
    no-progress watchdog.  The coordinator calls ``notify()`` when a
    lane produces candidates, and the consumer drains them in bounded
    batches.
    """

    runtime: DiscoveryBatchRuntime
    journal: Any  # PageJournalStore
    options: Any  # DiscoveryOptions
    worker_id: str
    paper_raw_dir: Path
    papers_dir: Path
    ledger_path: Path
    locks_dir: Path
    exports_dir: Path
    skip_duplicates: bool = False
    hide_existing: bool = False
    max_candidates: int = 50
    max_pending_candidates: int = 1000
    resume_pending_candidates: int = 700
    stage_to_paper_raw: bool = False
    apply: bool = False
    doi_resolution_budget: int = 10
    until_exhausted: bool = False

    # ── internal state ────────────────────────────────────────────────
    _queue: queue.Queue[tuple[str, int] | None] = field(
        default_factory=lambda: queue.Queue(maxsize=STAGING_QUEUE_CAPACITY),
        repr=False,
    )
    _slots: threading.BoundedSemaphore = field(
        default_factory=lambda: threading.BoundedSemaphore(STAGING_QUEUE_CAPACITY),
        repr=False,
    )
    _state_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _progress_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _backpressured: set[str] = field(default_factory=set, repr=False)
    _budget_exhausted: set[str] = field(default_factory=set, repr=False)
    _consumer_failures: list[str] = field(default_factory=list, repr=False)
    _last_progress: float = field(default=0.0, repr=False)
    _consumer: threading.Thread | None = field(default=None, repr=False)
    _drains: dict[str, list[DrainReport]] = field(default_factory=dict, repr=False)
    _closed: bool = field(default=False, repr=False)

    # ── public API ────────────────────────────────────────────────────

    def __enter__(self) -> "CandidateDrainCoordinator":
        self._last_progress = time.monotonic()
        self._consumer = threading.Thread(
            target=self._run_consumer,
            name="discovery-staging-consumer",
            daemon=False,
        )
        self._consumer.start()
        return self

    def __exit__(self, *args: Any) -> bool:
        self._closed = True
        diagnostic = self.close()
        if diagnostic is not None:
            # Surface consumer lifecycle issues — they indicate a bug
            import logging
            _logger = logging.getLogger(__name__)
            _logger.warning("CandidateDrainCoordinator shutdown: %s", diagnostic)
        return False

    def notify(self, keyword_id: str, candidate_count: int) -> None:
        """Called by lane executors when candidates are produced.

        Checks the runtime guard and drain closed state, updates
        backpressure tracking, and enqueues a notification for the
        consumer thread.  Bounded by the weighted semaphore — a full
        semaphore triggers dynamic backpressure for the keyword.
        """
        self.runtime.guard.ensure_open()
        if self._closed:
            return
        # Bounded acquire with short timeout — full semaphore triggers backpressure
        acquired = self._slots.acquire(timeout=1.0)
        if not acquired:
            with self._state_lock:
                self._backpressured.add(keyword_id)
            return
        try:
            self._queue.put((keyword_id, candidate_count), timeout=0.5)
        except queue.Full:
            self._slots.release()
            with self._state_lock:
                self._backpressured.add(keyword_id)
        with self._progress_lock:
            self._last_progress = time.monotonic()

    def budget_exhausted(self, keyword_id: str) -> bool:
        """Check whether the staging budget for *keyword_id* is exhausted."""
        with self._state_lock:
            return keyword_id in self._budget_exhausted

    def drain(self, keyword_id: str, budget: int, *, phase: str) -> DrainReport:
        """Execute one bounded drain for *keyword_id*.

        Returns the drain report. The caller is responsible for tracking
        accumulated counts for budget exhaustion via budget_exhausted().
        """
        self.runtime.guard.ensure_open()
        try:
            result = drain_pending_candidates(
                journal=self.journal,
                keyword_ids=[keyword_id],
                candidate_budget=max(0, budget),
                stage_to_paper_raw=self.stage_to_paper_raw,
                apply=self.apply,
                paper_raw_dir=self.paper_raw_dir,
                papers_dir=self.papers_dir,
                ledger_path=self.ledger_path,
                locks_dir=self.locks_dir,
                exports_dir=self.exports_dir,
                worker_id=self.worker_id,
                doi_resolution_budget=self.doi_resolution_budget,
                skip_duplicates=self.skip_duplicates,
                hide_existing=self.hide_existing,
                runtime=self.runtime,
            )
            # Track accumulated processed count for budget exhaustion (locked)
            with self._state_lock:
                prior = self._drains.setdefault(keyword_id, [])
                prior.append(result)
                total_processed = sum(r.processed for r in prior)
                if (
                    not self.until_exhausted
                    and self.max_candidates > 0
                    and total_processed >= self.max_candidates
                ):
                    self._budget_exhausted.add(keyword_id)
            return result
        except ProviderRequestBudgetExhausted:
            return DrainReport.budget_stopped(
                reason="provider_request_budget_reached",
            )
        except Exception as exc:
            return DrainReport.failed(exc, phase=phase)

    def close(self) -> str | None:
        """Send sentinel, wait for consumer, return diagnostic or None."""
        timeout = getattr(self.options, "staging_no_progress_timeout_seconds", 300.0)
        diagnostic: str | None = None
        consumer_join_timeout = min(10.0, timeout)

        # Send sentinel (bounded)
        sentinel_deadline = time.monotonic() + min(5.0, timeout)
        sentinel_sent = False
        while time.monotonic() < sentinel_deadline:
            # If consumer already dead, no point waiting
            if self._consumer is not None and not self._consumer.is_alive():
                diagnostic = "staging_consumer_died_before_close"
                break
            try:
                self._queue.put(None, timeout=min(1.0, timeout))
                sentinel_sent = True
                break
            except queue.Full:
                with self._progress_lock:
                    idle = time.monotonic() - self._last_progress
                if idle >= timeout and diagnostic is None:
                    diagnostic = f"staging_consumer_no_progress:{idle:.1f}s"

        if sentinel_sent:
            # Wait for queue to drain (bounded)
            drain_deadline = time.monotonic() + consumer_join_timeout
            with self._queue.all_tasks_done:
                while self._queue.unfinished_tasks:
                    if time.monotonic() > drain_deadline:
                        diagnostic = diagnostic or "staging_drain_timeout"
                        break
                    if self._consumer is not None and not self._consumer.is_alive():
                        diagnostic = diagnostic or "staging_consumer_died"
                        break
                    with self._progress_lock:
                        idle = time.monotonic() - self._last_progress
                    if idle >= timeout and diagnostic is None:
                        diagnostic = f"staging_consumer_no_progress:{idle:.1f}s"
                    self._queue.all_tasks_done.wait(timeout=min(1.0, consumer_join_timeout))

        # Join consumer (bounded)
        if self._consumer is not None and self._consumer.is_alive():
            self._consumer.join(timeout=consumer_join_timeout)
            if self._consumer.is_alive():
                diagnostic = diagnostic or "staging_consumer_join_timeout"

        with self._progress_lock:
            if self._consumer_failures:
                diagnostic = diagnostic or self._consumer_failures[0]

        return diagnostic

    @property
    def outcome(self) -> DrainOutcome:
        """Aggregate drain outcome across all drains."""
        worst: DrainOutcome = DrainOutcome.COMPLETED
        with self._state_lock:
            reports_list = [r for reports in self._drains.values() for r in reports]
        for report in reports_list:
                if report.outcome == DrainOutcome.REPAIR_REQUIRED:
                    return DrainOutcome.REPAIR_REQUIRED
                if report.outcome == DrainOutcome.INTERRUPTED:
                    worst = DrainOutcome.INTERRUPTED
                elif report.outcome in {DrainOutcome.RETRYABLE_FAILED, DrainOutcome.PERMANENT_FAILED} and worst not in (
                    DrainOutcome.INTERRUPTED, DrainOutcome.REPAIR_REQUIRED,
                ):
                    worst = report.outcome
        return worst

    @property
    def drain_reports(self) -> dict[str, list[DrainReport]]:
        with self._state_lock:
            return {k: list(v) for k, v in self._drains.items()}

    @property
    def dynamically_backpressured(self) -> frozenset[str]:
        with self._state_lock:
            return frozenset(self._backpressured)

    # ── internal ──────────────────────────────────────────────────────

    def _run_consumer(self) -> None:
        while True:
            notification = self._queue.get()
            try:
                if notification is None:
                    return
                keyword_id, candidate_count = notification
                try:
                    with self._state_lock:
                        prior = self._drains.setdefault(keyword_id, [])
                        processed = sum(report.processed for report in prior)
                    remaining = max(0, self.max_candidates - processed)
                    current = self.drain(
                        keyword_id,
                        min(candidate_count, remaining),
                        phase="consumer",
                    )
                    with self._state_lock:
                        prior.append(current)
                        if (
                            not self.until_exhausted
                            and self.max_candidates > 0
                            and sum(report.processed for report in prior) >= self.max_candidates
                        ):
                            self._budget_exhausted.add(keyword_id)
                except Exception as exc:
                    with self._state_lock:
                        self._drains.setdefault(keyword_id, []).append(
                            DrainReport.failed(exc, phase="consumer"),
                        )
                    with self._progress_lock:
                        self._consumer_failures.append(
                            f"staging_consumer_exception:{type(exc).__name__}:{str(exc)[:400]}"
                        )
            finally:
                if notification is not None:
                    self._slots.release()  # one slot per notification
                self._queue.task_done()
                with self._progress_lock:
                    self._last_progress = time.monotonic()

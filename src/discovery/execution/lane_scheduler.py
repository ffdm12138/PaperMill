"""Incremental bounded lane scheduler for discovery batches.

Extracted from coordinator.py so scheduling policy has a single owner.
Accepts ``LaneExecutionSpec`` values, honours ``max_workers``, dynamic
candidate backpressure, and KeyboardInterrupt cancellation.
"""
from __future__ import annotations

import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as futures_wait
from dataclasses import dataclass, field
from typing import Any, Callable

from loguru import logger

from src.discovery.execution.lane_models import (
    LaneCounters,
    LaneExecutionSpec,
    LaneOutcome,
    LaneState,
    StopReason,
)


@dataclass(frozen=True)
class CandidateBackpressureSnapshot:
    """Immutable snapshot of backpressure state for one keyword."""
    keyword_id: str
    pending_candidates: int
    queued_notifications: int
    active: bool
    reason: str | None = None


@dataclass
class SchedulerSnapshot:
    """Immutable snapshot of scheduler state after execution."""
    total_planned: int
    total_executed: int
    total_skipped: int
    total_interrupted: int
    backpressured_keyword_ids: frozenset[str]
    error: str | None = None


def _failed_outcome(spec: LaneExecutionSpec, exc: BaseException) -> LaneOutcome:
    return LaneOutcome(
        key=spec.key,
        state=LaneState.PERMANENT_FAILED,
        stop_reason=StopReason.LOCAL_CONSISTENCY_ERROR,
        counters=LaneCounters(),
        exhaustion_evidence=None,
    )


def _skipped_outcome(spec: LaneExecutionSpec) -> LaneOutcome:
    return LaneOutcome(
        key=spec.key,
        state=LaneState.SKIPPED,
        stop_reason=StopReason.CANDIDATE_BACKPRESSURE,
        counters=LaneCounters(),
        exhaustion_evidence=None,
    )


def _interrupted_outcome(spec: LaneExecutionSpec) -> LaneOutcome:
    return LaneOutcome(
        key=spec.key,
        state=LaneState.INTERRUPTED,
        stop_reason=StopReason.LOCAL_CONSISTENCY_ERROR,
        counters=LaneCounters(),
        exhaustion_evidence=None,
    )


def schedule_lanes(
    specs: list[LaneExecutionSpec],
    *,
    max_workers: int,
    execute_lane: Callable[[LaneExecutionSpec], LaneOutcome],
    backpressure_provider: Callable[[], frozenset[str]],
    cancellation_token: threading.Event | None = None,
) -> tuple[list[LaneOutcome], SchedulerSnapshot]:
    """Execute lane specs with bounded concurrency and backpressure awareness.

    Invariants:
    - ``len(in_flight) <= max_workers`` at all times.
    - Completion-driven refill via ``FIRST_COMPLETED``.
    - Keywords with active backpressure skip all pending lanes.
    - KeyboardInterrupt cancels pending futures and produces INTERRUPTED outcomes.
    - Every spec produces exactly one outcome.

    Returns ``(outcomes, snapshot)``.
    """
    if not specs:
        return [], SchedulerSnapshot(
            total_planned=0, total_executed=0, total_skipped=0,
            total_interrupted=0, backpressured_keyword_ids=frozenset(),
        )

    outcomes: list[LaneOutcome] = []
    pending_specs: list[LaneExecutionSpec] = list(specs)
    in_flight: dict[Any, LaneExecutionSpec] = {}
    backpressured_ids: set[str] = set()
    total_executed = 0
    total_skipped = 0
    total_interrupted = 0

    executor = ThreadPoolExecutor(max_workers=max_workers)

    def _refill() -> None:
        nonlocal total_skipped
        while len(in_flight) < max_workers and pending_specs:
            spec = pending_specs.pop(0)
            if spec.key.keyword_id in backpressured_ids:
                outcomes.append(_skipped_outcome(spec))
                total_skipped += 1
                continue
            try:
                in_flight[executor.submit(execute_lane, spec)] = spec
            except RuntimeError:
                outcomes.append(_interrupted_outcome(spec))
                total_interrupted += 1

    try:
        # Initial fill
        _refill()

        while in_flight:
            try:
                done, _ = futures_wait(list(in_flight), return_when=FIRST_COMPLETED)
            except KeyboardInterrupt:
                # Cancel all pending and in-flight work; produce outcomes for every spec.
                for future, spec in list(in_flight.items()):
                    try:
                        future.cancel()
                    except Exception as exc:
                        logger.debug("lane future cancel raced: {}", exc)
                    outcomes.append(_interrupted_outcome(spec))
                    total_interrupted += 1
                for spec in pending_specs:
                    outcomes.append(_interrupted_outcome(spec))
                    total_interrupted += 1
                pending_specs.clear()
                in_flight.clear()
                executor.shutdown(wait=False, cancel_futures=True)
                return outcomes, SchedulerSnapshot(
                    total_planned=len(specs),
                    total_executed=total_executed,
                    total_skipped=total_skipped,
                    total_interrupted=total_interrupted,
                    backpressured_keyword_ids=frozenset(backpressured_ids),
                    error="keyboard_interrupt",
                )
            except Exception:
                for future in list(in_flight):
                    try:
                        future.cancel()
                    except Exception as exc:
                        logger.debug("lane future cancel raced: {}", exc)
                break

            for future in done:
                spec = in_flight.pop(future)
                try:
                    outcomes.append(future.result())
                    total_executed += 1
                except KeyboardInterrupt:
                    outcomes.append(_interrupted_outcome(spec))
                    total_interrupted += 1
                    executor.shutdown(wait=False, cancel_futures=True)
                    for ps in pending_specs:
                        outcomes.append(_interrupted_outcome(ps))
                        total_interrupted += 1
                    pending_specs.clear()
                    # Also produce outcomes for remaining in-flight futures
                    for _, ispec in in_flight.items():
                        outcomes.append(_interrupted_outcome(ispec))
                        total_interrupted += 1
                    in_flight.clear()
                    return outcomes, SchedulerSnapshot(
                        total_planned=len(specs),
                        total_executed=total_executed,
                        total_skipped=total_skipped,
                        total_interrupted=total_interrupted,
                        backpressured_keyword_ids=frozenset(backpressured_ids),
                        error="keyboard_interrupt",
                    )
                except Exception as exc:
                    outcomes.append(_failed_outcome(spec, exc))
                    total_executed += 1

                # Check backpressure after each completion
                bp_now = backpressure_provider()
                new_bp = bp_now - backpressured_ids
                if new_bp:
                    backpressured_ids |= new_bp
                    # Skip remaining pending specs for backpressured keywords
                    new_pending: list[LaneExecutionSpec] = []
                    for ps in pending_specs:
                        if ps.key.keyword_id in backpressured_ids:
                            outcomes.append(_skipped_outcome(ps))
                            total_skipped += 1
                        else:
                            new_pending.append(ps)
                    pending_specs = new_pending

                # Check cancellation
                if cancellation_token is not None and cancellation_token.is_set():
                    executor.shutdown(wait=False, cancel_futures=True)
                    for ps in pending_specs:
                        outcomes.append(_interrupted_outcome(ps))
                        total_interrupted += 1
                    pending_specs.clear()
                    for _, ispec in in_flight.items():
                        outcomes.append(_interrupted_outcome(ispec))
                        total_interrupted += 1
                    in_flight.clear()
                    return outcomes, SchedulerSnapshot(
                        total_planned=len(specs),
                        total_executed=total_executed,
                        total_skipped=total_skipped,
                        total_interrupted=total_interrupted,
                        backpressured_keyword_ids=frozenset(backpressured_ids),
                        error="cancelled",
                    )

            # Refill after processing completions
            _refill()

    finally:
        # Cleanup remaining
        for future, spec in list(in_flight.items()):
            try:
                if not future.done():
                    future.cancel()
                outcomes.append(future.result())
                total_executed += 1
            except Exception:
                outcomes.append(_interrupted_outcome(spec))
                total_interrupted += 1
        for spec in pending_specs:
            outcomes.append(_interrupted_outcome(spec))
            total_interrupted += 1
        executor.shutdown(wait=False, cancel_futures=True)

    snapshot = SchedulerSnapshot(
        total_planned=len(specs),
        total_executed=total_executed,
        total_skipped=total_skipped,
        total_interrupted=total_interrupted,
        backpressured_keyword_ids=frozenset(backpressured_ids),
    )
    # Outcome conservation: every planned lane produces exactly one outcome.
    _planned = snapshot.total_planned
    _outcomes = len(outcomes)
    _accounted = snapshot.total_executed + snapshot.total_skipped + snapshot.total_interrupted
    if _planned != _outcomes or _planned != _accounted:
        raise RuntimeError(
            f"LaneScheduler outcome conservation violated: "
            f"planned={_planned}, outcomes={_outcomes}, "
            f"accounted={_accounted} "
            f"(executed={snapshot.total_executed}, "
            f"skipped={snapshot.total_skipped}, "
            f"interrupted={snapshot.total_interrupted})"
        )
    return outcomes, snapshot

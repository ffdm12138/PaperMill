"""Discovery v4 execution — lane scheduler, executor, candidate drain.

``CandidateDrainCoordinator`` is the single consumer.  ``LaneScheduler``
produces outcomes that satisfy conservation.

Import directly from submodules:
    from src.discovery.execution.lane_executor import execute_refresh_lane, execute_backfill_lane
    from src.discovery.execution.lane_models import DiscoveryLaneKey, LaneState, StopReason
"""
